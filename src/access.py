"""角色权限与顾客授权的双重门禁。

任何健康数据访问同时要求：角色具备权限、计划处于有效期、
顾客对应授权未撤销。服务终止后新的健康访问立即停止；
普通店员只能接触领取和配送事项，营销人员不接触健康信息。
"""

from __future__ import annotations

from datetime import datetime

from src.entities import (
    ConsentScope,
    PharmacistProfile,
    PlanStatus,
    Role,
    ServicePlan,
    StaffMember,
)
from src.errors import AccessDeniedError, ConsentError, PlanStateError
from src.store import DataStore

_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.CUSTOMER: frozenset(),
    Role.STORE_STAFF: frozenset({"logistics"}),
    Role.PHARMACIST: frozenset(
        {
            "logistics",
            "health_read",
            "health_write",
            "propose_adjustment",
            "review_adjustment",
        }
    ),
    Role.SAFETY_OFFICER: frozenset(
        {"health_read", "review_adjustment", "recall", "audit_read"}
    ),
    Role.OPERATIONS: frozenset({"logistics", "inventory_manage", "transfer"}),
    Role.MARKETING: frozenset(),  # 营销人员不得接触健康信息
}


def ensure(member: StaffMember, permission: str) -> None:
    """校验角色权限，拒绝时抛出 AccessDeniedError。"""
    if permission not in _PERMISSIONS.get(member.role, frozenset()):
        raise AccessDeniedError(f"角色 {member.role.value} 无权执行 {permission}")


def ensure_pharmacist(
    store: DataStore, member: StaffMember, now: datetime
) -> PharmacistProfile:
    """执业药师且资质在有效期内，返回资质档案。"""
    ensure(member, "health_write")
    profile = store.get_pharmacist(member.staff_id)
    if profile.license_valid_until < now:
        raise AccessDeniedError("药师资质已过有效期")
    return profile


def ensure_plan_access(plan: ServicePlan, scope: ConsentScope, now: datetime) -> None:
    """计划有效且对应授权未撤销；服务终止后立即拒绝。"""
    if plan.status is not PlanStatus.ACTIVE:
        raise PlanStateError("服务已终止，新的健康访问立即停止")
    consent = plan.consents.get(scope)
    if consent is None or not consent.is_active(now):
        raise ConsentError(f"缺少顾客授权: {scope.value}")


def ensure_health_access(
    store: DataStore,
    member: StaffMember,
    plan: ServicePlan,
    scope: ConsentScope,
    now: datetime,
) -> None:
    """健康数据访问的完整门禁：角色 + 计划状态 + 顾客授权。"""
    ensure(member, "health_read")
    ensure_plan_access(plan, scope, now)


def ensure_reviewer(store: DataStore, member: StaffMember, now: datetime) -> None:
    """高风险调整的复核人须为有权限人员（执业药师或安全质量人员）。"""
    ensure(member, "review_adjustment")
    if member.role is Role.PHARMACIST:
        profile = store.get_pharmacist(member.staff_id)
        if profile.license_valid_until < now:
            raise AccessDeniedError("药师资质已过有效期")
