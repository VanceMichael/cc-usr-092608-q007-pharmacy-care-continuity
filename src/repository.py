"""仓储：内存集合 + JSON 快照持久化。

所有领域行以字典保存；写入时整体快照到磁盘（临时文件原子替换），
系统重启后待联系顾客、预留、召回等全部恢复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

COLLECTIONS = (
    "staff",
    "customers",
    "stores",
    "service_plans",
    "enrollments",
    "consents",
    "drugs",
    "skus",
    "batches",
    "recalls",
    "orders",
    "plan_versions",
    "observations",
    "followups",
    "referrals",
    "adverse_events",
    "changes",
    "movements",
    "reservations",
    "transfers",
    "contacts",
    "access_logs",
    "attempts",
    "advice",
)


class Repository:
    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._data: dict[str, list[dict[str, Any]]] = {name: [] for name in COLLECTIONS}

    # ---- 基础读写 -------------------------------------------------
    def add(self, collection: str, row: dict[str, Any]) -> None:
        if collection not in self._data:
            raise KeyError(f"未知集合：{collection}")
        self._data[collection].append(row)

    def all(self, collection: str) -> list[dict[str, Any]]:
        return list(self._data[collection])

    def find(self, collection: str, **predicate: Any) -> dict[str, Any] | None:
        for row in self._data[collection]:
            if all(row.get(key) == value for key, value in predicate.items()):
                return row
        return None

    def filter(self, collection: str, **predicate: Any) -> list[dict[str, Any]]:
        return [
            row
            for row in self._data[collection]
            if all(row.get(key) == value for key, value in predicate.items())
        ]

    def update(self, collection: str, row_id: str, values: dict[str, Any], key: str = "id") -> None:
        row = self.find(collection, **{key: row_id})
        if row is None:
            raise KeyError(f"{collection} 中不存在 {row_id}")
        row.update(values)

    # ---- 持久化 ---------------------------------------------------
    def save(self) -> None:
        if self.snapshot_path is None:
            return
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.snapshot_path)

    def load(self) -> bool:
        """从快照恢复；无文件时返回 False。"""
        if self.snapshot_path is None or not self.snapshot_path.exists():
            return False
        raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        for name in COLLECTIONS:
            self._data[name] = list(raw.get(name, []))
        return True

    def reset(self, collections: Iterable[str] | None = None) -> None:
        targets = list(collections) if collections else COLLECTIONS
        for name in targets:
            self._data[name] = []
