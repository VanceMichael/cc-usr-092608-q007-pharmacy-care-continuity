"""协调器：到期随访、批次召回、预留到期、调拨到货 → 统一的“待联系顾客”队列。

协调规则：
- 同一顾客的多个原因合并为一条开放任务（原因去重，到期时间取最早）；
- 队列随仓储快照持久化，系统重启后开放任务不丢失；
- 到期随访与预留到期需要 reminders 授权；撤回授权或终止服务后不再新增；
- 召回通知属于安全义务：即使终止服务也保留；
- 召回发布时同步释放涉事批次的有效预留（锁释放，等顾客来店改配合格批次）。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.models import (
    ContactReason,
    ContactStatus,
    ReservationStatus,
    Scope,
)
from src.repository import Repository
from src.services.access import AccessService

_REMINDER_REASONS = frozenset(
    {ContactReason.FOLLOWUP_DUE, ContactReason.RESERVATION_EXPIRY}
)


class ContactCoordinator:
    def __init__(self, repo: Repository, access: AccessService) -> None:
        self.repo = repo
        self.access = access

    # ---- 统一入队（去重合并） -------------------------------------
    def enqueue(
        self,
        customer_id: str,
        reason: str,
        store_id: str,
        due_at: str,
        detail: str = "",
        refs: list[str] | None = None,
        at: str | None = None,
    ) -> dict[str, Any] | None:
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        due_iso = timeutil.format_value(timeutil.parse(due_at))

        # 提醒类任务需要当前有效 reminders 授权
        if reason in _REMINDER_REASONS:
            enrollment = self.access.active_enrollment(customer_id, due_iso)
            if enrollment is None:
                return None
            if not self.access.effective_consents(enrollment["id"], due_iso).get(Scope.REMINDERS, False):
                return None

        for task in self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN):
            if reason in task["reasons"]:
                return task  # 同一原因已有开放任务
        # 合并到该顾客任一开放任务
        for task in self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN):
            reasons = task["reasons"] + [reason]
            new_due = min(task["due_at"], due_iso)
            merged_refs = sorted(set(task.get("refs", [])) | set(refs or []))
            self.repo.update(
                "contacts", task["id"],
                {"reasons": reasons, "due_at": new_due, "refs": merged_refs,
                 "detail": "；".join(filter(None, [task.get("detail", ""), detail]))},
            )
            return self.repo.find("contacts", id=task["id"])

        row = {
            "id": f"ctc_{uuid.uuid4().hex[:10]}",
            "customer_id": customer_id,
            "reasons": [reason],
            "store_id": store_id,
            "due_at": due_iso,
            "status": ContactStatus.OPEN,
            "created_at": moment_iso,
            "detail": detail,
            "refs": refs or [],
        }
        self.repo.add("contacts", row)
        return row

    # ---- 扫描：到期随访 -------------------------------------------
    def scan_followups(self, at: str | None = None) -> list[dict[str, Any]]:
        moment = timeutil.parse(at) if at else timeutil.now()
        created = []
        for followup in self.repo.all("followups"):
            due = followup.get("next_due_at")
            if not due or timeutil.parse(due) > moment:
                continue
            ref = f"followup:{followup['id']}"
            if any(ref in task.get("refs", []) for task in self.repo.all("contacts")):
                continue
            task = self.enqueue(
                followup["customer_id"],
                ContactReason.FOLLOWUP_DUE,
                followup["store_id"],
                due,
                detail=f"随访到期（上次随访{followup['id']}）",
                refs=[ref],
                at=timeutil.format_value(moment),
            )
            if task is not None:
                created.append(task)
        return created

    # ---- 扫描：预留到期（释放锁定并提醒） -------------------------
    def scan_reservation_expiry(self, at: str | None = None) -> list[dict[str, Any]]:
        moment = timeutil.parse(at) if at else timeutil.now()
        created = []
        for reservation in self.repo.filter("reservations", status=ReservationStatus.RESERVED):
            if timeutil.parse(reservation["expires_at"]) > moment:
                continue
            ref = f"reservation:{reservation['id']}"
            self.repo.update("reservations", reservation["id"], {"status": ReservationStatus.EXPIRED})
            task = self.enqueue(
                reservation["customer_id"],
                ContactReason.RESERVATION_EXPIRY,
                reservation["store_id"],
                reservation["expires_at"],
                detail=f"预留{reservation['id']}到期释放，需联系顾客重新安排",
                refs=[ref],
                at=timeutil.format_value(moment),
            )
            if task is not None:
                created.append(task)
        return created

    # ---- 召回：通知所有持有涉事批次的顾客 + 释放预留 --------------
    def notify_recall(self, recall_id: str, at: str | None = None) -> list[dict[str, Any]]:
        recall = self.repo.find("recalls", id=recall_id)
        if recall is None:
            raise KeyError(f"召回单不存在：{recall_id}")
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        affected: dict[str, dict[str, Any]] = {}
        for reservation in self.repo.all("reservations"):
            if reservation["sku"] != recall["sku"]:
                continue
            if reservation["batch_no"] not in recall["batch_numbers"]:
                continue
            customer_id = reservation.get("customer_id")
            # 通知已预留或已领取（手中持有问题批次）的顾客
            if customer_id and reservation["status"] in (
                ReservationStatus.RESERVED,
                ReservationStatus.CLAIMED,
            ):
                affected[customer_id] = reservation
            # 有效预留立即释放锁定，阻止误发（claim 还有二次拦截）
            if reservation["status"] == ReservationStatus.RESERVED:
                self.repo.update(
                    "reservations", reservation["id"],
                    {"status": ReservationStatus.CANCELLED, "returned_at": moment_iso},
                )
        created = []
        for customer_id, reservation in affected.items():
            task = self.enqueue(
                customer_id,
                ContactReason.RECALL_NOTICE,
                reservation["store_id"],
                moment_iso,
                detail=f"批次召回{recall_id}：{recall['title']}，请顾客停止使用并到店处置",
                refs=[f"recall:{recall_id}"],
                at=moment_iso,
            )
            if task is not None:
                created.append(task)
        return created

    # ---- 不良反应回访 ---------------------------------------------
    def enqueue_adr_followup(self, adr_id: str, due_at: str | None = None) -> dict[str, Any] | None:
        adr = self.repo.find("adverse_events", id=adr_id)
        if adr is None:
            raise KeyError(f"不良反应报告不存在：{adr_id}")
        due = due_at or adr["occurred_at"]
        return self.enqueue(
            adr["customer_id"],
            ContactReason.ADR_FOLLOWUP,
            adr.get("store_id") or "",
            due,
            detail=f"不良反应{adr_id}需回访（严重程度{adr['severity']}）",
            refs=[f"adr:{adr_id}"],
        )

    # ---- 值班视图 -------------------------------------------------
    def due_contacts(self, store_id: str | None = None, at: str | None = None) -> list[dict[str, Any]]:
        moment = timeutil.parse(at) if at else timeutil.now()
        tasks = [
            task
            for task in self.repo.filter("contacts", status=ContactStatus.OPEN)
            if timeutil.parse(task["due_at"]) <= moment
            and (store_id is None or task["store_id"] == store_id)
        ]
        return sorted(tasks, key=lambda task: timeutil.parse(task["due_at"]))

    def open_contacts(self, customer_id: str) -> list[dict[str, Any]]:
        return self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN)

    def complete(self, task_id: str, note: str = "", at: str | None = None) -> dict[str, Any]:
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        task = self.repo.find("contacts", id=task_id)
        if task is None or task["status"] != ContactStatus.OPEN:
            raise KeyError("开放任务不存在或已关闭")
        self.repo.update(
            "contacts", task_id,
            {"status": ContactStatus.DONE, "closed_at": moment_iso,
             "detail": "；".join(filter(None, [task.get("detail", ""), note]))},
        )
        return self.repo.find("contacts", id=task_id)  # type: ignore[return-value]

    def scan_all(self, at: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """系统重启或定时触发时调用：开放任务从快照恢复，这里补齐扫描类任务。"""
        return {
            "followups": self.scan_followups(at),
            "reservation_expiry": self.scan_reservation_expiry(at),
        }
