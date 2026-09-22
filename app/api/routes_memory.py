# -*- coding: utf-8 -*-
"""记忆状态与用户偏好 CRUD。

Agent 记忆：会话 / RAG / 偏好三者分离。
`data/.workbuddy/memory/` 为人工笔记，不在此 API、不进设置页。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agents.agent import refresh_system_prompt
from memory.preferences import PreferenceError, VALID_SOURCES

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _session_stats(request: Request) -> Dict[str, Any]:
    store = request.app.state.sessions
    stats = store.stats() if hasattr(store, "stats") else {}
    return {
        "db_path": stats.get("db_path", ""),
        "db_name": stats.get("db_name", "history.db"),
        "db_bytes": int(stats.get("db_bytes") or 0),
        "session_count": int(stats.get("session_count") or 0),
        "message_count": int(stats.get("message_count") or 0),
    }


def _rag_stats(request: Request) -> Dict[str, Any]:
    store = getattr(request.app.state, "vector_store", None)
    enabled = bool(getattr(request.app.state, "rag_enabled", False) and store is not None)
    if not enabled or store is None:
        return {
            "enabled": False,
            "chunks": 0,
            "embedder": "",
            "embedder_mode": "",
            "indexed_at": None,
            "message": "RAG 未启用或初始化失败",
        }
    status = store.status()
    return {
        "enabled": True,
        "chunks": int(status.get("chunks") or 0),
        "embedder": status.get("embedder") or "",
        "embedder_mode": status.get("embedder_mode") or "",
        "indexed_at": status.get("indexed_at"),
    }


def _preferences(request: Request):
    prefs = getattr(request.app.state, "preferences", None)
    if prefs is None:
        runtime = getattr(request.app.state, "runtime", None)
        prefs = getattr(runtime, "preferences", None) if runtime else None
    if prefs is None:
        raise HTTPException(status_code=503, detail="偏好记忆未就绪")
    return prefs


def _refresh_prompt(request: Request) -> None:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        refresh_system_prompt(runtime)


@router.get("/status")
def memory_status(request: Request):
    """设置页用的轻量聚合，不探测 LLM。"""
    pref_stats: Dict[str, Any] = {
        "count": 0,
        "enabled_count": 0,
        "bytes": 0,
        "max_items": 500,
        "max_bytes": 10485760,
    }
    try:
        pref_stats = _preferences(request).stats()
    except HTTPException:
        pass
    return {
        "sessions": _session_stats(request),
        "rag": _rag_stats(request),
        "preferences": pref_stats,
    }


class PreferenceCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    source: str = "user"
    enabled: bool = True


class PreferencePatch(BaseModel):
    content: Optional[str] = Field(None, min_length=1, max_length=2000)
    enabled: Optional[bool] = None
    source: Optional[str] = None


@router.get("/preferences")
def list_preferences(request: Request, enabled_only: bool = False):
    prefs = _preferences(request)
    return {
        "items": prefs.list_items(enabled_only=enabled_only),
        "stats": prefs.stats(),
    }


@router.post("/preferences")
def create_preference(body: PreferenceCreate, request: Request):
    prefs = _preferences(request)
    source = (body.source or "user").strip()
    if source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"非法 source: {body.source}")
    try:
        item = prefs.add(body.content, source=source, enabled=body.enabled)
    except PreferenceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _refresh_prompt(request)
    return item


@router.patch("/preferences/{item_id}")
def patch_preference(item_id: str, body: PreferencePatch, request: Request):
    prefs = _preferences(request)
    if body.content is None and body.enabled is None and body.source is None:
        raise HTTPException(status_code=400, detail="至少提供 content / enabled / source 之一")
    try:
        item = prefs.update(
            item_id,
            content=body.content,
            enabled=body.enabled,
            source=body.source,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"未找到偏好: {item_id}") from e
    except PreferenceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _refresh_prompt(request)
    return item


@router.delete("/preferences/{item_id}")
def delete_preference(item_id: str, request: Request):
    prefs = _preferences(request)
    if not prefs.delete(item_id):
        raise HTTPException(status_code=404, detail=f"未找到偏好: {item_id}")
    _refresh_prompt(request)
    return {"ok": True, "id": item_id}


@router.post("/clear")
def clear_preferences(request: Request):
    """清空用户偏好记忆（不影响 history.db / RAG）。"""
    prefs = _preferences(request)
    deleted = prefs.clear()
    _refresh_prompt(request)
    return {"ok": True, "deleted": deleted}
