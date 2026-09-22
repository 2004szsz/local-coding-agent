# -*- coding: utf-8 -*-
"""
提示词模板后台匹配 Daemon。

职责：监听 Agent 当前模型，自动匹配模板族，通过 SSE 通知前端。
触发：启动立即匹配 / 模型激活或配置变更 / 定时轮询兜底。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from app.modules.rules import load_module_rules
from app.prompt_match.matcher import build_match_status
from app.prompt_match.store import PromptMatchStore

logger = logging.getLogger(__name__)


class PromptMatchDaemon:
    def __init__(self, app_state: Any) -> None:
        self._app = app_state
        self.store = PromptMatchStore()
        self._task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._last_fingerprint: str = ""

    def _interval(self) -> float:
        cfg = getattr(self._app, "cfg", {}) or {}
        mod_cfg = cfg.get("modules") or {}
        if mod_cfg.get("prompt_match_interval_seconds") is not None:
            try:
                return float(mod_cfg["prompt_match_interval_seconds"])
            except (TypeError, ValueError):
                pass
        rules = load_module_rules()
        pm_cfg = (rules.get("modules") or {}).get("prompt_match") or {}
        try:
            return float(pm_cfg.get("auto_adapt_interval_seconds", 15))
        except (TypeError, ValueError):
            return 15.0

    def _auto_enabled(self) -> bool:
        cfg = getattr(self._app, "cfg", {}) or {}
        mod_cfg = cfg.get("modules") or {}
        if mod_cfg.get("auto_prompt_match") is False:
            return False
        rules = load_module_rules()
        pm_cfg = (rules.get("modules") or {}).get("prompt_match") or {}
        return bool(pm_cfg.get("auto_adapt", True))

    def _read_runtime_model(self) -> tuple[str, str]:
        cfg = getattr(self._app, "cfg", {}) or {}
        llm = cfg.get("llm") or {}
        model = str(llm.get("model") or "").strip()
        provider = str(llm.get("provider") or "").strip()
        return model, provider

    def _fingerprint(self) -> str:
        model, provider = self._read_runtime_model()
        return f"{provider}:{model}"

    async def refresh(self, *, source: str = "auto") -> Dict[str, Any]:
        model, provider = self._read_runtime_model()
        status = build_match_status(model, provider=provider, source=source)
        changed = await self.store.set_status(status)
        self._last_fingerprint = self._fingerprint()
        if changed:
            logger.info(
                "[PromptMatch] %s → family=%s model=%s",
                status.get("matched_text"),
                status.get("family"),
                model or "(empty)",
            )
        return status

    def schedule_refresh(self, *, source: str = "model_change") -> None:
        """模型切换等事件触发的即时刷新（合并并发请求）。"""
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self.refresh(source=source),
            name="prompt-match-refresh",
        )

    async def start(self) -> None:
        if not self._auto_enabled():
            logger.info("PromptMatchDaemon disabled in config/module-rules")
            await self.refresh(source="disabled")
            return
        self._stopped.clear()
        await self.refresh(source="startup")
        self._task = asyncio.create_task(self._loop(), name="prompt-match")
        logger.info("PromptMatchDaemon started (interval=%.0fs)", self._interval())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        logger.info("PromptMatchDaemon stopped")

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                fp = self._fingerprint()
                if fp != self._last_fingerprint:
                    await self.refresh(source="poll")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[PromptMatch] poll refresh failed: %s", exc)

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval())
                break
            except asyncio.TimeoutError:
                pass
