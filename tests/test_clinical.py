"""临床记录：观察补录、随访任务、调整复核与不良反应。"""

import unittest

from src.clinical import (
    propose_adjustment,
    record_follow_up,
    record_observation,
    record_prescription_summary,
    report_adverse_reaction,
    review_adjustment,
    set_medication_plan,
)
from src.entities import AdjustmentStatus, RiskLevel, TaskKind
from src.errors import AccessDeniedError, PlanStateError, ValidationError
from src.inventory import receive_stock
from src.plans import enroll
from tests.support import ALL_CONSENTS, DAY, T0, make_store, med_item, member


def _stocked_store():
    store = make_store()
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-1", 100, T0)
    receive_stock(store, clerk, "store-1", "drug-2", "氯沙坦", "氯沙坦", "B-2", 100, T0, high_risk=True)
    return store


class ClinicalTest(unittest.TestCase):
    def test_backfilled_observation_keeps_true_measured_time(self) -> None:
        store = _stocked_store()
        happened = T0 - 2 * DAY  # 断网期间的真实测量时间
        observation = record_observation(
            store, member(store, "ph-1"), "plan-1", "blood_pressure", "128/82", happened, T0
        )
        self.assertEqual(observation.measured_at, happened)
        self.assertEqual(observation.recorded_at, T0)
        self.assertLess(observation.measured_at, observation.recorded_at)

    def test_late_observation_only_affects_future_suggestions(self) -> None:
        store = _stocked_store()
        # 随访发生在补录之前：晚到的信息只影响之后的建议
        early = record_observation(
            store, member(store, "ph-1"), "plan-1", "bp", "130/85", T0 - DAY, T0
        )
        fu1 = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW,
            (early.observation_id,), None, T0 + DAY,
        )
        # 断网期间的真实测量，随访之后才补录
        late = record_observation(
            store, member(store, "ph-1"), "plan-1", "bp", "150/95", T0 - DAY, T0 + 2 * DAY
        )
        self.assertNotIn(late.observation_id, fu1.basis_observation_ids)
        self.assertEqual(
            [o.observation_id for o in store.observations_known_at("plan-1", T0 + DAY)],
            [early.observation_id],
            "截至某录入时间，补录信息尚未进入视野",
        )
        fu2 = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "需调整用药", RiskLevel.MEDIUM,
            (late.observation_id,), None, T0 + 3 * DAY,
        )
        self.assertIn(late.observation_id, fu2.basis_observation_ids)

    def test_future_measurement_is_rejected(self) -> None:
        store = _stocked_store()
        with self.assertRaises(ValidationError):
            record_observation(
                store, member(store, "ph-1"), "plan-1", "bp", "120/80", T0 + DAY, T0
            )

    def test_follow_up_creates_due_task(self) -> None:
        store = _stocked_store()
        follow_up = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 14 * DAY, T0,
        )
        tasks = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].source, follow_up.follow_up_id)
        self.assertEqual(tasks[0].due_at, T0 + 14 * DAY)

    def test_basis_observation_must_belong_to_plan(self) -> None:
        store = _stocked_store()
        # 另一个顾客的计划里的观察
        plan2 = enroll(store, "cust-2", "慢病管理-糖尿病", "store-1", "ph-1", ALL_CONSENTS, T0)
        other = record_observation(store, member(store, "ph-1"), plan2.plan_id, "bp", "130/80", T0, T0)
        with self.assertRaises(ValidationError):
            record_follow_up(
                store, member(store, "ph-1"), "plan-1", "随访", RiskLevel.LOW,
                (other.observation_id,), None, T0,
            )

    def test_low_risk_adjustment_approved_immediately(self) -> None:
        store = _stocked_store()
        set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1"),), T0)
        adjustment = propose_adjustment(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-1", frequency=2),), "剂量微调", (), T0 + DAY,
        )
        self.assertEqual(adjustment.status, AdjustmentStatus.APPROVED)
        med_plan = store.current_medication_plan("plan-1")
        self.assertEqual(med_plan.version, 2)
        self.assertEqual(med_plan.items[0].frequency_per_day, 2)

    def test_high_risk_adjustment_requires_second_reviewer(self) -> None:
        store = _stocked_store()
        set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1"),), T0)
        adjustment = propose_adjustment(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-2"),), "换用高风险药", (), T0 + DAY,
        )
        self.assertEqual(adjustment.status, AdjustmentStatus.PENDING_REVIEW)
        self.assertEqual(store.current_medication_plan("plan-1").version, 1, "未复核前不生效")
        # 提出人不能复核自己的调整
        with self.assertRaises(AccessDeniedError):
            review_adjustment(store, member(store, "ph-1"), adjustment.adjustment_id, True, "", T0 + DAY)
        # 店员无复核权限
        with self.assertRaises(AccessDeniedError):
            review_adjustment(store, member(store, "clerk-1"), adjustment.adjustment_id, True, "", T0 + DAY)
        # 另一名药师复核通过
        approved = review_adjustment(
            store, member(store, "ph-2"), adjustment.adjustment_id, True, "同意", T0 + 2 * DAY
        )
        self.assertEqual(approved.status, AdjustmentStatus.APPROVED)
        self.assertEqual(approved.reviewed_by, "ph-2")
        self.assertEqual(store.current_medication_plan("plan-1").version, 2)
        # 已复核的调整不能再次复核
        with self.assertRaises(PlanStateError):
            review_adjustment(store, member(store, "ph-2"), adjustment.adjustment_id, True, "", T0 + 3 * DAY)

    def test_rejected_adjustment_not_applied(self) -> None:
        store = _stocked_store()
        adjustment = propose_adjustment(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-2"),), "换用高风险药", (), T0,
        )
        rejected = review_adjustment(
            store, member(store, "safety-1"), adjustment.adjustment_id, False, "依据不足", T0 + DAY
        )
        self.assertEqual(rejected.status, AdjustmentStatus.REJECTED)
        self.assertIsNone(store.current_medication_plan("plan-1"))

    def test_adr_makes_later_adjustments_high_risk(self) -> None:
        store = _stocked_store()
        set_medication_plan(store, member(store, "ph-1"), "plan-1", (med_item("drug-1"),), T0)
        report = report_adverse_reaction(
            store, member(store, "ph-1"), "plan-1", "drug-1", "B-1", "踝部水肿", RiskLevel.MEDIUM, T0 + DAY,
        )
        self.assertIn(report.report_id, store.adr_reports)
        adjustment = propose_adjustment(
            store, member(store, "ph-1"), "plan-1",
            (med_item("drug-1", frequency=2),), "剂量调整", (), T0 + 2 * DAY,
        )
        self.assertEqual(adjustment.risk_level, RiskLevel.HIGH)
        self.assertEqual(adjustment.status, AdjustmentStatus.PENDING_REVIEW)

    def test_prescription_summary_recorded(self) -> None:
        store = _stocked_store()
        summary = record_prescription_summary(
            store, member(store, "ph-1"), "plan-1", "某医院心内科", "氨氯地平 5mg qd", T0 - DAY, T0
        )
        self.assertIn(summary.summary_id, store.summaries)
        with self.assertRaises(ValidationError):
            record_prescription_summary(
                store, member(store, "ph-1"), "plan-1", "某医院", "内容", T0 + DAY, T0
            )


if __name__ == "__main__":
    unittest.main()
