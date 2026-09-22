# -*- coding: utf-8 -*-
"""
运行时项目与能力 API（Zcode 式选项目，无需改 config.yaml / 重启）。

POST /api/runtime/projects          添加本机目录为项目
POST /api/runtime/projects/{id}/activate  切换当前工作区并热重载工具
PUT  /api/runtime/capabilities      开关系统数据 / 系统动作
GET  /api/runtime/browse            文件夹选择器
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agents.runtime_reload import reconfigure_runtime, runtime_status
from app.config import load_config
from app.local_runtime import (
    DEFAULT_CAPABILITIES,
    LocalRuntimeState,
    ProjectEntry,
    apply_runtime_to_config,
    common_roots,
    load_runtime_state,
    save_runtime_state,
)

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


class ProjectCreate(BaseModel):
    path: str
    name: Optional[str] = None
    write: bool = True


class CapabilitiesUpdate(BaseModel):
    local_access: Optional[bool] = None
    system: Optional[bool] = None
    allow_actions: Optional[List[str]] = None


def _reload_app(request: Request, state: LocalRuntimeState) -> Dict[str, Any]:
    """保存状态 → 合并配置 → 热重载 Runtime → 同步 app.state。"""
    save_runtime_state(state)
    cfg = load_config()
    apply_runtime_to_config(cfg, state)
    confirm_handler = getattr(request.app.state, "_confirm_handler", None)
    runtime = request.app.state.runtime
    reconfigure_runtime(runtime, cfg, confirm_handler=confirm_handler)
    request.app.state.cfg = cfg
    request.app.state.workspace = runtime.workspace
    request.app.state.tools = runtime.tools
    request.app.state.broker = runtime.broker
    request.app.state.runner = runtime.runner
    request.app.state.loop_deps = runtime.loop_deps
    request.app.state.skills = runtime.skill_names
    request.app.state.indexer = runtime.indexer
    request.app.state.vector_store = runtime.vector_store
    request.app.state.rag_enabled = runtime.rag_enabled
    return runtime_status(runtime, cfg, state)


@router.get("")
async def get_runtime(request: Request):
    state = load_runtime_state()
    cfg = request.app.state.cfg
    return runtime_status(request.app.state.runtime, cfg, state)


@router.get("/roots")
async def list_common_roots():
    return {"roots": common_roots()}


@router.get("/browse")
async def browse_dirs(path: str = ""):
    """列出可选目录（文件夹选择器）。拒绝系统敏感路径。"""
    text = (path or "").strip()
    if not text:
        return {"path": "", "entries": common_roots(), "parent": None}

    target = Path(text)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="path 必须是绝对路径")
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="不是目录")

    from tools.fs_access import DENIED_PREFIXES
    cmp = os.path.normcase(str(resolved)).rstrip("\\/")
    for prefix in DENIED_PREFIXES:
        norm = os.path.normcase(str(prefix)).rstrip("\\/")
        if cmp == norm or cmp.startswith(norm + os.sep):
            raise HTTPException(status_code=403, detail="该路径在拒绝清单内")

    entries: List[Dict[str, str]] = []
    try:
        children = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限浏览该目录")
    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith(".") and child.name not in (".", ".."):
            continue
        entries.append({"name": child.name, "path": str(child.resolve())})

    parent = str(resolved.parent.resolve()) if resolved.parent != resolved else None
    return {"path": str(resolved), "entries": entries, "parent": parent}


@router.post("/projects")
async def add_project(body: ProjectCreate, request: Request):
    state = load_runtime_state()
    try:
        project = ProjectEntry.from_dict({
            "name": body.name,
            "path": body.path,
            "write": body.write,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not Path(project.path).is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {project.path}")

    for existing in state.projects:
        if os.path.normcase(existing.path) == os.path.normcase(project.path):
            raise HTTPException(status_code=409, detail="该项目目录已添加")

    state.projects.append(project)
    state.active_project_id = project.id
    if not state.capabilities:
        state.capabilities = dict(DEFAULT_CAPABILITIES)
    return await run_in_threadpool(_reload_app, request, state)


@router.post("/projects/{project_id}/activate")
async def activate_project(project_id: str, request: Request):
    state = load_runtime_state()
    if state.find(project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    state.active_project_id = project_id
    return await run_in_threadpool(_reload_app, request, state)


@router.delete("/projects/{project_id}")
async def remove_project(project_id: str, request: Request):
    state = load_runtime_state()
    before = len(state.projects)
    state.projects = [p for p in state.projects if p.id != project_id]
    if len(state.projects) == before:
        raise HTTPException(status_code=404, detail="项目不存在")
    if state.active_project_id == project_id:
        state.active_project_id = state.projects[0].id if state.projects else None
    return await run_in_threadpool(_reload_app, request, state)


@router.put("/capabilities")
async def update_capabilities(body: CapabilitiesUpdate, request: Request):
    state = load_runtime_state()
    caps = dict(state.capabilities or DEFAULT_CAPABILITIES)
    if body.local_access is not None:
        caps["local_access"] = body.local_access
    if body.system is not None:
        caps["system"] = body.system
    if body.allow_actions is not None:
        caps["allow_actions"] = body.allow_actions
    state.capabilities = caps
    return await run_in_threadpool(_reload_app, request, state)
