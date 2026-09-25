"""建议溯源与顾客视图。

药师出具的每条建议都固化证据快照：当时的授权状态、用药计划版本、
所依据的观察/随访/医嘱、药师资质与执业责任。顾客端只看到当前方案与下一步。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.models import Advice, Scope, to_row
from src.repository import Repository
from src.services.access import AccessService
from src.services.records import RecordService, visible_at


class AdviceService:
    def __init__(
        self,
        repo: Repository,
        access: AccessService,
        records: RecordService,
    ) -> None:
        self.repo = repo
        self.access = access
        self.records = records

    def issue_advice(
        self,
        customer_id: str,
        pharmacist_id: str,
        rationale: str,
        next_step: str,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """药师基于当前授权、计划与证据出具建议，并固化可追溯快照。"""
        business_iso = timeutil.format_value(
            timeutil.parse(occurred_at) if occurred_at else timeutil.now()
        )
        ctx = self.access.check_health(
            customer_id, pharmacist_id, Scope.HEALTH_RECORDS, "issue_advice", business_iso
        )
        staff = ctx["staff"]
        self.access.assert_practicing_pharmacist(staff, business_iso)
        enrollment = ctx["enrollment"]

        consents = self.access.effective_consents(enrollment["id"], business_iso)
        plan = self.records.effective_plan(customer_id, business_iso)

        observations = visible_at(
            self.repo.filter("observations", customer_id=customer_id), business_iso
        )
        followups = visible_at(
            self.repo.filter("followups", customer_id=customer_id), business_iso
        )
        orders = visible_at(self.repo.filter("orders", customer_id=customer_id), business_iso)

        # 证据取最近若干条，并显式记录其编号，建议永远可以回放依据
        evidence_obs = [row["id"] for row in observations[-5:]]
        evidence_fol = [row["id"] for row in followups[-3:]]
        evidence_ord = [row["id"] for row in orders[-3:]]

        row = to_row(
            Advice(
                id=f"adv_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                issued_at=business_iso,
                pharmacist_id=pharmacist_id,
                credential_no=staff["credential_no"],
                enrollment_id=enrollment["id"],
                consent_snapshot=dict(consents),
                plan_version_seq=plan["seq"] if plan else None,
                plan_lines=list(plan["lines"]) if plan else [],
                evidence_observations=evidence_obs,
                evidence_followups=evidence_fol,
                evidence_orders=evidence_ord,
                rationale=rationale,
                next_step=next_step,
            )
        )
        self.repo.add("advice", row)
        return row

    # ---- 药师侧：完整溯源 -----------------------------------------
    def trace(self, advice_id: str, pharmacist_id: str) -> dict[str, Any]:
        advice = self.repo.find("advice", id=advice_id)
        if advice is None:
            raise KeyError(f"建议不存在：{advice_id}")
        self.access.check_health(
            advice["customer_id"], pharmacist_id, Scope.HEALTH_RECORDS, "trace_advice",
            advice["issued_at"],
        )
        return {
            "advice": advice,
            "evidence": {
                "observations": [
                    self.repo.find("observations", id=ref) for ref in advice["evidence_observations"]
                ],
                "followups": [
                    self.repo.find("followups", id=ref) for ref in advice["evidence_followups"]
                ],
                "orders": [self.repo.find("orders", id=ref) for ref in advice["evidence_orders"]],
            },
            "pharmacist": self.repo.find("staff", id=advice["pharmacist_id"]),
        }

    # ---- 顾客侧：我的当前方案与下一步 -----------------------------
    def customer_view(self, customer_id: str, at: str | None = None) -> dict[str, Any]:
        """顾客本人视角：仅自己的当前方案、下一步与待办，不含内部安全标签外的他人信息。"""
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        snapshot = {
            "as_of": moment_iso,
            "active": False,
            "plan_lines": [],
            "next_step": "",
            "latest_advice": None,
            "open_contacts": [],
        }
        enrollment = self.access.active_enrollment(customer_id, moment_iso)
        if enrollment is None:
            return snapshot
        snapshot["active"] = True
        snapshot["enrollment_id"] = enrollment["id"]
        plan = self.records.effective_plan(customer_id, moment_iso)
        if plan is not None:
            lines = []
            for line in plan["lines"]:
                drug = self.repo.find("drugs", id=line["drug_id"])
                lines.append(
                    {
                        "drug": drug["generic_name"] if drug else line["drug_id"],
                        "dose": line["dose"],
                        "frequency": line["frequency"],
                        "days_supply": line["days_supply"],
                    }
                )
            snapshot["plan_lines"] = lines
        advices = [
            row
            for row in self.repo.filter("advice", customer_id=customer_id)
            if timeutil.parse(row["issued_at"]) <= timeutil.parse(moment_iso)
        ]
        if advices:
            latest = sorted(advices, key=lambda row: timeutil.parse(row["issued_at"]))[-1]
            snapshot["latest_advice"] = latest["id"]
            snapshot["next_step"] = latest["next_step"]
        snapshot["open_contacts"] = [
            {"reasons": task["reasons"], "due_at": task["due_at"], "detail": task["detail"]}
            for task in self.repo.filter("contacts", customer_id=customer_id, status="open")
        ]
        return snapshot
