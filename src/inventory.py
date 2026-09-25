"""批次库存台账：预留、领取、退回、报损、调拨与召回。

所有变动以追加式台账记录，任意时点可按门店与批次对账。
召回批次禁止预留、领取与调拨——应急保供也不提供绕过批次
风险的通道。领取支持传入 occurred_at 补录真实发生时间。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from src import tasks
from src.access import ensure
from src.entities import (
    BatchStatus,
    ContactTask,
    DrugBatch,
    DrugInfo,
    LedgerEvent,
    LedgerKind,
    Reservation,
    ReservationStatus,
    StaffMember,
    TaskKind,
)
from src.errors import NotFoundError, ValidationError
from src.store import DataStore


def _record(
    store: DataStore,
    store_id: str,
    drug_id: str,
    batch_no: str,
    kind: LedgerKind,
    quantity: int,
    occurred_at: datetime,
    recorded_at: datetime,
    reference: str | None = None,
) -> LedgerEvent:
    event = LedgerEvent(
        store.next_id("evt"),
        store_id,
        drug_id,
        batch_no,
        kind,
        quantity,
        occurred_at,
        recorded_at,
        reference,
    )
    store.ledger.append(event)
    return event


def _batch(store: DataStore, drug_id: str, batch_no: str) -> DrugBatch:
    batch = store.batches.get((drug_id, batch_no))
    if batch is None:
        raise NotFoundError(f"批次未登记: {drug_id}/{batch_no}")
    return batch


def _ensure_usable(batch: DrugBatch) -> None:
    if batch.status is BatchStatus.RECALLED:
        raise ValidationError(f"批次 {batch.batch_no} 已召回，不能继续流通")


def receive_stock(
    store: DataStore,
    member: StaffMember,
    store_id: str,
    drug_id: str,
    drug_name: str,
    ingredient: str,
    batch_no: str,
    quantity: int,
    now: datetime,
    high_risk: bool = False,
) -> LedgerEvent:
    """入库并登记药品与批次信息。"""
    ensure(member, "logistics")
    if quantity < 1:
        raise ValidationError("入库数量必须为正数")
    if drug_id not in store.drugs:
        store.drugs[drug_id] = DrugInfo(drug_id, drug_name, ingredient, high_risk)
    store.batches.setdefault(
        (drug_id, batch_no), DrugBatch(drug_id, batch_no, BatchStatus.USABLE)
    )
    return _record(
        store, store_id, drug_id, batch_no, LedgerKind.RECEIVED, quantity, now, now
    )


def on_hand(store: DataStore, store_id: str, drug_id: str, batch_no: str) -> int:
    """某门店某批次的现存量（不含预留）。"""
    total = 0
    for event in store.ledger:
        if (
            event.store_id == store_id
            and event.drug_id == drug_id
            and event.batch_no == batch_no
        ):
            if event.kind in (
                LedgerKind.RECEIVED,
                LedgerKind.RETURNED,
                LedgerKind.TRANSFER_IN,
            ):
                total += event.quantity
            elif event.kind in (
                LedgerKind.PICKED_UP,
                LedgerKind.WRITTEN_OFF,
                LedgerKind.TRANSFER_OUT,
            ):
                total -= event.quantity
    return total


def reserved_quantity(store: DataStore, store_id: str, drug_id: str, batch_no: str) -> int:
    """某门店某批次未履约的预留量。"""
    return sum(
        r.quantity
        for r in store.open_reservations(store_id=store_id)
        if r.drug_id == drug_id and r.batch_no == batch_no
    )


def available(store: DataStore, store_id: str, drug_id: str, batch_no: str) -> int:
    return on_hand(store, store_id, drug_id, batch_no) - reserved_quantity(
        store, store_id, drug_id, batch_no
    )


def reserve(
    store: DataStore,
    customer_id: str,
    store_id: str,
    drug_id: str,
    batch_no: str,
    quantity: int,
    now: datetime,
    channel: str = "pickup",
) -> Reservation:
    """库存预留；channel 区分领取（pickup）与配送（delivery）。"""
    store.get_customer(customer_id)
    batch = _batch(store, drug_id, batch_no)
    _ensure_usable(batch)
    if quantity < 1:
        raise ValidationError("预留数量必须为正数")
    if available(store, store_id, drug_id, batch_no) < quantity:
        raise ValidationError("可用库存不足")
    reservation = Reservation(
        store.next_id("res"),
        customer_id,
        store_id,
        drug_id,
        batch_no,
        quantity,
        ReservationStatus.OPEN,
        now,
        channel,
    )
    store.reservations[reservation.reservation_id] = reservation
    _record(
        store,
        store_id,
        drug_id,
        batch_no,
        LedgerKind.RESERVED,
        quantity,
        now,
        now,
        reference=reservation.reservation_id,
    )
    return reservation


def pickup(
    store: DataStore,
    member: StaffMember,
    reservation_id: str,
    now: datetime,
    occurred_at: datetime | None = None,
) -> LedgerEvent:
    """履约领取；occurred_at 支持断网后补录真实领取时间。"""
    ensure(member, "logistics")
    reservation = store.reservations.get(reservation_id)
    if reservation is None:
        raise NotFoundError(f"预留不存在: {reservation_id}")
    if reservation.status is not ReservationStatus.OPEN:
        raise ValidationError("预留已关闭，不能重复领取")
    batch = _batch(store, reservation.drug_id, reservation.batch_no)
    _ensure_usable(batch)  # 召回批次即使已预留也不能领取
    happened = occurred_at if occurred_at is not None else now
    if happened > now:
        raise ValidationError("领取时间不能晚于当前时间")
    reservation.status = ReservationStatus.FULFILLED
    event = _record(
        store,
        reservation.store_id,
        reservation.drug_id,
        reservation.batch_no,
        LedgerKind.PICKED_UP,
        reservation.quantity,
        happened,
        now,
        reference=reservation.reservation_id,
    )
    store.log(
        now, member.staff_id, "inventory.pickup", reservation_id, f"数量 {reservation.quantity}"
    )
    return event


def release_reservation(
    store: DataStore, reservation_id: str, now: datetime, reason: str = "取消"
) -> Reservation:
    """释放预留（取消、过期或召回）。"""
    reservation = store.reservations.get(reservation_id)
    if reservation is None:
        raise NotFoundError(f"预留不存在: {reservation_id}")
    if reservation.status is not ReservationStatus.OPEN:
        raise ValidationError("预留已关闭")
    reservation.status = ReservationStatus.RELEASED
    _record(
        store,
        reservation.store_id,
        reservation.drug_id,
        reservation.batch_no,
        LedgerKind.RESERVATION_RELEASED,
        reservation.quantity,
        now,
        now,
        reference=reservation.reservation_id,
    )
    store.log(now, "system", "inventory.release", reservation_id, reason)
    return reservation


def return_items(
    store: DataStore,
    member: StaffMember,
    store_id: str,
    drug_id: str,
    batch_no: str,
    quantity: int,
    now: datetime,
    reference: str | None = None,
) -> LedgerEvent:
    """顾客退回，重新计入库存。"""
    ensure(member, "logistics")
    _batch(store, drug_id, batch_no)
    if quantity < 1:
        raise ValidationError("退回数量必须为正数")
    return _record(
        store, store_id, drug_id, batch_no, LedgerKind.RETURNED, quantity, now, now, reference
    )


def write_off(
    store: DataStore,
    member: StaffMember,
    store_id: str,
    drug_id: str,
    batch_no: str,
    quantity: int,
    reason: str,
    now: datetime,
) -> LedgerEvent:
    """报损；数量不得超过现存。"""
    ensure(member, "inventory_manage")
    _batch(store, drug_id, batch_no)
    if quantity < 1:
        raise ValidationError("报损数量必须为正数")
    if on_hand(store, store_id, drug_id, batch_no) < quantity:
        raise ValidationError("报损数量超过现存库存")
    event = _record(
        store, store_id, drug_id, batch_no, LedgerKind.WRITTEN_OFF, quantity, now, now
    )
    store.log(now, member.staff_id, "inventory.write_off", f"{drug_id}/{batch_no}", reason)
    return event


def transfer_stock(
    store: DataStore,
    member: StaffMember,
    from_store: str,
    to_store: str,
    drug_id: str,
    batch_no: str,
    quantity: int,
    now: datetime,
) -> list[ContactTask]:
    """门店调拨；影响未履约预留时生成改址协调任务。"""
    ensure(member, "transfer")
    batch = _batch(store, drug_id, batch_no)
    _ensure_usable(batch)  # 召回批次禁止调拨
    if quantity < 1:
        raise ValidationError("调拨数量必须为正数")
    if on_hand(store, from_store, drug_id, batch_no) < quantity:
        raise ValidationError("调出门店库存不足")
    _record(store, from_store, drug_id, batch_no, LedgerKind.TRANSFER_OUT, quantity, now, now)
    _record(store, to_store, drug_id, batch_no, LedgerKind.TRANSFER_IN, quantity, now, now)
    created: list[ContactTask] = []
    # 协调：调拨后源门店可用量覆盖不了未履约预留时，需要改址或重新安排
    if available(store, from_store, drug_id, batch_no) < 0:
        for reservation in store.open_reservations(store_id=from_store):
            if reservation.drug_id == drug_id and reservation.batch_no == batch_no:
                created.append(
                    tasks.create_task(
                        store,
                        reservation.customer_id,
                        TaskKind.TRANSFER_REROUTE,
                        now,
                        f"预留 {reservation.reservation_id} 受调拨影响，需协调改址",
                        now,
                        source=reservation.reservation_id,
                    )
                )
    store.log(
        now,
        member.staff_id,
        "inventory.transfer",
        f"{drug_id}/{batch_no}",
        f"{from_store} -> {to_store} 数量 {quantity}",
    )
    return created


def recall_batch(
    store: DataStore,
    member: StaffMember,
    drug_id: str,
    batch_no: str,
    reason: str,
    now: datetime,
) -> list[ContactTask]:
    """批次召回：释放未履约预留，并为已领取顾客生成召回联系任务。

    召回联系任务与同一顾客的待办随访任务互相关联，便于协调为一次联系。
    """
    ensure(member, "recall")
    key = (drug_id, batch_no)
    batch = _batch(store, drug_id, batch_no)
    if batch.status is BatchStatus.RECALLED:
        raise ValidationError("批次已处于召回状态")
    store.batches[key] = replace(
        batch, status=BatchStatus.RECALLED, recall_reason=reason, recalled_at=now
    )
    for reservation in store.open_reservations():
        if reservation.drug_id == drug_id and reservation.batch_no == batch_no:
            release_reservation(store, reservation.reservation_id, now, reason="批次召回")
    customers = {
        r.customer_id
        for r in store.reservations.values()
        if r.drug_id == drug_id
        and r.batch_no == batch_no
        and r.status is ReservationStatus.FULFILLED
    }
    created: list[ContactTask] = []
    for customer_id in sorted(customers):
        follow_ups = store.pending_tasks(customer_id=customer_id, kind=TaskKind.FOLLOW_UP)
        task = tasks.create_task(
            store,
            customer_id,
            TaskKind.RECALL_CONTACT,
            now,
            f"召回批次 {batch_no}: {reason}",
            now,
            source=f"recall:{drug_id}/{batch_no}",
            related=tuple(t.task_id for t in follow_ups),
        )
        for follow_up_task in follow_ups:
            follow_up_task.related = tuple(
                sorted(set(follow_up_task.related) | {task.task_id})
            )
        created.append(task)
    store.log(now, member.staff_id, "inventory.recall", f"{drug_id}/{batch_no}", reason)
    return created


def last_pickup(
    store: DataStore, customer_id: str, drug_id: str
) -> LedgerEvent | None:
    """顾客最近一次领取某药品的台账事件（按真实发生时间）。"""
    events = []
    for event in store.ledger:
        if event.kind is LedgerKind.PICKED_UP and event.drug_id == drug_id and event.reference:
            reservation = store.reservations.get(event.reference)
            if reservation is not None and reservation.customer_id == customer_id:
                events.append(event)
    return max(events, key=lambda e: e.occurred_at, default=None)


@dataclass(frozen=True)
class ReconEntry:
    """单个批次的对账结果。"""

    drug_id: str
    batch_no: str
    received: int
    picked_up: int
    returned: int
    written_off: int
    transferred_out: int
    transferred_in: int
    reserved_open: int
    on_hand: int
    physical: int | None
    matches: bool | None


def reconcile(
    store: DataStore,
    member: StaffMember,
    store_id: str,
    physical_counts: dict[tuple[str, str], int] | None = None,
) -> list[ReconEntry]:
    """按批次对账：预留、领取、退回、报损、调拨全部纳入。"""
    ensure(member, "logistics")
    physical_counts = physical_counts or {}
    keys = {
        (event.drug_id, event.batch_no)
        for event in store.ledger
        if event.store_id == store_id
    } | set(physical_counts)

    def _sum(drug_id: str, batch_no: str, kind: LedgerKind) -> int:
        return sum(
            event.quantity
            for event in store.ledger
            if event.store_id == store_id
            and event.drug_id == drug_id
            and event.batch_no == batch_no
            and event.kind is kind
        )

    entries = []
    for drug_id, batch_no in sorted(keys):
        on_hand_now = on_hand(store, store_id, drug_id, batch_no)
        physical = physical_counts.get((drug_id, batch_no))
        entries.append(
            ReconEntry(
                drug_id=drug_id,
                batch_no=batch_no,
                received=_sum(drug_id, batch_no, LedgerKind.RECEIVED),
                picked_up=_sum(drug_id, batch_no, LedgerKind.PICKED_UP),
                returned=_sum(drug_id, batch_no, LedgerKind.RETURNED),
                written_off=_sum(drug_id, batch_no, LedgerKind.WRITTEN_OFF),
                transferred_out=_sum(drug_id, batch_no, LedgerKind.TRANSFER_OUT),
                transferred_in=_sum(drug_id, batch_no, LedgerKind.TRANSFER_IN),
                reserved_open=reserved_quantity(store, store_id, drug_id, batch_no),
                on_hand=on_hand_now,
                physical=physical,
                matches=None if physical is None else physical == on_hand_now,
            )
        )
    return entries
