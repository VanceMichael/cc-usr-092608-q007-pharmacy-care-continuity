"""领域错误：拒绝原因需要以明确文案返回给门店与药师。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违反的基类。"""


class AuthorizationError(DomainError):
    """无授权、授权已终止或角色无权访问。"""


class CredentialError(DomainError):
    """药师资质无效（不存在、已过期、被吊销）。"""


class InventoryError(DomainError):
    """库存状态不允许该操作（未预留、批次不符等）。"""


class BatchRiskError(DomainError):
    """批次召回或质量风险，禁止出库，应急保供也不能绕过。"""


class DuplicateError(DomainError):
    """唯一性冲突（重复编号、重复预留等）。"""
