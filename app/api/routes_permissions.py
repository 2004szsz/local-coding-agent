# -*- coding: utf-8 -*-
"""
审批通道：权限请求的登记、等待与人工决议。

这是本地访问能力「能否启用」的开关（docs/local-system-access-plan.md 模块五）：
Zcode 作为桌面应用靠权限提示 UI 兜底；本项目是 127.0.0.1 上的 Web 服务，
扩张本地能力时，SSE 把 `permission_request` 推给前端，人点了「允许/拒绝」
之后由本模块把决议喂回 `state_loop` 的 authorize 暂停点。

**失败关闭**：60 秒内没人答复（或没有审批处理器）→ 一律视为拒绝。
不把「等不到人」实现成「自动放行」——这是整套权限体系的地基。

流程：
    主循环 authorize 暂停 → emit permission_request(SSE) → 前端确认卡片
        → POST /api/permissions/{id} {"decision": "allow"|"deny"}
        → hub.decide() 决议 future → confirm_handler 返回 → 主循环继续/拒绝
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

#: 默认等待答复的秒数。配置键 permissions.timeout_seconds。
DEFAULT_TIMEOUT_SECONDS = 60.0


class DecisionBody(BaseModel):
    decision: str          # "allow" | "deny"


class PermissionHub:
    """
    未决权限请求的登记处。

    单事件循环内使用：`ask` 由 state_loop 的 confirm_handler 调用（await 等待），
    `decide` 由 HTTP 端点调用（set_result）。所有 future 都在同一事件循环，
    无跨线程竞争。

    审计：每一次决议（允许/拒绝/超时）追加一行 JSONL 到 `audit_log`。
    审计写在决议点而非请求点——「请求了但没人答复」与「答复了」必须分开记账，
    这也是切片 5 验收「审计日志条数正确」的落点。审计写失败只计数、不阻断决议。
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 audit_log: Optional[Path] = None):
        self.timeout_seconds = timeout_seconds
        self.audit_log = audit_log
        self._waiters: Dict[str, asyncio.Future] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._audit_failures = 0

    # ---------------- confirm_handler 侧 ----------------
    def register(self, kind: Any, calls: Tuple[Any, ...], request_id: str) -> str:
        """
        同步登记一个未决请求。必须在发出 SSE `permission_request` **之前**调用，
        否则前端（或测试）一收到事件就 POST，会打到空 waiters。
        """
        rid = str(request_id or "") or f"perm_{uuid.uuid4().hex[:8]}"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters[rid] = future
        self._meta[rid] = {
            "request_id": rid,
            "kind": str(kind),
            "calls": [getattr(call, "one_line", lambda: str(call))() for call in calls],
            "created_at": time.time(),
        }
        return rid

    async def wait(self, request_id: str,
                   timeout_seconds: Optional[float] = None) -> bool:
        """等待已登记请求的决议。超时/取消 = 拒绝。"""
        rid = str(request_id or "")
        future = self._waiters.get(rid)
        if future is None:
            return False
        try:
            granted = bool(await asyncio.wait_for(
                future, timeout=self.timeout_seconds if timeout_seconds is None
                else timeout_seconds))
            self._audit(rid, "allow" if granted else "deny")
            return granted
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._audit(rid, "timeout")
            return False
        finally:
            self._waiters.pop(rid, None)
            self._meta.pop(rid, None)

    async def ask(self, kind: Any, calls: Tuple[Any, ...],
                  request_id: str,
                  timeout_seconds: Optional[float] = None) -> bool:
        """
        state_loop 的 ConfirmHandler 实现：登记请求并等待人工决议。

        :returns: True=放行；超时/异常=拒绝（失败关闭）
        """
        rid = self.register(kind, calls, request_id)
        return await self.wait(rid, timeout_seconds=timeout_seconds)

    # ---------------- HTTP 侧 ----------------
    def decide(self, request_id: str, allow: bool) -> bool:
        """
        对未决请求给出决议。请求已超时/不存在时返回 False（HTTP 层转 404）。

        `set_result` 必须投递到 Future 所属的事件循环：生产环境里 POST 与
        ask 同属一条 uvicorn 循环；TestClient 并发请求可能从另一线程进来。
        """
        future = self._waiters.get(request_id)
        if os.environ.get("PERM_DIAG"):
            print(f"[perm-diag] decide {request_id} hub={id(self)} "
                  f"waiters={list(self._waiters)} future_done={future.done() if future else None}",
                  flush=True)
        if future is None or future.done():
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        owner = future.get_loop()
        if running is owner:
            future.set_result(bool(allow))
        else:
            owner.call_soon_threadsafe(future.set_result, bool(allow))
        return True

    def pending_view(self) -> list:
        """未决请求的只读视图（前端轮询/刷新用）。"""
        return list(self._meta.values())

    def is_registered(self, request_id: str) -> bool:
        return request_id in self._waiters

    @property
    def pending(self) -> int:
        return len(self._waiters)

    # ---------------- 审计 ----------------
    def _audit(self, request_id: str, decision: str) -> None:
        if self.audit_log is None:
            return
        meta = self._meta.get(request_id) or {}
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "request_id": request_id,
            "kind": meta.get("kind", ""),
            "calls": meta.get("calls", []),
            "decision": decision,
        }
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._audit_failures = 0
        except OSError:
            self._audit_failures += 1
            if self._audit_failures <= 1:
                print(f"[权限] 警告: 审批审计写入失败: {self.audit_log}")


class ConfirmAdapter:
    """
    LoopDeps.confirm_handler 的实现。做成对象而不是闭包函数，
    是为了让运行时能稳定取到 `.hub`，在发 SSE 之前同步登记请求。
    """

    def __init__(self, hub: PermissionHub):
        self.hub = hub

    async def __call__(self, kind, calls, request_id: str) -> bool:
        return await self.hub.ask(kind, calls, request_id)


def make_confirm_handler(hub: PermissionHub):
    """构造注入 LoopDeps 的 ConfirmHandler（签名见 agents/state_loop/runtime）。"""
    return ConfirmAdapter(hub)


# ======================================================================
# HTTP 路由
# ======================================================================
def _hub(request: Request) -> PermissionHub:
    hub = getattr(request.app.state, "permissions", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="审批通道未启用")
    return hub


@router.post("/{request_id}")
async def decide_permission(request_id: str, body: DecisionBody, request: Request):
    decision = (body.decision or "").strip().lower()
    if decision not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="decision 必须是 allow 或 deny")
    hub = _hub(request)
    if not hub.decide(request_id, decision == "allow"):
        raise HTTPException(status_code=404, detail="权限请求不存在或已超时")
    return {"ok": True, "request_id": request_id, "decision": decision}


@router.get("/pending")
async def pending_permissions(request: Request):
    hub = _hub(request)
    return {"pending": hub.pending_view()}