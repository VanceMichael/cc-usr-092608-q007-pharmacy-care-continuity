"""访问控制：角色权限、顾客授权与服务终止的即时生效。"""

import unittest

from src.access import ensure
from src.clinical import record_observation
from src.consultation import open_consultation
from src.entities import ConsentScope, PharmacistProfile, Role, StaffMember
from src.errors import AccessDeniedError, ConsentError, PlanStateError
from src.plans import revoke_consent, terminate_plan
from tests.support import DAY, T0, make_store, member


class AccessTest(unittest.TestCase):
    def test_store_staff_cannot_access_health_records(self) -> None:
        store = make_store()
        with self.assertRaises(AccessDeniedError):
            open_consultation(store, member(store, "clerk-1"), "cust-1", "store-1", T0)
        with self.assertRaises(AccessDeniedError):
            record_observation(store, member(store, "clerk-1"), "plan-1", "bp", "120/80", T0, T0)

    def test_marketing_cannot_access_health_information(self) -> None:
        store = make_store()
        with self.assertRaises(AccessDeniedError):
            open_consultation(store, member(store, "mkt-1"), "cust-1", "store-1", T0)
        with self.assertRaises(AccessDeniedError):
            ensure(member(store, "mkt-1"), "health_read")

    def test_expired_pharmacist_license_is_denied(self) -> None:
        store = make_store()
        store.pharmacists["ph-3"] = PharmacistProfile(
            "ph-3", "过期药师", "LICENSE-EXP", ("执业药师",), T0 - DAY
        )
        store.staff["ph-3"] = StaffMember("ph-3", "过期药师", Role.PHARMACIST, "store-1")
        with self.assertRaises(AccessDeniedError):
            record_observation(store, member(store, "ph-3"), "plan-1", "bp", "120/80", T0, T0)

    def test_termination_stops_new_health_access_immediately(self) -> None:
        store = make_store()
        terminate_plan(store, "plan-1", "cust-1", T0)
        with self.assertRaises(PlanStateError):
            open_consultation(store, member(store, "ph-1"), "cust-1", "store-1", T0)
        with self.assertRaises(PlanStateError):
            record_observation(store, member(store, "ph-1"), "plan-1", "bp", "120/80", T0, T0)

    def test_revoked_health_consent_blocks_access(self) -> None:
        store = make_store()
        revoke_consent(store, "plan-1", ConsentScope.HEALTH_RECORDS, T0)
        with self.assertRaises(ConsentError):
            open_consultation(store, member(store, "ph-1"), "cust-1", "store-1", T0)

    def test_safety_officer_can_read_but_not_write(self) -> None:
        store = make_store()
        context = open_consultation(store, member(store, "safety-1"), "cust-1", "store-1", T0)
        self.assertEqual(context.plan.plan_id, "plan-1")
        with self.assertRaises(AccessDeniedError):
            record_observation(store, member(store, "safety-1"), "plan-1", "bp", "120/80", T0, T0)


if __name__ == "__main__":
    unittest.main()
