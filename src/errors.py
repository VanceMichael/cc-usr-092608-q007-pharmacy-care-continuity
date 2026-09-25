"""领域异常类型。"""


class DomainError(Exception):
    """业务规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ValidationError(DomainError):
    """输入数据不合法。"""


class AccessDeniedError(DomainError):
    """当前角色无权执行该操作。"""


class ConsentError(AccessDeniedError):
    """缺少有效的顾客授权。"""


class PlanStateError(DomainError):
    """服务计划状态不允许该操作。"""
