"""购药决策：允许/阻断/转人工的原因与店员最小知情、应急降级。"""

import unittest

from src.errors import AuthorizationError, DomainError
from src.models import Decision, Scope
from tests.world import T0, build_world


class DispenseDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_normal_purchase_is_allowed_and_clerk_can_fulfill(self) -> None:
        result = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-10-01T09:00:00+00:00")
        self.assertEqual(result["decision"], Decision.ALLOW)
        self.assertEqual(result["detail"]["batch_no"], "B001")  # FEFO 近效期先出
        receipt = self.b.dispense.execute(result, "CK")
        self.assertEqual(receipt["reservation"]["status"], "claimed")

    def test_early_pickup_blocks_then_manual_window(self) -> None:
        first = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at=T0)
        self.b.dispense.execute(first, "CK", occurred_at=T0)

        severe = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-09-04T09:00:00+00:00")
        self.assertEqual(severe["decision"], Decision.BLOCK)
        self.assertIn("early_pickup", severe["reasons"])
        self.assertEqual(severe["clerk_message"], "停止发放，按门店处置流程处理")
        with self.assertRaises(DomainError):
            self.b.dispense.execute(severe, "CK")

        moderate = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-09-17T09:00:00+00:00")
        self.assertEqual(moderate["decision"], Decision.MANUAL)
        self.assertIn("early_pickup", moderate["manual_reasons"])

    def test_emergency_downgrades_early_pickup_but_not_recall(self) -> None:
        first = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at=T0)
        self.b.dispense.execute(first, "CK", occurred_at=T0)
        urgent = self.b.dispense.evaluate(
            "C1", "S1", "SKU_A", 1, "CK", at="2026-09-04T09:00:00+00:00", emergency=True
        )
        self.assertEqual(urgent["decision"], Decision.MANUAL)
        self.assertIn("early_pickup", urgent["reasons"])

        self.b.registry.issue_recall("RC1", "氨氯地平批次召回", "SKU_A", ("B001",), "杂质")
        recalled = self.b.dispense.evaluate(
            "C1", "S1", "SKU_A", 1, "CK", batch_no="B001",
            at="2026-09-04T10:00:00+00:00", emergency=True,
        )
        self.assertEqual(recalled["decision"], Decision.BLOCK)
        self.assertEqual(recalled["reasons"], ["batch_recalled"])

    def test_substitution_goes_manual_and_only_pharmacist_sees_detail(self) -> None:
        result = self.b.dispense.evaluate(
            "C1", "S1", "SKU_B", 1, "CK", at="2026-10-01T09:00:00+00:00"
        )
        self.assertEqual(result["decision"], Decision.MANUAL)
        self.assertIn("substitution_requires_review", result["reasons"])
        with self.assertRaises(AuthorizationError):
            self.b.dispense.pharmacist_explanation(result, "CK")
        explanation = self.b.dispense.pharmacist_explanation(result, "P1")
        self.assertEqual(explanation["plan_version"]["seq"], 1)

    def test_course_overlap_blocks(self) -> None:
        self.b.records.issue_plan(
            "C1", "P1",
            [
                {"drug_id": "DRUG_HTN", "sku": "SKU_A", "dose": "5mg", "frequency": "qd",
                 "days_supply": 30},
                {"drug_id": "DRUG_HTN", "sku": "SKU_A", "dose": "5mg", "frequency": "qd",
                 "days_supply": 30},
            ],
            note="重复疗程", occurred_at="2026-09-10T09:00:00+00:00",
        )
        result = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-09-11T09:00:00+00:00")
        self.assertEqual(result["decision"], Decision.BLOCK)
        self.assertIn("course_overlap", result["reasons"])

    def test_cross_store_requires_cross_store_consent(self) -> None:
        ok = self.b.dispense.evaluate("C1", "S2", "SKU_A", 1, "CK", at="2026-10-01T09:00:00+00:00")
        self.assertEqual(ok["decision"], Decision.ALLOW)
        self.b.enrollment.set_consent("C1", Scope.CROSS_STORE, False, at="2026-09-20T09:00:00+00:00")
        blocked = self.b.dispense.evaluate("C1", "S2", "SKU_A", 1, "CK", at="2026-10-02T09:00:00+00:00")
        self.assertEqual(blocked["decision"], Decision.MANUAL)
        self.assertIn("cross_store_consent_missing", blocked["reasons"])

    def test_unenrolled_customer_goes_manual(self) -> None:
        result = self.b.dispense.evaluate("C2", "S1", "SKU_A", 1, "CK", at=T0)
        self.assertEqual(result["decision"], Decision.MANUAL)
        self.assertIn("plan_terminated", result["reasons"])

    def test_every_attempt_is_recorded_with_reasons(self) -> None:
        self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at=T0)
        attempts = self.b.repo.all("attempts")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["decision"], Decision.ALLOW)


if __name__ == "__main__":
    unittest.main()
