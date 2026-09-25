"""购药决策：识别提前领取、疗程重叠、替代品牌与召回批次。

每次评估给出允许、阻断或转人工的明确原因。应急保供可以放宽
依从性类提示（提前领取、疗程重叠、替代品牌），但批次风险
（召回批次、未登记批次）一律不得绕过。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src import inventory
from src.entities import (
    BatchStatus,
    DispenseDecision,
    DispenseReason,
    DispenseResult,
)
from src.store import DataStore

EARLY_PICKUP_GRACE_DAYS = 3  # 剩余可覆盖天数超过该值视为提前领取

RECALLED_BATCH = "RECALLED_BATCH"
UNKNOWN_BATCH = "UNKNOWN_BATCH"
EARLY_PICKUP = "EARLY_PICKUP"
COURSE_OVERLAP = "COURSE_OVERLAP"
BRAND_SUBSTITUTION = "BRAND_SUBSTITUTION"
EMERGENCY_OVERRIDE = "EMERGENCY_OVERRIDE"

_ADHERENCE_CODES = {EARLY_PICKUP, COURSE_OVERLAP, BRAND_SUBSTITUTION}


def evaluate_purchase(
    store: DataStore,
    customer_id: str,
    drug_id: str,
    batch_no: str,
    quantity: int,
    now: datetime,
    emergency: bool = False,
) -> DispenseResult:
    """评估一次购药，返回允许、阻断或转人工及明确原因。"""
    blocks: list[DispenseReason] = []
    reviews: list[DispenseReason] = []

    batch = store.batches.get((drug_id, batch_no))
    if batch is None:
        reviews.append(DispenseReason(UNKNOWN_BATCH, f"批次 {batch_no} 未登记，需人工核验"))
    elif batch.status is BatchStatus.RECALLED:
        blocks.append(
            DispenseReason(
                RECALLED_BATCH, f"批次 {batch_no} 已召回: {batch.recall_reason or '未说明原因'}"
            )
        )

    plan = store.active_plan_for(customer_id)
    med_plan = store.current_medication_plan(plan.plan_id) if plan is not None else None
    if med_plan is not None:
        active_items = [item for item in med_plan.items if item.course_end >= now]
        requested = store.drugs.get(drug_id)
        same_drug = [item for item in active_items if item.drug_id == drug_id]
        if same_drug:
            item = same_drug[0]
            for other_item in active_items:
                if other_item.drug_id == drug_id:
                    continue
                other = store.drugs.get(other_item.drug_id)
                if other is not None and requested is not None and other.ingredient == requested.ingredient:
                    reviews.append(
                        DispenseReason(
                            COURSE_OVERLAP,
                            f"与计划内在用的 {other.name} 成分相同，疗程重叠",
                        )
                    )
                    break
            last = inventory.last_pickup(store, customer_id, drug_id)
            if last is not None:
                run_out = last.occurred_at + timedelta(days=item.days_supply)
                remaining = (run_out - now).days
                if remaining > EARLY_PICKUP_GRACE_DAYS:
                    reviews.append(
                        DispenseReason(
                            EARLY_PICKUP,
                            f"上次领取可覆盖至 {run_out.date()}，提前 {remaining} 天",
                        )
                    )
        elif requested is not None and active_items:
            for other_item in active_items:
                other = store.drugs.get(other_item.drug_id)
                if other is not None and other.ingredient == requested.ingredient:
                    reviews.append(
                        DispenseReason(
                            BRAND_SUBSTITUTION,
                            f"以 {requested.name} 替代计划内的 {other.name}",
                        )
                    )
                    break

    reasons: list[DispenseReason] = list(blocks)
    if emergency:
        overridden = [r for r in reviews if r.code in _ADHERENCE_CODES]
        kept = [r for r in reviews if r.code not in _ADHERENCE_CODES]
        reasons.extend(kept)
        if overridden:
            reasons.append(
                DispenseReason(
                    EMERGENCY_OVERRIDE,
                    "应急保供放宽: " + "、".join(sorted(r.code for r in overridden)),
                )
            )
    else:
        kept = reviews
        reasons.extend(kept)

    if blocks:
        decision = DispenseDecision.BLOCK
    elif kept:
        decision = DispenseDecision.MANUAL_REVIEW
    else:
        decision = DispenseDecision.ALLOW
    result = DispenseResult(decision, tuple(reasons), emergency)
    store.log(
        now,
        "system",
        "dispense.evaluate",
        customer_id,
        f"{drug_id}/{batch_no} -> {decision.value}",
    )
    return result
