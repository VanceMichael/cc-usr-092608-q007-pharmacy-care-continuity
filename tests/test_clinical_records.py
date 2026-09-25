"""医嘱、用药计划版本、随访、转诊、不良反应与高风险双人复核。"""

import unittest

from src.errors import AuthorizationError, DomainError
from src.models import ADRStatus, ChangeRisk, ChangeStatus, PlanLineStatus
from tests.world import T0, build_world


class ClinicalRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_pharmacist_records_full_chain(self) -> None:
        self.b.records.record_order("C1", "P1", "社区医院处方", "继续降压治疗", T0)
        self.b.records.record_observation(
            "C1", "P1", "S1", "blood_pressure", "136/84", "mmHg",
            "2026-09-02T09:00:00+00:00",
        )
        followup = self.b.records.record_followup(
            "C1", "P1", "S1", "full", "用药规律", "血压控制平稳，可继续原方案",
            "2026-09-05T09:00:00+00:00", next_due_at="2026-09-19T09:00:00+00:00",
        )
        self.assertEqual(followup["plan_version_seq"], 1)
        self.b.records.record_referral(
            "C1", "P1", "心内科", "血压波动", "2026-09-05T09:30:00+00:00",
        )

        safety = self.b.records.latest_safety_conclusion("C1", "2026-09-06T00:00:00+00:00")
        self.assertEqual(safety["conclusion"], "血压控制平稳，可继续原方案")

    def test_plan_versions_are_incremental(self) -> None:
        v1 = self.b.records.effective_plan("C1", T0)
        v1_id = v1["id"]
        self.b.records.issue_plan(
            "C1", "P1",
            [{"drug_id": "DRUG_HTN", "sku": "SKU_A", "dose": "10mg", "frequency": "qd",
              "days_supply": 30}],
            note="加量", occurred_at="2026-09-10T09:00:00+00:00",
        )
        self.assertEqual(self.b.repo.find("plan_versions", id=v1_id)["status"],
                         PlanLineStatus.SUPERSEDED)
        current = self.b.records.effective_plan("C1", "2026-09-10T10:00:00+00:00")
        self.assertEqual(current["seq"], 2)
        # 既往时刻仍回放旧版本
        self.assertEqual(self.b.records.effective_plan("C1", T0)["seq"], 1)

    def test_high_risk_change_requires_second_reviewer(self) -> None:
        change = self.b.records.propose_change(
            "C1", "P1",
            [{"drug_id": "DRUG_WARF", "sku": "SKU_W", "dose": "3mg", "frequency": "qd",
              "days_supply": 30}],
            reason="启动抗凝", occurred_at="2026-09-06T09:00:00+00:00",
        )
        self.assertEqual(change["risk"], ChangeRisk.HIGH)  # 计划高风险药品自动升级
        with self.assertRaises(DomainError):
            self.b.records.apply_standard_change(change["id"])
        with self.assertRaises(AuthorizationError):
            self.b.records.review_change(change["id"], "P1", True)  # 不能自审
        with self.assertRaises(AuthorizationError):
            self.b.records.review_change(change["id"], "CK", True)  # 店员无权复核

        reviewed = self.b.records.review_change(
            change["id"], "RV", True, "指标支持，剂量合理",
            occurred_at="2026-09-06T10:00:00+00:00",
        )
        self.assertEqual(reviewed["status"], ChangeStatus.APPROVED)
        plan = self.b.records.effective_plan("C1", "2026-09-06T11:00:00+00:00")
        self.assertEqual(plan["change_id"], change["id"])

    def test_rejected_high_risk_change_does_not_publish(self) -> None:
        change = self.b.records.propose_change(
            "C1", "P1",
            [{"drug_id": "DRUG_WARF", "sku": "SKU_W", "dose": "9mg", "frequency": "qd",
              "days_supply": 30}],
            reason="激进加量", risk=ChangeRisk.HIGH,
            occurred_at="2026-09-07T09:00:00+00:00",
        )
        reviewed = self.b.records.review_change(
            change["id"], "RV", False, "剂量过大，驳回",
            occurred_at="2026-09-07T10:00:00+00:00",
        )
        self.assertEqual(reviewed["status"], ChangeStatus.REJECTED)
        self.assertEqual(self.b.records.effective_plan("C1", "2026-09-08T00:00:00+00:00")["seq"], 1)

    def test_clerk_may_report_adr_without_health_access(self) -> None:
        adr = self.b.records.report_adverse_event(
            "C1", "CK", "DRUG_HTN", "B001", "服用后皮疹", "mild",
            "2026-09-09T08:00:00+00:00",
        )
        self.assertEqual(adr["status"], ADRStatus.REPORTED)
        closed = self.b.records.close_adverse_event(
            adr["id"], "P1", "暂停该批次并上报，换平压牌观察",
            occurred_at="2026-09-09T09:00:00+00:00",
        )
        safety = self.b.records.latest_safety_conclusion("C1", "2026-09-10T00:00:00+00:00")
        self.assertEqual(safety["kind"], "adverse_event")
        self.assertEqual(closed["status"], ADRStatus.CLOSED)


if __name__ == "__main__":
    unittest.main()
