# -*- coding: utf-8 -*-
"""
FastAPI 应用入口。

启动期（lifespan）只做一件事：调用 agents.agent.build_runtime 完成装配。
配置、工具、技能、记忆、MCP 与框架选择都在该入口内，不在 HTTP 层重复。

退出期负责释放进程级资源：MCP 的 stdio 子进程必须在这里关掉，
否则进程退出后会留下孤儿进程。

前端：原生 HTML/CSS/JS 静态托管在 /static，根路径 / 返回聊天页面。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from agents.agent import build_runtime
from app.api import (
    routes_chat,
    routes_context,
    routes_files,
    routes_memory,
    routes_models,
    routes_modules,
    routes_permissions,
    routes_prompt_match,
    routes_rag,
    routes_runtime,
    routes_sessions,
    routes_stats,
)
from app.context import ContextDaemon
from app.modules import RagAutoIndexDaemon
from app.prompt_match import PromptMatchDaemon
from app.api.routes_permissions import PermissionHub, make_confirm_handler
from app.config import load_config
from tools.workspace import PathTraversalError

WEB_DIR = Path(__file__).resolve().parent / "web"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：装配运行时；RAG / MCP / 可选框架失败均不阻断启动。"""
    cfg = load_config()
    app.state.cfg = cfg

    # 审批通道：先建 Hub，再把 confirm_handler 注入主循环。
    # 60 秒无人答复 = 拒绝（失败关闭），超时秒数由 permissions.timeout_seconds 配置。
    # 决议（允许/拒绝/超时）落审计日志，文件由 permissions.audit_log 配置。
    permissions_cfg = cfg.get("permissions") or {}
    hub = PermissionHub(timeout_seconds=float(permissions_cfg.get("timeout_seconds", 60)))
    audit_raw = str(permissions_cfg.get("audit_log") or "")
    if audit_raw:
        audit_path = Path(audit_raw)
        hub.audit_log = (audit_path if audit_path.is_absolute()
                         else Path(cfg.get("project_root", Path(__file__).resolve().parent.parent))
                         / audit_path)
    confirm_handler = make_confirm_handler(hub)
    app.state._confirm_handler = confirm_handler
    runtime = await run_in_threadpool(build_runtime, cfg, confirm_handler)

    app.state.runtime = runtime
    app.state.permissions = hub
    app.state.workspace = runtime.workspace
    app.state.sessions = runtime.sessions
    app.state.tools = runtime.tools
    app.state.executor = runtime.executor
    app.state.rag_enabled = runtime.rag_enabled
    app.state.vector_store = runtime.vector_store
    app.state.indexer = runtime.indexer
    app.state.llm = runtime.llm
    app.state.agent = runtime.agent
    app.state.framework_name = runtime.framework_name
    app.state.skills = runtime.skill_names
    app.state.mcp = runtime.mcp
    app.state.loop_deps = runtime.loop_deps
    app.state.broker = runtime.broker
    app.state.preferences = runtime.preferences

    context_daemon = ContextDaemon(runtime.workspace)
    app.state.context_daemon = context_daemon
    await context_daemon.start()

    rag_daemon = RagAutoIndexDaemon(app.state)
    app.state.rag_daemon = rag_daemon
    await rag_daemon.start()

    prompt_match_daemon = PromptMatchDaemon(app.state)
    app.state.prompt_match_daemon = prompt_match_daemon
    await prompt_match_daemon.start()

    try:
        yield
    finally:
        await prompt_match_daemon.stop()
        await rag_daemon.stop()
        await context_daemon.stop()
        # 释放进程级资源：MCP 的 stdio 子进程不关会成为孤儿进程
        await run_in_threadpool(runtime.shutdown)


app = FastAPI(title="本地编码智能体", version="1.0.0", lifespan=lifespan)

# 路由注册
app.include_router(routes_chat.router)
app.include_router(routes_sessions.router)
app.include_router(routes_memory.router)
app.include_router(routes_files.router)
app.include_router(routes_rag.router)
app.include_router(routes_permissions.router)
app.include_router(routes_runtime.router)
app.include_router(routes_models.router)
app.include_router(routes_context.router)
app.include_router(routes_prompt_match.router)
app.include_router(routes_modules.router)
app.include_router(routes_stats.router)


@app.exception_handler(PathTraversalError)
async def path_traversal_handler(request: Request, exc: PathTraversalError):
    """所有越界访问统一返回 403。"""
    return JSONResponse(status_code=403, content={"ok": False, "detail": str(exc)})


@app.get("/api/health")
async def health(request: Request):
    """健康检查：返回框架、工具、RAG、MCP 与模型注册表衔接状态（不探测 LLM 连通性）。"""
    state = request.app.state
    from agents.runtime_reload import runtime_model_summary
    from app.model_registry import integration_summary, load_registry
    from app.usage_tracker import get_usage_tracker

    loop_deps = getattr(state, "loop_deps", None)
    reg = load_registry()
    return {
        "status": "ok",
        "framework": state.framework_name,
        "skills": state.skills,
        "model": state.cfg["llm"].get("model", ""),
        "tools": state.tools.names(),
        "rag_enabled": state.rag_enabled,
        "rag": state.vector_store.status() if state.vector_store else None,
        "preferences": (
            state.preferences.stats()
            if getattr(state, "preferences", None) is not None
            else None
        ),
        "mcp": state.mcp.status() if getattr(state, "mcp", None) else [],
        "exec_mode": str(getattr(loop_deps, "mode", "")) or None,
        "models": runtime_model_summary(state.runtime, state.cfg),
        "model_integration": integration_summary(reg, state.cfg),
        "usage": get_usage_tracker().summary(
            session_count=len(state.sessions.list_sessions()),
        )["totals"],
    }


# 前端静态资源（原生 HTML/CSS/JS，零打包）
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    """聊天主页面。"""
    return FileResponse(str(WEB_DIR / "index.html"))
