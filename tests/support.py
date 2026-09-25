"""测试公共构造：顾客、药师、店员与基础服务计划。"""

from datetime import datetime, timedelta, timezone

from src.entities import (
    ConsentScope,
    Customer,
    MedicationItem,
    PharmacistProfile,
    Role,
    StaffMember,
)
from src.plans import enroll
from src.store import DataStore

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)

ALL_CONSENTS = (
    ConsentScope.HEALTH_RECORDS,
    ConsentScope.REMINDERS,
    ConsentScope.CROSS_STORE,
)


def make_store(with_plan: bool = True, consents=ALL_CONSENTS) -> DataStore:
    store = DataStore()
    store.customers["cust-1"] = Customer("cust-1", "张某")
    store.customers["cust-2"] = Customer("cust-2", "李某")
    valid_until = datetime(2027, 6, 1, tzinfo=timezone.utc)
    store.pharmacists["ph-1"] = PharmacistProfile(
        "ph-1", "王药师", "LICENSE-001", ("执业药师",), valid_until
    )
    store.pharmacists["ph-2"] = PharmacistProfile(
        "ph-2", "赵药师", "LICENSE-002", ("执业药师", "主管药师"), valid_until
    )
    store.staff["ph-1"] = StaffMember("ph-1", "王药师", Role.PHARMACIST, "store-1")
    store.staff["ph-2"] = StaffMember("ph-2", "赵药师", Role.PHARMACIST, "store-2")
    store.staff["clerk-1"] = StaffMember("clerk-1", "店员甲", Role.STORE_STAFF, "store-1")
    store.staff["safety-1"] = StaffMember("safety-1", "质管乙", Role.SAFETY_OFFICER)
    store.staff["ops-1"] = StaffMember("ops-1", "运营丙", Role.OPERATIONS)
    store.staff["mkt-1"] = StaffMember("mkt-1", "营销丁", Role.MARKETING)
    if with_plan:
        enroll(store, "cust-1", "慢病管理-高血压", "store-1", "ph-1", consents, T0)
    return store


def member(store: DataStore, staff_id: str) -> StaffMember:
    return store.staff[staff_id]


def med_item(
    drug_id: str,
    start: datetime = T0,
    days: int = 30,
    days_supply: int = 30,
    frequency: int = 1,
) -> MedicationItem:
    return MedicationItem(
        drug_id=drug_id,
        dose="5mg",
        frequency_per_day=frequency,
        course_start=start,
        course_end=start + days * DAY,
        days_supply=days_supply,
    )
