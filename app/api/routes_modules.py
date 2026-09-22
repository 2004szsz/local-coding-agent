# -*- coding: utf-8 -*-
"""模块协同 API：规则读取、链路状态、手动/自动 adapt 触发。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.modules.coordinator import ModuleCoordinator

router = APIRouter(prefix="/api/modules", tags=["modules"])
_coordinator = ModuleCoordinator()


@router.get("/rules")
def get_module_rules():
    """返回结构化规则 + Markdown 摘要，供各模块与提示词优化读取。"""
    return {"ok": True, **_coordinator.rules_payload()}


@router.get("/status")
def get_modules_status(request: Request):
    """返回 fs / rag / agent / access 四链路模块状态。"""
    return {"ok": True, **_coordinator.module_status(request.app.state)}


@router.post("/adapt/{module_id}")
async def adapt_module(module_id: str, request: Request):
    """执行指定模块的 adapt 逻辑（优化入口后端实现）。"""
    rules = _coordinator.rules_payload().get("modules") or {}
    if module_id not in rules:
        raise HTTPException(status_code=404, detail=f"未知模块: {module_id}")

    if module_id == "rag":
        result = await run_in_threadpool(_coordinator.adapt, module_id, request.app.state)
    else:
        result = _coordinator.adapt(module_id, request.app.state)

    return {"ok": bool(result.get("ok")), **result}
