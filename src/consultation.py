"""跨店咨询：先定位当前有效计划与最近的安全结论。

在本店咨询需要健康记录授权；跨店续接还需要跨店授权。
咨询上下文汇总当前用药计划、最近随访结论、未结不良反应、
涉及顾客的召回批次与截至当前已知的测量观察。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.access import ensure_health_access
from src.entities import (
    AdverseReactionReport,
    BatchStatus,
    ConsentScope,
    DrugBatch,
    FollowUp,
    MedicationPlan,
    Observation,
    ServicePlan,
    StaffMember,
)
from src.errors import PlanStateError
from src.store import DataStore


@dataclass(frozen=True)
class ConsultationContext:
    """一次咨询的专业判断基础。"""

    plan: ServicePlan
    medication_plan: MedicationPlan | None
    latest_follow_up: FollowUp | None
    open_adverse_reactions: tuple[AdverseReactionReport, ...]
    recall_alerts: tuple[DrugBatch, ...]
    recent_observations: tuple[Observation, ...]


def open_consultation(
    store: DataStore, member: StaffMember, customer_id: str, store_id: str, now: datetime
) -> ConsultationContext:
    """在门店（含跨店）发起咨询，返回当前有效计划与最近安全结论。"""
    plan = store.active_plan_for(customer_id)
    if plan is None:
        raise PlanStateError("顾客没有进行中的服务计划")
    ensure_health_access(store, member, plan, ConsentScope.HEALTH_RECORDS, now)
    if store_id != plan.home_store_id:
        # 跨店续接需顾客另行授权
        ensure_health_access(store, member, plan, ConsentScope.CROSS_STORE, now)
    follow_ups = store.follow_ups_for(plan.plan_id)
    received = store.customer_received_batches(customer_id)
    recall_alerts = tuple(
        batch
        for key, batch in store.batches.items()
        if key in received and batch.status is BatchStatus.RECALLED
    )
    context = ConsultationContext(
        plan=plan,
        medication_plan=store.current_medication_plan(plan.plan_id),
        latest_follow_up=follow_ups[-1] if follow_ups else None,
        open_adverse_reactions=tuple(store.adrs_for(plan.plan_id)),
        recall_alerts=recall_alerts,
        recent_observations=tuple(store.observations_known_at(plan.plan_id, now)),
    )
    store.log(
        now, member.staff_id, "consultation.open", plan.plan_id, f"门店 {store_id}"
    )
    return context
