# -*- coding: utf-8 -*-
"""
运行期热重载：切换本机项目 / 能力时无需重启服务。

重注册工作区绑定工具（list_dir / run_command 等）与 fs_* / sys_* 工具，
并同步更新 Runtime、LoopDeps、Agent 依赖中的 workspace / broker / 提示词。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from agents.agent import (
    Runtime,
    _build_local_access,
    _enabled_skills,
    _select_tools,
    compose_system_prompt,
    load_agent_spec,
    resolve_exec_mode,
    resolve_framework,
)
from app.local_runtime import WORKSPACE_TOOL_NAMES
from skills import tools_for_skills
from tools.base import ToolRegistry
from tools.file_tools import build_file_tools
from tools.search import build_file_search_tool, register_rag_tools
from tools.shell import WorkspaceCommandRunner, build_shell_tool
from tools.workspace import WorkspaceSecurity


def _is_dynamic_tool(name: str) -> bool:
    return name.startswith("fs_") or name.startswith("sys_")


def _strip_workspace_tools(registry: ToolRegistry) -> None:
    for name in list(registry.names()):
        if name in WORKSPACE_TOOL_NAMES or _is_dynamic_tool(name):
            registry.unregister(name)


def _register_workspace_tools(
    registry: ToolRegistry,
    workspace: WorkspaceSecurity,
    cfg: Dict[str, Any],
    vector_store: Any,
) -> WorkspaceCommandRunner:
    for tool in build_file_tools(workspace):
        registry.replace(tool)
    registry.replace(build_file_search_tool(workspace))
    register_rag_tools(registry, vector_store)

    executor_cfg = cfg.get("executor") or {}
    runner = WorkspaceCommandRunner(
        workspace,
        default_seconds=int(executor_cfg.get("command_timeout_seconds", 60)),
        ceiling_seconds=int(executor_cfg.get("command_timeout_ceiling", 120)),
    )
    registry.replace(build_shell_tool(
        runner,
        output_limit=int(executor_cfg.get("max_result_chars", 20000)),
    ))
    return runner


def reconfigure_runtime(
    runtime: Runtime,
    cfg: Dict[str, Any],
    *,
    confirm_handler: Any = None,
) -> Runtime:
    """
    按最新配置重绑工作区与本地访问工具。MCP 工具保留不动。

    :return: 更新后的 Runtime（同一实例，字段已就地替换）
    """
    spec = load_agent_spec()
    yaml_skills = list(spec.get("skills") or [])
    skill_names = _enabled_skills(spec)

    # 去掉上次动态并入的 local_system* 技能，由 _build_local_access 重新决定
    for skill in ("local_system", "local_system_write"):
        while skill in skill_names and skill not in yaml_skills:
            skill_names.remove(skill)

    workspace = WorkspaceSecurity(cfg["server"]["workspace_root"])
    _strip_workspace_tools(runtime.tools)
    runner = _register_workspace_tools(
        runtime.tools, workspace, cfg, runtime.vector_store,
    )

    broker = _build_local_access(cfg, runtime.tools, skill_names, workspace)

    framework_name = resolve_framework(spec, cfg)
    pref_section = ""
    if runtime.preferences is not None:
        pref_section = runtime.preferences.prompt_section()
    system_prompt = compose_system_prompt(
        spec,
        include_task_flow=framework_name != "state_loop",
        extra_skills=tuple(s for s in skill_names if s not in yaml_skills),
        preferences_section=pref_section,
    )

    # 技能过滤（MCP 工具名以 mcp_ 开头，不在技能清单里，不会被摘掉）
    filtered = _select_tools(runtime.tools, tools_for_skills(skill_names))
    for name in runtime.tools.names():
        if name.startswith("mcp_") and not filtered.has(name):
            filtered.register(runtime.tools.get(name))

    runtime.workspace = workspace
    runtime.runner = runner
    runtime.broker = broker
    runtime.tools = filtered
    runtime.skill_names = skill_names
    runtime.system_prompt = system_prompt

    # 知识库索引器绑定当前工作区，否则 rag_search / 增量索引仍扫旧根，与 Agent 文件工具脱节。
    if runtime.vector_store is not None:
        from memory.indexer import CodeIndexer
        runtime.indexer = CodeIndexer(workspace, cfg, runtime.vector_store)

    loop = runtime.loop_deps
    if loop is not None:
        loop.workspace = workspace
        loop.runner = runner
        loop.broker = broker
        loop.registry = filtered
        loop.system_prompt = system_prompt
        loop.allowed_tools = tuple(filtered.names())
        if confirm_handler is not None:
            loop.confirm_handler = confirm_handler
        loop.mode = resolve_exec_mode(spec, cfg)
        loop.plan_confirmed = False

    agent = runtime.agent
    if agent is not None and hasattr(agent, "deps"):
        agent.deps.tools = filtered
        agent.deps.system_prompt = system_prompt
        agent.deps.loop_deps = loop

    return runtime


def reload_llm(runtime: Runtime, cfg: Dict[str, Any]) -> Any:
    """按最新 cfg['llm'] 热切换 LLM 客户端，同步 Agent 循环依赖。"""
    from tools.api_client import LLMClient

    llm = LLMClient(cfg)
    llm.sync_registry(cfg.get("llm") or {})
    runtime.llm = llm
    loop = runtime.loop_deps
    if loop is not None:
        loop.llm = llm
    agent = runtime.agent
    if agent is not None and hasattr(agent, "deps"):
        agent.deps.llm = llm
    return llm


def runtime_model_summary(runtime: Runtime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """当前运行时模型摘要（供 /api/models 与 health 使用）。"""
    llm_cfg = cfg.get("llm") or {}
    reg = llm_cfg.get("registry") or {}
    return {
        "model": llm_cfg.get("model", ""),
        "provider": llm_cfg.get("provider", ""),
        "base_url": llm_cfg.get("base_url", ""),
        "reasoning_level": llm_cfg.get("reasoning_level") or reg.get("reasoning_level"),
        "registry": reg,
        "rag_embedding": (llm_cfg.get("embedding") or {}).get("model"),
    }


def runtime_status(runtime: Runtime, cfg: Dict[str, Any],
                   state: Any) -> Dict[str, Any]:
    """供 /api/runtime 返回的摘要。"""
    tool_names = runtime.tools.names()
    return {
        "workspace_root": cfg["server"]["workspace_root"],
        "active_project_id": state.active_project_id,
        "projects": [p.to_dict() for p in state.projects],
        "capabilities": state.capabilities,
        "local_access": {
            "enabled": bool((cfg.get("local_access") or {}).get("enabled")),
            "roots": (runtime.broker.roots_view() if runtime.broker else []),
        },
        "system": {
            "enabled": bool((cfg.get("system") or {}).get("enabled")),
            "psutil": _psutil_ok(),
            "allow_actions": list((cfg.get("system") or {}).get("allow_actions") or []),
        },
        "tools": {
            "fs": [n for n in tool_names if n.startswith("fs_")],
            "sys": [n for n in tool_names if n.startswith("sys_")],
            "workspace": [n for n in tool_names if n in WORKSPACE_TOOL_NAMES],
        },
        "exec_mode": str(getattr(runtime.loop_deps, "mode", "") or
                         (state.capabilities or {}).get("exec_mode") or ""),
        "exec_modes": _exec_mode_catalog(),
    }


def _exec_mode_catalog():
    from agents.state_loop.permissions import exec_mode_catalog
    return exec_mode_catalog()


def _psutil_ok() -> bool:
    try:
        from tools.system_tools import psutil_available
        return bool(psutil_available)
    except Exception:  # noqa: BLE001
        return False
