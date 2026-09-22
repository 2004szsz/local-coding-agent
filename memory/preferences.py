# -*- coding: utf-8 -*-
"""
用户偏好记忆：跨会话、结构化、与 RAG / history.db 分离。

存储默认：data/memory/preferences.json
生命周期：用户手动添加，或对话摘要写入（source=conversation_summary）。
读取：compose_system_prompt 注入 enabled 条目；Agent 不得直接写盘，须走工具 + 闸门。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_USER = "user"
SOURCE_SUMMARY = "conversation_summary"
VALID_SOURCES = frozenset({SOURCE_USER, SOURCE_SUMMARY})

DEFAULT_MAX_ITEMS = 500
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


class PreferenceError(ValueError):
    """偏好校验 / 容量错误（给 API 与工具友好文案）。"""


class PreferenceStore:
    """JSON 单文件偏好库；线程安全。"""

    kind = "preferences"

    def __init__(
        self,
        path: str | Path,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.is_file():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._items = []
            return
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self._items = []
            return
        cleaned: List[Dict[str, Any]] = []
        for entry in items:
            norm = self._normalize_item(entry, require_id=True)
            if norm is not None:
                cleaned.append(norm)
        self._items = cleaned

    def _dump(self) -> None:
        payload = {"version": 1, "items": self._items}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise PreferenceError(
                f"偏好库超过容量上限（{self.max_bytes} 字节），请删除部分条目后再试"
            )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _normalize_item(entry: Any, *, require_id: bool) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        content = str(entry.get("content") or "").strip()
        if not content:
            return None
        source = str(entry.get("source") or SOURCE_USER).strip()
        if source not in VALID_SOURCES:
            source = SOURCE_USER
        item_id = str(entry.get("id") or "").strip()
        if require_id and not item_id:
            return None
        if not item_id:
            item_id = uuid.uuid4().hex
        now = time.time()
        try:
            created = float(entry.get("created_at") or now)
        except (TypeError, ValueError):
            created = now
        try:
            updated = float(entry.get("updated_at") or created)
        except (TypeError, ValueError):
            updated = created
        enabled = entry.get("enabled", True)
        return {
            "id": item_id,
            "content": content,
            "source": source,
            "enabled": bool(enabled),
            "created_at": created,
            "updated_at": updated,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_items(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(x) for x in self._items]
        if enabled_only:
            items = [x for x in items if x.get("enabled")]
        return items

    def get(self, item_id: str) -> Dict[str, Any]:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    return dict(item)
        raise KeyError(item_id)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            count = len(self._items)
            enabled = sum(1 for x in self._items if x.get("enabled"))
        size = self.path.stat().st_size if self.path.is_file() else 0
        return {
            "path": str(self.path),
            "count": count,
            "enabled_count": enabled,
            "bytes": size,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
        }

    def prompt_section(self) -> str:
        """拼进系统提示词的一节；无启用条目时返回空串。"""
        items = self.list_items(enabled_only=True)
        if not items:
            return ""
        lines = [
            "## 用户偏好记忆",
            "以下是用户跨会话偏好，请在回复与改动中遵守（与技能说明同等优先级）：",
            "",
        ]
        for item in items:
            lines.append(f"- {item['content']}")
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(
        self,
        content: str,
        *,
        source: str = SOURCE_USER,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise PreferenceError("偏好内容不能为空")
        if len(text) > 2000:
            raise PreferenceError("单条偏好不超过 2000 字符")
        src = str(source or SOURCE_USER).strip()
        if src not in VALID_SOURCES:
            raise PreferenceError(f"非法 source: {source}（可选: user, conversation_summary）")

        now = time.time()
        item = {
            "id": uuid.uuid4().hex,
            "content": text,
            "source": src,
            "enabled": bool(enabled),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            if len(self._items) >= self.max_items:
                raise PreferenceError(
                    f"偏好条数已达上限（{self.max_items}），请删除后再添加"
                )
            self._items.append(item)
            try:
                self._dump()
            except PreferenceError:
                self._items.pop()
                raise
        return dict(item)

    def update(
        self,
        item_id: str,
        *,
        content: Optional[str] = None,
        enabled: Optional[bool] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            target = None
            for item in self._items:
                if item["id"] == item_id:
                    target = item
                    break
            if target is None:
                raise KeyError(item_id)

            if content is not None:
                text = str(content).strip()
                if not text:
                    raise PreferenceError("偏好内容不能为空")
                if len(text) > 2000:
                    raise PreferenceError("单条偏好不超过 2000 字符")
                target["content"] = text
            if enabled is not None:
                target["enabled"] = bool(enabled)
            if source is not None:
                src = str(source).strip()
                if src not in VALID_SOURCES:
                    raise PreferenceError(
                        f"非法 source: {source}（可选: user, conversation_summary）"
                    )
                target["source"] = src
            target["updated_at"] = time.time()
            self._dump()
            return dict(target)

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x["id"] != item_id]
            if len(self._items) == before:
                return False
            self._dump()
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items = []
            self._dump()
            return n
