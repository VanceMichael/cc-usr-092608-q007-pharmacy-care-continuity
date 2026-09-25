"""断网补录与时间线：真实发生时间保留，晚到信息不回溯。

补录入口允许门店把断网期间完成的测量/领取按 occurred_at 补登；
recorded_at 恒为入库时刻。所有“截至 T 的判断”都同时要求
occurred_at ≤ T 且 recorded_at ≤ T，因此晚到信息只影响它之后产生的建议。
"""

from __future__ import annotations

from typing import Any

from src import timeutil
from src.models import Decision, ReservationStatus
from src.repository import Repository
from src.services.dispense import DispenseService
from src.services.records import RecordService


class TimelineService:
    def __init__(self, repo: Repository, records: RecordService, dispense: DispenseService) -> None:
        self.repo = repo
        self.records = records
        self.dispense = dispense

    def backfill_observation(
        self,
        customer_id: str,
        staff_id: str,
        store_id: str,
        kind: str,
        value: str,
        unit: str,
        occurred_at: str,
        note: str = "",
    ) -> dict[str, Any]:
        """补录测量：业务时间必须早于当前（否则是正常录入），真实时间原样保留。"""
        business = timeutil.parse(occurred_at)
        if business > timeutil.now():
            raise ValueError("补录时间不能晚于当前时间")
        business_iso = timeutil.format_value(business)
        recorded_iso = timeutil.format_value(timeutil.now())
        row = self.records.record_observation(
            customer_id, staff_id, store_id, kind, value, unit,
            occurred_at=business_iso, note=note or "断网补录",
            recorded_at=recorded_iso,
        )
        row["late"] = timeutil.parse(row["recorded_at"]) > business
        return row

    def backfill_dispense(
        self,
        customer_id: str | None,
        store_id: str,
        sku: str,
        batch_no: str,
        qty: int,
        staff_id: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """补录断网期间已完成的领取：按真实时间登记实物出库与预留领取。

        不重算当时的销售决策，但对“在召回生效后仍售出”等情形打标供追溯。
        """
        business = timeutil.parse(occurred_at)
        if business > timeutil.now():
            raise ValueError("补录时间不能晚于当前时间")
        business_iso = timeutil.format_value(business)
        # 履约权限校验（店员可办，不触及健康内容）
        self.dispense.access.assert_fulfillment(customer_id, staff_id, "backfill_dispense", business_iso)

        inventory = self.dispense.inventory
        flags: list[str] = ["offline_backfill"]
        recall_id = inventory.is_recalled(sku, batch_no)
        if recall_id:
            recall = self.repo.find("recalls", id=recall_id)
            if recall is not None and timeutil.parse(recall["issued_at"]) <= business:
                flags.append("sold_after_recall_effective")

        reservation = inventory.reserve(
            customer_id=customer_id, store_id=store_id, sku=sku, batch_no=batch_no,
            qty=qty, staff_id=staff_id, ttl_hours=0.0, occurred_at=business_iso,
            historical=True,
        )
        claimed = inventory.claim(reservation["id"], staff_id, business_iso, historical=True)

        import uuid
        attempt_id = f"att_{uuid.uuid4().hex[:10]}"
        self.repo.add(
            "attempts",
            {
                "id": attempt_id,
                "customer_id": customer_id,
                "store_id": store_id,
                "sku": sku,
                "batch_no": batch_no,
                "qty": qty,
                "occurred_at": business_iso,
                "recorded_at": timeutil.format_value(timeutil.now()),
                "decision": Decision.ALLOW,
                "reasons": flags,
                "staff_id": staff_id,
                "reservation_id": reservation["id"],
            },
        )
        return {"reservation": claimed, "attempt_id": attempt_id, "flags": flags}

    # ---- 时间线查询 -----------------------------------------------
    def customer_timeline(self, customer_id: str, as_of: str | None = None) -> list[dict[str, Any]]:
        """截至 T 可见的全部业务事件（含补录，按真实发生时间排序）。"""
        moment_iso = timeutil.format_value(timeutil.parse(as_of) if as_of else timeutil.now())
        events: list[dict[str, Any]] = []
        for row in self.repo.filter("observations", customer_id=customer_id):
            if self._visible(row, moment_iso):
                events.append({"at": row["occurred_at"], "type": "observation", "ref": row["id"],
                               "kind": row["kind"], "value": f"{row['value']}{row['unit']}",
                               "late": timeutil.parse(row["recorded_at"]) > timeutil.parse(row["occurred_at"])})
        for row in self.repo.filter("followups", customer_id=customer_id):
            if self._visible(row, moment_iso):
                events.append({"at": row["occurred_at"], "type": "followup", "ref": row["id"],
                               "conclusion": row["conclusion"]})
        for row in self.repo.filter("reservations", customer_id=customer_id):
            if row["status"] == ReservationStatus.CLAIMED and row.get("claimed_at"):
                claimed_at = row["claimed_at"]
                if timeutil.parse(claimed_at) <= timeutil.parse(moment_iso):
                    events.append({"at": claimed_at, "type": "dispense", "ref": row["id"],
                                   "sku": row["sku"], "qty": row["qty"],
                                   "store_id": row["store_id"]})
        for row in self.repo.filter("adverse_events", customer_id=customer_id):
            if self._visible(row, moment_iso):
                events.append({"at": row["occurred_at"], "type": "adverse_event", "ref": row["id"],
                               "severity": row["severity"]})
        return sorted(events, key=lambda event: timeutil.parse(event["at"]))

    @staticmethod
    def _visible(row: dict[str, Any], moment_iso: str) -> bool:
        moment = timeutil.parse(moment_iso)
        return (
            timeutil.parse(row["occurred_at"]) <= moment
            and timeutil.parse(row["recorded_at"]) <= moment
        )
