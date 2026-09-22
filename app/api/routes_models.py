# -*- coding: utf-8 -*-
"""
模型供应商配置 API：CRUD、激活、探活、与 Agent / RAG 热衔接。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from agents.runtime_reload import reload_llm, runtime_model_summary
from app.config import load_config
from app.model_registry import (
    AGENT_SUPPORTED_FORMATS,
    ModelEntry,
    ProviderEntry,
    add_provider_from_preset,
    apply_registry_to_config,
    integration_summary,
    load_registry,
    save_registry,
    test_model_chat,
    test_provider_connection,
    _new_id,
    _default_capabilities,
)

router = APIRouter(prefix="/api/models", tags=["models"])


class ProviderCreate(BaseModel):
    vendor: str = "custom"
    name: Optional[str] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    api_format: Optional[str] = None
    api_key: Optional[str] = None
    notes: Optional[str] = None


class ModelCreate(BaseModel):
    name: str
    role: str = "chat"


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    role: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    input_types: Optional[List[str]] = None
    capabilities: Optional[Dict[str, bool]] = None
    reasoning_levels: Optional[List[str]] = None
    reasoning_param_map: Optional[Dict[str, Any]] = None
    default_reasoning_level: Optional[str] = None
    smart_config: Optional[bool] = None


class ReasoningUpdate(BaseModel):
    level: str


class ActivateRequest(BaseModel):
    provider_id: str
    model_id: str


class RagBindingUpdate(BaseModel):
    use_active_provider: bool = True
    provider_id: Optional[str] = None
    model_id: Optional[str] = None


def _get_provider(state, provider_id: str) -> ProviderEntry:
    provider = state.find_provider(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return provider


def _notify_prompt_match(request: Request) -> None:
    daemon = getattr(request.app.state, "prompt_match_daemon", None)
    if daemon is not None:
        daemon.schedule_refresh(source="model_change")


def _apply_and_reload(request: Request) -> Dict[str, Any]:
    """保存注册表 → 合并配置 → 热切换 LLM → 返回摘要。"""
    reg = load_registry()
    cfg = apply_registry_to_config(load_config(), reg)
    reload_llm(request.app.state.runtime, cfg)
    request.app.state.cfg = cfg
    request.app.state.llm = request.app.state.runtime.llm
    _notify_prompt_match(request)
    return {
        "ok": True,
        "integration": integration_summary(reg, cfg),
        "runtime": runtime_model_summary(request.app.state.runtime, cfg),
    }


@router.get("")
async def get_models():
    reg = load_registry()
    cfg = apply_registry_to_config(load_config(), reg)
    return {
        **reg.to_public_dict(),
        "integration": integration_summary(reg, cfg),
    }


@router.post("/providers")
async def create_provider(body: ProviderCreate, request: Request):
    reg = load_registry()
    entry = add_provider_from_preset(reg, body.vendor.strip(), body.name)
    if not reg.active_provider_id:
        chat = next((m for m in entry.models if m.role == "chat"), None)
        if chat:
            reg.active_provider_id = entry.id
            reg.active_model_id = chat.id
    save_registry(reg)
    return _apply_and_reload(request)


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderUpdate, request: Request):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    if body.name is not None:
        provider.name = body.name.strip()
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.base_url is not None:
        provider.base_url = body.base_url.strip()
    if body.api_format is not None:
        provider.api_format = body.api_format.strip()
    if body.api_key is not None:
        provider.api_key = body.api_key.strip()
    if body.notes is not None:
        provider.notes = body.notes
    save_registry(reg)
    return _apply_and_reload(request)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request):
    reg = load_registry()
    before = len(reg.providers)
    reg.providers = [p for p in reg.providers if p.id != provider_id]
    if len(reg.providers) == before:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if reg.active_provider_id == provider_id:
        reg.active_provider_id = None
        reg.active_model_id = None
    if reg.rag_provider_id == provider_id:
        reg.rag_provider_id = None
        reg.rag_model_id = None
    save_registry(reg)
    return _apply_and_reload(request)


@router.post("/providers/{provider_id}/models")
async def add_model(provider_id: str, body: ModelCreate, request: Request):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="模型名称不能为空")
    role = body.role if body.role in ("chat", "embedding") else "chat"
    caps = _default_capabilities()
    if role == "embedding":
        caps = _default_capabilities(tools=False, system_message=False)
    model = ModelEntry(id=_new_id(), name=name, role=role, capabilities=caps)
    provider.models.append(model)
    save_registry(reg)
    return _apply_and_reload(request)


@router.put("/providers/{provider_id}/models/{model_id}")
async def update_model(provider_id: str, model_id: str, body: ModelUpdate,
                       request: Request):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    model = provider.find_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(model, field, value)
    save_registry(reg)
    return _apply_and_reload(request)


@router.delete("/providers/{provider_id}/models/{model_id}")
async def delete_model(provider_id: str, model_id: str, request: Request):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    before = len(provider.models)
    provider.models = [m for m in provider.models if m.id != model_id]
    if len(provider.models) == before:
        raise HTTPException(status_code=404, detail="模型不存在")
    if reg.active_model_id == model_id:
        reg.active_model_id = None
    if reg.rag_model_id == model_id:
        reg.rag_model_id = None
    save_registry(reg)
    return _apply_and_reload(request)


@router.post("/activate")
async def activate_model(body: ActivateRequest, request: Request):
    reg = load_registry()
    provider = _get_provider(reg, body.provider_id)
    model = provider.find_model(body.model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not provider.enabled or not model.enabled:
        raise HTTPException(status_code=400, detail="供应商或模型未启用")
    if model.role != "chat":
        raise HTTPException(status_code=400, detail="仅对话模型可作为 Agent 主模型")
    if provider.api_format not in AGENT_SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="当前 API 格式不支持 Agent 循环，请使用 OpenAI Chat Completions 兼容端点",
        )
    reg.active_provider_id = body.provider_id
    reg.active_model_id = body.model_id
    from app.reasoning import clamp_level, effective_reasoning_levels
    allowed = effective_reasoning_levels(model.reasoning_levels, provider.vendor)
    reg.active_reasoning_level = clamp_level(
        model.default_reasoning_level or "medium",
        allowed,
    )
    save_registry(reg)
    return _apply_and_reload(request)


@router.put("/reasoning")
async def update_reasoning_level(body: ReasoningUpdate, request: Request):
    from app.reasoning import LEVEL_ORDER, clamp_level, effective_reasoning_levels

    reg = load_registry()
    level = (body.level or "").strip()
    if level not in LEVEL_ORDER:
        raise HTTPException(status_code=400, detail="无效的思考强度档位")
    provider = reg.find_provider(reg.active_provider_id or "")
    model = provider.find_model(reg.active_model_id or "") if provider else None
    allowed = (
        effective_reasoning_levels(model.reasoning_levels, provider.vendor)
        if model and provider else LEVEL_ORDER
    )
    reg.active_reasoning_level = clamp_level(level, allowed)
    save_registry(reg)
    return _apply_and_reload(request)


@router.put("/rag")
async def update_rag_binding(body: RagBindingUpdate, request: Request):
    reg = load_registry()
    reg.rag_use_active_provider = body.use_active_provider
    if body.use_active_provider:
        reg.rag_provider_id = None
        reg.rag_model_id = None
    else:
        if not body.provider_id or not body.model_id:
            raise HTTPException(status_code=400, detail="请指定 RAG 嵌入模型")
        provider = _get_provider(reg, body.provider_id)
        model = provider.find_model(body.model_id)
        if not model:
            raise HTTPException(status_code=404, detail="嵌入模型不存在")
        reg.rag_provider_id = body.provider_id
        reg.rag_model_id = body.model_id
    save_registry(reg)
    return _apply_and_reload(request)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    return await run_in_threadpool(test_provider_connection, provider)


@router.post("/providers/{provider_id}/models/{model_id}/test")
async def test_model(provider_id: str, model_id: str):
    reg = load_registry()
    provider = _get_provider(reg, provider_id)
    model = provider.find_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    return await run_in_threadpool(test_model_chat, provider, model)
