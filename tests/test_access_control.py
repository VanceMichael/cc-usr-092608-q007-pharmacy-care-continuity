"""角色边界、分项授权与终止即停。"""

import unittest

from src.errors import AuthorizationError, CredentialError
from src.models import Scope, StaffRole
from tests.world import T0, build_world


class AccessControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_clerk_only_sees_fulfillment(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.b.access.check_health("C1", "CK", Scope.HEALTH_RECORDS, "peek")
        # 领取配送可办
        ctx = self.b.access.assert_fulfillment("C1", "CK", "handover")
        self.assertEqual(ctx["staff"]["role"], StaffRole.CLERK)
        # 拒绝也留痕
        denied = [log for log in self.b.repo.all("access_logs") if not log["granted"]]
        self.assertTrue(any("仅可处理领取与配送事项" in log["reason"] for log in denied))

    def test_operator_cannot_touch_health_or_fulfillment(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.b.access.check_health("C1", "OP", Scope.HEALTH_RECORDS, "peek")
        with self.assertRaises(AuthorizationError):
            self.b.access.assert_fulfillment("C1", "OP", "handover")

    def test_expired_credential_blocks_pharmacist(self) -> None:
        with self.assertRaises(CredentialError):
            self.b.access.assert_practicing_pharmacist(self.b.registry.get_staff("PE"), T0)

    def test_scopes_are_independent(self) -> None:
        self.b.enrollment.set_consent("C1", Scope.HEALTH_RECORDS, False, at="2026-09-02T09:00:00+00:00")
        consents = self.b.access.effective_consents(
            self.b.access.active_enrollment("C1")["id"], "2026-09-03T09:00:00+00:00"
        )
        self.assertFalse(consents[Scope.HEALTH_RECORDS])
        self.assertTrue(consents[Scope.REMINDERS])  # 另一项不受影响
        with self.assertRaises(AuthorizationError):
            self.b.records.record_observation(
                "C1", "P1", "S1", "blood_pressure", "138/86", "mmHg",
                "2026-09-03T09:00:00+00:00",
            )

    def test_termination_stops_new_health_access_immediately(self) -> None:
        self.b.enrollment.terminate(
            "C1", "顾客要求终止", staff_id="P1", at="2026-09-08T09:00:00+00:00"
        )
        with self.assertRaises(AuthorizationError):
            self.b.cross_store_consult("C1", "P2", "S2", "2026-09-08T10:00:00+00:00")
        self.assertIsNone(self.b.access.active_enrollment("C1", "2026-09-09T00:00:00+00:00"))
        # 终止时刻之前的授权状态仍可回放（依法追溯）
        enrollment_id = self.b.repo.find("enrollments", customer_id="C1")["id"]
        earlier = self.b.access.effective_consents(enrollment_id, "2026-09-01T10:00:00+00:00")
        self.assertTrue(earlier[Scope.HEALTH_RECORDS])


if __name__ == "__main__":
    unittest.main()
