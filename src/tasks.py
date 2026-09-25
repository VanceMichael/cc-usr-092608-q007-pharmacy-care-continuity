"""随访、召回与调拨的统一待联系工作清单。

任务按来源（source）去重并互相关联（related），使到期随访、
批次召回和门店调拨可以协调为一次联系；服务终止不清空任务，
重新启动服务后待联系顾客不丢失。
"""

from __future__ import annotations

from datetime import datetime

from src.entities import ContactTask, TaskKind, TaskStatus
from src.errors import NotFoundError
from src.store import DataStore


def create_task(
    store: DataStore,
    customer_id: str,
    kind: TaskKind,
    due_at: datetime,
    detail: str,
    now: datetime,
    source: str,
    related: tuple[str, ...] = (),
) -> ContactTask:
    """创建待联系任务；同一来源的任务去重并合并关联。"""
    for existing in store.pending_tasks(customer_id=customer_id, kind=kind):
        if existing.source == source:
            existing.related = tuple(sorted(set(existing.related) | set(related)))
            return existing
    task = ContactTask(
        task_id=store.next_id("task"),
        customer_id=customer_id,
        kind=kind,
        status=TaskStatus.PENDING,
        due_at=due_at,
        created_at=now,
        detail=detail,
        source=source,
        related=tuple(related),
    )
    store.tasks[task.task_id] = task
    return task


def complete_task(store: DataStore, task_id: str, now: datetime) -> ContactTask:
    task = store.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    task.status = TaskStatus.DONE
    task.completed_at = now
    return task


def due_tasks(
    store: DataStore, now: datetime, kind: TaskKind | None = None
) -> list[ContactTask]:
    """到期的待联系任务。"""
    return [t for t in store.pending_tasks(kind=kind) if t.due_at <= now]


def restore_pending_followups(
    store: DataStore, customer_id: str, now: datetime
) -> list[ContactTask]:
    """服务重启后，依据最近一次随访结论的下次随访时间补齐待联系任务。

    终止服务不清空任务；若任务记录缺失（例如曾被完成或取消），
    则从随访记录重新推导，保证待联系顾客不丢失。
    """
    follow_ups = [
        fu
        for plan in store.plans.values()
        if plan.customer_id == customer_id
        for fu in store.follow_ups_for(plan.plan_id)
        if fu.next_due_at is not None
    ]
    if not follow_ups:
        return []
    latest = max(follow_ups, key=lambda f: f.created_at)
    known = [t for t in store.tasks.values() if t.source == latest.follow_up_id]
    if any(t.status is TaskStatus.PENDING for t in known):
        return [t for t in known if t.status is TaskStatus.PENDING]
    if any(t.status is TaskStatus.DONE for t in known):
        return []  # 该次随访已联系过
    task = create_task(
        store,
        customer_id,
        TaskKind.FOLLOW_UP,
        latest.next_due_at,
        f"随访到期: {latest.conclusion}",
        now,
        source=latest.follow_up_id,
    )
    return [task]
