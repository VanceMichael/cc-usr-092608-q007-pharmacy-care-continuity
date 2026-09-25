"""药店健康管理后端门面：装配服务、快照持久化、跨店咨询入口。"""

from __future__ import annotations

from pathlib import Path

from src import timeutil
from src.errors import AuthorizationError
from src.models import Scope
from src.repository import Repository
from src.services.access import AccessService
from src.services.advice import AdviceService
from src.services.contacts import ContactCoordinator
from src.services.dispense import DispenseService
from src.services.enrollment import EnrollmentService
from src.services.inventory import InventoryService
from src.services.records import RecordService
from src.services.registry import RegistryService
from src.services.timeline import TimelineService


class PharmacyBackend:
    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self.repo = Repository(snapshot_path)
        self.repo.load()
        self.access = AccessService(self.repo)
        self.registry = RegistryService(self.repo)
        self.enrollment = EnrollmentService(self.repo, self.access)
        self.records = RecordService(self.repo, self.access)
        self.inventory = InventoryService(self.repo)
        self.coordinator = ContactCoordinator(self.repo, self.access)
        self.dispense = DispenseService(self.repo, self.access, self.records, self.inventory)
        self.timeline = TimelineService(self.repo, self.records, self.dispense)
        self.advice = AdviceService(self.repo, self.access, self.records)

    def save(self) -> None:
        self.repo.save()

    def restart(self) -> "PharmacyBackend":
        """模拟重启：从快照重建，待联系顾客等开放状态不丢失。"""
        backend = PharmacyBackend(self.repo.snapshot_path)
        backend.coordinator.scan_all()  # 补齐扫描类任务
        return backend

    def cross_store_consult(
        self, customer_id: str, pharmacist_id: str, store_id: str, at: str | None = None
    ) -> dict:
        """跨店咨询标准入口：先找当前有效计划与最近安全结论，再开放服务。

        需要该顾客 cross_store 授权有效、药师资质有效；返回内容只含续接所需：
        有效计划、当前用药版本、最近安全结论与归属门店。
        """
        moment_iso = timeutil.format_value(timeutil.parse(at) if at else timeutil.now())
        ctx = self.access.check_health(
            customer_id, pharmacist_id, Scope.CROSS_STORE, "cross_store_consult", moment_iso
        )
        self.access.assert_practicing_pharmacist(ctx["staff"], moment_iso)
        enrollment = ctx["enrollment"]
        if enrollment is None:
            raise AuthorizationError("顾客当前没有有效服务计划")
        return {
            "enrollment_id": enrollment["id"],
            "plan_id": enrollment["plan_id"],
            "home_store_id": enrollment["home_store_id"],
            "responsible_pharmacist_id": enrollment.get("responsible_pharmacist_id", ""),
            "consult_store_id": store_id,
            "consents": self.access.effective_consents(enrollment["id"], moment_iso),
            "medication_plan": self.records.effective_plan(customer_id, moment_iso),
            "latest_safety_conclusion": self.records.latest_safety_conclusion(customer_id, moment_iso),
            "pharmacist_id": pharmacist_id,
            "credential_no": ctx["staff"]["credential_no"],
            "at": moment_iso,
        }
