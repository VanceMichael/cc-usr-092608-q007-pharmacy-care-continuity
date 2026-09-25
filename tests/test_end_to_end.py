"""端到端：跨店随访 → 召回阻断 → 调拨协调 → 应急保供 → 重启 → 终止与追溯。"""

import tempfile
import unittest
from pathlib import Path

from src import timeutil
from src.models import ContactReason, Decision, Scope
from tests.world import T0, build_world


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot = str(Path(self.tmp.name) / "state.json")
        self.b = build_world(self.snapshot)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_continuity_journey(self) -> None:
        b = self.b
        # 1) 顾客在 S1 领取首疗程
        first = b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at=T0)
        self.assertEqual(first["decision"], Decision.ALLOW)
        b.dispense.execute(first, "CK", occurred_at=T0)

        # 2) 顾客到 S2 跨店咨询：先看当前计划与最近安全结论
        packet = b.cross_store_consult("C1", "P2", "S2", "2026-09-06T09:00:00+00:00")
        self.assertEqual(packet["medication_plan"]["lines"][0]["drug_id"], "DRUG_HTN")

        # 3) S2 尝试发药：疗程未结束 → 转人工
        early = b.dispense.evaluate("C1", "S2", "SKU_A", 1, "CK", at="2026-09-06T10:00:00+00:00")
        self.assertEqual(early["decision"], Decision.MANUAL)

        # 4) 批次召回：在库批次阻断、有效预留释放、顾客进入召回待联系
        recall = b.registry.issue_recall("RC1", "氨氯地平召回", "SKU_A", ("B001",), "杂质")
        b.coordinator.notify_recall(recall["id"], at="2026-09-07T09:00:00+00:00")
        blocked = b.dispense.evaluate(
            "C1", "S1", "SKU_A", 1, "CK", batch_no="B001",
            at="2026-09-07T10:00:00+00:00", emergency=True,
        )
        self.assertEqual(blocked["decision"], Decision.BLOCK)
        self.assertEqual(blocked["reasons"], ["batch_recalled"])

        # 5) 门店调拨合格批次应急保供：B002 到货后可售
        transfer = b.inventory.create_transfer(
            "S2", "S1", "SKU_A", "B002", 2, "CK", "2026-09-07T11:00:00+00:00",
        )
        b.inventory.arrive_transfer(transfer["id"], "CK", "2026-09-08T09:00:00+00:00")
        # 召回批次仍阻断，系统自动选 B002（FEFO：B001 被跳过）
        allowed = b.dispense.evaluate(
            "C1", "S1", "SKU_A", 1, "CK",
            at="2026-10-02T09:00:00+00:00", emergency=True,
        )
        self.assertEqual(allowed["decision"], Decision.ALLOW)
        self.assertEqual(allowed["detail"]["batch_no"], "B002")
        b.dispense.execute(allowed, "CK", occurred_at="2026-10-02T09:00:00+00:00")

        # 6) 不良反应上报与安全结论，进入回访待办
        adr = b.records.report_adverse_event(
            "C1", "CK", "DRUG_HTN", "B002", "轻度头晕", "mild",
            "2026-10-03T08:00:00+00:00",
        )
        b.records.close_adverse_event(
            adr["id"], "P1", "继续观察，低盐饮食，三日内回访",
            occurred_at="2026-10-03T09:00:00+00:00",
        )
        b.coordinator.enqueue_adr_followup(adr["id"], due_at="2026-10-06T09:00:00+00:00")

        # 7) 到期随访 + 召回通知合并到同一条待联系任务
        b.records.record_followup(
            "C1", "P1", "S1", "partial", "基本规律", "轻度反应可控",
            occurred_at="2026-09-20T09:00:00+00:00",
            next_due_at="2026-10-04T09:00:00+00:00",
        )
        b.save()

        # 8) 系统重启：待联系顾客不丢失，扫描补齐到期随访
        restarted = b.restart()
        restarted.coordinator.scan_followups("2026-10-07T09:00:00+00:00")
        due = restarted.coordinator.due_contacts(at="2026-10-07T10:00:00+00:00")
        reasons = {r for task in due for r in task["reasons"]}
        self.assertIn(ContactReason.RECALL_NOTICE, reasons)
        self.assertIn(ContactReason.FOLLOWUP_DUE, reasons)
        self.assertIn(ContactReason.ADR_FOLLOWUP, reasons)

        # 9) 顾客终止服务：新的健康访问立即停止，召回/ADR 安全待办保留
        restarted.enrollment.terminate("C1", "疗程结束，终止服务", staff_id="P1",
                                       at="2026-10-08T09:00:00+00:00")
        with self.assertRaises(Exception):
            restarted.cross_store_consult("C1", "P2", "S2", "2026-10-08T10:00:00+00:00")
        retained = [
            task for task in restarted.coordinator.open_contacts("C1")
            if ContactReason.RECALL_NOTICE in task["reasons"]
            or ContactReason.ADR_FOLLOWUP in task["reasons"]
        ]
        self.assertTrue(retained)

        # 10) 既有交易与处置仍可依法追溯
        attempts = restarted.repo.all("attempts")
        self.assertTrue(any(a["decision"] == Decision.BLOCK for a in attempts))
        ledger = restarted.inventory.stock_take("S1")
        self.assertTrue(any(row["recall_id"] for row in ledger))


class OfflineBackfillEndToEndTest(unittest.TestCase):
    def test_offline_window_backfill_then_future_advice(self) -> None:
        b = build_world()
        # 断网窗口：09-08 的测量与 09-09 的领取，09-11 恢复网络
        timeutil.set_now("2026-09-11T09:00:00+00:00")
        try:
            obs = b.timeline.backfill_observation(
                "C1", "P1", "S1", "blood_pressure", "150/95", "mmHg",
                occurred_at="2026-09-08T09:00:00+00:00",
            )
            dispense = b.timeline.backfill_dispense(
                "C1", "S1", "SKU_A", "B002", 1, "CK",
                occurred_at="2026-09-09T09:00:00+00:00",
            )
        finally:
            timeutil.set_now(None)
        self.assertTrue(obs["late"])
        self.assertEqual(dispense["reservation"]["claimed_at"], "2026-09-09T09:00:00+00:00")

        # 09-12 的建议只受晚到信息影响
        advice = b.advice.issue_advice(
            "C1", "P1", "补录血压偏高", "连续监测三日并到店面谈",
            occurred_at="2026-09-12T09:00:00+00:00",
        )
        self.assertIn(obs["id"], advice["evidence_observations"])
        # 09-10 立即领取被识别为提前（09-09 刚领取过）
        decision = b.dispense.evaluate("C1", "S1", "SKU_A", 1, "CK", at="2026-09-10T09:00:00+00:00")
        self.assertEqual(decision["decision"], Decision.BLOCK)


if __name__ == "__main__":
    unittest.main()
