"""服务计划生命周期：建档、分别授权、终止与重启。"""

import unittest

from src.clinical import record_follow_up
from src.entities import ConsentScope, PlanStatus, RiskLevel, TaskKind
from src.errors import PlanStateError, ValidationError
from src.plans import enroll, restart_plan, revoke_consent, terminate_plan
from tests.support import ALL_CONSENTS, DAY, T0, make_store, member


class PlanLifecycleTest(unittest.TestCase):
    def test_enroll_creates_plan_with_separate_consents(self) -> None:
        store = make_store()
        plan = store.plans["plan-1"]
        self.assertEqual(plan.status, PlanStatus.ACTIVE)
        self.assertEqual(plan.version, 1)
        self.assertEqual(
            set(plan.consents), set(ALL_CONSENTS), "三种授权应分别记录"
        )
        self.assertTrue(all(c.is_active(T0) for c in plan.consents.values()))

    def test_enroll_rejects_duplicate_active_plan(self) -> None:
        store = make_store()
        with self.assertRaises(PlanStateError):
            enroll(store, "cust-1", "另一计划", "store-1", "ph-1", ALL_CONSENTS, T0)

    def test_terminate_revokes_all_consents_but_keeps_records(self) -> None:
        store = make_store()
        follow_up = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 30 * DAY, T0,
        )
        terminate_plan(store, "plan-1", "cust-1", T0 + DAY)
        plan = store.plans["plan-1"]
        self.assertEqual(plan.status, PlanStatus.TERMINATED)
        self.assertTrue(all(c.revoked_at is not None for c in plan.consents.values()))
        self.assertIn(follow_up.follow_up_id, store.follow_ups, "随访记录应保留")
        self.assertTrue(store.audit, "审计日志应保留")

    def test_restart_creates_new_version_and_restores_contact_tasks(self) -> None:
        store = make_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 30 * DAY, T0,
        )
        terminate_plan(store, "plan-1", "cust-1", T0 + DAY)
        restarted = restart_plan(store, "cust-1", "ph-1", ALL_CONSENTS, T0 + 2 * DAY)
        self.assertEqual(restarted.version, 2)
        self.assertEqual(restarted.status, PlanStatus.ACTIVE)
        pending = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)
        self.assertEqual(len(pending), 1, "重启后待联系顾客不应丢失")
        self.assertEqual(pending[0].due_at, T0 + 30 * DAY)

    def test_consent_grant_and_revoke(self) -> None:
        store = make_store(consents=(ConsentScope.HEALTH_RECORDS,))
        plan = store.plans["plan-1"]
        self.assertNotIn(ConsentScope.CROSS_STORE, plan.consents)
        revoke_consent(store, "plan-1", ConsentScope.HEALTH_RECORDS, T0)
        self.assertFalse(plan.consents[ConsentScope.HEALTH_RECORDS].is_active(T0 + DAY))
        with self.assertRaises(ValidationError):
            revoke_consent(store, "plan-1", ConsentScope.HEALTH_RECORDS, T0 + 2 * DAY)


if __name__ == "__main__":
    unittest.main()
