"""购药决策：提前领取、疗程重叠、替代品牌、召回批次与应急保供。"""

import unittest

from src.clinical import set_medication_plan
from src.dispensing import (
    BRAND_SUBSTITUTION,
    COURSE_OVERLAP,
    EARLY_PICKUP,
    EMERGENCY_OVERRIDE,
    RECALLED_BATCH,
    UNKNOWN_BATCH,
    evaluate_purchase,
)
from src.entities import DispenseDecision
from src.inventory import pickup, recall_batch, receive_stock, reserve
from tests.support import DAY, T0, make_store, med_item, member


def _dispensing_store():
    store = make_store()
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平A", "氨氯地平", "B-1", 100, T0)
    receive_stock(store, clerk, "store-1", "drug-2", "氨氯地平B", "氨氯地平", "B-2", 100, T0)
    receive_stock(store, clerk, "store-1", "drug-3", "氯沙坦", "氯沙坦", "B-3", 100, T0)
    set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1", days_supply=30),), T0)
    return store


class DispensingTest(unittest.TestCase):
    def test_normal_purchase_is_allowed(self) -> None:
        store = _dispensing_store()
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0)
        self.assertEqual(result.decision, DispenseDecision.ALLOW)
        self.assertEqual(result.reasons, ())

    def test_retail_sale_without_plan_is_allowed(self) -> None:
        store = _dispensing_store()
        result = evaluate_purchase(store, "cust-2", "drug-3", "B-3", 10, T0)
        self.assertEqual(result.decision, DispenseDecision.ALLOW)

    def test_recalled_batch_is_blocked_even_in_emergency(self) -> None:
        store = _dispensing_store()
        recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "含量不合格", T0)
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0 + DAY)
        self.assertEqual(result.decision, DispenseDecision.BLOCK)
        self.assertIn(RECALLED_BATCH, [r.code for r in result.reasons])
        emergency = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0 + DAY, emergency=True)
        self.assertEqual(emergency.decision, DispenseDecision.BLOCK, "应急保供不能绕过召回批次")

    def test_unknown_batch_requires_manual_review(self) -> None:
        store = _dispensing_store()
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-X", 30, T0)
        self.assertEqual(result.decision, DispenseDecision.MANUAL_REVIEW)
        self.assertIn(UNKNOWN_BATCH, [r.code for r in result.reasons])
        emergency = evaluate_purchase(store, "cust-1", "drug-1", "B-X", 30, T0, emergency=True)
        self.assertEqual(emergency.decision, DispenseDecision.MANUAL_REVIEW, "批次风险不在应急放宽范围")

    def test_early_pickup_requires_manual_review(self) -> None:
        store = _dispensing_store()
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0 + 5 * DAY)
        self.assertEqual(result.decision, DispenseDecision.MANUAL_REVIEW)
        self.assertIn(EARLY_PICKUP, [r.code for r in result.reasons])

    def test_emergency_overrides_adherence_checks(self) -> None:
        store = _dispensing_store()
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0 + 5 * DAY, emergency=True)
        self.assertEqual(result.decision, DispenseDecision.ALLOW)
        self.assertIn(EMERGENCY_OVERRIDE, [r.code for r in result.reasons])

    def test_brand_substitution_requires_manual_review(self) -> None:
        store = _dispensing_store()
        result = evaluate_purchase(store, "cust-1", "drug-2", "B-2", 30, T0 + DAY)
        self.assertEqual(result.decision, DispenseDecision.MANUAL_REVIEW)
        self.assertIn(BRAND_SUBSTITUTION, [r.code for r in result.reasons])

    def test_course_overlap_requires_manual_review(self) -> None:
        store = _dispensing_store()
        # 计划内同时存在两种同成分药品
        set_medication_plan(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-1"), med_item("drug-2")), T0 + DAY,
        )
        result = evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0 + 2 * DAY)
        self.assertEqual(result.decision, DispenseDecision.MANUAL_REVIEW)
        self.assertIn(COURSE_OVERLAP, [r.code for r in result.reasons])

    def test_evaluation_is_audited(self) -> None:
        store = _dispensing_store()
        evaluate_purchase(store, "cust-1", "drug-1", "B-1", 30, T0)
        entries = [e for e in store.audit if e.action == "dispense.evaluate"]
        self.assertEqual(len(entries), 1)
        self.assertIn("allow", entries[0].detail)


if __name__ == "__main__":
    unittest.main()
