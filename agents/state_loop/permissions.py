# -*- coding: utf-8 -*-
"""
权限分级与执行档策略。

**本模块是权限判定的唯一权威点。** 工具自己声明的 `Tool.read_only`、模型自报的
`effect`、server 给的描述文本，都不能直接决定放行——只有这里能。

判定的三条原则（架构文档第 6.2 节）：
1. **看副作用，不看工具名像不像只读。** `run_python_code` 名字里有 code，但它是 L2 沙箱；
   `rag_search` 名字里有 search，它读索引也读文件，但都不落盘，所以是 L0。
2. **失败关闭。** 判定不出来的一律按更危险的等级处理（未登记工具 → L4）。
3. **越界是拒绝而不是降级。** L5 不是「更严格的 L4」，它没有确认通道。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.shell import (
    ARGV_ALLOWLIST,
    PIP_SUBCOMMANDS,
    argv0_violation,
    interpreter_violation,
    normalize_argv0,
)
from tools.workspace import PathTraversalError, WorkspaceSecurity

from .state import (
    EFFECT_LABELS,
    Effect,
    ExecMode,
    Failure,
    FailureKind,
    ToolCall,
)

__all__ = [
    "Policy", "Classification", "Authorization", "MODE_POLICY",
    "TOOL_EFFECTS", "ARGV_ALLOWLIST", "classify", "authorize",
    "normalize_rel", "path_in_scope", "write_targets", "external_write_targets",
    "describe_policy",
]


# ======================================================================
# 策略表
# ======================================================================
class Policy(str, Enum):
    """一个副作用等级在当前执行档下的处置方式。"""

    auto = "auto"          # 直接放行
    confirm = "confirm"    # 暂停等人确认（第一期没有审批 UI，等价于拒绝）
    deny = "deny"          # 直接拒绝

    def __str__(self) -> str:
        return self.value


#: 策略优先级：取批次内**最严格**的一条，而不是取数值最大的副作用等级。
#: 反例：confirm_writes 档下 L2 自动、L1 需确认——若按等级取 max 会得到 L2(自动) 而放行整批，
#: 于是「一次 edit_file + 一次 run_python_code」就把写盘动作悄悄放过了。
_POLICY_RANK: Dict[Policy, int] = {Policy.auto: 0, Policy.confirm: 1, Policy.deny: 2}

MODE_POLICY: Dict[ExecMode, Dict[Effect, Policy]] = {
    # 默认档：工作区内自动，只有「可能改外部系统」才需要确认
    ExecMode.auto_workspace: {
        Effect.L0_READ: Policy.auto,
        Effect.L1_WRITE: Policy.auto,
        Effect.L2_SANDBOX: Policy.auto,
        Effect.L3_COMMAND: Policy.auto,
        Effect.L4_MCP_MUTATE: Policy.confirm,
        Effect.L5_ESCAPE: Policy.deny,
    },
    # 写盘与起进程都要人点头
    ExecMode.confirm_writes: {
        Effect.L0_READ: Policy.auto,
        Effect.L1_WRITE: Policy.confirm,
        Effect.L2_SANDBOX: Policy.auto,
        Effect.L3_COMMAND: Policy.confirm,
        Effect.L4_MCP_MUTATE: Policy.confirm,
        Effect.L5_ESCAPE: Policy.deny,
    },
    # 计划确认之后按默认档走；确认之前禁止一切 L1+ 是**结构性保证**：
    # 状态机上不经过 authorize(plan) 就到达不了 act，不靠这里再加一层判断。
    ExecMode.plan: {
        Effect.L0_READ: Policy.auto,
        Effect.L1_WRITE: Policy.auto,
        Effect.L2_SANDBOX: Policy.auto,
        Effect.L3_COMMAND: Policy.auto,
        Effect.L4_MCP_MUTATE: Policy.confirm,
        Effect.L5_ESCAPE: Policy.deny,
    },
}

#: 内置工具的副作用等级。新增内置工具必须在这里登记，否则会被按 L4 保守处理。
TOOL_EFFECTS: Dict[str, Effect] = {
    "list_dir": Effect.L0_READ,
    "read_file": Effect.L0_READ,
    "file_search": Effect.L0_READ,
    "rag_search": Effect.L0_READ,
    "calculator": Effect.L0_READ,
    # ---- 用户偏好记忆（跨会话 JSON，与 RAG/history 分离）----
    "preference_list": Effect.L0_READ,
    "preference_add": Effect.L2_SANDBOX,
    "preference_update": Effect.L2_SANDBOX,
    "preference_delete": Effect.L2_SANDBOX,
    "write_file": Effect.L1_WRITE,
    "edit_file": Effect.L1_WRITE,
    "run_python_code": Effect.L2_SANDBOX,
    "run_command": Effect.L3_COMMAND,
    # ---- 本地访问（workbuddy 本地文件系统能力，见 docs/local-system-access-plan.md）----
    # 只读组：授权根内读取，无副作用 → L0 自动放行
    "fs_roots": Effect.L0_READ,
    "fs_list": Effect.L0_READ,
    "fs_read": Effect.L0_READ,
    "fs_search": Effect.L0_READ,
    "fs_stat": Effect.L0_READ,
    # 写入组：作用域在工作区外 → 一律 L4 走人工确认。
    # 「可写根上的写需要确认、只读根上的写被门闩拒绝」的矩阵落在
    # L4 确认通道 + AccessBroker.resolve(need_write) 两层，这里只登记等级。
    "fs_write": Effect.L4_MCP_MUTATE,
    "fs_edit": Effect.L4_MCP_MUTATE,
    "fs_copy": Effect.L4_MCP_MUTATE,
    "fs_move": Effect.L4_MCP_MUTATE,
    "fs_delete": Effect.L4_MCP_MUTATE,
    "fs_restore": Effect.L4_MCP_MUTATE,
    # ---- 系统数据只读层：只读事实源，无副作用 → L0 ----
    "sys_overview": Effect.L0_READ,
    "sys_processes": Effect.L0_READ,
    "sys_disks": Effect.L0_READ,
    "sys_network": Effect.L0_READ,
    "sys_env": Effect.L0_READ,
    "sys_battery": Effect.L0_READ,
    "sys_hardware": Effect.L0_READ,
    "sys_services": Effect.L0_READ,
    "sys_installed_apps": Effect.L0_READ,
    "sys_clipboard": Effect.L0_READ,
    "sys_windows": Effect.L0_READ,
    # ---- 受控系统动作：桌面通知无文件副作用 → L2；其余触碰外部系统 → L4 ----
    "sys_notify": Effect.L2_SANDBOX,
    "sys_open_path": Effect.L4_MCP_MUTATE,
    "sys_launch": Effect.L4_MCP_MUTATE,
    "sys_screenshot": Effect.L4_MCP_MUTATE,
}

# 命令允许列表与解释器形态判定**不在这里定义**，从 tools/shell.py 引入。
# 它们是命令执行器的安全边界，规划期的验收命令校验也用同一套值——
# 两处各存一份必然出现「规划时允许、执行时拒绝」的不一致。


# ======================================================================
# 路径语义
# ======================================================================
def normalize_rel(path: str) -> str:
    """把路径统一成「相对、正斜杠、无前导 ./」的形式，便于跨平台比较。"""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") if text != "/" else text


def path_in_scope(rel_path: str, scope: Iterable[str]) -> bool:
    """
    判断写入路径是否落在任务声明的 scope 内。

    scope 项可以是：
    - 工作区相对路径或目录前缀（`app/main.py`、`app/`）
    - 授权根引用 `root:rel`（`desktop:notes.txt`、`desktop:.`）

    工作区项与外部根项互不匹配。目录前缀按 `startswith(entry + "/")` 判断，
    避免 `app/` 命中 `application.py`。
    """
    target = normalize_rel(rel_path)
    target_root, target_rest = _split_root_rel(target)
    for raw in scope:
        entry = normalize_rel(raw)
        if not entry:
            continue
        entry_root, entry_rest = _split_root_rel(entry)
        if target_root != entry_root:
            continue
        if target_rest == entry_rest:
            return True
        if not entry_rest or entry_rest in (".",):
            return True
        if target_rest.startswith(entry_rest + "/"):
            return True
    return False


def _split_root_rel(path: str) -> Tuple[Optional[str], str]:
    """`desktop:a/b` → ('desktop', 'a/b')；工作区路径 → (None, path)。"""
    text = normalize_rel(path)
    if ":" not in text:
        return None, text
    root, _, rest = text.partition(":")
    root = root.strip()
    rest = rest.strip().lstrip("/") or "."
    if not root:
        return None, text
    return root, rest


def write_targets(name: str, arguments: Dict[str, Any]) -> List[str]:
    """从调用参数里取出「这次会写到哪些相对路径」。非写工具返回空列表。"""
    if name not in ("write_file", "edit_file"):
        return []
    raw = arguments.get("path")
    if raw is None:
        return []
    return [str(raw)]


#: 外部根写入工具的「根参数名 → 路径参数名」映射。
#: 用于写前快照记账（outcome.run_tool）与回滚复查（journal.restore）。
_EXTERNAL_WRITE_TOOLS: Dict[str, Tuple[str, str]] = {
    "fs_write": ("root", "path"),
    "fs_edit": ("root", "path"),
    "fs_copy": ("dst_root", "dst_path"),
    "fs_move": ("dst_root", "dst_path"),
    "fs_delete": ("root", "path"),
    # fs_restore 不在这里：它有隔离目录 meta 校验，且「还原」的副作用是恢复原状
}


def external_write_targets(name: str, arguments: Dict[str, Any]) -> List[Tuple[str, str]]:
    """外部根写入调用 → [(root, rel)]。参数缺失或非外部写工具 → 空列表。"""
    pair = _EXTERNAL_WRITE_TOOLS.get(name)
    if pair is None:
        return []
    root_key, path_key = pair
    root = arguments.get(root_key)
    rel = arguments.get(path_key)
    if root is None or rel is None or not str(root).strip() or not str(rel).strip():
        return []
    return [(str(root), str(rel))]


# ======================================================================
# 单次判定
# ======================================================================
@dataclass(frozen=True)
class Classification:
    """
    一次工具调用的权限判定结果。

    `failure` 非空表示**必须拒绝该调用**；此时 `effect` 恒为 L5（不可接受），
    只是为了日志里能看到「它被判成了什么等级」。
    """

    effect: Effect
    failure: Optional[Failure] = None

    @property
    def accepted(self) -> bool:
        return self.failure is None


def _deny(kind: FailureKind, message: str, tool: str,
          retryable: bool, artifacts: Optional[Dict[str, Any]] = None) -> Classification:
    return Classification(Effect.L5_ESCAPE, Failure(
        kind=str(kind), message=message, tool=tool, retryable=retryable,
        artifacts=dict(artifacts or {})))


def classify(name: str, arguments: Dict[str, Any],
             registry: Any, workspace: Optional[WorkspaceSecurity]) -> Classification:
    """
    判定一次调用的副作用等级，或给出拒绝理由。

    顺序很重要：**先查越界（L5），再查登记表**。否则一个 `edit_file path=../../x`
    会先被登记成 L1，然后靠后续的 scope 检查兜住——两层检查比一层更容易漏。
    """
    if registry is not None and not registry.has(name):
        known = ", ".join(registry.names()[:12])
        return _deny(FailureKind.NotFound,
                     f"工具不存在: {name}（可用工具: {known}）",
                     name, retryable=True)

    if name == "run_command":
        return _classify_command(name, arguments, workspace)
    if name == "run_python_code":
        return Classification(Effect.L2_SANDBOX)

    effect = TOOL_EFFECTS.get(name)
    if effect is None:
        # 未登记 → 第三方工具（MCP 桥接进来的）。只读声明可信才降到 L0。
        tool = registry.get(name) if registry is not None else None
        if getattr(tool, "read_only", False):
            return Classification(Effect.L0_READ)
        return Classification(Effect.L4_MCP_MUTATE)

    if effect is Effect.L1_WRITE and workspace is not None:
        for target in write_targets(name, arguments):
            try:
                workspace.resolve(target)
            except PathTraversalError as e:
                return _deny(FailureKind.PathDenied, str(e), name, retryable=False)
            except (OSError, ValueError) as e:
                return _deny(FailureKind.ArgError, f"路径不可用: {e}", name, retryable=True)
    return Classification(effect)


def _classify_command(name: str, arguments: Dict[str, Any],
                      workspace: Optional[WorkspaceSecurity]) -> Classification:
    """L3 判定：argv 形态、argv0 白名单、解释器逃逸、cwd 归属。"""
    argv = arguments.get("argv")

    # 形态不合法不当成权限问题——交给工具处理器抛 ArgError，
    # 这样模型收到的是「参数错了，请改成数组」，而不是「你没权限」。
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        return Classification(Effect.L3_COMMAND)

    violation = argv0_violation(str(argv[0]))
    if violation is not None:
        return _deny(FailureKind.PermissionDenied, violation, name, retryable=False)

    argv0 = normalize_argv0(str(argv[0]))
    violation = interpreter_violation(argv0, [str(item) for item in argv[1:]])
    if violation is not None:
        return _deny(FailureKind.PermissionDenied, violation, name, retryable=False)

    if argv0 == "pip":
        sub = str(argv[1]).lower() if len(argv) > 1 else ""
        if sub not in PIP_SUBCOMMANDS:
            return _deny(FailureKind.PermissionDenied,
                         f"pip 只允许 {', '.join(sorted(PIP_SUBCOMMANDS))}（不修改环境）",
                         name, retryable=False)

    cwd = arguments.get("cwd")
    if workspace is not None and cwd not in (None, ""):
        try:
            resolved = workspace.resolve(str(cwd))
        except PathTraversalError as e:
            return _deny(FailureKind.PathDenied, str(e), name, retryable=False)
        if not resolved.is_dir():
            return _deny(FailureKind.ArgError, f"cwd 不是目录: {cwd}", name, retryable=True)

    return Classification(Effect.L3_COMMAND)


# ======================================================================
# 批次判定
# ======================================================================
@dataclass
class Authorization:
    """
    一个待执行批次的授权结论。

    :param calls:   effect 已回填的调用（模型自报的 effect 在任何情况下都被覆盖）
    :param policy: 批次内**最严格**的处置方式
    :param failure: 批次被拒绝时的原因（取第一个不可接受的调用）
    """

    calls: Tuple[ToolCall, ...] = ()
    policy: Policy = Policy.auto
    failure: Optional[Failure] = None
    annotations: Dict[str, Any] = field(default_factory=dict)

    @property
    def needs_confirm(self) -> bool:
        return self.failure is None and self.policy is Policy.confirm

    @property
    def granted(self) -> bool:
        return self.failure is None and self.policy is Policy.auto

    @property
    def denied(self) -> bool:
        return self.failure is not None or self.policy is Policy.deny

    @property
    def max_effect(self) -> Effect:
        return max((c.effect for c in self.calls), default=Effect.L0_READ)

    def summary(self) -> str:
        if self.failure is not None:
            return f"拒绝：{self.failure.one_line()}"
        if self.policy is Policy.deny:
            return "拒绝：当前执行档不允许该等级操作"
        if self.policy is Policy.confirm:
            return f"待确认：{', '.join(c.name for c in self.calls)}"
        return f"放行：{', '.join(c.name for c in self.calls)}"


def authorize(calls: Iterable[ToolCall], mode: ExecMode,
              registry: Any, workspace: Optional[WorkspaceSecurity],
              scope: Iterable[str] = ()) -> Authorization:
    """
    对一批调用做权限判定。

    :param scope: 当前任务的写权限范围。空表示「本批次不允许写入」，
                  这样「任务没声明 scope 却要写文件」会当场被拒，而不是事后才发现。
    """
    policy_table = MODE_POLICY.get(mode, MODE_POLICY[ExecMode.auto_workspace])
    annotated: List[ToolCall] = []
    worst = Policy.auto
    first_failure: Optional[Failure] = None
    blocked_effects: List[Effect] = []

    for call in calls:
        result = classify(call.name, call.arguments, registry, workspace)
        annotated.append(ToolCall(id=call.id, name=call.name,
                                 arguments=call.arguments, effect=result.effect))

        if result.failure is not None:
            blocked_effects.append(result.effect)
            if first_failure is None:
                first_failure = result.failure
            worst = Policy.deny
            continue

        # 写盘必须落在任务 scope 内。这是 authorize 的守卫，不是提示词里的叮嘱。
        # 工作区写入看 write_targets；外部根写入看 external_write_targets（root:rel）。
        if result.effect is Effect.L1_WRITE and not all(
                path_in_scope(t, scope) for t in write_targets(call.name, call.arguments)):
            blocked_effects.append(result.effect)
            if first_failure is None:
                targets = ", ".join(write_targets(call.name, call.arguments)) or "(空路径)"
                first_failure = Failure(
                    kind=str(FailureKind.ScopeViolation),
                    message=(f"写入 {targets} 不在本任务允许范围 {list(scope) or '（未声明）'} 内，"
                             f"工具未执行。"),
                    tool=call.name, retryable=False,
                )
            worst = Policy.deny
            continue

        if result.effect is Effect.L4_MCP_MUTATE:
            external = external_write_targets(call.name, call.arguments)
            if external and not all(
                    path_in_scope(f"{root}:{rel}", scope) for root, rel in external):
                blocked_effects.append(result.effect)
                if first_failure is None:
                    targets = ", ".join(f"{r}:{p}" for r, p in external) or "(空路径)"
                    first_failure = Failure(
                        kind=str(FailureKind.ScopeViolation),
                        message=(f"外部根写入 {targets} 不在本任务允许范围 "
                                 f"{list(scope) or '（未声明）'} 内，工具未执行。"),
                        tool=call.name, retryable=False,
                    )
                worst = Policy.deny
                continue

        call_policy = policy_table.get(result.effect, Policy.confirm)
        if _POLICY_RANK[call_policy] > _POLICY_RANK[worst]:
            worst = call_policy

    return Authorization(
        calls=tuple(annotated),
        policy=worst,
        failure=first_failure,
        annotations={
            "blocked_effects": [e.code for e in blocked_effects],
            "labels": {e.code: EFFECT_LABELS[e] for e in set(effect_map(annotated))},
        },
    )


def effect_map(calls: Iterable[ToolCall]) -> List[Effect]:
    """批次涉及的副作用等级（去重，升序）。用于日志与事件。"""
    return sorted({c.effect for c in calls})


def describe_policy(mode: ExecMode) -> str:
    """执行档的一句话说明，供启动日志与 /api/health 展示。"""
    table = MODE_POLICY.get(mode, {})
    buckets: Dict[Policy, List[str]] = {p: [] for p in Policy}
    for effect, policy in sorted(table.items(), key=lambda kv: int(kv[0])):
        buckets[policy].append(effect.code)
    return (f"{mode}: 自动 {','.join(buckets[Policy.auto]) or '-'}"
            f"｜需确认 {','.join(buckets[Policy.confirm]) or '-'}"
            f"｜拒绝 {','.join(buckets[Policy.deny]) or '-'}")
