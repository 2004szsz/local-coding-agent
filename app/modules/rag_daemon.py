# -*- coding: utf-8 -*-
"""
RAG 后台自动索引 Daemon。

当知识库处于「待索引」状态时自动执行增量索引，无需用户点击。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from app.modules.rules import load_module_rules

logger = logging.getLogger(__name__)


class RagAutoIndexDaemon:
    def __init__(self, app_state: Any) -> None:
        self._app = app_state
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self.indexing = False
        self._last_run = 0.0
        self._last_stats: Dict[str, Any] = {}
        self._consecutive_failures = 0

    @property
    def last_stats(self) -> Dict[str, Any]:
        return dict(self._last_stats)

    def _interval(self) -> float:
        rules = load_module_rules()
        rag_cfg = (rules.get("modules") or {}).get("rag") or {}
        try:
            return float(rag_cfg.get("auto_adapt_interval_seconds", 90))
        except (TypeError, ValueError):
            return 90.0

    def _auto_enabled(self) -> bool:
        cfg = getattr(self._app, "cfg", {}) or {}
        mod_cfg = cfg.get("modules") or {}
        if mod_cfg.get("auto_rag_index") is False:
            return False
        rules = load_module_rules()
        rag_cfg = (rules.get("modules") or {}).get("rag") or {}
        return bool(rag_cfg.get("auto_adapt", True))

    def needs_index(self) -> bool:
        if not getattr(self._app, "rag_enabled", False):
            return False
        if getattr(self._app, "indexer", None) is None:
            return False
        store = getattr(self._app, "vector_store", None)
        if store is None:
            return False
        # 空库要建索引；已有切片也要跑增量（工作区切换 / 文件变更后与 Agent rag_search 对齐）。
        return True

    def run_index_now(self) -> Dict[str, Any]:
        """同步执行增量索引（在线程池中调用）。"""
        indexer = getattr(self._app, "indexer", None)
        if indexer is None:
            return {"ok": False, "message": "索引器未就绪"}
        self.indexing = True
        try:
            stats = indexer.index()
            self._last_stats = stats
            self._last_run = time.monotonic()
            self._consecutive_failures = 0
            logger.info(
                "[RAG Daemon] 自动索引完成: scanned=%s added=%s chunks=%s",
                stats.get("scanned"), stats.get("added"), stats.get("chunks_total"),
            )
            return stats
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            logger.warning("[RAG Daemon] 索引失败: %s", exc)
            return {"ok": False, "message": str(exc)}
        finally:
            self.indexing = False

    async def start(self) -> None:
        if not self._auto_enabled():
            logger.info("RAG auto-index disabled in module-rules.yaml")
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="rag-auto-index")
        logger.info("RagAutoIndexDaemon started (interval=%.0fs)", self._interval())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("RagAutoIndexDaemon stopped")

    async def _loop(self) -> None:
        # 启动后短暂延迟，等待 runtime 完全就绪
        await asyncio.sleep(3.0)
        while not self._stopped.is_set():
            try:
                if self.needs_index() and not self.indexing:
                    await asyncio.to_thread(self.run_index_now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[RAG Daemon] loop error: %s", exc)

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval())
                break
            except asyncio.TimeoutError:
                pass
