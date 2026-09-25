"""时间工具：业务时间与记录时间分离。

- 业务时间（occurred_at）：事件真实发生的时间，断网补录时显式提供。
- 记录时间（recorded_at）：系统收到事件的时间，总是由时钟给出。
晚到事件按业务时间插入时间线，只能影响业务时间之后生成的建议。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO = "%Y-%m-%dT%H:%M:%S%z"


def now() -> datetime:
    """当前系统时间；测试可通过 set_now 固定（模拟断网恢复等场景）。"""
    return _override if _override is not None else datetime.now(timezone.utc)


_override: datetime | None = None


def set_now(value: str | datetime | None) -> None:
    """固定系统时钟；传 None 恢复真实时钟。"""
    global _override
    if value is None:
        _override = None
    elif isinstance(value, datetime):
        _override = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        _override = parse(value)


def parse(value: str) -> datetime:
    """解析 ISO-8601 时间，裸时间视为 UTC。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_value(value: datetime) -> str:
    """ISO-8601 秒精度字符串，UTC 偏移带冒号（如 +00:00）。"""
    return value.isoformat(timespec="seconds")


def hours(value: float) -> timedelta:
    return timedelta(hours=value)


def days(value: float) -> timedelta:
    return timedelta(days=value)
