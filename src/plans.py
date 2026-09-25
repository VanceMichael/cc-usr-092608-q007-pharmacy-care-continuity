"""服务计划生命周期：选择计划、分别授权、终止与重新启动。

终止服务时撤销全部授权，新的健康访问立即停止；既有交易与
安全处置记录保留在仓储与审计日志中，仍可依法追溯。重启服务
生成递增的新版本计划，并恢复待联系的随访任务。
"""

from __future__ import annotations

from datetime import datetime

from src import tasks
from src.entities import Consent, ConsentScope, PlanStatus, ServicePlan
from src.errors import PlanStateError, ValidationError
from src.store import DataStore


def enroll(
    store: DataStore,
    customer_id: str,
    plan_type: str,
    home_store_id: str,
    pharmacist_id: str,
    consent_scopes: tuple[ConsentScope, ...],
    now: datetime,
) -> ServicePlan:
    """顾客选择服务计划并分别授权（健康记录、提醒、跨店续接）。"""
    store.get_customer(customer_id)
    store.get_pharmacist(pharmacist_id)  # 负责药师必须已登记资质
    if not plan_type.strip():
        raise ValidationError("计划类型不能为空")
    if store.active_plan_for(customer_id) is not None:
        raise PlanStateError("顾客已有进行中的服务计划")
    previous = store.latest_plan_for(customer_id)
    version = previous.version + 1 if previous is not None else 1
    plan = ServicePlan(
        plan_id=store.next_id("plan"),
        customer_id=customer_id,
        plan_type=plan_type,
        home_store_id=home_store_id,
        responsible_pharmacist_id=pharmacist_id,
        status=PlanStatus.ACTIVE,
        version=version,
        started_at=now,
    )
    for scope in consent_scopes:
        plan.consents[scope] = Consent(scope=scope, granted_at=now)
    store.plans[plan.plan_id] = plan
    store.log(
        now,
        customer_id,
        "plan.enroll",
        plan.plan_id,
        f"计划类型 {plan_type}，授权 {[s.value for s in consent_scopes]}",
    )
    return plan


def grant_consent(
    store: DataStore, plan_id: str, scope: ConsentScope, now: datetime
) -> ServicePlan:
    plan = store.get_plan(plan_id)
    if plan.status is not PlanStatus.ACTIVE:
        raise PlanStateError("服务已终止，不能授予授权")
    consent = plan.consents.get(scope)
    if consent is None or consent.revoked_at is not None:
        plan.consents[scope] = Consent(scope=scope, granted_at=now)
    store.log(now, plan.customer_id, "plan.consent.grant", plan_id, scope.value)
    return plan


def revoke_consent(
    store: DataStore, plan_id: str, scope: ConsentScope, now: datetime
) -> ServicePlan:
    plan = store.get_plan(plan_id)
    consent = plan.consents.get(scope)
    if consent is None or consent.revoked_at is not None:
        raise ValidationError(f"授权不存在或已撤销: {scope.value}")
    consent.revoked_at = now
    store.log(now, plan.customer_id, "plan.consent.revoke", plan_id, scope.value)
    return plan


def terminate_plan(
    store: DataStore, plan_id: str, actor_id: str, now: datetime
) -> ServicePlan:
    """终止服务：全部授权撤销，新的健康访问立即停止。"""
    plan = store.get_plan(plan_id)
    if plan.status is PlanStatus.TERMINATED:
        raise PlanStateError("服务计划已终止")
    plan.status = PlanStatus.TERMINATED
    plan.terminated_at = now
    for consent in plan.consents.values():
        if consent.revoked_at is None:
            consent.revoked_at = now
    store.log(now, actor_id, "plan.terminate", plan_id, "服务终止，全部授权撤销")
    return plan


def restart_plan(
    store: DataStore,
    customer_id: str,
    pharmacist_id: str,
    consent_scopes: tuple[ConsentScope, ...],
    now: datetime,
    plan_type: str | None = None,
    home_store_id: str | None = None,
) -> ServicePlan:
    """重新启动服务：生成递增版本的新计划，并恢复待联系任务。"""
    previous = store.latest_plan_for(customer_id)
    if previous is None:
        raise PlanStateError("顾客无历史计划，请直接建档")
    plan = enroll(
        store,
        customer_id,
        plan_type or previous.plan_type,
        home_store_id or previous.home_store_id,
        pharmacist_id,
        consent_scopes,
        now,
    )
    restored = tasks.restore_pending_followups(store, customer_id, now)
    store.log(
        now,
        customer_id,
        "plan.restart",
        plan.plan_id,
        f"服务重启，恢复 {len(restored)} 个待联系任务",
    )
    return plan
