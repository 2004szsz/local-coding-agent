# -*- coding: utf-8 -*-
"""
IDE 上下文后台 Daemon：清单扫描、文件 watch、SSE 推送源。
不创建任何 DOM；生命周期由 FastAPI lifespan 管理。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.context.store import ContextStore
from app.context.techstack import MANIFEST_FILES, parse_tech_stack

logger = logging.getLogger(__name__)

TECH_STACK_INTERVAL = 300.0
WATCH_DEBOUNCE = 0.3


class ContextDaemon:
    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace
        self.store = ContextStore()
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()
        self._manifest_fp: Dict[str, tuple[int, int]] = {}
        self._pending_watch: Dict[str, float] = {}
        self._watch_flush_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._stopped.clear()
        await self.detect_tech_stack()
        self._tasks.append(asyncio.create_task(self._tech_stack_loop(), name="ctx-tech-stack"))
        self._tasks.append(asyncio.create_task(self._watch_loop(), name="ctx-watch"))
        logger.info("ContextDaemon started")

    async def stop(self) -> None:
        self._stopped.set()
        for task in self._tasks:
            task.cancel()
        if self._watch_flush_task:
            self._watch_flush_task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("ContextDaemon stopped")

    async def detect_tech_stack(self) -> Dict[str, Any]:
        entries: Dict[str, str] = {}
        for name in MANIFEST_FILES:
            try:
                content = self.workspace.read_text(name)
                entries[name] = content
                p = self.workspace.resolve(name)
                st = p.stat()
                self._manifest_fp[name] = (st.st_mtime_ns, st.st_size)
            except (FileNotFoundError, IsADirectoryError, OSError):
                self._manifest_fp.pop(name, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("manifest read skip %s: %s", name, exc)
        stack = parse_tech_stack(entries)
        await self.store.set_tech_stack(stack)
        return stack

    async def _tech_stack_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=TECH_STACK_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            if self._manifests_changed():
                try:
                    await self.detect_tech_stack()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tech stack refresh failed: %s", exc)

    def _manifests_changed(self) -> bool:
        for name in MANIFEST_FILES:
            try:
                p = self.workspace.resolve(name)
                st = p.stat()
                fp = (st.st_mtime_ns, st.st_size)
                if self._manifest_fp.get(name) != fp:
                    return True
            except (FileNotFoundError, OSError):
                if name in self._manifest_fp:
                    return True
        return False

    async def _watch_loop(self) -> None:
        root = Path(self.workspace.root)
        try:
            from watchfiles import awatch
        except ImportError:
            logger.info("watchfiles unavailable; context file watch disabled")
            return

        try:
            async for changes in awatch(root, stop_event=self._stopped):
                for _change, path_str in changes:
                    rel = self._relpath(path_str)
                    if not rel or rel in MANIFEST_FILES:
                        if rel in MANIFEST_FILES:
                            asyncio.create_task(self.detect_tech_stack())
                        continue
                    self._pending_watch[rel] = time.monotonic()
                    if not self._watch_flush_task or self._watch_flush_task.done():
                        self._watch_flush_task = asyncio.create_task(self._flush_watch())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("context watch loop ended: %s", exc)

    async def _flush_watch(self) -> None:
        await asyncio.sleep(WATCH_DEBOUNCE)
        paths = list(self._pending_watch.keys())
        self._pending_watch.clear()
        for rel in paths[:20]:
            await self.store.record_edit(rel, "工作区文件变更（后台 watch）")

    def _relpath(self, path_str: str) -> str:
        try:
            p = Path(path_str).resolve()
            return self.workspace.relpath(p)
        except (ValueError, OSError):
            return ""
