"""跨店咨询：先定位当前有效计划与最近安全结论，再继续服务。"""

import unittest

from src.errors import AuthorizationError
from src.models import Scope
from tests.world import T0, build_world


class CrossStoreConsultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_consult_returns_active_plan_and_latest_safety(self) -> None:
        self.b.records.record_followup(
            "C1", "P1", "S1", "partial", "偶有漏服，已指导设提醒",
            "继续当前方案，两周内复测血压",
            occurred_at="2026-09-05T09:00:00+00:00",
        )
        packet = self.b.cross_store_consult("C1", "P2", "S2", "2026-09-06T09:00:00+00:00")
        self.assertEqual(packet["home_store_id"], "S1")
        self.assertEqual(packet["consult_store_id"], "S2")
        self.assertEqual(packet["medication_plan"]["seq"], 1)
        self.assertEqual(
            packet["latest_safety_conclusion"]["conclusion"],
            "继续当前方案，两周内复测血压",
        )
        self.assertEqual(packet["credential_no"], "ZY-2002")
        # 访问有日志
        logs = [
            log for log in self.b.repo.all("access_logs")
            if log["action"] == "cross_store_consult" and log["granted"]
        ]
        self.assertTrue(logs)

    def test_consult_denied_without_cross_store_consent(self) -> None:
        self.b.enrollment.set_consent("C1", Scope.CROSS_STORE, False, at="2026-09-04T09:00:00+00:00")
        with self.assertRaises(AuthorizationError):
            self.b.cross_store_consult("C1", "P2", "S2", "2026-09-06T09:00:00+00:00")

    def test_consult_denied_after_termination(self) -> None:
        self.b.enrollment.terminate("C1", "搬家", staff_id="P1", at="2026-09-07T09:00:00+00:00")
        with self.assertRaises(AuthorizationError):
            self.b.cross_store_consult("C1", "P2", "S2", "2026-09-08T09:00:00+00:00")

    def test_clerk_cannot_open_cross_store_packet(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.b.cross_store_consult("C1", "CK", "S2", T0)

    def test_late_safety_conclusion_does_not_appear_in_earlier_consult(self) -> None:
        # 随访真实发生在 09-05，但断网恢复后 09-10 才入库（显式记录入库时间）
        self.b.records.record_followup(
            "C1", "P1", "S1", "full", "记录延迟补登", "晚间结论：需尽快复诊",
            occurred_at="2026-09-05T20:00:00+00:00",
            recorded_at="2026-09-10T12:00:00+00:00",
        )
        # 09-08 的咨询看不到这条晚到结论
        earlier = self.b.cross_store_consult("C1", "P2", "S2", "2026-09-08T09:00:00+00:00")
        self.assertIsNone(earlier["latest_safety_conclusion"])
        # 入库之后的咨询可以看到
        later = self.b.cross_store_consult("C1", "P2", "S2", "2026-09-11T09:00:00+00:00")
        self.assertIsNotNone(later["latest_safety_conclusion"])


if __name__ == "__main__":
    unittest.main()
