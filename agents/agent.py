# -*- coding: utf-8 -*-
"""
Agent 装配入口。

读取 agents/agent.yaml 与 prompts/*.md，注册工具，接上短期记忆、向量库、
工作区命令执行器与 MCP 连接，再按 yaml 里的 framework 构造可插拔运行时。
HTTP 层只消费 Runtime，不再自己拼依赖。

装配顺序是**有讲究的**，改动前请先读这一段的理由：

    1. 先定框架名   —— 系统提示词是否包含「任务流程」取决于框架（state_loop 自带流程控制）
    2. 再建基础能力 —— 工作区门闩、会话存储、文件/搜索/计算/执行器工具
    3. 然后按技能过滤 —— 模型可见工具 = 启用技能声明的工具并集
    4. 最后接 MCP   —— MCP 工具是**动态工具源**，不写死在技能里，
                       因此在技能过滤之后并入，才不会被过滤掉
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from memory.history import HistoryStore
from memory.indexer import CodeIndexer
from memory.preferences import PreferenceStore
from memory.vector_store import CodeVectorStore
from skills import get_skill, render_skill_prompt, tools_for_skills
from tools.base import ToolRegistry
from tools.calculator import register_calculator
from tools.executor import RestrictedPythonExecutor, build_execution_tool
from tools.file_tools import register_file_tools
from tools.preference_tools import register_preference_tools
from tools.search import register_search_tools
from tools.shell import WorkspaceCommandRunner, build_shell_tool
from tools.workspace import WorkspaceSecurity

ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML = Path(__file__).resolve().parent / "agent.yaml"

#: 自研状态驱动主循环的框架名（多处引用，集中成一个常量避免拼写漂移）
STATE_LOOP = "state_loop"


@dataclass
class Runtime:
    """一次进程内可运行的 Agent 及其依赖。"""

    workspace: WorkspaceSecurity
    sessions: HistoryStore
    tools: ToolRegistry
    executor: RestrictedPythonExecutor
    rag_enabled: bool
    vector_store: Optional[CodeVectorStore]
    indexer: Optional[CodeIndexer]
    llm: Any
    agent: Any
    framework_name: str
    skill_names: List[str] = field(default_factory=list)
    system_prompt: str = ""
    # ---- 自研运行时专有（其他框架下为 None / 空）----
    runner: Optional[WorkspaceCommandRunner] = None
    mcp: Any = None
    loop_deps: Any = None
    # ---- 本地访问（fs_*/sys_*，未启用时为 None）----
    broker: Any = None
    # ---- 用户偏好记忆（与 history / RAG 分离）----
    preferences: Optional[PreferenceStore] = None

    def shutdown(self) -> None:
        """
        释放进程级资源：MCP 子进程、SQLite 连接、向量库句柄。

        为什么要显式释放（而不是等进程退出）：**Windows 下进程持有文件句柄时文件删不掉**。
        SQLite 的 `history.db` 就是典型——不释放会导致「临时目录清理失败」
        「移动或备份 data 目录失败」这类看起来毫不相干的问题。
        关闭失败一律忽略：退出路径上不该因为清理动作再抛异常。
        """
        for target, label in ((self.mcp, "MCP"),
                              (self.sessions, "会话库"),
                              (self.vector_store, "向量库")):
            closer = getattr(target, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception as e:  # noqa: BLE001
                print(f"[Agent] 警告: 关闭{label}失败: {type(e).__name__}: {e}")


# ======================================================================
# 定义与提示词
# ======================================================================
def load_agent_spec(path: Path = AGENT_YAML) -> Dict[str, Any]:
    """读取 Agent 定义。文件缺失时给出明确错误，避免静默退回空角色。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少 Agent 定义: {path}")
    with path.open("r", encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    if not isinstance(spec, dict):
        raise ValueError(f"Agent 定义必须是映射: {path}")
    return spec


def load_prompt_file(relpath: str) -> str:
    path = ROOT / relpath
    if not path.is_file():
        raise FileNotFoundError(f"缺少提示词文件: {path}")
    return path.read_text(encoding="utf-8").strip()


def compose_system_prompt(spec: Dict[str, Any], *, include_task_flow: bool = True,
                          extra_skills: Tuple[str, ...] = (),
                          preferences_section: str = "") -> str:
    """
    角色与目标 + 系统约束 +（可选）任务流程 + 技能说明 + 用户偏好 + 示例。

    :param include_task_flow: 是否把 `prompts/task.md` 的六步清单写进提示词。
        `state_loop` 传 False——**流程由状态机执行**，再把清单交给模型只会
        让它以为「步骤由自己决定」，与「控制流归机器」直接冲突。
        `prompts/task.md` 不删：ReAct 系框架仍在用。
    :param extra_skills: 装配期动态并入的技能（如按配置启用的 local_system）。
        不进 agent.yaml，因为它们的可用性取决于 config 而非角色定义。
    :param preferences_section: 已启用的跨会话用户偏好片段（由 PreferenceStore 生成）。
    """
    prompts = spec.get("prompts") or {}
    system = load_prompt_file(prompts.get("system") or "prompts/system.md")
    examples = load_prompt_file(prompts.get("examples") or "prompts/examples.md")
    skill_list = list(spec.get("skills") or []) + list(extra_skills)
    skills = render_skill_prompt(list(dict.fromkeys(skill_list)))
    identity = _identity_section(spec)

    parts = [identity, system]
    if include_task_flow:
        task = load_prompt_file(prompts.get("task") or "prompts/task.md")
        parts.append("## 任务流程\n" + task)
    parts.append(skills)
    pref = (preferences_section or "").strip()
    if pref:
        parts.append(pref)
    parts.append("## 示例\n" + examples)
    return "\n\n".join(part for part in parts if part)


def refresh_system_prompt(runtime: "Runtime") -> str:
    """按当前技能名单与偏好库重拼系统提示词，并写回 Runtime / Agent / LoopDeps。"""
    spec = load_agent_spec()
    yaml_skills = list(spec.get("skills") or [])
    skill_names = list(runtime.skill_names or yaml_skills)
    framework_name = resolve_framework(spec)
    pref = ""
    if runtime.preferences is not None:
        pref = runtime.preferences.prompt_section()
    system_prompt = compose_system_prompt(
        spec,
        include_task_flow=framework_name != STATE_LOOP,
        extra_skills=tuple(s for s in skill_names if s not in yaml_skills),
        preferences_section=pref,
    )
    runtime.system_prompt = system_prompt
    loop = runtime.loop_deps
    if loop is not None:
        loop.system_prompt = system_prompt
    agent = runtime.agent
    if agent is not None and hasattr(agent, "deps"):
        agent.deps.system_prompt = system_prompt
    return system_prompt


def _identity_section(spec: Dict[str, Any]) -> str:
    """把 agent.yaml 的角色和目标写进提示词，避免定义文件对模型不可见。"""
    lines: List[str] = ["## 角色"]
    role = str(spec.get("role") or "").strip()
    if role:
        lines.append(role)
    description = str(spec.get("description") or "").strip()
    if description:
        lines.append(description)
    goals = [str(item).strip() for item in (spec.get("goals") or []) if str(item).strip()]
    if goals:
        lines.append("")
        lines.append("目标：")
        lines.extend(f"- {goal}" for goal in goals)
    return "\n".join(lines).strip()


def resolve_framework(spec: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> str:
    """框架以 AGENT_FRAMEWORK 为准，其次 agent.yaml，最后才是配置默认值。"""
    import os
    override = os.getenv("AGENT_FRAMEWORK", "").strip()
    if override:
        return override
    if spec.get("framework"):
        return str(spec["framework"]).strip()
    if cfg and cfg.get("agent", {}).get("framework"):
        return str(cfg["agent"]["framework"]).strip()
    from agents.registry import DEFAULT_FRAMEWORK
    return DEFAULT_FRAMEWORK


def resolve_exec_mode(spec: Dict[str, Any], cfg: Dict[str, Any] | None = None):
    """
    执行档：运行时能力（本机项目页选择）优先，其次 agent.yaml。
    未知取值由 ExecMode.parse 落到 auto_workspace，不让服务起不来。
    """
    from agents.state_loop.state import ExecMode

    runtime_mode = None
    if cfg:
        runtime_mode = (cfg.get("agent") or {}).get("exec_mode")
    return ExecMode.parse(runtime_mode or spec.get("exec_mode"))


# ======================================================================
# 工具与技能
# ======================================================================
def _enabled_skills(spec: Dict[str, Any]) -> List[str]:
    names = list(spec.get("skills") or [])
    for name in names:
        get_skill(name)
    return names


def _select_tools(registry: ToolRegistry, names: List[str]) -> ToolRegistry:
    """只保留启用技能声明的工具。空列表表示不暴露任何工具。"""
    selected = ToolRegistry()
    missing = []
    for name in names:
        if registry.has(name):
            selected.register(registry.get(name))
        else:
            missing.append(name)
    if missing:
        print(f"[Agent] 警告: 下列工具未注册，已跳过: {', '.join(missing)}")
    return selected


def _build_tools(cfg: Dict[str, Any], workspace: WorkspaceSecurity
                 ) -> Tuple[ToolRegistry, RestrictedPythonExecutor, WorkspaceCommandRunner]:
    """注册内置工具：文件、搜索、计算、受限沙箱、工作区命令。"""
    tools = ToolRegistry()
    register_file_tools(tools, workspace)
    register_calculator(tools)

    executor_cfg = cfg.get("executor") or {}
    executor = RestrictedPythonExecutor(
        timeout_seconds=int(executor_cfg.get("timeout_seconds", 10)),
        max_result_chars=int(executor_cfg.get("max_result_chars", 20000)),
    )
    tools.register(build_execution_tool(executor))

    runner = WorkspaceCommandRunner(
        workspace,
        default_seconds=int(executor_cfg.get("command_timeout_seconds", 60)),
        ceiling_seconds=int(executor_cfg.get("command_timeout_ceiling", 120)),
    )
    tools.register(build_shell_tool(runner,
                                    output_limit=int(executor_cfg.get("max_result_chars", 20000))))
    return tools, executor, runner


def _build_local_access(cfg: Dict[str, Any], tools: ToolRegistry,
                        skill_names: List[str],
                        workspace: Optional[WorkspaceSecurity] = None) -> Any:
    """
    装配本地访问能力（fs_* / sys_*），返回 AccessBroker（未启用时为 None）。

    **N3 默认关闭在这里落地**：
        local_access.enabled=false → 不构造 broker、不注册任何 fs_* 工具
        没有可写根              → 不注册任何 fs_ 写入工具（模型根本看不到）
        system.enabled=false    → 不注册任何 sys_* 工具
    技能名单动态并入：工具存在，技能才进提示词——「模型可见工具」与「技能声明」
    不会因为配置组合出「声明了但根本没注册」的缺口。
    """
    from tools.fs_access import AccessBroker
    from tools.fs_tools import (
        apply_fs_write_descriptions,
        register_fs_read_tools,
        register_fs_write_tools,
    )
    from tools.system_tools import register_system_action_tools, register_system_tools

    broker = None
    local = cfg.get("local_access") or {}
    system = cfg.get("system") or {}
    exec_mode = (cfg.get("agent") or {}).get("exec_mode")

    if local.get("enabled"):
        broker = AccessBroker.from_config(local, ROOT)
        for warning in broker.warnings:
            print(f"[本地访问] 警告: {warning}")
        names = register_fs_read_tools(tools, broker, local)
        print(f"[本地访问] 只读工具已注册（{len(broker.roots)} 个授权根）: {', '.join(names)}")
        if "local_system" not in skill_names:
            skill_names.append("local_system")
        if broker.has_writable_roots():
            names = register_fs_write_tools(tools, broker, local)
            apply_fs_write_descriptions(tools, exec_mode)
            mode_label = str(exec_mode or "auto_workspace")
            confirm_hint = (
                "full_access 下已授权可写根可自动"
                if mode_label == "full_access"
                else "L4，默认需人工确认"
            )
            print(f"[本地访问] 写入工具已注册（{confirm_hint}｜档 {mode_label}）: {', '.join(names)}")
            if "local_system_write" not in skill_names:
                skill_names.append("local_system_write")

    if system.get("enabled"):
        names = register_system_tools(tools, system)
        if names:
            print(f"[系统数据] 只读工具已注册: {', '.join(names)}")
            if "local_system" not in skill_names:
                skill_names.append("local_system")
        action_names = register_system_action_tools(tools, system, broker, workspace)
        if action_names:
            print(f"[系统动作] 已注册（需人工确认）: {', '.join(action_names)}")
            if "local_system_write" not in skill_names:
                skill_names.append("local_system_write")

    return broker


def _build_rag(cfg: Dict[str, Any], workspace: WorkspaceSecurity, tools: ToolRegistry
               ) -> Tuple[Optional[CodeVectorStore], Optional[CodeIndexer], bool]:
    """初始化 RAG。**失败不阻止对话**——可选增强不该成为单点故障。"""
    vector_store: Optional[CodeVectorStore] = None
    indexer: Optional[CodeIndexer] = None
    enabled = bool(cfg.get("rag", {}).get("enabled", True))
    if enabled:
        try:
            vector_store = CodeVectorStore.create(cfg)
            indexer = CodeIndexer(workspace, cfg, vector_store)
            print(f"[RAG] 知识库就绪: {vector_store.status()}")
        except Exception as e:  # noqa: BLE001
            enabled = False
            print(f"[RAG] 警告: 向量库初始化失败，本次运行禁用 RAG: {type(e).__name__}: {e}")
    register_search_tools(tools, workspace, vector_store)
    return vector_store, indexer, enabled


def _connect_mcp(cfg: Dict[str, Any], tools: ToolRegistry) -> Any:
    """
    连接 MCP server 并把工具并入注册表。

    **在技能过滤之后调用**：MCP 是动态工具源，不该被技能的静态清单挡住。
    单个 server 失败只打警告，工具不注册，对话继续。
    """
    from tools.mcp import McpManager, load_servers

    servers, warnings = load_servers(cfg)
    for warning in warnings:
        print(f"[MCP] 警告: {warning}")
    if not servers:
        return None

    manager = McpManager(servers)
    ready = manager.connect_all()
    if not ready:
        print("[MCP] 没有 server 连接成功，本次运行不提供 MCP 工具")
        manager.close()
        return None
    names = manager.register_all(tools)
    print(f"[MCP] 已就绪 {len(ready)} 个 server，注册 {len(names)} 个工具: {', '.join(names)}")
    return manager


# ======================================================================
# 运行时装配
# ======================================================================
def _build_loop_deps(spec: Dict[str, Any], cfg: Dict[str, Any], *, llm, tools, workspace,
                     runner, system_prompt: str, rag_enabled: bool, mcp, broker,
                     confirm_handler=None) -> Any:
    """构造 state_loop 的依赖包。**只对它自己有意义的键**都从 agent.yaml 读。"""
    from agents.state_loop.runtime import LoopDeps, build_limits

    limits = build_limits(spec, cfg)
    mode = resolve_exec_mode(spec, cfg)
    return LoopDeps(
        llm=llm,
        registry=tools,
        workspace=workspace,
        runner=runner,
        config=limits,
        mode=mode,
        system_prompt=system_prompt,
        allowed_tools=tuple(tools.names()),
        rag_available=rag_enabled,
        mcp_manager=mcp,
        broker=broker,
        confirm_handler=confirm_handler,
        max_iterations=int(spec.get("max_iterations") or 12),
        debug=bool(cfg.get("server", {}).get("debug", False)),
    )


def _instantiate(framework_name: str, deps) -> Tuple[Any, str]:
    """构造 Agent，失败时回退到 native_react（保证服务能起来）。"""
    from agents.registry import (
        OptionalFrameworkMissingError,
        UnknownFrameworkError,
        build_agent,
    )

    try:
        return build_agent(framework_name, deps), framework_name
    except (OptionalFrameworkMissingError, UnknownFrameworkError) as e:
        print(f"[Agent] 警告: {e}\n[Agent] 已回退到内置 native_react 框架。")
        from agents.native_react import NativeReActAgent
        return NativeReActAgent(deps), "native_react(fallback)"
    except Exception as e:  # noqa: BLE001 - 构造期任何异常都不该让服务起不来
        print(f"[Agent] 警告: 框架 {framework_name} 初始化失败: {type(e).__name__}: {e}"
              f"\n[Agent] 已回退到内置 native_react 框架。")
        from agents.native_react import NativeReActAgent
        return NativeReActAgent(deps), "native_react(fallback)"


def build_runtime(cfg: Dict[str, Any], confirm_handler=None) -> Runtime:
    """
    按配置与 agent.yaml 装配运行时。RAG / MCP / 可选框架失败均不阻断启动。

    :param confirm_handler: 审批通道处理器（state_loop 的 ConfirmHandler）。
        缺省 None = 失败关闭（L4 / confirm_writes 一律按拒绝处理）。
    """
    from tools.api_client import LLMClient
    from agents.base import AgentDependencies

    spec = load_agent_spec()
    skill_names = _enabled_skills(spec)
    yaml_skills = list(skill_names)
    # ① 框架名先定：系统提示词是否包含「任务流程」取决于它
    framework_name = resolve_framework(spec, cfg)

    workspace = WorkspaceSecurity(cfg["server"]["workspace_root"])
    sessions = HistoryStore(
        cfg["storage"]["history_db"],
        max_messages=int(cfg["storage"].get("max_messages_per_session", 500)),
        legacy_dir=cfg["storage"].get("sessions_dir"),
    )

    storage = cfg.get("storage") or {}
    preferences = PreferenceStore(
        storage.get("preferences_path") or "./data/memory/preferences.json",
        max_items=int(storage.get("preferences_max_items", 500)),
        max_bytes=int(storage.get("preferences_max_bytes", 10 * 1024 * 1024)),
    )

    # ② 基础工具
    tools, executor, runner = _build_tools(cfg, workspace)
    # 偏好工具：写入走闸门 L2；on_change 在 Runtime 建好后才真正刷新提示词
    runtime_holder: Dict[str, Any] = {"runtime": None}

    def _on_pref_change() -> None:
        rt = runtime_holder.get("runtime")
        if rt is not None:
            refresh_system_prompt(rt)

    register_preference_tools(tools, preferences, on_change=_on_pref_change)
    # ②b 本地访问（fs_*/sys_*）：按配置注册，并动态把 local_system 技能并入名单
    broker = _build_local_access(cfg, tools, skill_names, workspace)
    system_prompt = compose_system_prompt(
        spec, include_task_flow=framework_name != STATE_LOOP,
        extra_skills=tuple(s for s in skill_names if s not in yaml_skills),
        preferences_section=preferences.prompt_section(),
    )
    # ③ RAG（检索工具必须参与技能过滤）
    vector_store, indexer, rag_enabled = _build_rag(cfg, workspace, tools)
    tools = _select_tools(tools, tools_for_skills(skill_names))
    # ④ MCP：动态并入，不受技能清单限制
    mcp = _connect_mcp(cfg, tools)

    llm = LLMClient(cfg)
    loop_deps = _build_loop_deps(spec, cfg, llm=llm, tools=tools, workspace=workspace,
                                 runner=runner, system_prompt=system_prompt,
                                 rag_enabled=rag_enabled, mcp=mcp, broker=broker,
                                 confirm_handler=confirm_handler)

    max_iterations = int(spec.get("max_iterations") or cfg["agent"].get("max_iterations") or 12)
    deps = AgentDependencies(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        loop_deps=loop_deps,
    )
    agent, framework_name = _instantiate(framework_name, deps)

    print(
        f"[Agent] 当前框架: {framework_name} | 技能: {', '.join(skill_names)} | "
        f"工具: {', '.join(tools.names())}"
    )
    if framework_name.startswith(STATE_LOOP):
        print(f"[Agent] 执行档: {loop_deps.mode} | 上下文预算: "
              f"{loop_deps.config.context_char_budget} 字符 | "
              f"步数上限: {loop_deps.config.max_steps}")

    runtime = Runtime(
        workspace=workspace,
        sessions=sessions,
        tools=tools,
        executor=executor,
        rag_enabled=rag_enabled,
        vector_store=vector_store,
        indexer=indexer,
        llm=llm,
        agent=agent,
        framework_name=framework_name,
        skill_names=skill_names,
        system_prompt=system_prompt,
        runner=runner,
        mcp=mcp,
        loop_deps=loop_deps,
        broker=broker,
        preferences=preferences,
    )
    runtime_holder["runtime"] = runtime
    return runtime
