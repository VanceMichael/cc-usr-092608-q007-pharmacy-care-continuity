"""库存预留/领取/退回/报损/调拨对账，以及召回与应急保供的批次红线。"""

import unittest

from src.errors import BatchRiskError, InventoryError
from src.models import ReservationStatus
from tests.world import T0, build_world


class InventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = build_world()

    def test_reserve_claim_return_ledger_balances(self) -> None:
        inv = self.b.inventory
        reservation = inv.reserve("C1", "S1", "SKU_A", "B001", 2, "CK", occurred_at=T0)
        self.assertEqual(inv.physical_qty("S1", "SKU_A", "B001"), 10)
        self.assertEqual(inv.available_qty("S1", "SKU_A", "B001"), 8)
        claimed = inv.claim(reservation["id"], "CK", "2026-09-01T12:00:00+00:00")
        self.assertEqual(claimed["status"], ReservationStatus.CLAIMED)
        self.assertEqual(inv.physical_qty("S1", "SKU_A", "B001"), 8)
        self.assertEqual(inv.available_qty("S1", "SKU_A", "B001"), 8)

        returned = inv.return_goods(reservation["id"], "CK", "2026-09-02T12:00:00+00:00")
        self.assertEqual(returned["status"], ReservationStatus.RETURNED)
        self.assertEqual(inv.physical_qty("S1", "SKU_A", "B001"), 10)

    def test_cancel_releases_lock_without_movement(self) -> None:
        inv = self.b.inventory
        reservation = inv.reserve("C1", "S1", "SKU_A", "B001", 3, "CK", occurred_at=T0)
        inv.release(reservation["id"], "CK", T0)
        self.assertEqual(inv.available_qty("S1", "SKU_A", "B001"), 10)

    def test_write_off_reduces_physical(self) -> None:
        self.b.inventory.write_off("S1", "SKU_A", "B001", 2, "CK", "包装破损", T0)
        self.assertEqual(self.b.inventory.physical_qty("S1", "SKU_A", "B001"), 8)

    def test_transfer_arrives_and_is_sellable(self) -> None:
        inv = self.b.inventory
        transfer = inv.create_transfer("S1", "S2", "SKU_A", "B002", 2, "CK", T0)
        self.assertEqual(inv.physical_qty("S1", "SKU_A", "B002"), 6)
        inv.arrive_transfer(transfer["id"], "CK", "2026-09-02T09:00:00+00:00")
        self.assertEqual(inv.physical_qty("S2", "SKU_A", "B002"), 10)

    def test_recalled_batch_blocks_every_path_including_emergency(self) -> None:
        inv = self.b.inventory
        self.b.registry.issue_recall("RC1", "某厂氨氯地平召回", "SKU_A", ("B001",), "杂质超标")
        with self.assertRaises(BatchRiskError):
            inv.reserve("C1", "S1", "SKU_A", "B001", 1, "CK", emergency=True, occurred_at=T0)
        with self.assertRaises(BatchRiskError):
            inv.create_transfer("S2", "S1", "SKU_A", "B001", 1, "CK", T0)
        self.assertEqual(inv.available_qty("S1", "SKU_A", "B001"), 0)
        # 另一批次不受影响
        self.assertEqual(inv.available_qty("S1", "SKU_A", "B002"), 8)

    def test_recall_during_transit_quarantines_on_arrival(self) -> None:
        inv = self.b.inventory
        transfer = inv.create_transfer("S1", "S2", "SKU_A", "B002", 2, "CK", T0)
        self.b.registry.issue_recall("RC2", "运输途中召回", "SKU_A", ("B002",), "风险提示")
        with self.assertRaises(BatchRiskError):
            inv.arrive_transfer(transfer["id"], "CK", "2026-09-02T09:00:00+00:00")

    def test_stock_take_flags_recall_and_totals(self) -> None:
        self.b.registry.issue_recall("RC3", "批次召回", "SKU_W", ("BW01",), "")
        rows = {row["batch_no"]: row for row in self.b.inventory.stock_take("S1")}
        self.assertTrue(rows["BW01"]["recall_id"])
        self.assertEqual(rows["BW01"]["available"], 0)
        self.assertEqual(rows["B002"]["available"], 8)


if __name__ == "__main__":
    unittest.main()
