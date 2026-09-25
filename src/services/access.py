"""访问控制：角色边界、分项授权与每次健康访问的留痕。

- 普通店员（clerk）只能接触领取/配送事项，任何健康 scope 一律拒绝；
- 执业药师在顾客对应授权有效时可访问健康信息，且须持有效执业资质；
- 安全复核人可在复核所需范围内读取健康记录，但不能复核自己提出的调整；
- 运营人员不可接触健康内容；
- 每次授权判定都写访问日志（含拒绝原因），顾客终止授权后新的访问立即失败。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.errors import AuthorizationError, CredentialError
from src.models import Scope, StaffRole
from src.repository import Repository

# 各角色可访问的健康 scope
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    StaffRole.PHARMACIST: frozenset(
        {Scope.HEALTH_RECORDS, Scope.REMINDERS, Scope.CROSS_STORE}
    ),
    StaffRole.SAFETY_REVIEWER: frozenset({Scope.HEALTH_RECORDS}),
    StaffRole.CLERK: frozenset(),
    StaffRole.OPERATOR: frozenset(),
}


class AccessService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ---- 授权状态 -------------------------------------------------
    def effective_consents(
        self, enrollment_id: str, at: str | None = None
    ) -> dict[str, bool]:
        """返回每个 scope 在指定时刻的有效授权。

        授权行在 decided_at 生效；若 revoked_at 有值，则在该时刻撤回。
        按 scope 取时刻之前最后一个事件，晚到的授权决定不追溯生效。
        """
        moment = timeutil.parse(at) if at else timeutil.now()
        events: dict[str, tuple[Any, bool]] = {}
        for consent in self.repo.filter("consents", enrollment_id=enrollment_id):
            scope = consent["scope"]
            decided = timeutil.parse(consent["decided_at"])
            if decided <= moment:
                current = events.get(scope)
                if current is None or decided >= current[0]:
                    events[scope] = (decided, bool(consent["granted"]))
            revoked_at = consent.get("revoked_at")
            if revoked_at:
                revoked = timeutil.parse(revoked_at)
                if revoked <= moment:
                    current = events.get(scope)
                    if current is None or revoked >= current[0]:
                        events[scope] = (revoked, False)
        return {scope: state[1] for scope, state in events.items()}

    def active_enrollment(self, customer_id: str, at: str | None = None) -> dict[str, Any] | None:
        moment = timeutil.parse(at) if at else timeutil.now()
        candidates = [
            row
            for row in self.repo.filter("enrollments", customer_id=customer_id, status="active")
            if timeutil.parse(row["started_at"]) <= moment
        ]
        return candidates[-1] if candidates else None

    # ---- 资质 -----------------------------------------------------
    def assert_practicing_pharmacist(
        self, staff: dict[str, Any], at: str | None = None
    ) -> None:
        moment = timeutil.parse(at) if at else timeutil.now()
        if staff["role"] != StaffRole.PHARMACIST:
            raise AuthorizationError(f"{staff['name']}不是执业药师，不能执行专业操作")
        if not staff.get("credential_no"):
            raise CredentialError("药师执业资质编号缺失")
        valid_until = staff.get("credential_valid_until")
        if valid_until and timeutil.parse(valid_until) < moment:
            raise CredentialError("药师执业资质已过期")

    # ---- 访问判定 -------------------------------------------------
    def check_health(
        self,
        customer_id: str,
        staff_id: str,
        scope: str,
        action: str,
        at: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """判定一次健康信息访问；无论允许与否都留痕，拒绝时抛异常。

        at 为业务时间（授权/资质按此时刻判定）；recorded_at 为入库时间，
        在线录入默认等于业务时间，断网补录由调用方显式给出更晚的时刻。
        """
        moment = timeutil.parse(at) if at else timeutil.now()
        moment_iso = timeutil.format_value(moment)
        recorded_iso = recorded_at or moment_iso
        staff = self.repo.find("staff", id=staff_id)
        if staff is None:
            raise AuthorizationError(f"人员不存在：{staff_id}")

        granted, reason = self._evaluate(customer_id, staff, scope, at)
        self._log(customer_id, staff_id, scope, action, granted, reason, recorded_iso)
        if not granted:
            raise AuthorizationError(reason)
        enrollment = self.active_enrollment(customer_id, at)
        return {"staff": staff, "enrollment": enrollment, "at": moment_iso, "recorded_at": recorded_iso}

    def _evaluate(
        self,
        customer_id: str,
        staff: dict[str, Any],
        scope: str,
        at: str | None,
    ) -> tuple[bool, str]:
        allowed_scopes = _ROLE_SCOPES.get(staff["role"], frozenset())
        if scope not in allowed_scopes:
            return False, f"角色{staff['role']}无权访问{scope}，仅可处理领取与配送事项"
        enrollment = self.active_enrollment(customer_id, at)
        if enrollment is None:
            return False, "顾客当前没有有效服务计划"
        consents = self.effective_consents(enrollment["id"], at)
        if not consents.get(scope, False):
            return False, f"顾客未授权或已撤回{scope}"
        if staff["role"] == StaffRole.PHARMACIST:
            try:
                self.assert_practicing_pharmacist(staff, at)
            except (AuthorizationError, CredentialError) as exc:
                return False, str(exc)
        return True, "ok"

    def assert_fulfillment(
        self, customer_id: str, staff_id: str, action: str, at: str | None = None
    ) -> dict[str, Any]:
        """领取/配送事项的访问：店员与药师可办，看不到健康内容。"""
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        staff = self.repo.find("staff", id=staff_id)
        if staff is None or staff["role"] not in (StaffRole.CLERK, StaffRole.PHARMACIST):
            self._log(customer_id, staff_id, "fulfillment", action, False, "无领取配送权限", moment_iso)
            raise AuthorizationError("该人员不能办理领取与配送")
        self._log(customer_id, staff_id, "fulfillment", action, True, "ok", moment_iso)
        return {"staff": staff, "at": moment_iso}

    def _log(
        self,
        customer_id: str,
        staff_id: str,
        scope: str,
        action: str,
        granted: bool,
        reason: str,
        at_iso: str,
    ) -> None:
        self.repo.add(
            "access_logs",
            {
                "id": f"log_{uuid.uuid4().hex[:10]}",
                "occurred_at": at_iso,
                "staff_id": staff_id,
                "customer_id": customer_id,
                "scope": scope,
                "action": action,
                "granted": granted,
                "reason": reason,
            },
        )
