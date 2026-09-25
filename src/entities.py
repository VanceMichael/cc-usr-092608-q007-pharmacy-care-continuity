"""药店健康管理后端的核心实体与枚举。

时间约定：所有时间均为带时区的 datetime。
- occurred_at / measured_at 表示真实发生时间（断网补录时早于录入时间）；
- recorded_at / created_at 表示系统录入时间。
晚到的补录信息只影响其录入时间之后的建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Role(str, Enum):
    """系统内的人员角色。"""

    CUSTOMER = "customer"
    STORE_STAFF = "store_staff"  # 普通店员：仅领取与配送事项
    PHARMACIST = "pharmacist"  # 执业药师
    SAFETY_OFFICER = "safety_officer"  # 安全质量人员
    OPERATIONS = "operations"  # 连锁运营人员
    MARKETING = "marketing"  # 营销人员：不得接触健康信息


class ConsentScope(str, Enum):
    """顾客可分别授权的范围。"""

    HEALTH_RECORDS = "health_records"  # 健康记录
    REMINDERS = "reminders"  # 用药与随访提醒
    CROSS_STORE = "cross_store"  # 跨店续接


class PlanStatus(str, Enum):
    ACTIVE = "active"
    TERMINATED = "terminated"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AdjustmentStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DispenseDecision(str, Enum):
    ALLOW = "allow"  # 允许
    BLOCK = "block"  # 阻断
    MANUAL_REVIEW = "manual_review"  # 转人工


class BatchStatus(str, Enum):
    USABLE = "usable"
    RECALLED = "recalled"


class LedgerKind(str, Enum):
    """库存台账事件类型，覆盖预留到领取、退回、报损全链路。"""

    RECEIVED = "received"  # 入库
    RESERVED = "reserved"  # 预留
    RESERVATION_RELEASED = "reservation_released"  # 预留释放
    PICKED_UP = "picked_up"  # 领取
    RETURNED = "returned"  # 退回
    WRITTEN_OFF = "written_off"  # 报损
    TRANSFER_OUT = "transfer_out"  # 调拨出
    TRANSFER_IN = "transfer_in"  # 调拨入


class ReservationStatus(str, Enum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    RELEASED = "released"


class TaskKind(str, Enum):
    FOLLOW_UP = "follow_up"  # 到期随访
    RECALL_CONTACT = "recall_contact"  # 批次召回联系
    TRANSFER_REROUTE = "transfer_reroute"  # 调拨改址协调


class TaskStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Customer:
    customer_id: str
    display_name: str


@dataclass(frozen=True)
class StaffMember:
    staff_id: str
    display_name: str
    role: Role
    store_id: str | None = None


@dataclass(frozen=True)
class PharmacistProfile:
    """负责药师的执业资质。"""

    pharmacist_id: str
    display_name: str
    license_no: str
    qualifications: tuple[str, ...]
    license_valid_until: datetime


@dataclass(frozen=True)
class DrugInfo:
    drug_id: str
    name: str
    ingredient: str  # 通用成分，用于替代品牌与疗程重叠判断
    high_risk: bool = False


@dataclass
class Consent:
    scope: ConsentScope
    granted_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, at: datetime) -> bool:
        return self.granted_at <= at and (self.revoked_at is None or at < self.revoked_at)


@dataclass
class ServicePlan:
    """顾客选择的服务计划；重启服务时版本递增，历史计划保留。"""

    plan_id: str
    customer_id: str
    plan_type: str
    home_store_id: str
    responsible_pharmacist_id: str
    status: PlanStatus
    version: int
    started_at: datetime
    terminated_at: datetime | None = None
    consents: dict[ConsentScope, Consent] = field(default_factory=dict)

    def active_consents(self, at: datetime) -> tuple[ConsentScope, ...]:
        return tuple(
            scope for scope, consent in self.consents.items() if consent.is_active(at)
        )


@dataclass(frozen=True)
class PrescriptionSummary:
    """医嘱摘要。"""

    summary_id: str
    plan_id: str
    prescriber: str
    content: str
    issued_at: datetime
    recorded_at: datetime


@dataclass(frozen=True)
class MedicationItem:
    drug_id: str
    dose: str
    frequency_per_day: int
    course_start: datetime
    course_end: datetime
    days_supply: int  # 单次领取量可覆盖天数


@dataclass(frozen=True)
class MedicationPlan:
    """用药计划，版本递增。"""

    plan_id: str
    version: int
    items: tuple[MedicationItem, ...]
    set_by: str
    set_at: datetime
    based_on_summary_id: str | None = None


@dataclass(frozen=True)
class Observation:
    """测量观察；measured_at 为真实发生时间，支持断网后补录。"""

    observation_id: str
    plan_id: str
    kind: str
    value: str
    measured_at: datetime
    recorded_at: datetime
    recorded_by: str


@dataclass(frozen=True)
class FollowUp:
    """随访结论；记录依据的观察与当时的授权快照。"""

    follow_up_id: str
    plan_id: str
    pharmacist_id: str
    conclusion: str
    risk_level: RiskLevel
    basis_observation_ids: tuple[str, ...]
    consent_snapshot: tuple[ConsentScope, ...]
    created_at: datetime
    next_due_at: datetime | None = None


@dataclass(frozen=True)
class Referral:
    """转诊建议。"""

    referral_id: str
    plan_id: str
    pharmacist_id: str
    reason: str
    target: str
    created_at: datetime


@dataclass(frozen=True)
class AdverseReactionReport:
    """不良反应报告。"""

    report_id: str
    plan_id: str
    drug_id: str
    batch_no: str | None
    description: str
    severity: RiskLevel
    reported_by: str
    reported_at: datetime


@dataclass
class MedicationAdjustment:
    """药师提出的用药调整；高风险时需另一名有权限人员复核。"""

    adjustment_id: str
    plan_id: str
    proposed_by: str
    proposed_at: datetime
    note: str
    new_items: tuple[MedicationItem, ...]
    risk_level: RiskLevel
    status: AdjustmentStatus
    basis_observation_ids: tuple[str, ...]
    consent_snapshot: tuple[ConsentScope, ...]
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None


@dataclass(frozen=True)
class DrugBatch:
    drug_id: str
    batch_no: str
    status: BatchStatus
    recall_reason: str | None = None
    recalled_at: datetime | None = None


@dataclass(frozen=True)
class LedgerEvent:
    """库存台账事件；occurred_at 支持断网补录真实发生时间。"""

    event_id: str
    store_id: str
    drug_id: str
    batch_no: str
    kind: LedgerKind
    quantity: int
    occurred_at: datetime
    recorded_at: datetime
    reference: str | None = None


@dataclass
class Reservation:
    """库存预留；channel 区分领取与配送。"""

    reservation_id: str
    customer_id: str
    store_id: str
    drug_id: str
    batch_no: str
    quantity: int
    status: ReservationStatus
    created_at: datetime
    channel: str = "pickup"


@dataclass
class ContactTask:
    """待联系任务；source 用于按来源去重，related 用于任务间协调。"""

    task_id: str
    customer_id: str
    kind: TaskKind
    status: TaskStatus
    due_at: datetime
    created_at: datetime
    detail: str
    source: str
    related: tuple[str, ...] = ()
    completed_at: datetime | None = None


@dataclass(frozen=True)
class AuditEntry:
    """审计日志；服务终止后仍可依法追溯。"""

    entry_id: str
    at: datetime
    actor_id: str
    action: str
    subject: str
    detail: str


@dataclass(frozen=True)
class DispenseReason:
    code: str
    message: str


@dataclass(frozen=True)
class DispenseResult:
    decision: DispenseDecision
    reasons: tuple[DispenseReason, ...]
    emergency: bool = False
