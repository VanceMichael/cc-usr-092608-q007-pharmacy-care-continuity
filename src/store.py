"""内存数据仓储与审计日志。

既有交易（库存台账）与安全处置（不良反应、召回、复核）以追加式
记录保存；服务计划终止后，这些记录仍可依法追溯。
"""

from __future__ import annotations

from datetime import datetime

from src.entities import (
    AdverseReactionReport,
    AuditEntry,
    ContactTask,
    Customer,
    DrugBatch,
    DrugInfo,
    FollowUp,
    LedgerEvent,
    MedicationAdjustment,
    MedicationPlan,
    Observation,
    PharmacistProfile,
    PlanStatus,
    PrescriptionSummary,
    Referral,
    Reservation,
    ReservationStatus,
    ServicePlan,
    StaffMember,
    TaskKind,
    TaskStatus,
)
from src.errors import NotFoundError


class DataStore:
    """聚合所有实体集合的内存仓储。"""

    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {}
        self.staff: dict[str, StaffMember] = {}
        self.pharmacists: dict[str, PharmacistProfile] = {}
        self.drugs: dict[str, DrugInfo] = {}
        self.plans: dict[str, ServicePlan] = {}
        self.summaries: dict[str, PrescriptionSummary] = {}
        self.medication_plans: dict[str, list[MedicationPlan]] = {}
        self.observations: dict[str, Observation] = {}
        self.follow_ups: dict[str, FollowUp] = {}
        self.referrals: dict[str, Referral] = {}
        self.adr_reports: dict[str, AdverseReactionReport] = {}
        self.adjustments: dict[str, MedicationAdjustment] = {}
        self.batches: dict[tuple[str, str], DrugBatch] = {}
        self.ledger: list[LedgerEvent] = []
        self.reservations: dict[str, Reservation] = {}
        self.tasks: dict[str, ContactTask] = {}
        self.audit: list[AuditEntry] = []
        self._counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]}"

    def log(
        self, at: datetime, actor_id: str, action: str, subject: str, detail: str
    ) -> AuditEntry:
        entry = AuditEntry(self.next_id("audit"), at, actor_id, action, subject, detail)
        self.audit.append(entry)
        return entry

    # --- 基础对象 ---

    def get_customer(self, customer_id: str) -> Customer:
        customer = self.customers.get(customer_id)
        if customer is None:
            raise NotFoundError(f"顾客不存在: {customer_id}")
        return customer

    def get_member(self, staff_id: str) -> StaffMember:
        member = self.staff.get(staff_id)
        if member is None:
            raise NotFoundError(f"工作人员不存在: {staff_id}")
        return member

    def get_pharmacist(self, pharmacist_id: str) -> PharmacistProfile:
        profile = self.pharmacists.get(pharmacist_id)
        if profile is None:
            raise NotFoundError(f"药师资质未登记: {pharmacist_id}")
        return profile

    def get_plan(self, plan_id: str) -> ServicePlan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"服务计划不存在: {plan_id}")
        return plan

    # --- 计划与临床记录查询 ---

    def active_plan_for(self, customer_id: str) -> ServicePlan | None:
        plans = [
            p
            for p in self.plans.values()
            if p.customer_id == customer_id and p.status is PlanStatus.ACTIVE
        ]
        return max(plans, key=lambda p: p.version, default=None)

    def latest_plan_for(self, customer_id: str) -> ServicePlan | None:
        plans = [p for p in self.plans.values() if p.customer_id == customer_id]
        return max(plans, key=lambda p: p.version, default=None)

    def current_medication_plan(self, plan_id: str) -> MedicationPlan | None:
        versions = self.medication_plans.get(plan_id) or []
        return versions[-1] if versions else None

    def observations_for(self, plan_id: str) -> list[Observation]:
        return sorted(
            (o for o in self.observations.values() if o.plan_id == plan_id),
            key=lambda o: o.recorded_at,
        )

    def observations_known_at(self, plan_id: str, as_of: datetime) -> list[Observation]:
        """截至某录入时间已知的观察；晚到的补录只影响之后的建议。"""
        return [o for o in self.observations_for(plan_id) if o.recorded_at <= as_of]

    def follow_ups_for(self, plan_id: str) -> list[FollowUp]:
        return sorted(
            (f for f in self.follow_ups.values() if f.plan_id == plan_id),
            key=lambda f: f.created_at,
        )

    def adrs_for(self, plan_id: str) -> list[AdverseReactionReport]:
        return [a for a in self.adr_reports.values() if a.plan_id == plan_id]

    # --- 物流与任务查询 ---

    def open_reservations(
        self, customer_id: str | None = None, store_id: str | None = None
    ) -> list[Reservation]:
        result = [
            r for r in self.reservations.values() if r.status is ReservationStatus.OPEN
        ]
        if customer_id is not None:
            result = [r for r in result if r.customer_id == customer_id]
        if store_id is not None:
            result = [r for r in result if r.store_id == store_id]
        return result

    def pending_tasks(
        self, customer_id: str | None = None, kind: TaskKind | None = None
    ) -> list[ContactTask]:
        tasks = [t for t in self.tasks.values() if t.status is TaskStatus.PENDING]
        if customer_id is not None:
            tasks = [t for t in tasks if t.customer_id == customer_id]
        if kind is not None:
            tasks = [t for t in tasks if t.kind is kind]
        return sorted(tasks, key=lambda t: t.due_at)

    def customer_received_batches(self, customer_id: str) -> set[tuple[str, str]]:
        """顾客已领取的 (药品, 批次) 集合，来自已履约的预留。"""
        return {
            (r.drug_id, r.batch_no)
            for r in self.reservations.values()
            if r.customer_id == customer_id and r.status is ReservationStatus.FULFILLED
        }
