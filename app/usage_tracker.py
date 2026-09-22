# -*- coding: utf-8 -*-
"""
Agent 使用统计：对话轮次、工具调用、Token 与思考强度分布。

持久化到 data/usage_stats.json；不含对话正文，仅聚合指标。
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import PROJECT_ROOT

USAGE_FILE = PROJECT_ROOT / "data" / "usage_stats.json"
USAGE_VERSION = 1

_lock = threading.Lock()


def _empty_store() -> Dict[str, Any]:
    return {
        "version": USAGE_VERSION,
        "totals": {
            "chat_turns": 0,
            "messages": 0,
            "tool_calls": 0,
            "llm_calls": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "latency_ms": 0,
        },
        "by_reasoning_level": {},
        "daily": {},
    }


def _today_key() -> str:
    return date.today().isoformat()


def merge_usage_dict(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """累加 usage 数值字段。"""
    out = dict(base)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        if key in extra and extra[key] is not None:
            out[key] = int(out.get(key, 0)) + int(extra[key])
    return out


class UsageTracker:
    """线程安全的本地使用统计。"""

    def __init__(self, path: Path = USAGE_FILE) -> None:
        self.path = path
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return _empty_store()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty_store()
        if not isinstance(data, dict):
            return _empty_store()
        for key in ("totals", "by_reasoning_level", "daily"):
            data.setdefault(key, _empty_store()[key])
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_chat_turn(
        self,
        *,
        reasoning_level: str = "medium",
        framework: str = "",
        usage: Optional[Dict[str, Any]] = None,
        latency_ms: int = 0,
        tool_calls: int = 0,
        llm_calls: int = 0,
    ) -> None:
        """记录一轮用户对话的聚合指标（不含消息正文）。"""
        usage = usage or {}
        level = reasoning_level or "medium"
        total_tokens = int(usage.get("total_tokens") or 0)
        reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
        latency = max(0, int(latency_ms or 0))
        tools = max(0, int(tool_calls or 0))
        llm = max(0, int(llm_calls or 0))

        with _lock:
            totals = self._data["totals"]
            totals["chat_turns"] = int(totals.get("chat_turns", 0)) + 1
            totals["messages"] = int(totals.get("messages", 0)) + 2
            totals["tool_calls"] = int(totals.get("tool_calls", 0)) + tools
            totals["llm_calls"] = int(totals.get("llm_calls", 0)) + llm
            totals["total_tokens"] = int(totals.get("total_tokens", 0)) + total_tokens
            totals["reasoning_tokens"] = int(totals.get("reasoning_tokens", 0)) + reasoning_tokens
            totals["latency_ms"] = int(totals.get("latency_ms", 0)) + latency

            by_level = self._data["by_reasoning_level"].setdefault(level, {})
            by_level["turns"] = int(by_level.get("turns", 0)) + 1
            by_level["total_tokens"] = int(by_level.get("total_tokens", 0)) + total_tokens
            by_level["reasoning_tokens"] = int(by_level.get("reasoning_tokens", 0)) + reasoning_tokens
            by_level["latency_ms"] = int(by_level.get("latency_ms", 0)) + latency
            by_level["llm_calls"] = int(by_level.get("llm_calls", 0)) + llm

            day = self._data["daily"].setdefault(_today_key(), {
                "chat_turns": 0,
                "tool_calls": 0,
                "total_tokens": 0,
            })
            day["chat_turns"] = int(day.get("chat_turns", 0)) + 1
            day["tool_calls"] = int(day.get("tool_calls", 0)) + tools
            day["total_tokens"] = int(day.get("total_tokens", 0)) + total_tokens

            self._save()

    def summary(self, days: int = 7, session_count: Optional[int] = None) -> Dict[str, Any]:
        """供 /api/stats 与设置页使用的摘要。"""
        with _lock:
            data = copy.deepcopy(self._data)
        totals = data.get("totals") or {}
        daily = data.get("daily") or {}
        by_level = data.get("by_reasoning_level") or {}

        trend: List[Dict[str, Any]] = []
        today = date.today()
        for offset in range(days - 1, -1, -1):
            key = (today - timedelta(days=offset)).isoformat()
            bucket = daily.get(key) or {}
            trend.append({
                "date": key,
                "chat_turns": int(bucket.get("chat_turns", 0)),
                "tool_calls": int(bucket.get("tool_calls", 0)),
                "total_tokens": int(bucket.get("total_tokens", 0)),
            })

        hours = round(int(totals.get("latency_ms", 0)) / 3_600_000, 1)

        level_rows = []
        for level, bucket in sorted(by_level.items(), key=lambda x: x[0]):
            turns = int(bucket.get("turns", 0))
            if turns <= 0:
                continue
            avg_latency = int(bucket.get("latency_ms", 0) // max(1, turns))
            level_rows.append({
                "level": level,
                "turns": turns,
                "total_tokens": int(bucket.get("total_tokens", 0)),
                "reasoning_tokens": int(bucket.get("reasoning_tokens", 0)),
                "avg_latency_ms": avg_latency,
            })

        return {
            "totals": {
                "tasks": session_count if session_count is not None else totals.get("chat_turns", 0),
                "messages": int(totals.get("messages", 0)),
                "tool_calls": int(totals.get("tool_calls", 0)),
                "llm_calls": int(totals.get("llm_calls", 0)),
                "total_tokens": int(totals.get("total_tokens", 0)),
                "reasoning_tokens": int(totals.get("reasoning_tokens", 0)),
                "hours": hours,
            },
            "by_reasoning_level": level_rows,
            "trend": trend,
        }


_tracker: Optional[UsageTracker] = None


def get_usage_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
