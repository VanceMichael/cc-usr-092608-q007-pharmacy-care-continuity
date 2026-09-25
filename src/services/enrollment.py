"""入组与授权：顾客选择服务计划，并对三类事项分别授权。

终止服务立即停止新的健康访问；交易记录与安全处置不删除，仍可依法追溯。
到期随访类待办随终止取消；召回、不良反应等安全义务相关待办保留。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.errors import AuthorizationError, DuplicateError
from src.models import (
    ContactReason,
    ContactStatus,
    Consent,
    Enrollment,
    EnrollmentStatus,
    Scope,
    StaffRole,
    to_row,
)
from src.repository import Repository
from src.services.access import AccessService

# 终止后仍需保留的安全/履约类联系
_RETAINED_REASONS = frozenset(
    {ContactReason.RECALL_NOTICE, ContactReason.ADR_FOLLOWUP, ContactReason.TRANSFER_NOTICE}
)


class EnrollmentService:
    def __init__(self, repo: Repository, access: AccessService) -> None:
        self.repo = repo
        self.access = access

    def enroll(
        self,
        customer_id: str,
        plan_id: str,
        home_store_id: str,
        grants: dict[str, bool],
        at: str | None = None,
        responsible_pharmacist_id: str = "",
    ) -> dict[str, Any]:
        if self.repo.find("customers", id=customer_id) is None:
            raise KeyError(f"顾客不存在：{customer_id}")
        if self.repo.find("service_plans", id=plan_id) is None:
            raise KeyError(f"服务计划不存在：{plan_id}")
        if self.access.active_enrollment(customer_id, at) is not None:
            raise DuplicateError("顾客已有有效服务计划")
        unknown = set(grants) - {s.value for s in Scope}
        if unknown:
            raise ValueError(f"未知授权项：{sorted(unknown)}")

        moment = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        if responsible_pharmacist_id:
            pharmacist = self.repo.find("staff", id=responsible_pharmacist_id)
            if pharmacist is None:
                raise KeyError(f"负责药师不存在：{responsible_pharmacist_id}")
            self.access.assert_practicing_pharmacist(pharmacist, moment)
        enrollment = to_row(
            Enrollment(
                id=f"enr_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                plan_id=plan_id,
                home_store_id=home_store_id,
                started_at=moment,
                responsible_pharmacist_id=responsible_pharmacist_id,
            )
        )
        self.repo.add("enrollments", enrollment)
        for scope, granted in grants.items():
            self.repo.add(
                "consents",
                to_row(
                    Consent(
                        id=f"cst_{uuid.uuid4().hex[:10]}",
                        enrollment_id=enrollment["id"],
                        scope=scope,
                        granted=bool(granted),
                        decided_at=moment,
                        statement="顾客在入组时逐项选择授权",
                    )
                ),
            )
        return enrollment

    def set_consent(
        self,
        customer_id: str,
        scope: str,
        granted: bool,
        at: str | None = None,
    ) -> dict[str, Any]:
        """单项授权变更：新决定按其发生时间生效，不影响其他授权项。"""
        enrollment = self.access.active_enrollment(customer_id, at)
        if enrollment is None:
            raise AuthorizationError("没有有效服务计划，不能变更授权")
        moment = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        row = to_row(
            Consent(
                id=f"cst_{uuid.uuid4().hex[:10]}",
                enrollment_id=enrollment["id"],
                scope=scope,
                granted=granted,
                decided_at=moment,
                statement="顾客逐项变更授权",
            )
        )
        self.repo.add("consents", row)
        if not granted:
            self._suspend_reminder_contacts(customer_id, moment)
        return row

    def terminate(
        self,
        customer_id: str,
        reason: str,
        staff_id: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """终止服务：新健康访问立即停止；记录保留可追溯。"""
        enrollment = self.access.active_enrollment(customer_id, at)
        if enrollment is None:
            raise AuthorizationError("没有有效服务计划，无法终止")
        moment = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())

        self.repo.update(
            "enrollments",
            enrollment["id"],
            {
                "status": EnrollmentStatus.TERMINATED,
                "terminated_at": moment,
                "terminate_reason": reason,
            },
        )
        # 所有授权在终止时刻撤回（新增撤回事件，历史授权决定仍可审计）
        for scope, granted in self.access.effective_consents(enrollment["id"], moment).items():
            if granted:
                self.repo.add(
                    "consents",
                    to_row(
                        Consent(
                            id=f"cst_{uuid.uuid4().hex[:10]}",
                            enrollment_id=enrollment["id"],
                            scope=scope,
                            granted=False,
                            decided_at=moment,
                            statement=f"服务终止：{reason}",
                        )
                    ),
                )
        # 提醒类待办取消；安全义务类待办保留
        for task in self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN):
            if any(r not in _RETAINED_REASONS for r in task["reasons"]):
                kept = [r for r in task["reasons"] if r in _RETAINED_REASONS]
                if kept:
                    self.repo.update("contacts", task["id"], {"reasons": kept})
                else:
                    self.repo.update(
                        "contacts",
                        task["id"],
                        {"status": ContactStatus.CANCELLED, "closed_at": moment,
                         "detail": (task.get("detail", "") + "|服务终止").strip("|")},
                    )
        return self.repo.find("enrollments", id=enrollment["id"])  # type: ignore[return-value]

    def _suspend_reminder_contacts(self, customer_id: str, moment_iso: str) -> None:
        """撤回提醒授权后，纯到期随访待办取消；含安全原因的合并任务保留安全部分。"""
        for task in self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN):
            reasons = task["reasons"]
            if ContactReason.FOLLOWUP_DUE in reasons or ContactReason.RESERVATION_EXPIRY in reasons:
                kept = [
                    r
                    for r in reasons
                    if r not in (ContactReason.FOLLOWUP_DUE, ContactReason.RESERVATION_EXPIRY)
                ]
                if kept:
                    self.repo.update("contacts", task["id"], {"reasons": kept})
                else:
                    self.repo.update(
                        "contacts",
                        task["id"],
                        {"status": ContactStatus.CANCELLED, "closed_at": moment_iso,
                         "detail": (task.get("detail", "") + "|提醒授权撤回").strip("|")},
                    )

    def current_snapshot(self, customer_id: str, at: str | None = None) -> dict[str, Any]:
        """顾客/跨店咨询起点：当前有效计划、授权状态与归属门店。"""
        enrollment = self.access.active_enrollment(customer_id, at)
        if enrollment is None:
            return {"active": False}
        plan = self.repo.find("service_plans", id=enrollment["plan_id"])
        return {
            "active": True,
            "enrollment": enrollment,
            "plan": plan,
            "consents": self.access.effective_consents(enrollment["id"], at),
        }
