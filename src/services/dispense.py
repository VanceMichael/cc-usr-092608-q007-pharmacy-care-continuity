"""购药决策：提前领取、疗程重叠、替代品牌、召回批次 → 允许/阻断/转人工。

关键边界：
- 判定由规则引擎完成并完整留痕；普通店员只看到允许/阻断/转人工与办事指引，
  不看到计划、观察等健康内容（健康原因在药师视图才展开）；
- 阻断级：召回批次（最高优先，应急保供也不得绕过）、严重提前领取、疗程重叠、库存不足；
- 转人工级：替代品牌确认、轻度提前领取、跨店未授权、身份/计划无法确认、
  高风险调整待复核、服务已终止；
- 应急保供可把提前领取/重叠的阻断降级为转人工，但召回仍然阻断。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.errors import AuthorizationError, DomainError
from src.models import (
    ChangeRisk,
    ChangeStatus,
    ContactReason,
    Decision,
    MovementKind,
    PlanLineStatus,
    ReservationStatus,
    StaffRole,
    to_row,
)
from src.repository import Repository
from src.services.access import AccessService
from src.services.inventory import InventoryService
from src.services.records import RecordService

# 补充原因码
REASON_CROSS_STORE_NO_CONSENT = "cross_store_consent_missing"

_EARLY_BLOCK_FRACTION = 0.85  # 剩余疗程超过 85%（刚领走数日）阻断；其余提前转人工
_EARLY_MANUAL_GRACE_DAYS = 1  # 剩余 1 天以上 → 转人工

# 店员可见的脱敏指引（不含任何健康信息）
_CLERK_HINTS = {
    "allow": "可以办理领取",
    "block": "停止发放，按门店处置流程处理",
    "manual": "请联系值班执业药师处理，不要自行发药",
}


class DispenseService:
    def __init__(
        self,
        repo: Repository,
        access: AccessService,
        records: RecordService,
        inventory: InventoryService,
    ) -> None:
        self.repo = repo
        self.access = access
        self.records = records
        self.inventory = inventory

    # ---- 规则引擎 -------------------------------------------------
    def evaluate(
        self,
        customer_id: str | None,
        store_id: str,
        sku: str,
        qty: int,
        staff_id: str,
        batch_no: str | None = None,
        at: str | None = None,
        emergency: bool = False,
    ) -> dict[str, Any]:
        moment = timeutil.parse(at) if at else timeutil.now()
        moment_iso = timeutil.format_value(moment)
        staff = self.repo.find("staff", id=staff_id)
        sku_row = self.repo.find("skus", code=sku)

        blocks: list[str] = []
        manuals: list[str] = []
        detail: dict[str, Any] = {}

        # 1) 批次风险：最高优先，按门店现有该品规批次逐个判定，应急也不绕过
        recalled_batch = self._recalled_batch(store_id, sku, batch_no)
        if recalled_batch:
            blocks.append("batch_recalled")
            detail["recalled_batch"] = recalled_batch

        # 2) 身份与计划上下文（批次已明确召回时短路：召回为最高优先阻断）
        enrollment = self.access.active_enrollment(customer_id, at) if customer_id else None
        if recalled_batch:
            pass
        elif customer_id is None or sku_row is None:
            manuals.append("missing_customer_context")
        elif enrollment is None:
            manuals.append("plan_terminated")
        else:
            plan_version = self.records.effective_plan(customer_id, moment_iso)
            drug_id = sku_row["drug_id"]
            detail["drug_id"] = drug_id

            # 3) 跨店续接授权
            if store_id != enrollment["home_store_id"]:
                consents = self.access.effective_consents(enrollment["id"], moment_iso)
                if not consents.get("cross_store", False):
                    manuals.append(REASON_CROSS_STORE_NO_CONSENT)

            # 4) 待复核的高风险调整
            pending = [
                ch
                for ch in self.repo.filter("changes", customer_id=customer_id, status=ChangeStatus.PROPOSED)
                if ch["risk"] == ChangeRisk.HIGH
                and any(line["drug_id"] == drug_id for line in ch["lines"])
                and timeutil.parse(ch["created_at"]) <= moment
            ]
            if pending:
                manuals.append("high_risk_change_pending_review")
                detail["pending_change"] = pending[-1]["id"]

            if plan_version is not None:
                lines = [line for line in plan_version["lines"] if line["drug_id"] == drug_id]
                detail["plan_version_seq"] = plan_version["seq"]

                # 5) 替代品牌：同通用名不同品规
                if lines:
                    planned_skus = {line["sku"] for line in lines if line.get("sku")}
                    if planned_skus and sku not in planned_skus:
                        manuals.append("substitution_requires_review")
                        detail["planned_skus"] = sorted(planned_skus)

                # 6) 疗程重叠：同一药品在同一版本中有两条并行疗程
                if len(lines) > 1:
                    blocks.append("course_overlap")
                    detail["overlapping_lines"] = len(lines)

                # 7) 基于既往领取的提前领取 / 新旧课程重叠
                last_claim = self._last_claimed(customer_id, drug_id, moment_iso)
                if last_claim is not None and lines:
                    days_supply = max(int(line["days_supply"]) for line in lines)
                    claimed_at = timeutil.parse(last_claim["claimed_at"])
                    runout = claimed_at + timeutil.days(days_supply)
                    if runout > moment:
                        remaining_days = (runout - moment).total_seconds() / 86400
                        detail["remaining_days"] = round(remaining_days, 2)
                        new_course_started = timeutil.parse(plan_version["issued_at"]) > claimed_at
                        if new_course_started and remaining_days > _EARLY_MANUAL_GRACE_DAYS:
                            blocks.append("course_overlap")
                        elif remaining_days / days_supply > _EARLY_BLOCK_FRACTION:
                            blocks.append("early_pickup")
                        elif remaining_days > _EARLY_MANUAL_GRACE_DAYS:
                            manuals.append("early_pickup")

        # 8) 库存（召回已拦截时不再暴露库存细节给阻断决策之外）
        if "batch_recalled" not in blocks:
            chosen = self._pick_batch(store_id, sku, batch_no)
            if chosen is None:
                blocks.append("insufficient_stock")
            elif self.inventory.available_qty(store_id, sku, chosen) < qty:
                blocks.append("insufficient_stock")
            else:
                detail["batch_no"] = chosen

        # 应急保供：临床类阻断降级为转人工（召回、库存不足不降级）
        clinical_blocks = {"early_pickup", "course_overlap"}
        if emergency:
            downgraded = [reason for reason in blocks if reason in clinical_blocks]
            blocks = [reason for reason in blocks if reason not in clinical_blocks]
            manuals.extend(downgraded)
            detail["emergency"] = True

        if blocks:
            decision = Decision.BLOCK
        elif manuals:
            decision = Decision.MANUAL
        else:
            decision = Decision.ALLOW

        # 原因去重保序（疗程重叠等规则可能从多条路径命中）
        blocks = list(dict.fromkeys(blocks))
        manuals = list(dict.fromkeys(manuals))

        result = {
            "decision": decision,
            "reasons": blocks + manuals,
            "block_reasons": blocks,
            "manual_reasons": manuals,
            "detail": detail,
            "customer_id": customer_id,
            "store_id": store_id,
            "sku": sku,
            "qty": qty,
            "at": moment_iso,
            "staff_id": staff_id,
            "clerk_message": _CLERK_HINTS[decision],
        }
        self._record_attempt(result, batch_no or result["detail"].get("batch_no", ""))
        return result

    # ---- 执行：仅允许单可发药 -------------------------------------
    def execute(
        self, evaluation: dict[str, Any], staff_id: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        if evaluation["decision"] != Decision.ALLOW:
            raise DomainError(f"决策为{evaluation['decision']}，不能发药：{evaluation['reasons']}")
        ctx = self.access.assert_fulfillment(
            evaluation["customer_id"], staff_id, "sell", evaluation["at"]
        )
        business_at = occurred_at or evaluation["at"]
        batch_no = evaluation["detail"]["batch_no"]
        emergency = bool(evaluation["detail"].get("emergency"))
        reservation = self.inventory.reserve(
            customer_id=evaluation["customer_id"],
            store_id=evaluation["store_id"],
            sku=evaluation["sku"],
            batch_no=batch_no,
            qty=evaluation["qty"],
            staff_id=staff_id,
            ttl_hours=24.0,
            emergency=emergency,
            occurred_at=business_at,
        )
        claimed = self.inventory.claim(reservation["id"], staff_id, business_at)
        self.repo.update("attempts", evaluation["attempt_id"], {"reservation_id": reservation["id"]})
        return {"reservation": claimed, "movement_ref": reservation["id"], "at": ctx["at"]}

    # ---- 辅助 -----------------------------------------------------
    def _recalled_batch(self, store_id: str, sku: str, batch_no: str | None) -> str | None:
        if batch_no is not None:
            return self.inventory.is_recalled(sku, batch_no)
        # 未指定批次：门店该品规的全部在库批次都被召回才算批次风险
        batches = {
            (m["sku"], m["batch_no"])
            for m in self.repo.filter("movements", store_id=store_id)
            if m["sku"] == sku
        }
        recalled = [b for _, b in batches if self.inventory.is_recalled(sku, b)]
        if batches and len(recalled) == len(batches):
            return recalled[0]
        return None

    def _pick_batch(self, store_id: str, sku: str, batch_no: str | None) -> str | None:
        if batch_no is not None:
            if self.inventory.physical_qty(store_id, sku, batch_no) > 0:
                return batch_no
            return None
        # 先到期先出（FEFO）：跳过召回，取可售>0 中到期最早的批次
        candidates = []
        seen = {
            b
            for m in self.repo.filter("movements", store_id=store_id, sku=sku)
            for b in [m["batch_no"]]
        }
        for number in seen:
            if self.inventory.is_recalled(sku, number):
                continue
            if self.inventory.available_qty(store_id, sku, number) > 0:
                batch = self.repo.find("batches", sku=sku, batch_no=number)
                expiry = batch["expiry"] if batch and batch.get("expiry") else "9999-12-31T23:59:59+00:00"
                candidates.append((expiry, number))
        return sorted(candidates)[0][1] if candidates else None

    def _last_claimed(
        self, customer_id: str, drug_id: str, moment_iso: str
    ) -> dict[str, Any] | None:
        """该药品（任意品牌品规）截至时刻最近一次已领取预留。"""
        skus = {row["code"] for row in self.repo.filter("skus", drug_id=drug_id)}
        claims = []
        for reservation in self.repo.filter("reservations", customer_id=customer_id):
            if reservation["status"] != ReservationStatus.CLAIMED:
                continue
            if reservation["sku"] not in skus:
                continue
            claimed_at = reservation.get("claimed_at")
            if claimed_at and timeutil.parse(claimed_at) <= timeutil.parse(moment_iso):
                claims.append(reservation)
        return sorted(claims, key=lambda row: timeutil.parse(row["claimed_at"]))[-1] if claims else None

    def _record_attempt(self, result: dict[str, Any], batch_no: str) -> None:
        attempt_id = f"att_{uuid.uuid4().hex[:10]}"
        result["attempt_id"] = attempt_id
        self.repo.add(
            "attempts",
            {
                "id": attempt_id,
                "customer_id": result["customer_id"],
                "store_id": result["store_id"],
                "sku": result["sku"],
                "batch_no": batch_no,
                "qty": result["qty"],
                "occurred_at": result["at"],
                "recorded_at": timeutil.format_value(timeutil.now()),
                "decision": result["decision"],
                "reasons": result["reasons"],
                "staff_id": result["staff_id"],
            },
        )

    # ---- 药师视图：展开完整原因（需要健康记录授权） --------------
    def pharmacist_explanation(
        self, evaluation: dict[str, Any], pharmacist_id: str
    ) -> dict[str, Any]:
        """药师请求查看决策依据时，再次校验授权与资质，审计留痕。"""
        if evaluation["customer_id"] is None:
            raise AuthorizationError("无顾客上下文，无法调阅")
        self.access.check_health(
            evaluation["customer_id"], pharmacist_id, "health_records",
            "view_purchase_reasons", evaluation["at"],
        )
        plan = self.records.effective_plan(evaluation["customer_id"], evaluation["at"])
        safety = self.records.latest_safety_conclusion(evaluation["customer_id"], evaluation["at"])
        return {
            "decision": evaluation["decision"],
            "reasons": evaluation["reasons"],
            "detail": evaluation["detail"],
            "plan_version": plan,
            "latest_safety_conclusion": safety,
        }
