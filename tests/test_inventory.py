"""库存台账：预留、领取、退回、报损、调拨、召回与对账。"""

import unittest

from src.entities import LedgerKind, ReservationStatus, TaskKind
from src.errors import AccessDeniedError, ValidationError
from src.inventory import (
    available,
    on_hand,
    pickup,
    recall_batch,
    receive_stock,
    reconcile,
    release_reservation,
    reserve,
    return_items,
    transfer_stock,
    write_off,
)
from tests.support import DAY, T0, make_store, member


def _inventory_store():
    store = make_store()
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-1", 100, T0)
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-2", 50, T0)
    return store


class InventoryTest(unittest.TestCase):
    def test_full_lifecycle_reconciles(self) -> None:
        store = _inventory_store()
        clerk = member(store, "clerk-1")
        ops = member(store, "ops-1")
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        self.assertEqual(available(store, "store-1", "drug-1", "B-1"), 70)
        pickup(store, clerk, reservation.reservation_id, T0 + DAY)
        return_items(store, clerk, "store-1", "drug-1", "B-1", 5, T0 + 2 * DAY)
        write_off(store, ops, "store-1", "drug-1", "B-1", 3, "破损", T0 + 3 * DAY)
        # 100 - 30 + 5 - 3 = 72
        self.assertEqual(on_hand(store, "store-1", "drug-1", "B-1"), 72)
        entries = reconcile(store, clerk, "store-1", {("drug-1", "B-1"): 72})
        entry = entries[0]
        self.assertEqual(entry.received, 100)
        self.assertEqual(entry.picked_up, 30)
        self.assertEqual(entry.returned, 5)
        self.assertEqual(entry.written_off, 3)
        self.assertEqual(entry.on_hand, 72)
        self.assertTrue(entry.matches)

    def test_reconcile_flags_physical_mismatch(self) -> None:
        store = _inventory_store()
        entries = reconcile(store, member(store, "clerk-1"), "store-1", {("drug-1", "B-1"): 90})
        self.assertFalse(entries[0].matches)

    def test_reserve_beyond_available_is_rejected(self) -> None:
        store = _inventory_store()
        reserve(store, "cust-1", "store-1", "drug-1", "B-1", 100, T0)
        with self.assertRaises(ValidationError):
            reserve(store, "cust-2", "store-1", "drug-1", "B-1", 1, T0)

    def test_recalled_batch_cannot_be_reserved_picked_up_or_transferred(self) -> None:
        store = _inventory_store()
        clerk = member(store, "clerk-1")
        ops = member(store, "ops-1")
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 10, T0)
        recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "污染风险", T0 + DAY)
        # 召回后未履约预留已被释放
        self.assertEqual(store.reservations[reservation.reservation_id].status, ReservationStatus.RELEASED)
        with self.assertRaises(ValidationError):
            reserve(store, "cust-1", "store-1", "drug-1", "B-1", 1, T0 + DAY)
        with self.assertRaises(ValidationError):
            pickup(store, clerk, reservation.reservation_id, T0 + DAY)
        with self.assertRaises(ValidationError):
            transfer_stock(store, ops, "store-1", "store-2", "drug-1", "B-1", 10, T0 + DAY)

    def test_recall_creates_contact_tasks_for_picked_up_customers(self) -> None:
        store = _inventory_store()
        clerk = member(store, "clerk-1")
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, clerk, reservation.reservation_id, T0 + DAY)
        created = recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "含量不合格", T0 + 2 * DAY)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].customer_id, "cust-1")
        self.assertEqual(created[0].kind, TaskKind.RECALL_CONTACT)

    def test_transfer_moves_stock_and_coordinates_reservations(self) -> None:
        store = _inventory_store()
        ops = member(store, "ops-1")
        reserve(store, "cust-1", "store-1", "drug-1", "B-1", 80, T0)
        created = transfer_stock(store, ops, "store-1", "store-2", "drug-1", "B-1", 50, T0 + DAY)
        self.assertEqual(on_hand(store, "store-1", "drug-1", "B-1"), 50)
        self.assertEqual(on_hand(store, "store-2", "drug-1", "B-1"), 50)
        # 调出后可用量 50-80 = -30，预留受影响，生成改址协调任务
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].kind, TaskKind.TRANSFER_REROUTE)

    def test_pickup_backfill_keeps_true_occurrence_time(self) -> None:
        store = _inventory_store()
        clerk = member(store, "clerk-1")
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 10, T0 - 3 * DAY)
        happened = T0 - 2 * DAY  # 断网期间真实领取时间
        event = pickup(store, clerk, reservation.reservation_id, T0, occurred_at=happened)
        self.assertEqual(event.occurred_at, happened)
        self.assertEqual(event.recorded_at, T0)
        self.assertEqual(event.kind, LedgerKind.PICKED_UP)

    def test_write_off_beyond_stock_is_rejected(self) -> None:
        store = _inventory_store()
        with self.assertRaises(ValidationError):
            write_off(store, member(store, "ops-1"), "store-1", "drug-1", "B-1", 200, "损耗", T0)

    def test_clerk_cannot_write_off_or_transfer(self) -> None:
        store = _inventory_store()
        clerk = member(store, "clerk-1")
        with self.assertRaises(AccessDeniedError):
            write_off(store, clerk, "store-1", "drug-1", "B-1", 1, "损耗", T0)
        with self.assertRaises(AccessDeniedError):
            transfer_stock(store, clerk, "store-1", "store-2", "drug-1", "B-1", 1, T0)

    def test_release_reservation_frees_stock(self) -> None:
        store = _inventory_store()
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 40, T0)
        self.assertEqual(available(store, "store-1", "drug-1", "B-1"), 60)
        release_reservation(store, reservation.reservation_id, T0 + DAY)
        self.assertEqual(available(store, "store-1", "drug-1", "B-1"), 100)


if __name__ == "__main__":
    unittest.main()
