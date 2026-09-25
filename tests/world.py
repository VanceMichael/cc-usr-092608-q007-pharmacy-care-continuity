"""构造一个脱敏的标准测试世界：两家门店、药师/复核人/店员/运营、两名顾客。"""

from __future__ import annotations

from src.backend import PharmacyBackend
from src.models import Scope

T0 = "2026-09-01T09:00:00+00:00"
CRED_VALID = "2030-12-31T23:59:59+00:00"
CRED_EXPIRED = "2026-01-01T00:00:00+00:00"


def build_world(snapshot_path: str | None = None, enroll: bool = True) -> PharmacyBackend:
    b = PharmacyBackend(snapshot_path)
    reg = b.registry

    reg.register_store("S1", "健康驿站一店")
    reg.register_store("S2", "健康驿站二店")
    reg.register_staff("P1", "甲药师", "pharmacist", "ZY-1001", CRED_VALID, "S1")
    reg.register_staff("P2", "乙药师", "pharmacist", "ZY-2002", CRED_VALID, "S2")
    reg.register_staff("PE", "资质过期药师", "pharmacist", "ZY-EXPIRED", CRED_EXPIRED, "S1")
    reg.register_staff("RV", "安全复核员", "safety_reviewer")
    reg.register_staff("CK", "普通店员", "clerk", store_id="S1")
    reg.register_staff("OP", "连锁运营", "operator")
    reg.register_customer("C1", "顾客*明")
    reg.register_customer("C2", "顾客*华")

    reg.register_plan("CHRONIC", "慢病陪伴计划", high_risk_drugs=("DRUG_WARF",))
    reg.register_drug("DRUG_HTN", "氨氯地平")
    reg.register_drug("DRUG_WARF", "华法林")
    reg.register_sku("SKU_A", "DRUG_HTN", "安心牌", "安心药业")
    reg.register_sku("SKU_B", "DRUG_HTN", "平压牌", "平压药业")
    reg.register_sku("SKU_W", "DRUG_WARF", "抗凝牌")
    reg.register_batch("B001", "SKU_A", "2027-06-30T00:00:00+00:00")
    reg.register_batch("B002", "SKU_A", "2027-12-31T00:00:00+00:00")
    reg.register_batch("B100", "SKU_B", "2027-09-30T00:00:00+00:00")
    reg.register_batch("BW01", "SKU_W", "2027-03-31T00:00:00+00:00")

    for store in ("S1", "S2"):
        b.inventory.inbound(store, "SKU_A", "B001", 10, "OP", T0, "期初入库")
        b.inventory.inbound(store, "SKU_A", "B002", 8, "OP", T0, "期初入库")
        b.inventory.inbound(store, "SKU_B", "B100", 6, "OP", T0, "期初入库")
        b.inventory.inbound(store, "SKU_W", "BW01", 4, "OP", T0, "期初入库")

    if enroll:
        b.enrollment.enroll(
            "C1",
            "CHRONIC",
            "S1",
            {
                Scope.HEALTH_RECORDS: True,
                Scope.REMINDERS: True,
                Scope.CROSS_STORE: True,
            },
            at=T0,
            responsible_pharmacist_id="P1",
        )
        b.records.issue_plan(
            "C1",
            "P1",
            [
                {"drug_id": "DRUG_HTN", "sku": "SKU_A", "dose": "5mg", "frequency": "qd",
                 "days_supply": 30}
            ],
            note="初始降压方案",
            occurred_at=T0,
        )
    return b
