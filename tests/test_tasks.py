"""待联系工作清单：随访、召回与调拨的协调，重启不丢待联系顾客。"""

import unittest

from src.clinical import record_follow_up
from src.entities import RiskLevel, TaskKind, TaskStatus
from src.inventory import pickup, recall_batch, receive_stock, reserve
from src.plans import restart_plan, terminate_plan
from src.tasks import complete_task, due_tasks
from tests.support import ALL_CONSENTS, DAY, T0, make_store, member


def _task_store():
    store = make_store()
    clerk = member(store, "clerk-1")
    receive_stock(store, clerk, "store-1", "drug-1", "氨氯地平", "氨氯地平", "B-1", 100, T0)
    return store


class TaskCoordinationTest(unittest.TestCase):
    def test_follow_up_task_dedupes_by_source(self) -> None:
        store = _task_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 14 * DAY, T0,
        )
        # 同一随访来源的任务只保留一个
        tasks = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].due_at, T0 + 14 * DAY)

    def test_recall_task_links_to_pending_follow_up(self) -> None:
        store = _task_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 14 * DAY, T0,
        )
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        created = recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "污染", T0 + 2 * DAY)
        follow_up_task = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)[0]
        recall_task = created[0]
        # 两类任务互相关联，可协调为一次联系
        self.assertIn(follow_up_task.task_id, recall_task.related)
        self.assertIn(recall_task.task_id, follow_up_task.related)

    def test_restart_service_keeps_pending_contacts(self) -> None:
        store = _task_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 30 * DAY, T0,
        )
        reservation = reserve(store, "cust-1", "store-1", "drug-1", "B-1", 30, T0)
        pickup(store, member(store, "clerk-1"), reservation.reservation_id, T0 + DAY)
        recall_batch(store, member(store, "safety-1"), "drug-1", "B-1", "污染", T0 + 2 * DAY)
        terminate_plan(store, "plan-1", "cust-1", T0 + 3 * DAY)
        restart_plan(store, "cust-1", "ph-1", ALL_CONSENTS, T0 + 4 * DAY)
        follow_ups = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)
        recalls = store.pending_tasks(customer_id="cust-1", kind=TaskKind.RECALL_CONTACT)
        self.assertEqual(len(follow_ups), 1, "重启后随访待联系不应丢失")
        self.assertEqual(len(recalls), 1, "重启后召回待联系不应丢失")

    def test_restart_does_not_recreate_completed_contact(self) -> None:
        store = _task_store()
        follow_up = record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 30 * DAY, T0,
        )
        # 任务被完成（联系过）后，重启不再重建
        task = store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP)[0]
        complete_task(store, task.task_id, T0 + DAY)
        terminate_plan(store, "plan-1", "cust-1", T0 + 2 * DAY)
        restart_plan(store, "cust-1", "ph-1", ALL_CONSENTS, T0 + 3 * DAY)
        self.assertEqual(
            store.pending_tasks(customer_id="cust-1", kind=TaskKind.FOLLOW_UP), []
        )

    def test_due_tasks_filter_by_time_and_kind(self) -> None:
        store = _task_store()
        record_follow_up(
            store, member(store, "ph-1"), "plan-1", "血压平稳", RiskLevel.LOW, (),
            T0 + 10 * DAY, T0,
        )
        self.assertEqual(due_tasks(store, T0 + 5 * DAY), [])
        due = due_tasks(store, T0 + 10 * DAY, kind=TaskKind.FOLLOW_UP)
        self.assertEqual(len(due), 1)
        completed = complete_task(store, due[0].task_id, T0 + 10 * DAY)
        self.assertEqual(completed.status, TaskStatus.DONE)
        self.assertEqual(due_tasks(store, T0 + 11 * DAY), [])


if __name__ == "__main__":
    unittest.main()
