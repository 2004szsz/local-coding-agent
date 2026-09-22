# -*- coding: utf-8 -*-
"""提示词模板匹配状态存储 + SSE 订阅。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.prompt_match.matcher import build_match_status


class PromptMatchStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.seq = 0
        self._status: Dict[str, Any] = build_match_status("")
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)

    async def set_status(self, status: Dict[str, Any]) -> bool:
        """写入新状态；内容变化时 bump seq 并推送。返回是否发生变化。"""
        async with self._lock:
            prev = self._status
            changed = (
                prev.get("model") != status.get("model")
                or prev.get("family") != status.get("family")
                or prev.get("status") != status.get("status")
            )
            self._status = dict(status)
            if changed:
                self.seq += 1
                await self._notify()
            return changed

    async def _notify(self) -> None:
        payload = {"seq": self.seq, "status": self.get_status()}
        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
