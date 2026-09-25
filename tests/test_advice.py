"""建议溯源：药师可说明每次建议基于何种授权、药品、观察与执业责任；顾客看当前方案。"""

import unittest

from src.models import Scope
from tests.world import T0, build_world


class AdviceTraceabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_advice_snapshots_consent_plan_evidence_and_credential(self) -> None:
        self.b.records.record_order("C1", "P1", "社区医院", "氨氯地平 5mg qd", T0)
        self.b.records.record_observation(
            "C1", "P1", "S1", "blood_pressure", "134/82", "mmHg",
            "2026-09-08T09:00:00+00:00",
        )
        self.b.records.record_followup(
            "C1", "P1", "S1", "full", "规律服药", "血压达标",
            "2026-09-09T09:00:00+00:00",
        )
        advice = self.b.advice.issue_advice(
            "C1", "P1",
            rationale="近一周血压达标且无不良反应，维持 5mg 方案",
            next_step="继续每日服药，09-23 前复测血压",
            occurred_at="2026-09-10T09:00:00+00:00",
        )
        self.assertEqual(advice["credential_no"], "ZY-1001")
        self.assertTrue(advice["consent_snapshot"][Scope.HEALTH_RECORDS])
        self.assertTrue(advice["consent_snapshot"][Scope.CROSS_STORE])
        self.assertEqual(advice["plan_version_seq"], 1)
        self.assertEqual(advice["plan_lines"][0]["drug_id"], "DRUG_HTN")
        self.assertEqual(len(advice["evidence_observations"]), 1)
        self.assertEqual(len(advice["evidence_orders"]), 1)

        trace = self.b.advice.trace(advice["id"], "P2")
        self.assertEqual(trace["evidence"]["observations"][0]["value"], "134/82")
        self.assertEqual(trace["pharmacist"]["credential_no"], "ZY-1001")

    def test_advice_after_consent_withdrawal_records_current_snapshot(self) -> None:
        self.b.enrollment.set_consent("C1", Scope.CROSS_STORE, False, at="2026-09-08T09:00:00+00:00")
        advice = self.b.advice.issue_advice(
            "C1", "P1", "仅本店随访", "按时服药",
            occurred_at="2026-09-09T09:00:00+00:00",
        )
        self.assertFalse(advice["consent_snapshot"][Scope.CROSS_STORE])
        self.assertTrue(advice["consent_snapshot"][Scope.HEALTH_RECORDS])

    def test_customer_view_shows_current_plan_and_next_step(self) -> None:
        self.b.advice.issue_advice(
            "C1", "P1", "方案稳定", "09-20 前复测血压",
            occurred_at="2026-09-10T09:00:00+00:00",
        )
        view = self.b.advice.customer_view("C1", "2026-09-11T09:00:00+00:00")
        self.assertTrue(view["active"])
        self.assertEqual(view["plan_lines"][0]["drug"], "氨氯地平")
        self.assertEqual(view["next_step"], "09-20 前复测血压")

    def test_customer_view_after_termination_is_inactive_but_history_remains(self) -> None:
        self.b.advice.issue_advice(
            "C1", "P1", "方案稳定", "继续观察",
            occurred_at="2026-09-10T09:00:00+00:00",
        )
        self.b.enrollment.terminate("C1", "顾客主动终止", staff_id="P1", at="2026-09-12T09:00:00+00:00")
        view = self.b.advice.customer_view("C1", "2026-09-13T09:00:00+00:00")
        self.assertFalse(view["active"])
        self.assertEqual(view["plan_lines"], [])
        # 历史建议仍可依法追溯
        self.assertTrue(self.b.repo.all("advice"))


if __name__ == "__main__":
    unittest.main()
