"""医嘱摘要、用药计划、测量观察、随访、转诊、不良反应与用药调整。

所有临床记录要求药师资质有效且顾客健康授权在有效期；随访与调整
记录依据的观察及当时的授权快照，作为后续说明建议依据的材料。
"""

from __future__ import annotations

from datetime import datetime

from src import tasks
from src.access import ensure_health_access, ensure_pharmacist, ensure_reviewer
from src.entities import (
    AdjustmentStatus,
    AdverseReactionReport,
    ConsentScope,
    FollowUp,
    MedicationAdjustment,
    MedicationItem,
    MedicationPlan,
    Observation,
    PrescriptionSummary,
    Referral,
    RiskLevel,
    StaffMember,
    TaskKind,
)
from src.errors import AccessDeniedError, NotFoundError, PlanStateError, ValidationError
from src.store import DataStore


def _clinical_plan(store: DataStore, member: StaffMember, plan_id: str, now: datetime):
    """校验药师资质与健康授权，返回 (资质档案, 计划)。"""
    profile = ensure_pharmacist(store, member, now)
    plan = store.get_plan(plan_id)
    ensure_health_access(store, member, plan, ConsentScope.HEALTH_RECORDS, now)
    return profile, plan


def _validate_items(store: DataStore, items: tuple[MedicationItem, ...]) -> None:
    if not items:
        raise ValidationError("用药计划不能为空")
    for item in items:
        if item.drug_id not in store.drugs:
            raise ValidationError(f"药品未登记: {item.drug_id}")
        if item.course_end < item.course_start:
            raise ValidationError("疗程结束时间早于开始时间")
        if item.frequency_per_day < 1 or item.days_supply < 1:
            raise ValidationError("用药频次与可覆盖天数必须为正数")


def record_prescription_summary(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    prescriber: str,
    content: str,
    issued_at: datetime,
    now: datetime,
) -> PrescriptionSummary:
    """记录医嘱摘要。"""
    _clinical_plan(store, member, plan_id, now)
    if issued_at > now:
        raise ValidationError("医嘱开具时间不能晚于当前时间")
    summary = PrescriptionSummary(
        store.next_id("summary"), plan_id, prescriber, content, issued_at, now
    )
    store.summaries[summary.summary_id] = summary
    store.log(now, member.staff_id, "clinical.summary", plan_id, prescriber)
    return summary


def set_medication_plan(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    items: tuple[MedicationItem, ...],
    now: datetime,
    based_on_summary_id: str | None = None,
) -> MedicationPlan:
    """制定用药计划，版本递增。"""
    _clinical_plan(store, member, plan_id, now)
    _validate_items(store, items)
    if based_on_summary_id is not None and based_on_summary_id not in store.summaries:
        raise NotFoundError(f"医嘱摘要不存在: {based_on_summary_id}")
    version = len(store.medication_plans.get(plan_id, [])) + 1
    med_plan = MedicationPlan(
        plan_id, version, tuple(items), member.staff_id, now, based_on_summary_id
    )
    store.medication_plans.setdefault(plan_id, []).append(med_plan)
    store.log(now, member.staff_id, "clinical.med_plan", plan_id, f"版本 {version}")
    return med_plan


def record_observation(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    kind: str,
    value: str,
    measured_at: datetime,
    now: datetime,
) -> Observation:
    """记录测量观察；measured_at 为真实发生时间，支持断网后补录。"""
    _clinical_plan(store, member, plan_id, now)
    if measured_at > now:
        raise ValidationError("测量时间不能晚于录入时间")
    observation = Observation(
        store.next_id("obs"), plan_id, kind, value, measured_at, now, member.staff_id
    )
    store.observations[observation.observation_id] = observation
    return observation


def record_follow_up(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    conclusion: str,
    risk_level: RiskLevel,
    basis_observation_ids: tuple[str, ...],
    next_due_at: datetime | None,
    now: datetime,
) -> FollowUp:
    """记录随访结论；到期时间生成待联系任务。"""
    plan = store.get_plan(plan_id)
    _clinical_plan(store, member, plan_id, now)
    for observation_id in basis_observation_ids:
        observation = store.observations.get(observation_id)
        if observation is None or observation.plan_id != plan_id:
            raise ValidationError(f"依据观察不属于该计划: {observation_id}")
    follow_up = FollowUp(
        follow_up_id=store.next_id("fu"),
        plan_id=plan_id,
        pharmacist_id=member.staff_id,
        conclusion=conclusion,
        risk_level=risk_level,
        basis_observation_ids=tuple(basis_observation_ids),
        consent_snapshot=plan.active_consents(now),
        created_at=now,
        next_due_at=next_due_at,
    )
    store.follow_ups[follow_up.follow_up_id] = follow_up
    if next_due_at is not None:
        tasks.create_task(
            store,
            plan.customer_id,
            TaskKind.FOLLOW_UP,
            next_due_at,
            f"随访到期: {conclusion}",
            now,
            source=follow_up.follow_up_id,
        )
    store.log(now, member.staff_id, "clinical.follow_up", plan_id, conclusion)
    return follow_up


