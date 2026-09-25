"""顾客视图、店员物流看板、药师建议依据说明与合规追溯。

顾客看到的是自己的当前方案和下一步；普通店员的看板只含领取
和配送事项；药师的每条建议都能说明基于何种授权、药品、观察
与执业责任；服务终止后既有交易和安全处置仍可依法追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.access import ensure
from src.entities import (
    AdverseReactionReport,
    AuditEntry,
    LedgerEvent,
    LedgerKind,
    Observation,
    PharmacistProfile,
    StaffMember,
)
from src.errors import NotFoundError
from src.store import DataStore


@dataclass(frozen=True)
class CustomerView:
    """顾客视角：当前方案与下一步。"""

    customer_id: str
    plan_status: str  # active / terminated / none
    plan_type: str | None
    medications: tuple[str, ...]
    next_follow_up_at: datetime | None
    pending_pickups: tuple[str, ...]
    next_steps: tuple[str, ...]


def customer_view(store: DataStore, customer_id: str, now: datetime) -> CustomerView:
    plan = store.active_plan_for(customer_id)
    if plan is None:
        latest = store.latest_plan_for(customer_id)
        status = "terminated" if latest is not None else "none"
        return CustomerView(
            customer_id, status, None, (), None, (), ("当前没有进行中的服务方案",)
        )
    med_plan = store.current_medication_plan(plan.plan_id)
    medications = tuple(
        f"{store.drugs[item.drug_id].name} {item.dose}，每日{item.frequency_per_day}次，"
        f"疗程至{item.course_end.date()}"
        for item in (med_plan.items if med_plan else ())
        if item.drug_id in store.drugs
    )
    follow_ups = store.follow_ups_for(plan.plan_id)
    next_follow_up = follow_ups[-1].next_due_at if follow_ups else None
    pickups = tuple(
        f"{store.drugs[r.drug_id].name} x{r.quantity}（{'配送' if r.channel == 'delivery' else '领取'}，{r.store_id}）"
        for r in store.open_reservations(customer_id=customer_id)
        if r.drug_id in store.drugs
    )
    steps: list[str] = []
    if next_follow_up is not None:
        steps.append(f"下次随访：{next_follow_up.date()}")
    steps.extend(f"待领取/配送：{text}" for text in pickups)
    if not steps:
        steps.append("按当前方案继续用药")
    return CustomerView(
        customer_id=customer_id,
        plan_status="active",
        plan_type=plan.plan_type,
        medications=medications,
        next_follow_up_at=next_follow_up,
        pending_pickups=pickups,
        next_steps=tuple(steps),
    )


@dataclass(frozen=True)
class PickupEntry:
    """店员看板条目：仅物流信息，不含诊断、计划、观察等健康数据。"""

    reservation_id: str
    customer_name: str
    drug_name: str
    quantity: int
    channel: str
    status: str


def staff_pickup_board(
    store: DataStore, member: StaffMember, store_id: str, now: datetime
) -> list[PickupEntry]:
    """普通店员的领取/配送看板。"""
    ensure(member, "logistics")
    entries = []
    for reservation in store.open_reservations(store_id=store_id):
        customer = store.customers.get(reservation.customer_id)
        drug = store.drugs.get(reservation.drug_id)
        entries.append(
            PickupEntry(
                reservation_id=reservation.reservation_id,
                customer_name=customer.display_name if customer else reservation.customer_id,
                drug_name=drug.name if drug else reservation.drug_id,
                quantity=reservation.quantity,
                channel=reservation.channel,
                status=reservation.status.value,
            )
        )
    return entries


@dataclass(frozen=True)
class Rationale:
    """建议依据说明：授权、药品、观察与执业责任。"""

    subject_id: str
    kind: str  # adjustment / follow_up
    consents: tuple[str, ...]
    drugs: tuple[str, ...]
    observations: tuple[Observation, ...]
    proposed_by: PharmacistProfile
    reviewed_by: str | None
    explanation: str


def adjustment_rationale(
    store: DataStore, member: StaffMember, adjustment_id: str, now: datetime
) -> Rationale:
    """说明一次用药调整基于何种授权、药品、观察与执业责任。"""
    ensure(member, "health_read")
    adjustment = store.adjustments.get(adjustment_id)
    if adjustment is None:
        raise NotFoundError(f"调整不存在: {adjustment_id}")
    observations = tuple(
        store.observations[o] for o in adjustment.basis_observation_ids if o in store.observations
    )
    drugs = tuple(
        store.drugs[item.drug_id].name
        for item in adjustment.new_items
        if item.drug_id in store.drugs
    )
    profile = store.get_pharmacist(adjustment.proposed_by)
    explanation = (
        f"调整由药师{profile.display_name}（执照{profile.license_no}）基于"
        f"授权[{', '.join(s.value for s in adjustment.consent_snapshot)}]、"
        f"药品[{', '.join(drugs)}]、{len(observations)}条观察提出，"
        f"风险等级{adjustment.risk_level.value}"
    )
    if adjustment.reviewed_by is not None:
        explanation += f"，经{adjustment.reviewed_by}复核（{adjustment.review_note or '无复核意见'}）"
    return Rationale(
        subject_id=adjustment.adjustment_id,
        kind="adjustment",
        consents=tuple(s.value for s in adjustment.consent_snapshot),
        drugs=drugs,
        observations=observations,
        proposed_by=profile,
        reviewed_by=adjustment.reviewed_by,
        explanation=explanation,
    )


def follow_up_rationale(
    store: DataStore, member: StaffMember, follow_up_id: str, now: datetime
) -> Rationale:
    """说明一次随访结论的依据。"""
    ensure(member, "health_read")
    follow_up = store.follow_ups.get(follow_up_id)
    if follow_up is None:
        raise NotFoundError(f"随访不存在: {follow_up_id}")
    observations = tuple(
        store.observations[o] for o in follow_up.basis_observation_ids if o in store.observations
    )
    med_plan = store.current_medication_plan(follow_up.plan_id)
    drugs = tuple(
        store.drugs[item.drug_id].name
        for item in (med_plan.items if med_plan else ())
        if item.drug_id in store.drugs
    )
    profile = store.get_pharmacist(follow_up.pharmacist_id)
    explanation = (
        f"随访结论由药师{profile.display_name}（执照{profile.license_no}）基于"
        f"授权[{', '.join(s.value for s in follow_up.consent_snapshot)}]、"
        f"药品[{', '.join(drugs)}]、{len(observations)}条观察作出：{follow_up.conclusion}"
    )
    return Rationale(
        subject_id=follow_up.follow_up_id,
        kind="follow_up",
        consents=tuple(s.value for s in follow_up.consent_snapshot),
        drugs=drugs,
        observations=observations,
        proposed_by=profile,
        reviewed_by=None,
        explanation=explanation,
    )


@dataclass(frozen=True)
class ComplianceTrace:
    """服务终止后的合规追溯：交易、安全处置与访问记录。"""

    audit: tuple[AuditEntry, ...]
    transactions: tuple[LedgerEvent, ...]
    safety_reports: tuple[AdverseReactionReport, ...]


def compliance_trace(
    store: DataStore, member: StaffMember, customer_id: str, now: datetime
) -> ComplianceTrace:
    """依法追溯既有交易与安全处置；仅安全质量人员可查。"""
    ensure(member, "audit_read")
    plan_ids = {
        plan.plan_id for plan in store.plans.values() if plan.customer_id == customer_id
    }
    audit = tuple(
        entry
        for entry in store.audit
        if entry.subject == customer_id or entry.subject in plan_ids
    )
    reservation_ids = {
        r.reservation_id for r in store.reservations.values() if r.customer_id == customer_id
    }
    transactions = tuple(
        event
        for event in store.ledger
        if event.kind in (LedgerKind.PICKED_UP, LedgerKind.RETURNED)
        and event.reference in reservation_ids
    )
    safety_reports = tuple(
        report
        for report in store.adr_reports.values()
        if report.plan_id in plan_ids
    )
    return ComplianceTrace(audit=audit, transactions=transactions, safety_reports=safety_reports)
