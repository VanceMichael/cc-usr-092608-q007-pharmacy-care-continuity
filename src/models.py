"""领域模型：授权、健康计划、临床记录、库存台账、待办队列。

模型只承载结构，业务规则在 services 层。时间字段统一保存带时区的 ISO-8601
字符串，业务时间（*_at 描述事件发生）与记录时间（recorded_*）成对出现。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    """顾客可分别授权的三件事，授权彼此独立。"""

    HEALTH_RECORDS = "health_records"  # 健康记录读写
    REMINDERS = "reminders"            # 用药/测量提醒与到期随访
    CROSS_STORE = "cross_store"        # 跨店调档与续接


class StaffRole(StrEnum):
    PHARMACIST = "pharmacist"          # 执业药师：可在授权范围内接触健康信息
    CLERK = "clerk"                    # 普通店员：只接触领取与配送事项
    SAFETY_REVIEWER = "safety_reviewer"  # 高风险调整复核人（安全质量岗）
    OPERATOR = "operator"              # 连锁运营：调度，不接触健康内容


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    TERMINATED = "terminated"


class PlanLineStatus(StrEnum):
    EFFECTIVE = "effective"
    SUPERSEDED = "superseded"
    DISCONTINUED = "discontinued"


class ChangeRisk(StrEnum):
    STANDARD = "standard"
    HIGH = "high"


class ChangeStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ObservationKind(StrEnum):
    BLOOD_PRESSURE = "blood_pressure"
    BLOOD_GLUCOSE = "blood_glucose"
    WEIGHT = "weight"
    HEART_RATE = "heart_rate"
    OTHER = "other"


class Adherence(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class ADRStatus(StrEnum):
    REPORTED = "reported"          # 已登记上报
    EVALUATING = "evaluating"      # 评估处置中
    CLOSED = "closed"              # 已出具安全处置结论


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    CLAIMED = "claimed"
    RETURNED = "returned"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MovementKind(StrEnum):
    INBOUND = "inbound"            # 收货入库
    RESERVE = "reserve"            # 预留锁定
    CLAIM = "claim"                # 领取出库
    RETURN = "return"              # 退回入库
    WRITE_OFF = "write_off"        # 报损出库
    TRANSFER_OUT = "transfer_out"  # 调拨出库
    TRANSFER_IN = "transfer_in"    # 调拨入库


class TransferStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    ARRIVED = "arrived"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    MANUAL = "manual"


class ContactReason(StrEnum):
    FOLLOWUP_DUE = "followup_due"      # 到期随访
    RECALL_NOTICE = "recall_notice"    # 批次召回通知
    ADR_FOLLOWUP = "adr_followup"      # 不良反应回访
    TRANSFER_NOTICE = "transfer_notice"  # 调拨药品到达待领取
    RESERVATION_EXPIRY = "reservation_expiry"


class ContactStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


# 原因码：门店界面看到的明确理由
REASON_EARLY_PICKUP = "early_pickup"                  # 提前领取
REASON_COURSE_OVERLAP = "course_overlap"              # 疗程重叠
REASON_SUBSTITUTION = "substitution_requires_review"  # 替代品牌需药师确认
REASON_BATCH_RECALLED = "batch_recalled"              # 召回批次
REASON_PLAN_TERMINATED = "plan_terminated"            # 服务已终止
REASON_NO_STOCK = "insufficient_stock"
REASON_MISSING_CONTEXT = "missing_customer_context"   # 身份/计划无法确认
REASON_HIGH_DOSE = "high_risk_change_pending_review"


@dataclass
class Staff:
    id: str
    name: str
    role: str
    credential_no: str = ""
    credential_valid_until: str = ""  # ISO；空表示该岗位无需执业资质
    store_id: str | None = None


@dataclass
class Customer:
    id: str
    label: str  # 脱敏显示名，不存真实身份信息


@dataclass
class Store:
    id: str
    name: str


@dataclass
class ServicePlan:
    """顾客可选择的服务计划（不同随访频次与服务深度）。"""

    id: str
    name: str
    description: str = ""
    high_risk_drugs: tuple[str, ...] = ()  # 该计划下调整需双人复核的药品


@dataclass
class Enrollment:
    id: str
    customer_id: str
    plan_id: str
    home_store_id: str
    status: str = EnrollmentStatus.ACTIVE
    started_at: str = ""
    terminated_at: str = ""
    terminate_reason: str = ""
    responsible_pharmacist_id: str = ""


@dataclass
class Consent:
    """单项授权记录；同一 scope 可多次给出/撤回，最新一条有效。"""

    id: str
    enrollment_id: str
    scope: str
    granted: bool
    decided_at: str
    revoked_at: str = ""
    statement: str = ""


@dataclass
class Drug:
    id: str
    generic_name: str


@dataclass
class Sku:
    """可售品规：同一通用名可有多个品牌（替代品牌）。"""

    code: str
    drug_id: str
    brand: str
    maker: str = ""


@dataclass
class Batch:
    batch_no: str
    sku: str
    expiry: str = ""          # ISO 到期日
    recall_id: str | None = None  # 非空即批次处于召回中


@dataclass
class Recall:
    id: str
    title: str
    sku: str
    batch_numbers: tuple[str, ...]
    issued_at: str
    closed_at: str = ""
    reason: str = ""


@dataclass
class PrescriberOrder:
    """医嘱摘要：只保存药师服务所需的摘要，不替代处方原件。"""

    id: str
    customer_id: str
    occurred_at: str
    recorded_at: str
    source: str
    summary: str
    recorded_by: str


@dataclass
class PlanLine:
    drug_id: str
    sku: str | None
    dose: str
    frequency: str
    days_supply: int  # 一个疗程天数，用于重叠与提前领取判断


@dataclass
class MedicationPlanVersion:
    """用药计划版本，只能递增；新版本生效时旧版本置 SUPERSEDED。"""

    id: str
    customer_id: str
    seq: int
    status: str
    issued_at: str        # 版本生效时间（业务时间，可补录）
    recorded_at: str
    lines: list[PlanLine]
    note: str = ""
    pharmacist_id: str = ""
    change_id: str | None = None  # 由高风险调整复核通过产生时关联


@dataclass
class Observation:
    id: str
    customer_id: str
    kind: str
    value: str
    unit: str
    occurred_at: str
    recorded_at: str
    store_id: str
    recorded_by: str
    note: str = ""


@dataclass
class FollowUp:
    id: str
    customer_id: str
    occurred_at: str
    recorded_at: str
    store_id: str
    pharmacist_id: str
    adherence: str
    conclusion: str            # 随访结论
    safety_conclusion: str     # 安全结论（跨店咨询首先读取），可为空串
    next_due_at: str = ""      # 下次到期随访
    plan_version_seq: int | None = None


@dataclass
class Referral:
    id: str
    customer_id: str
    occurred_at: str
    recorded_at: str
    pharmacist_id: str
    target: str
    reason: str
    urgency: str = "routine"
    outcome: str = ""


@dataclass
class AdverseEvent:
    id: str
    customer_id: str
    occurred_at: str
    recorded_at: str
    reporter_staff_id: str
    drug_id: str
    batch_no: str
    description: str
    severity: str                      # mild / moderate / severe
    status: str = ADRStatus.REPORTED
    disposition: str = ""              # 安全处置结论（停药/换药/上报等）


@dataclass
class PlanChange:
    """药师提出的计划调整；高风险必须由另一名有权限人复核后生效。"""

    id: str
    customer_id: str
    proposed_by: str
    risk: str
    reason: str
    lines: list[PlanLine]
    status: str = ChangeStatus.PROPOSED
    created_at: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""
    new_version_seq: int | None = None


@dataclass
class StockMovement:
    id: str
    occurred_at: str
    recorded_at: str
    store_id: str
    sku: str
    batch_no: str
    kind: str
    qty: int          # 带符号：入库为正、出库为负
    ref: str = ""     # 预留单/调拨单/报损单号
    operator: str = ""


@dataclass
class Reservation:
    id: str
    customer_id: str
    store_id: str
    sku: str
    batch_no: str
    qty: int
    status: str
    created_at: str
    expires_at: str
    claimed_at: str = ""
    returned_at: str = ""
    emergency: bool = False  # 应急保供预留


@dataclass
class Transfer:
    id: str
    from_store_id: str
    to_store_id: str
    sku: str
    batch_no: str
    qty: int
    status: str
    created_at: str
    arrived_at: str = ""
    reservation_id: str | None = None  # 为某笔预留/顾客调拨时关联


@dataclass
class ContactTask:
    """待联系顾客：多个触发原因合并到同一条开放任务，重启不丢失。"""

    id: str
    customer_id: str
    reasons: list[str]
    store_id: str
    due_at: str
    status: str = ContactStatus.OPEN
    created_at: str = ""
    closed_at: str = ""
    detail: str = ""
    refs: list[str] = field(default_factory=list)


@dataclass
class AccessLogEntry:
    id: str
    occurred_at: str
    staff_id: str
    customer_id: str
    scope: str
    action: str
    granted: bool
    reason: str = ""


@dataclass
class PurchaseAttempt:
    """一次购药请求的决策留痕：允许/阻断/转人工及原因。"""

    id: str
    customer_id: str | None
    store_id: str
    sku: str
    batch_no: str
    qty: int
    occurred_at: str
    recorded_at: str
    decision: str
    reasons: list[str]
    staff_id: str
    reservation_id: str | None = None


@dataclass
class Advice:
    """药师建议的溯源记录：基于何种授权、药品、观察与执业责任。"""

    id: str
    customer_id: str
    issued_at: str
    pharmacist_id: str
    credential_no: str
    enrollment_id: str
    consent_snapshot: dict[str, bool]
    plan_version_seq: int | None
    plan_lines: list[dict[str, Any]]
    evidence_observations: list[str]
    evidence_followups: list[str]
    evidence_orders: list[str]
    rationale: str
    next_step: str          # 顾客看到的“下一步”
    customer_facing: bool = True


def to_row(obj: Any) -> dict[str, Any]:
    """dataclass 转可 JSON 化字典。"""
    return asdict(obj)
