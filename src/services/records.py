"""临床记录：医嘱摘要、用药计划版本、测量观察、随访、转诊、不良反应。

所有记录都保留 occurred_at（真实发生时间，可断网补录）与 recorded_at（入库时间）。
高风险计划调整：药师提出 → 另一名有复核权限的人员复核 → 通过后才生成新版本。
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from src import timeutil
from src.errors import AuthorizationError, DomainError
from src.models import (
    Adherence,
    AdverseEvent,
    ADRStatus,
    ChangeRisk,
    ChangeStatus,
    FollowUp,
    Observation,
    ObservationKind,
    PlanChange,
    PlanLine,
    PlanLineStatus,
    PrescriberOrder,
    Referral,
    StaffRole,
    to_row,
    MedicationPlanVersion,
)
from src.repository import Repository
from src.services.access import AccessService


def _line_from_dict(line: dict[str, Any]) -> PlanLine:
    return PlanLine(
        drug_id=line["drug_id"],
        sku=line.get("sku"),
        dose=line["dose"],
        frequency=line["frequency"],
        days_supply=int(line["days_supply"]),
    )


def visible_at(rows: Iterable[dict[str, Any]], moment_iso: str) -> list[dict[str, Any]]:
    """时刻 T 可见的记录：业务时间与入库时间都不晚于 T（晚到信息不回溯影响既往建议）。"""
    moment = timeutil.parse(moment_iso)
    return [
        row
        for row in rows
        if timeutil.parse(row["occurred_at"]) <= moment
        and timeutil.parse(row["recorded_at"]) <= moment
    ]


def sorted_by_occurred(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: timeutil.parse(row["occurred_at"]))


class RecordService:
    def __init__(self, repo: Repository, access: AccessService) -> None:
        self.repo = repo
        self.access = access

    # ---- 医嘱摘要 -------------------------------------------------
    def record_order(
        self,
        customer_id: str,
        pharmacist_id: str,
        source: str,
        summary: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        ctx = self.access.check_health(
            customer_id, pharmacist_id, "health_records", "record_order",
            self._norm(occurred_at),
        )
        self.access.assert_practicing_pharmacist(ctx["staff"], occurred_at)
        row = to_row(
            PrescriberOrder(
                id=f"ord_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                occurred_at=self._norm(occurred_at),
                recorded_at=ctx["recorded_at"],
                source=source,
                summary=summary,
                recorded_by=pharmacist_id,
            )
        )
        self.repo.add("orders", row)
        return row

    # ---- 用药计划版本 ---------------------------------------------
    def effective_plan(
        self, customer_id: str, at: str | None = None
    ) -> dict[str, Any] | None:
        """指定时刻生效的用药计划版本（issued_at 不晚于该时刻的最后一个有效版本）。"""
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        visible = [
            row
            for row in self.repo.filter("plan_versions", customer_id=customer_id)
            if timeutil.parse(row["issued_at"]) <= timeutil.parse(moment_iso)
            and timeutil.parse(row["recorded_at"]) <= timeutil.parse(moment_iso)
        ]
        if not visible:
            return None
        return sorted(visible, key=lambda row: (row["seq"], timeutil.parse(row["issued_at"])))[-1]

    def issue_plan(
        self,
        customer_id: str,
        pharmacist_id: str,
        lines: list[dict[str, Any]],
        note: str = "",
        occurred_at: str | None = None,
        change_id: str | None = None,
    ) -> dict[str, Any]:
        """药师直接发布一个用药计划版本（常规调整或复核通过后的落版）。"""
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        ctx = self.access.check_health(customer_id, pharmacist_id, "health_records", "issue_plan", business_at)
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)
        return self._publish_version(
            customer_id, pharmacist_id, lines, business_at, ctx["recorded_at"], note, change_id
        )

    def _publish_version(
        self,
        customer_id: str,
        pharmacist_id: str,
        lines: list[dict[str, Any]],
        business_at: str,
        recorded_at: str,
        note: str,
        change_id: str | None,
    ) -> dict[str, Any]:
        prior = self._latest_version(customer_id)
        seq = (prior["seq"] + 1) if prior else 1
        if prior is not None and prior["status"] == PlanLineStatus.EFFECTIVE:
            self.repo.update("plan_versions", prior["id"], {"status": PlanLineStatus.SUPERSEDED})
        row = to_row(
            MedicationPlanVersion(
                id=f"mpv_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                seq=seq,
                status=PlanLineStatus.EFFECTIVE,
                issued_at=business_at,
                recorded_at=recorded_at,
                lines=[to_row(_line_from_dict(line)) for line in lines],
                note=note,
                pharmacist_id=pharmacist_id,
                change_id=change_id,
            )
        )
        self.repo.add("plan_versions", row)
        return row

    def _latest_version(self, customer_id: str) -> dict[str, Any] | None:
        rows = self.repo.filter("plan_versions", customer_id=customer_id)
        return sorted(rows, key=lambda row: row["seq"])[-1] if rows else None

    # ---- 高风险调整：提出 + 双人复核 ------------------------------
    def propose_change(
        self,
        customer_id: str,
        pharmacist_id: str,
        lines: list[dict[str, Any]],
        reason: str,
        risk: str = ChangeRisk.STANDARD,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        ctx = self.access.check_health(customer_id, pharmacist_id, "health_records", "propose_change", business_at)
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)

        enrollment = self.access.active_enrollment(customer_id, business_at)
        if risk != ChangeRisk.HIGH and enrollment is not None:
            plan = self.repo.find("service_plans", id=enrollment["plan_id"])
            high_risk = set(plan.get("high_risk_drugs", ())) if plan else set()
            if any(line["drug_id"] in high_risk for line in lines):
                risk = ChangeRisk.HIGH

        row = to_row(
            PlanChange(
                id=f"chg_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                proposed_by=pharmacist_id,
                risk=risk,
                reason=reason,
                lines=[to_row(_line_from_dict(line)) for line in lines],
                created_at=business_at,
            )
        )
        self.repo.add("changes", row)
        return row

    def review_change(
        self,
        change_id: str,
        reviewer_id: str,
        approve: bool,
        note: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """另一名有权限人员复核；高风险调整无复核通过不得生效。"""
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        change = self.repo.find("changes", id=change_id)
        if change is None:
            raise KeyError(f"调整单不存在：{change_id}")
        if change["status"] != ChangeStatus.PROPOSED:
            raise DomainError("该调整已完成复核")
        reviewer = self.repo.find("staff", id=reviewer_id)
        if reviewer is None:
            raise AuthorizationError(f"复核人不存在：{reviewer_id}")
        if reviewer_id == change["proposed_by"]:
            raise AuthorizationError("提出调整的药师不能复核自己的调整，须由另一名有权限人员复核")
        if reviewer["role"] != StaffRole.SAFETY_REVIEWER:
            raise AuthorizationError("复核人须具备安全质量复核权限")
        if change["risk"] != ChangeRisk.HIGH:
            raise DomainError("常规调整无需双人复核")

        ctx = self.access.check_health(
            change["customer_id"], reviewer_id, "health_records", "review_change", business_at
        )
        new_status = ChangeStatus.APPROVED if approve else ChangeStatus.REJECTED
        self.repo.update(
            "changes",
            change_id,
            {
                "status": new_status,
                "reviewed_by": reviewer_id,
                "reviewed_at": business_at,
                "review_note": note,
            },
        )
        if approve:
            version = self._publish_version(
                change["customer_id"],
                change["proposed_by"],
                change["lines"],
                business_at,
                ctx["recorded_at"],
                f"高风险调整复核通过：{change['reason']}",
                change_id=change_id,
            )
            self.repo.update("changes", change_id, {"new_version_seq": version["seq"]})
        return self.repo.find("changes", id=change_id)  # type: ignore[return-value]

    def apply_standard_change(
        self, change_id: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        """常规调整：药师确认后直接落版。"""
        change = self.repo.find("changes", id=change_id)
        if change is None:
            raise KeyError(f"调整单不存在：{change_id}")
        if change["risk"] == ChangeRisk.HIGH:
            raise DomainError("高风险调整必须经双人复核通过，不能直接生效")
        if change["status"] != ChangeStatus.PROPOSED:
            raise DomainError("该调整已处理")
        business_at = occurred_at or change["created_at"]
        version = self.issue_plan(
            change["customer_id"],
            change["proposed_by"],
            change["lines"],
            note=f"常规调整：{change['reason']}",
            occurred_at=business_at,
            change_id=change_id,
        )
        self.repo.update("changes", change_id, {"status": ChangeStatus.APPROVED,
                                                 "reviewed_by": change["proposed_by"],
                                                 "reviewed_at": business_at,
                                                 "new_version_seq": version["seq"]})
        return self.repo.find("changes", id=change_id)  # type: ignore[return-value]

    # ---- 测量观察（支持断网补录） ---------------------------------
    def record_observation(
        self,
        customer_id: str,
        staff_id: str,
        store_id: str,
        kind: str,
        value: str,
        unit: str,
        occurred_at: str,
        note: str = "",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        business_at = self._norm(occurred_at)
        ctx = self.access.check_health(
            customer_id, staff_id, "health_records", "record_observation", business_at,
            recorded_at=recorded_at,
        )
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)
        row = to_row(
            Observation(
                id=f"obs_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                kind=kind,
                value=value,
                unit=unit,
                occurred_at=business_at,
                recorded_at=ctx["recorded_at"],
                store_id=store_id,
                recorded_by=staff_id,
                note=note,
            )
        )
        self.repo.add("observations", row)
        return row

    # ---- 随访与安全结论 -------------------------------------------
    def record_followup(
        self,
        customer_id: str,
        pharmacist_id: str,
        store_id: str,
        adherence: str,
        conclusion: str,
        safety_conclusion: str,
        occurred_at: str,
        next_due_at: str = "",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        business_at = self._norm(occurred_at)
        ctx = self.access.check_health(
            customer_id, pharmacist_id, "health_records", "record_followup", business_at,
            recorded_at=recorded_at,
        )
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)
        plan = self.effective_plan(customer_id, business_at)
        row = to_row(
            FollowUp(
                id=f"fol_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                occurred_at=business_at,
                recorded_at=ctx["recorded_at"],
                store_id=store_id,
                pharmacist_id=pharmacist_id,
                adherence=adherence,
                conclusion=conclusion,
                safety_conclusion=safety_conclusion,
                next_due_at=self._norm(next_due_at) if next_due_at else "",
                plan_version_seq=plan["seq"] if plan else None,
            )
        )
        self.repo.add("followups", row)
        return row

    def latest_safety_conclusion(
        self, customer_id: str, at: str | None = None
    ) -> dict[str, Any] | None:
        """跨店咨询首先读取：截至该时刻最近的安全结论（随访结论或不良反应处置）。"""
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        candidates: list[dict[str, Any]] = []
        for row in visible_at(self.repo.filter("followups", customer_id=customer_id), moment_iso):
            if row.get("safety_conclusion"):
                candidates.append(
                    {
                        "kind": "followup",
                        "ref": row["id"],
                        "at": row["occurred_at"],
                        "conclusion": row["safety_conclusion"],
                        "pharmacist_id": row["pharmacist_id"],
                        "store_id": row["store_id"],
                    }
                )
        for row in visible_at(self.repo.filter("adverse_events", customer_id=customer_id), moment_iso):
            if row.get("disposition") and row["status"] != ADRStatus.REPORTED:
                candidates.append(
                    {
                        "kind": "adverse_event",
                        "ref": row["id"],
                        "at": row["occurred_at"],
                        "conclusion": row["disposition"],
                        "pharmacist_id": row["reporter_staff_id"],
                        "store_id": None,
                    }
                )
        return sorted(candidates, key=lambda item: timeutil.parse(item["at"]))[-1] if candidates else None

    # ---- 转诊 -----------------------------------------------------
    def record_referral(
        self,
        customer_id: str,
        pharmacist_id: str,
        target: str,
        reason: str,
        occurred_at: str,
        urgency: str = "routine",
    ) -> dict[str, Any]:
        business_at = self._norm(occurred_at)
        ctx = self.access.check_health(customer_id, pharmacist_id, "health_records", "record_referral", business_at)
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)
        row = to_row(
            Referral(
                id=f"ref_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                occurred_at=business_at,
                recorded_at=ctx["recorded_at"],
                pharmacist_id=pharmacist_id,
                target=target,
                reason=reason,
                urgency=urgency,
            )
        )
        self.repo.add("referrals", row)
        return row

    # ---- 不良反应报告 ---------------------------------------------
    def report_adverse_event(
        self,
        customer_id: str,
        reporter_staff_id: str,
        drug_id: str,
        batch_no: str,
        description: str,
        severity: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        business_at = self._norm(occurred_at)
        # 店员可代报安全事件（不读取健康档案）；药师按授权记录
        reporter = self.repo.find("staff", id=reporter_staff_id)
        if reporter is None:
            raise AuthorizationError(f"报告人不存在：{reporter_staff_id}")
        recorded_at = business_at
        if reporter["role"] == StaffRole.PHARMACIST:
            check = self.access.check_health(
                customer_id, reporter_staff_id, "health_records", "report_adr", business_at
            )
            recorded_at = check["recorded_at"]
        elif reporter["role"] not in (StaffRole.CLERK, StaffRole.SAFETY_REVIEWER):
            raise AuthorizationError("该角色不能提交不良反应报告")
        row = to_row(
            AdverseEvent(
                id=f"adr_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                occurred_at=business_at,
                recorded_at=recorded_at,
                reporter_staff_id=reporter_staff_id,
                drug_id=drug_id,
                batch_no=batch_no,
                description=description,
                severity=severity,
            )
        )
        self.repo.add("adverse_events", row)
        return row

    def close_adverse_event(
        self, adr_id: str, pharmacist_id: str, disposition: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        business_at = self._norm(occurred_at) if occurred_at else timeutil.format_value(timeutil.now())
        adr = self.repo.find("adverse_events", id=adr_id)
        if adr is None:
            raise KeyError(f"不良反应报告不存在：{adr_id}")
        ctx = self.access.check_health(adr["customer_id"], pharmacist_id, "health_records", "close_adr", business_at)
        self.access.assert_practicing_pharmacist(ctx["staff"], business_at)
        self.repo.update(
            "adverse_events",
            adr_id,
            {"status": ADRStatus.CLOSED, "disposition": disposition},
        )
        return self.repo.find("adverse_events", id=adr_id)  # type: ignore[return-value]

    @staticmethod
    def _norm(value: str) -> str:
        return timeutil.format_value(timeutil.parse(value))
