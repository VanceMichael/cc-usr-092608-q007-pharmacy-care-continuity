"""断网补录保留真实发生时间、晚到信息只影响之后、重启不丢待联系顾客。"""

import tempfile
import unittest
from pathlib import Path

from src.models import ContactReason, ContactStatus, Scope
from tests.world import T0, build_world


class TimelineAndRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot = str(Path(self.tmp.name) / "state.json")
        self.b = build_world(self.snapshot)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_backfilled_observation_keeps_real_time_and_is_late(self) -> None:
        from src import timeutil

        # 09-10 出具建议时没有血压记录
        self.b.advice.issue_advice(
            "C1", "P1", "暂无近期血压，维持原方案", "按时服药，两周后复测",
            occurred_at="2026-09-10T09:00:00+00:00",
        )
        # 断网恢复：系统时钟为 09-12 早 8 点，补录 09-08 的测量
        timeutil.set_now("2026-09-12T08:00:00+00:00")
        try:
            row = self.b.timeline.backfill_observation(
                "C1", "P1", "S1", "blood_pressure", "152/98", "mmHg",
                occurred_at="2026-09-08T09:00:00+00:00",
            )
        finally:
            timeutil.set_now(None)
        self.assertTrue(row["late"])
        self.assertEqual(row["occurred_at"], "2026-09-08T09:00:00+00:00")
        self.assertEqual(row["recorded_at"], "2026-09-12T08:00:00+00:00")

        # 09-10 的建议不回溯纳入晚到观察
        early_advice = self.b.repo.all("advice")[0]
        self.assertNotIn(row["id"], early_advice["evidence_observations"])

        # 之后（09-12 9 点）的建议可以引用补录观察
        later = self.b.advice.issue_advice(
            "C1", "P1", "补录血压偏高，建议复测并记录", "连续三日测血压",
            occurred_at="2026-09-12T09:00:00+00:00",
        )
        self.assertIn(row["id"], later["evidence_observations"])

        # 时间线按真实发生时间归位
        timeline = self.b.timeline.customer_timeline("C1", "2026-09-13T00:00:00+00:00")
        obs_events = [e for e in timeline if e["type"] == "observation"]
        self.assertEqual(obs_events[0]["at"], "2026-09-08T09:00:00+00:00")
        self.assertTrue(obs_events[0]["late"])

    def test_backfilled_dispense_keeps_ledger_at_real_time(self) -> None:
        result = self.b.timeline.backfill_dispense(
            "C1", "S1", "SKU_A", "B001", 1, "CK",
            occurred_at="2026-09-03T08:30:00+00:00",
        )
        self.assertIn("offline_backfill", result["flags"])
        self.assertEqual(result["reservation"]["claimed_at"], "2026-09-03T08:30:00+00:00")
        # 该笔领取立即参与之后的提前领取判断
        early = self.b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-09-05T09:00:00+00:00")
        self.assertIn("early_pickup", early["reasons"])

    def test_backfill_after_recall_is_flagged_but_retained(self) -> None:
        self.b.registry.issue_recall(
            "RC1", "批次召回", "SKU_A", ("B001",), "杂质",
            at="2026-09-10T09:00:00+00:00",
        )
        result = self.b.timeline.backfill_dispense(
            "C1", "S1", "SKU_A", "B001", 1, "CK",
            occurred_at="2026-09-12T08:30:00+00:00",
        )
        self.assertIn("sold_after_recall_effective", result["flags"])

    def test_open_contacts_survive_restart(self) -> None:
        # 到期随访
        self.b.records.record_followup(
            "C1", "P1", "S1", "full", "依从性好", "平稳",
            occurred_at="2026-09-05T09:00:00+00:00",
            next_due_at="2026-09-15T09:00:00+00:00",
        )
        self.b.coordinator.scan_followups("2026-09-16T09:00:00+00:00")
        self.assertTrue(self.b.coordinator.due_contacts(at="2026-09-16T09:00:00+00:00"))
        self.b.save()

        restarted = self.b.restart()
        due = restarted.coordinator.due_contacts(at="2026-09-16T10:00:00+00:00")
        self.assertEqual(len(due), 1)
        self.assertIn(ContactReason.FOLLOWUP_DUE, due[0]["reasons"])
        self.assertEqual(due[0]["status"], ContactStatus.OPEN)

    def test_recall_contacts_are_created_and_reasons_merge(self) -> None:
        # 先预留两笔，召回释放预留并通知；随访到期后原因合并
        r1 = self.b.inventory.reserve("C1", "S1", "SKU_A", "B001", 1, "CK", occurred_at=T0)
        self.b.records.record_followup(
            "C1", "P1", "S1", "full", "好", "平稳",
            occurred_at="2026-09-05T09:00:00+00:00",
            next_due_at="2026-09-10T09:00:00+00:00",
        )
        tasks = self.b.coordinator.notify_recall(
            self.b.registry.issue_recall("RC1", "召回", "SKU_A", ("B001",), "杂质")["id"],
            at="2026-09-06T09:00:00+00:00",
        )
        self.assertEqual(len(tasks), 1)
        # 召回释放了有效预留，无法再领取
        fresh = self.b.repo.find("reservations", id=r1["id"])
        self.assertEqual(fresh["status"], "cancelled")

        self.b.coordinator.scan_followups("2026-09-11T09:00:00+00:00")
        open_tasks = self.b.coordinator.open_contacts("C1")
        self.assertEqual(len(open_tasks), 1)  # 合并到召回任务
        self.assertCountEqual(
            open_tasks[0]["reasons"],
            [ContactReason.RECALL_NOTICE, ContactReason.FOLLOWUP_DUE],
        )

    def test_revoking_reminders_cancels_only_followup_task(self) -> None:
        self.b.records.record_followup(
            "C1", "P1", "S1", "full", "好", "平稳",
            occurred_at="2026-09-05T09:00:00+00:00",
            next_due_at="2026-09-10T09:00:00+00:00",
        )
        self.b.coordinator.scan_followups("2026-09-11T09:00:00+00:00")
        self.assertEqual(len(self.b.coordinator.open_contacts("C1")), 1)
        self.b.enrollment.set_consent("C1", Scope.REMINDERS, False, at="2026-09-12T09:00:00+00:00")
        self.assertEqual(self.b.coordinator.open_contacts("C1"), [])
        # 撤回提醒不影响跨店授权
        consents = self.b.access.effective_consents(
            self.b.access.active_enrollment("C1")["id"], "2026-09-12T10:00:00+00:00"
        )
        self.assertTrue(consents[Scope.CROSS_STORE])


if __name__ == "__main__":
    unittest.main()
