"""视图层：顾客当前方案、店员物流看板、药师建议依据与合规追溯。"""

import unittest

from src.clinical import (
    propose_adjustment,
    record_follow_up,
    record_observation,
    report_adverse_reaction,
    review_adjustment,
    set_medication_plan,
)
from src.entities import RiskLevel
from src.errors import AccessDeniedError
from src.inventory import pickup, receive_stock, reserve
from src.plans import terminate_plan
from src.views import (
    adjustment_rationale,
    compliance_trace,
    customer_view,
    follow_up_rationale,
    staff_pickup_board,
)
from tests.support import DAY, T0, make_store, med_item, member


def _view_store():
    store = make_store()
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-1", 100, T0)
    receive_stock(store, clerk, "store-1", "drug-2", "氯沙坦", "氯沙坦", "B-2", 100, T0, high_risk=True)
    set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1"),), T0)
    return store


class CustomerViewTest(unittest.TestCase):
    def test_customer_sees_current_plan_and_next_steps(self) -> None:
        store = _view_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 30 * DAY, T0,
        )
        reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0, channel="delivery")
        view = customer_view(store, "cust-1", T0 + DAY)
        self.assertEqual(view.plan_status, "active")
        self.assertEqual(view.plan_type, "慢病管理-高血压")
        self.assertEqual(len(view.medications), 1)
        self.assertIn("氨氯地平", view.medications[0])
        self.assertEqual(view.next_follow_up_at, T0 + 30 * DAY)
        self.assertEqual(len(view.pending_pickups), 1)
        self.assertIn("配送", view.pending_pickups[0])
        self.assertTrue(any("随访" in step for step in view.next_steps))

    def test_customer_view_after_termination(self) -> None:
        store = _view_store()
        terminate_plan(store, "plan-1", "cust-1", T0 + DAY)
        view = customer_view(store, "cust-1", T0 + 2 * DAY)
        self.assertEqual(view.plan_status, "terminated")
        self.assertEqual(view.medications, ())


class StaffBoardTest(unittest.TestCase):
    def test_staff_board_contains_only_logistics(self) -> None:
        store = _view_store()
        reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        board = staff_pickup_board(store, member(store, "clerk-1"), "store-1", T0)
        self.assertEqual(len(board), 1)
        entry = board[0]
        self.assertEqual(entry.customer_name, "张某")
        self.assertEqual(entry.drug_name, "氨氯地平")
        self.assertEqual(entry.channel, "pickup")
        # 看板字段仅限物流事项
        self.assertEqual(
            set(entry.__dataclass_fields__),
            {"reservation_id", "customer_name", "drug_name", "quantity", "channel", "status"},
        )

    def test_marketing_cannot_view_pickup_board(self) -> None:
        store = _view_store()
        with self.assertRaises(AccessDeniedError):
            staff_pickup_board(store, member(store, "mkt-1"), "store-1", T0)


class RationaleTest(unittest.TestCase):
    def test_adjustment_rationale_shows_basis_and_responsibility(self) -> None:
        store = _view_store()
        observation = record_observation(
            store, member(store, "ph-1"), "plan-1", "blood_pressure", "150/95", T0, T0
        )
        adjustment = propose_adjustment(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-2"),), "血压未达标，换用氯沙坦", (observation.observation_id,), T0 + DAY,
        )
        review_adjustment(store, member(store, "ph-2"), adjustment.adjustment_id, True, "同意", T0 + 2 * DAY)
        rationale = adjustment_rationale(store, member(store, "ph-1"), adjustment.adjustment_id, T0 + 3 * DAY)
        self.assertEqual(rationale.kind, "adjustment")
        self.assertEqual(rationale.proposed_by.license_no, "LICENSE-001")
        self.assertIn("health_records", rationale.consents)
        self.assertIn("氯沙坦", rationale.drugs)
        self.assertEqual(len(rationale.observations), 1)
        self.assertEqual(rationale.observations[0].measured_at, T0)
        self.assertEqual(rationale.reviewed_by, "ph-2")
        self.assertIn("LICENSE-001", rationale.explanation)

    def test_follow_up_rationale(self) -> None:
        store = _view_store()
        observation = record_observation(
            store, member(store, "ph-1"), "plan-1", "blood_pressure", "128/82", T0, T0
        )
        follow_up = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW,
            (observation.observation_id,), None, T0 + DAY,
        )
        rationale = follow_up_rationale(store, member(store, "ph-1"), follow_up.follow_up_id, T0 + 2 * DAY)
        self.assertEqual(rationale.kind, "follow_up")
        self.assertIn("血压平稳", rationale.explanation)
        self.assertEqual(len(rationale.observations), 1)


class ComplianceTraceTest(unittest.TestCase):
    def test_transactions_and_safety_reports_traceable_after_termination(self) -> None:
        store = _view_store()
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        report = report_adverse_reaction(
            store, member(store, "ph-1"), "plan-1", "drug-1", "B-1", "皮疹", RiskLevel.MEDIUM, T0 + 2 * DAY
        )
        terminate_plan(store, "plan-1", "cust-1", T0 + 3 * DAY)
        trace = compliance_trace(store, member(store, "safety-1"), "cust-1", T0 + 4 * DAY)
        self.assertEqual(len(trace.transactions), 1, "既有交易仍可追溯")
        self.assertEqual(trace.transactions[0].reference, reservation.reservation_id)
        self.assertEqual(len(trace.safety_reports), 1, "安全处置仍可追溯")
        self.assertEqual(trace.safety_reports[0].report_id, report.report_id)
        self.assertTrue(trace.audit)

    def test_clerk_cannot_access_compliance_trace(self) -> None:
        store = _view_store()
        with self.assertRaises(AccessDeniedError):
            compliance_trace(store, member(store, "clerk-1"), "cust-1", T0)


if __name__ == "__main__":
    unittest.main()
