"""基础档案：门店、人员、顾客、服务计划、药品、品规、批次、召回。"""

from __future__ import annotations

import uuid
from typing import Any

from src import timeutil
from src.errors import DuplicateError
from src.models import (
    Batch,
    Customer,
    Drug,
    Recall,
    ServicePlan,
    Sku,
    Staff,
    Store,
    to_row,
)
from src.repository import Repository


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class RegistryService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def register_store(self, store_id: str, name: str) -> dict[str, Any]:
        if self.repo.find("stores", id=store_id):
            raise DuplicateError(f"门店已存在：{store_id}")
        row = to_row(Store(id=store_id, name=name))
        self.repo.add("stores", row)
        return row

    def register_staff(
        self,
        staff_id: str,
        name: str,
        role: str,
        credential_no: str = "",
        credential_valid_until: str = "",
        store_id: str | None = None,
    ) -> dict[str, Any]:
        if self.repo.find("staff", id=staff_id):
            raise DuplicateError(f"人员已存在：{staff_id}")
        row = to_row(
            Staff(
                id=staff_id,
                name=name,
                role=role,
                credential_no=credential_no,
                credential_valid_until=credential_valid_until,
                store_id=store_id,
            )
        )
        self.repo.add("staff", row)
        return row

    def register_customer(self, customer_id: str, label: str) -> dict[str, Any]:
        if self.repo.find("customers", id=customer_id):
            raise DuplicateError(f"顾客已存在：{customer_id}")
        row = to_row(Customer(id=customer_id, label=label))
        self.repo.add("customers", row)
        return row

    def register_plan(
        self, plan_id: str, name: str, description: str = "", high_risk_drugs: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        if self.repo.find("service_plans", id=plan_id):
            raise DuplicateError(f"服务计划已存在：{plan_id}")
        row = to_row(
            ServicePlan(
                id=plan_id, name=name, description=description, high_risk_drugs=tuple(high_risk_drugs)
            )
        )
        self.repo.add("service_plans", row)
        return row

    def register_drug(self, drug_id: str, generic_name: str) -> dict[str, Any]:
        if self.repo.find("drugs", id=drug_id):
            raise DuplicateError(f"药品已存在：{drug_id}")
        row = to_row(Drug(id=drug_id, generic_name=generic_name))
        self.repo.add("drugs", row)
        return row

    def register_sku(self, code: str, drug_id: str, brand: str, maker: str = "") -> dict[str, Any]:
        if self.repo.find("skus", code=code):
            raise DuplicateError(f"品规已存在：{code}")
        row = to_row(Sku(code=code, drug_id=drug_id, brand=brand, maker=maker))
        self.repo.add("skus", row)
        return row

    def register_batch(self, batch_no: str, sku: str, expiry: str = "") -> dict[str, Any]:
        if self.repo.find("batches", batch_no=batch_no, sku=sku):
            raise DuplicateError(f"批次已存在：{sku}/{batch_no}")
        row = to_row(Batch(batch_no=batch_no, sku=sku, expiry=expiry))
        self.repo.add("batches", row)
        return row

    def issue_recall(
        self, recall_id: str, title: str, sku: str, batch_numbers: tuple[str, ...],
        reason: str = "", at: str | None = None,
    ) -> dict[str, Any]:
        if self.repo.find("recalls", id=recall_id):
            raise DuplicateError(f"召回单已存在：{recall_id}")
        issued_at = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        row = to_row(
            Recall(
                id=recall_id,
                title=title,
                sku=sku,
                batch_numbers=tuple(batch_numbers),
                issued_at=issued_at,
                reason=reason,
            )
        )
        self.repo.add("recalls", row)
        for batch_no in batch_numbers:
            batch = self.repo.find("batches", sku=sku, batch_no=batch_no)
            if batch is not None:
                self.repo.update("batches", batch_no, {"recall_id": recall_id}, key="batch_no")
        return row

    # ---- 查询 -----------------------------------------------------
    def get_staff(self, staff_id: str) -> dict[str, Any]:
        row = self.repo.find("staff", id=staff_id)
        if row is None:
            raise KeyError(f"人员不存在：{staff_id}")
        return row

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.repo.find("customers", id=customer_id)

    def get_sku(self, code: str) -> dict[str, Any]:
        row = self.repo.find("skus", code=code)
        if row is None:
            raise KeyError(f"品规不存在：{code}")
        return row
