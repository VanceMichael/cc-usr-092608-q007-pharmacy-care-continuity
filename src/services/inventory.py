"""库存与批次台账：预留 → 领取 → 退回/报损 → 调拨，全程对账。

口径：
- 实物库存 = 物理流水（入库/领取/退回/报损/调拨出入）带符号求和；
- 锁定库存 = 状态 RESERVED 的预留数量；
- 可售 = 实物 - 锁定；召回批次可售恒为 0，应急保供同样不得出库。
所有动作同时记录 occurred_at（真实发生）与 recorded_at（入库时间）。
"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.errors import BatchRiskError, InventoryError
from src.models import (
    ContactReason,
    ContactStatus,
    MovementKind,
    Reservation,
    ReservationStatus,
    StockMovement,
    Transfer,
    TransferStatus,
    to_row,
)
from src.repository import Repository

_PHYSICAL_KINDS = frozenset(
    {
        MovementKind.INBOUND,
        MovementKind.CLAIM,
        MovementKind.RETURN,
        MovementKind.WRITE_OFF,
        MovementKind.TRANSFER_OUT,
        MovementKind.TRANSFER_IN,
    }
)


class InventoryService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ---- 余额 -----------------------------------------------------
    def physical_qty(self, store_id: str, sku: str, batch_no: str) -> int:
        return sum(
            int(m["qty"])
            for m in self.repo.filter("movements", store_id=store_id, sku=sku, batch_no=batch_no)
            if m["kind"] in _PHYSICAL_KINDS
        )

    def locked_qty(self, store_id: str, sku: str, batch_no: str) -> int:
        return sum(
            int(r["qty"])
            for r in self.repo.filter(
                "reservations", store_id=store_id, sku=sku, batch_no=batch_no,
                status=ReservationStatus.RESERVED,
            )
        )

    def is_recalled(self, sku: str, batch_no: str, at: str | None = None) -> str | None:
        """返回召回单号；指定 at 时判定该时刻是否处于召回期，否则按当前状态。"""
        moment = timeutil.parse(at) if at else timeutil.now()
        for recall in self.repo.filter("recalls", sku=sku):
            if batch_no not in recall.get("batch_numbers", ()):
                continue
            if timeutil.parse(recall["issued_at"]) > moment:
                continue
            if recall.get("closed_at") and timeutil.parse(recall["closed_at"]) <= moment:
                continue
            return recall["id"]
        return None

    def available_qty(self, store_id: str, sku: str, batch_no: str) -> int:
        if self.is_recalled(sku, batch_no):
            return 0
        return self.physical_qty(store_id, sku, batch_no) - self.locked_qty(store_id, sku, batch_no)

    def stock_take(self, store_id: str) -> list[dict[str, Any]]:
        """按门店/品规/批次对账：实物、锁定、可售、召回标记。"""
        keys = {
            (m["sku"], m["batch_no"])
            for m in self.repo.filter("movements", store_id=store_id)
            if m["kind"] in _PHYSICAL_KINDS
        }
        keys.update(
            (r["sku"], r["batch_no"]) for r in self.repo.filter("reservations", store_id=store_id)
        )
        report = []
        for sku, batch_no in sorted(keys):
            physical = self.physical_qty(store_id, sku, batch_no)
            locked = self.locked_qty(store_id, sku, batch_no)
            recall_id = self.is_recalled(sku, batch_no)
            report.append(
                {
                    "store_id": store_id,
                    "sku": sku,
                    "batch_no": batch_no,
                    "physical": physical,
                    "locked": locked,
                    "available": 0 if recall_id else physical - locked,
                    "recall_id": recall_id,
                }
            )
        return report

    # ---- 流水 -----------------------------------------------------
    def _movement(
        self,
        store_id: str,
        sku: str,
        batch_no: str,
        kind: str,
        qty: int,
        occurred_at: str,
        ref: str,
        operator: str,
    ) -> dict[str, Any]:
        row = to_row(
            StockMovement(
                id=f"mov_{uuid.uuid4().hex[:10]}",
                occurred_at=timeutil.format_value(timeutil.parse(occurred_at)),
                recorded_at=timeutil.format_value(timeutil.now()),
                store_id=store_id,
                sku=sku,
                batch_no=batch_no,
                kind=kind,
                qty=qty,
                ref=ref,
                operator=operator,
            )
        )
        self.repo.add("movements", row)
        return row

    def inbound(
        self,
        store_id: str,
        sku: str,
        batch_no: str,
        qty: int,
        operator: str,
        occurred_at: str | None = None,
        ref: str = "",
    ) -> dict[str, Any]:
        if qty <= 0:
            raise InventoryError("入库数量必须为正")
        if self.is_recalled(sku, batch_no):
            raise BatchRiskError("召回批次不得入库，应隔离处置")
        return self._movement(
            store_id, sku, batch_no, MovementKind.INBOUND, qty,
            occurred_at or timeutil.format_value(timeutil.now()), ref or "采购入库", operator,
        )

    # ---- 预留 -----------------------------------------------------
    def reserve(
        self,
        customer_id: str | None,
        store_id: str,
        sku: str,
        batch_no: str,
        qty: int,
        staff_id: str,
        ttl_hours: float = 48.0,
        emergency: bool = False,
        occurred_at: str | None = None,
        historical: bool = False,
    ) -> dict[str, Any]:
        """预留锁定。应急保供走同一通道：批次召回一律拒绝，不得绕过。

        historical=True 仅用于断网补录既成事实：跳过召回与库存校验，
        由调用方负责风险打标；正常业务不得使用。
        """
        if qty <= 0:
            raise InventoryError("预留数量必须为正")
        recall_id = self.is_recalled(sku, batch_no)
        if recall_id and not historical:
            raise BatchRiskError(f"批次{batch_no}已被召回（{recall_id}），应急保供也不能预留")
        business_at = timeutil.parse(occurred_at) if occurred_at else timeutil.now()
        if (
            not historical
            and self.physical_qty(store_id, sku, batch_no) - self.locked_qty(store_id, sku, batch_no) < qty
        ):
            raise InventoryError("可售库存不足，无法预留")
        expires = business_at + timeutil.hours(ttl_hours)
        row = to_row(
            Reservation(
                id=f"rsv_{uuid.uuid4().hex[:10]}",
                customer_id=customer_id,
                store_id=store_id,
                sku=sku,
                batch_no=batch_no,
                qty=qty,
                status=ReservationStatus.RESERVED,
                created_at=timeutil.format_value(business_at),
                expires_at=timeutil.format_value(expires),
                emergency=emergency,
            )
        )
        self.repo.add("reservations", row)
        return row

    def claim(
        self,
        reservation_id: str,
        staff_id: str,
        occurred_at: str | None = None,
        historical: bool = False,
    ) -> dict[str, Any]:
        """凭预留领取出库；领取前再次核对批次风险（召回可能发生在预留之后）。"""
        reservation = self.repo.find("reservations", id=reservation_id)
        if reservation is None:
            raise InventoryError(f"预留单不存在：{reservation_id}")
        if reservation["status"] != ReservationStatus.RESERVED:
            raise InventoryError(f"预留单状态为{reservation['status']}，不能领取")
        recall_id = self.is_recalled(reservation["sku"], reservation["batch_no"])
        if recall_id and not historical:
            raise BatchRiskError(
                f"批次{reservation['batch_no']}已被召回（{recall_id}），停止发放并改安排合格批次"
            )
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        self._movement(
            reservation["store_id"],
            reservation["sku"],
            reservation["batch_no"],
            MovementKind.CLAIM,
            -int(reservation["qty"]),
            business_at,
            reservation_id,
            staff_id,
        )
        self.repo.update(
            "reservations",
            reservation_id,
            {"status": ReservationStatus.CLAIMED, "claimed_at": timeutil.format_value(timeutil.parse(business_at))},
        )
        return self.repo.find("reservations", id=reservation_id)  # type: ignore[return-value]

    def release(
        self, reservation_id: str, staff_id: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        """未领取的预留取消：仅释放锁定，无实物移动。"""
        reservation = self.repo.find("reservations", id=reservation_id)
        if reservation is None or reservation["status"] != ReservationStatus.RESERVED:
            raise InventoryError("预留单不存在或不可取消")
        self.repo.update(
            "reservations",
            reservation_id,
            {
                "status": ReservationStatus.CANCELLED,
                "returned_at": occurred_at or timeutil.format_value(timeutil.now()),
            },
        )
        return self.repo.find("reservations", id=reservation_id)  # type: ignore[return-value]

    def return_goods(
        self, reservation_id: str, staff_id: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        """已领取药品退回：实物回库；召回批次退回后隔离，不得再次销售。"""
        reservation = self.repo.find("reservations", id=reservation_id)
        if reservation is None or reservation["status"] != ReservationStatus.CLAIMED:
            raise InventoryError("只能退回已领取的预留")
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        self._movement(
            reservation["store_id"],
            reservation["sku"],
            reservation["batch_no"],
            MovementKind.RETURN,
            int(reservation["qty"]),
            business_at,
            reservation_id,
            staff_id,
        )
        self.repo.update(
            "reservations", reservation_id,
            {"status": ReservationStatus.RETURNED, "returned_at": business_at},
        )
        return self.repo.find("reservations", id=reservation_id)  # type: ignore[return-value]

    def write_off(
        self,
        store_id: str,
        sku: str,
        batch_no: str,
        qty: int,
        operator: str,
        reason: str,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        if qty <= 0:
            raise InventoryError("报损数量必须为正")
        recalled = bool(self.is_recalled(sku, batch_no))
        if not recalled and self.physical_qty(store_id, sku, batch_no) - self.locked_qty(store_id, sku, batch_no) < qty:
            raise InventoryError("报损数量超过未锁定实物库存")
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        return self._movement(
            store_id, sku, batch_no, MovementKind.WRITE_OFF, -qty, business_at,
            f"报损：{reason}", operator,
        )

    # ---- 调拨 -----------------------------------------------------
    def create_transfer(
        self,
        from_store_id: str,
        to_store_id: str,
        sku: str,
        batch_no: str,
        qty: int,
        operator: str,
        occurred_at: str | None = None,
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        if qty <= 0:
            raise InventoryError("调拨数量必须为正")
        if self.is_recalled(sku, batch_no):
            raise BatchRiskError("召回批次不得调拨，应急保供也不能绕过批次风险")
        if self.physical_qty(from_store_id, sku, batch_no) - self.locked_qty(from_store_id, sku, batch_no) < qty:
            raise InventoryError("调出店可售库存不足")
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        transfer = to_row(
            Transfer(
                id=f"trf_{uuid.uuid4().hex[:10]}",
                from_store_id=from_store_id,
                to_store_id=to_store_id,
                sku=sku,
                batch_no=batch_no,
                qty=qty,
                status=TransferStatus.IN_TRANSIT,
                created_at=business_at,
                reservation_id=reservation_id,
            )
        )
        self.repo.add("transfers", transfer)
        self._movement(
            from_store_id, sku, batch_no, MovementKind.TRANSFER_OUT, -qty,
            business_at, transfer["id"], operator,
        )
        return transfer

    def arrive_transfer(
        self, transfer_id: str, operator: str, occurred_at: str | None = None
    ) -> dict[str, Any]:
        transfer = self.repo.find("transfers", id=transfer_id)
        if transfer is None:
            raise InventoryError(f"调拨单不存在：{transfer_id}")
        if transfer["status"] != TransferStatus.IN_TRANSIT:
            raise InventoryError("调拨单不在在途状态")
        # 到货时再次核对：运输期间批次若被召回，不得入可售库存
        if self.is_recalled(transfer["sku"], transfer["batch_no"]):
            raise BatchRiskError("调拨批次在途中被召回，到货后隔离不得上架")
        business_at = occurred_at or timeutil.format_value(timeutil.now())
        self._movement(
            transfer["to_store_id"], transfer["sku"], transfer["batch_no"],
            MovementKind.TRANSFER_IN, int(transfer["qty"]), business_at,
            transfer["id"], operator,
        )
        self.repo.update("transfers", transfer_id, {"status": TransferStatus.ARRIVED, "arrived_at": business_at})
        # 为顾客预留而调拨的，到货后生成待领取联系（由协调器去重写入）
        if transfer.get("reservation_id"):
            reservation = self.repo.find("reservations", id=transfer["reservation_id"])
            if reservation is not None:
                self._queue_transfer_notice(reservation, business_at)
        return self.repo.find("transfers", id=transfer_id)  # type: ignore[return-value]

    def _queue_transfer_notice(self, reservation: dict[str, Any], at_iso: str) -> None:
        customer_id = reservation.get("customer_id")
        if not customer_id:
            return
        for task in self.repo.filter("contacts", customer_id=customer_id, status=ContactStatus.OPEN):
            if ContactReason.TRANSFER_NOTICE in task["reasons"]:
                return
        self.repo.add(
            "contacts",
            {
                "id": f"ctc_{uuid.uuid4().hex[:10]}",
                "customer_id": customer_id,
                "reasons": [ContactReason.TRANSFER_NOTICE],
                "store_id": reservation["store_id"],
                "due_at": at_iso,
                "status": ContactStatus.OPEN,
                "created_at": at_iso,
                "detail": f"调拨药品{reservation['sku']}已到店，预留单{reservation['id']}",
                "refs": [f"transfer:{reservation['id']}"],
            },
        )
