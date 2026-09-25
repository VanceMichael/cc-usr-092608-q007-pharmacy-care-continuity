"""跨店咨询：定位当前有效计划与最近安全结论。"""

import unittest

from src.clinical import record_follow_up, set_medication_plan
from src.consultation import open_consultation
from src.entities import ConsentScope, RiskLevel
from src.errors import ConsentError, PlanStateError
from src.inventory import pickup, recall_batch, receive_stock, reserve
from src.plans import terminate_plan
from tests.support import ALL_CONSENTS, DAY, T0, make_store, med_item, member


def _consultation_store(with_cross_store: bool = True):
    consents = ALL_CONSENTS if with_cross_store else (
        ConsentScope.HEALTH_RECORDS,
        ConsentScope.REMINDERS,
    )
    store = make_store(consents=consents)
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-1", 100, T0)
    set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1"),), T0)
    record_follow_up(
        store, member(store, "ph-1"), "plan-1", "血压平稳，继续用药", RiskLevel.LOW, (),
        T0 + 30 * DAY, T0,
    )
    return store


class ConsultationTest(unittest.TestCase):
    def test_home_store_consultation_finds_plan_and_safety_conclusion(self) -> None:
        store = _consultation_store()
        context = open_consultation(store, member(store, "ph-1"), "cust-1", "store-1", T0 + DAY)
        self.assertEqual(context.plan.plan_id, "plan-1")
        self.assertIsNotNone(context.medication_plan)
        self.assertEqual(context.latest_follow_up.conclusion, "血压平稳，继续用药")

    def test_cross_store_requires_cross_store_consent(self) -> None:
        store = _consultation_store(with_cross_store=False)
        with self.assertRaises(ConsentError):
            open_consultation(store, member(store, "ph-2"), "cust-1", "store-2", T0 + DAY)

    def test_cross_store_consultation_with_consent(self) -> None:
        store = _consultation_store()
        context = open_consultation(store, member(store, "ph-2"), "cust-1", "store-2", T0 + DAY)
        self.assertEqual(context.plan.home_store_id, "store-1")
        self.assertEqual(context.medication_plan.items[0].drug_id, "drug-1")

    def test_consultation_after_termination_is_denied(self) -> None:
        store = _consultation_store()
        terminate_plan(store, "plan-1", "cust-1", T0 + DAY)
        with self.assertRaises(PlanStateError):
            open_consultation(store, member(store, "ph-1"), "cust-1", "store-1", T0 + 2 * DAY)

    def test_recall_alert_surfaces_for_picked_up_batch(self) -> None:
        store = _consultation_store()
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "含量不合格", T0 + 2 * DAY)
        context = open_consultation(store, member(store, "ph-1"), "cust-1", "store-1", T0 + 3 * DAY)
        self.assertEqual(len(context.recall_alerts), 1)
        self.assertEqual(context.recall_alerts[0].batch_no, "B-1")

    def test_consultation_is_audited(self) -> None:
        store = _consultation_store()
        open_consultation(store, member(store, "ph-2"), "cust-1", "store-2", T0 + DAY)
        entries = [e for e in store.audit if e.action == "consultation.open"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].actor_id, "ph-2")
        self.assertIn("store-2", entries[0].detail)


if __name__ == "__main__":
    unittest.main()