def record_referral(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    reason: str,
    target: str,
    now: datetime,
) -> Referral:
    """记录转诊建议。"""
    _clinical_plan(store, member, plan_id, now)
    referral = Referral(
        store.next_id("ref"), plan_id, member.staff_id, reason, target, now
    )
    store.referrals[referral.referral_id] = referral
    store.log(now, member.staff_id, "clinical.referral", plan_id, target)
    return referral


def report_adverse_reaction(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    drug_id: str,
    batch_no: str | None,
    description: str,
    severity: RiskLevel,
    now: datetime,
) -> AdverseReactionReport:
    """记录不良反应报告；涉及药品的后续调整按高风险处理。"""
    _clinical_plan(store, member, plan_id, now)
    if drug_id not in store.drugs:
        raise ValidationError(f"药品未登记: {drug_id}")
    report = AdverseReactionReport(
        store.next_id("adr"),
        plan_id,
        drug_id,
        batch_no,
        description,
        severity,
        member.staff_id,
        now,
    )
    store.adr_reports[report.report_id] = report
    store.log(
        now, member.staff_id, "clinical.adr", plan_id, f"{drug_id} {severity.value}"
    )
    return report


def propose_adjustment(
    store: DataStore,
    member: StaffMember,
    plan_id: str,
    new_items: tuple[MedicationItem, ...],
    note: str,
    basis_observation_ids: tuple[str, ...],
    now: datetime,
) -> MedicationAdjustment:
    """药师提出用药调整；高风险调整进入待复核，其余直接生效。"""
    _clinical_plan(store, member, plan_id, now)
    _validate_items(store, new_items)
    for observation_id in basis_observation_ids:
        observation = store.observations.get(observation_id)
        if observation is None or observation.plan_id != plan_id:
            raise ValidationError(f"依据观察不属于该计划: {observation_id}")
    plan = store.get_plan(plan_id)
    risk = _assess_risk(store, plan_id, new_items)
    status = (
        AdjustmentStatus.PENDING_REVIEW if risk is RiskLevel.HIGH else AdjustmentStatus.APPROVED
    )
    adjustment = MedicationAdjustment(
        adjustment_id=store.next_id("adj"),
        plan_id=plan_id,
        proposed_by=member.staff_id,
        proposed_at=now,
        note=note,
        new_items=tuple(new_items),
        risk_level=risk,
        status=status,
        basis_observation_ids=tuple(basis_observation_ids),
        consent_snapshot=plan.active_consents(now),
    )
    store.adjustments[adjustment.adjustment_id] = adjustment
    store.log(
        now, member.staff_id, "clinical.adjust.propose", plan_id, f"风险 {risk.value}"
    )
    if status is AdjustmentStatus.APPROVED:
        _apply_adjustment(store, adjustment, now)
    return adjustment


def review_adjustment(
    store: DataStore,
    member: StaffMember,
    adjustment_id: str,
    approve: bool,
    note: str,
    now: datetime,
) -> MedicationAdjustment:
    """高风险调整由另一名有权限人员复核；通过后才生效。"""
    ensure_reviewer(store, member, now)
    adjustment = store.adjustments.get(adjustment_id)
    if adjustment is None:
        raise NotFoundError(f"调整不存在: {adjustment_id}")
    if adjustment.status is not AdjustmentStatus.PENDING_REVIEW:
        raise PlanStateError("该调整不在待复核状态")
    if member.staff_id == adjustment.proposed_by:
        raise AccessDeniedError("复核人不能是提出人")
    adjustment.reviewed_by = member.staff_id
    adjustment.reviewed_at = now
    adjustment.review_note = note
    adjustment.status = (
        AdjustmentStatus.APPROVED if approve else AdjustmentStatus.REJECTED
    )
    store.log(
        now,
        member.staff_id,
        "clinical.adjust.review",
        adjustment.plan_id,
        "通过" if approve else "驳回",
    )
    if approve:
        _apply_adjustment(store, adjustment, now)
    return adjustment


def _assess_risk(
    store: DataStore, plan_id: str, items: tuple[MedicationItem, ...]
) -> RiskLevel:
    """高风险药品或曾发生不良反应的药品，其调整按高风险复核。"""
    adr_drugs = {report.drug_id for report in store.adrs_for(plan_id)}
    for item in items:
        drug = store.drugs.get(item.drug_id)
        if drug is not None and drug.high_risk:
            return RiskLevel.HIGH
        if item.drug_id in adr_drugs:
            return RiskLevel.HIGH
    return RiskLevel.LOW


def _apply_adjustment(
    store: DataStore, adjustment: MedicationAdjustment, now: datetime
) -> MedicationPlan:
    """调整生效：生成递增版本的用药计划。"""
    version = len(store.medication_plans.get(adjustment.plan_id, [])) + 1
    med_plan = MedicationPlan(
        adjustment.plan_id, version, adjustment.new_items, adjustment.proposed_by, now
    )
    store.medication_plans.setdefault(adjustment.plan_id, []).append(med_plan)
    return med_plan
