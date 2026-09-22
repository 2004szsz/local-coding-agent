# -*- coding: utf-8 -*-
"""
工作区命令执行器（L3 沙箱终端）。

**为什么必须补这一层**：现有的 `RestrictedPythonExecutor`（L2）禁止 import、看不到工作区文件，
它能做纯计算验证，但**不能编译项目、不能跑 pytest、不能读刚改的文件**——
「运行检验自测」这条链路无处落地。L3 是它缺失的另一半。

安全立场（与 L2 的分工，不是重复实现）：

| 维度 | L2 RestrictedPython | L3 WorkspaceCommandRunner |
|------|---------------------|---------------------------|
| 隔离手段 | 受限字节码 + 禁 import | OS 进程 + 权限分级 + argv 白名单 |
| 能看见工作区 | 不能 | 能（cwd 锁在工作区内） |

实现约束（每一条都有具体理由，改动前请先读注释）：

1. **`shell=False`，只接受 argv 数组。** 拼字符串 = 命令注入入口；argv 数组让白名单判定
   能精确到第 0 个元素。
2. **白名单在这里再做一次。** `permissions.classify` 已经判过，这里复查是纵深防御——
   命令执行是整条链路上唯一能起进程的地方，值得两层。
3. **不提供 `env` 参数给模型。** 环境变量是注入面（`LD_PRELOAD`、`NODE_OPTIONS`…），
   模型不该能改它。
4. **输出保尾不保头。** 错误栈在尾部；头尾对半截断会把关键信息切掉。
   过长时保留「前 20 行 + 后 60 行」，两头都留一点上下文。
5. **超时强杀进程组。** POSIX 用 `killpg`，Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /T`，
   最后兜底 `proc.kill()`；杀完必须 `communicate` 回收，否则句柄泄漏。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import Tool, ToolResult
from .workspace import PathTraversalError, WorkspaceSecurity

__all__ = ["WorkspaceCommandRunner", "build_shell_tool"]

#: 允许执行的 argv[0]（小写比较，Windows 会去掉 .exe）
ARGV_ALLOWLIST = frozenset({"python", "python3", "pytest", "ruff", "pyright", "pip"})
PIP_SUBCOMMANDS = frozenset({"show", "list"})
#: 一律禁止的解释器直执行开关
_INTERPRETER_ESCAPE_FLAGS = frozenset({"-c", "--command"})
#: `python -m <module>` 允许的模块。见 interpreter_violation 的说明：
#: `-c` 与 `-m` 必须区别对待，否则要么放行任意代码，要么禁掉最常用的跑测试写法。
INTERPRETER_MODULE_ALLOWLIST = frozenset({
    "pytest", "unittest", "py_compile", "compileall", "json.tool", "pip",
})

#: 环境变量黑名单：这些是「让解释器去做别的事」的注入入口，一律剔除
_ENV_DENYLIST = frozenset({
    "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONHOME", "PYTHONUSERBASE",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "BASH_ENV", "ENV", "SHELLOPTS", "PROMPT_COMMAND",
    "NODE_OPTIONS", "PERL5OPT", "RUBYOPT",
})

_HEAD_LINES = 20
_TAIL_LINES = 60


def normalize_argv0(raw: str) -> str:
    """
    把 argv[0] 归一化成可比对的命令名：取文件名、去 `.exe`、转小写。

    **单一实现点**：命令执行器与 `planner` 的验收命令校验都用它，
    否则「规划时允许、执行时拒绝」这类不一致会很难查。
    """
    name = Path(str(raw)).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def interpreter_violation(argv0: str, rest: Sequence[str]) -> Optional[str]:
    """
    检查解释器形态是否被允许，返回违规原因（None 表示合法）。

    **这里的区分很关键**（容易一刀切切错）：
    - `python -c "任意代码"` → 拒绝。它等价于「绕过文件级审查直接执行代码」，
      模型可以用它做任何事，而审查与回滚都只针对文件。
    - `python -m pytest` → **允许**，但模块名必须在白名单内。
      `-m` 是把控制权交给一个已安装的模块，和直接跑 `pytest` 等价，
      而 `-m pytest` 恰恰是跑测试最常用的写法——一刀切禁掉 `-m` 会让自测验证没法用。
    """
    if argv0 not in ("python", "python3"):
        return None
    if "-c" in rest or "--command" in rest:
        return ("禁止 `python -c` 直执行形态（绕过文件级审查），"
                "请把代码写成工作区内的脚本文件后运行。")
    for index, token in enumerate(rest):
        if token != "-m":
            continue
        module = rest[index + 1] if index + 1 < len(rest) else ""
        if module not in INTERPRETER_MODULE_ALLOWLIST:
            return (f"`python -m` 只允许 {', '.join(sorted(INTERPRETER_MODULE_ALLOWLIST))}"
                    f"，收到 {module or '(空)'}。")
        break
    return None


def argv0_violation(raw: str) -> Optional[str]:
    """
    校验 argv[0]：必须是白名单里的**命令名**，不接受路径。返回违规原因，None 表示合法。

    为什么连路径都要拒：
    白名单的意义是「只能运行这几个程序」。如果允许 `C:\\somewhere\\python.exe`，
    那白名单约束的就不是「运行哪个程序」，而只是「文件名长什么样」——
    模型完全可以在工作区里放一个同名可执行文件把约束绕过去。
    """
    text = str(raw or "").strip()
    if not text:
        return "argv[0] 不能为空"
    if "/" in text or "\\" in text:
        return (f"argv[0] 必须是命令名而不是路径（收到 {text}）；"
                f"允许: {', '.join(sorted(ARGV_ALLOWLIST))}")
    if normalize_argv0(text) not in ARGV_ALLOWLIST:
        return f"命令不在允许列表: {text}（允许: {', '.join(sorted(ARGV_ALLOWLIST))}）"
    return None


class WorkspaceCommandRunner:
    """在工作区目录内启动 argv 子进程，捕获输出并按窗口截断。"""

    def __init__(
        self,
        workspace: WorkspaceSecurity,
        *,
        default_seconds: int = 60,
        ceiling_seconds: int = 120,
        tail_lines: int = 80,
    ) -> None:
        self.workspace = workspace
        self.default_seconds = max(1, int(default_seconds))
        self.ceiling_seconds = max(self.default_seconds, int(ceiling_seconds))
        self.tail_lines = max(10, int(tail_lines))

    # ---------------- 对外 ----------------
    def run(self, argv: Sequence[str], cwd: str = ".",
            timeout_seconds: Optional[int] = None) -> ToolResult:
        """
        执行一条命令。**不抛异常**，失败一律以 `ToolResult(ok=False, error_type=…)` 返回，
        error_type 取值为 CommandFailed / Timeout / PermissionDenied / PathDenied / ArgError。
        """
        checked = self._check_argv(argv)
        if isinstance(checked, ToolResult):
            return checked
        normalized = checked

        try:
            workdir = self.workspace.resolve(cwd or ".")
        except PathTraversalError as e:
            return ToolResult.failure(str(e), "PathDenied", str(e))
        if not workdir.is_dir():
            return ToolResult.failure(f"cwd 不是目录: {cwd}", "ArgError", f"cwd 不是目录: {cwd}")

        timeout = self._clamp_timeout(timeout_seconds)
        env = self._build_env()
        started = time.perf_counter()

        try:
            proc = subprocess.Popen(
                list(normalized),
                cwd=str(workdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **self._popen_isolation(),
            )
        except (OSError, ValueError) as e:
            message = f"无法启动命令 {' '.join(normalized)}: {type(e).__name__}: {e}"
            return ToolResult.failure(message, "CommandFailed", str(e))

        timed_out = False
        stdout = stderr = ""
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except Exception:  # noqa: BLE001 - 回收失败也不能让整个循环崩掉
                stdout, stderr = stdout or "", stderr or ""

        duration_ms = int((time.perf_counter() - started) * 1000)
        exit_code = -1 if timed_out else int(proc.returncode or 0)
        relcwd = self.workspace.relpath(workdir)

        artifacts: Dict[str, Any] = {
            "argv": list(normalized),
            "cwd": relcwd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "tail": self._tail("\n".join(part for part in (stdout, stderr) if part)),
        }
        text = self._format(normalized, relcwd, exit_code, duration_ms,
                            stdout or "", stderr or "", timed_out)

        if timed_out:
            return ToolResult.failure(
                text, "Timeout",
                f"命令超时（>{timeout}s）已被强杀: {' '.join(normalized)}", artifacts)
        if exit_code != 0:
            return ToolResult.failure(
                text, "CommandFailed",
                f"退出码 {exit_code}: {' '.join(normalized)}", artifacts)
        return ToolResult(text=text, artifacts=artifacts)

    # ---------------- 校验 ----------------
    def _check_argv(self, argv: Any) -> "List[str] | ToolResult":
        if not isinstance(argv, (list, tuple)) or not argv:
            return ToolResult.failure(
                "run_command 需要非空的 argv 数组，例如 [\"python\", \"-m\", \"pytest\", \"-q\"]。"
                "本工具不接受 shell 字符串。",
                "ArgError", "argv 缺失或不是数组")
        if not all(isinstance(item, str) and item for item in argv):
            return ToolResult.failure("argv 每一项都必须是非空字符串。", "ArgError", "argv 元素非法")

        normalized = [str(item) for item in argv]
        violation = argv0_violation(normalized[0])
        if violation is not None:
            return ToolResult.failure(violation, "PermissionDenied", violation)

        argv0 = normalize_argv0(normalized[0])
        if argv0 in ("python", "python3"):
            violation = interpreter_violation(argv0, normalized[1:])
            if violation is not None:
                return ToolResult.failure(violation, "PermissionDenied", violation)

        if argv0 == "pip":
            sub = normalized[1].lower() if len(normalized) > 1 else ""
            if sub not in PIP_SUBCOMMANDS:
                message = (f"pip 只允许 {', '.join(sorted(PIP_SUBCOMMANDS))}"
                           f"（不允许改动解释器环境）")
                return ToolResult.failure(message, "PermissionDenied", message)

        return normalized

    def _clamp_timeout(self, requested: Optional[int]) -> int:
        try:
            value = int(requested) if requested is not None else self.default_seconds
        except (TypeError, ValueError):
            value = self.default_seconds
        return max(1, min(value, self.ceiling_seconds))

    # ---------------- 进程与环境 ----------------
    @staticmethod
    def _popen_isolation() -> Dict[str, Any]:
        """让子进程自成进程组，便于超时时整棵树强杀。"""
        if os.name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    def _build_env(self) -> Dict[str, str]:
        """
        当前进程环境的副本，剔除注入类变量。

        保留 `PYTHONPATH`（项目自身导入需要它）；额外固定编码相关变量，
        避免中文输出在 Windows 上变成乱码——乱码会让修复阶段读不懂错误栈。
        """
        env = {k: v for k, v in os.environ.items() if k.upper() not in _ENV_DENYLIST}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONUTF8", "1")
        return env

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """强杀进程树。每一步都容错——杀不干净也不能把主循环带崩。"""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=5)
            else:
                import signal

                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 输出 ----------------
    def _tail(self, text: str) -> str:
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        return "\n".join(lines[-self.tail_lines:])

    def _format(self, argv: Sequence[str], relcwd: str, exit_code: int,
                duration_ms: int, stdout: str, stderr: str, timed_out: bool) -> str:
        head = [
            f"$ {' '.join(argv)}",
            f"目录: {relcwd or '.'}｜退出码: {exit_code}"
            f"｜耗时 {duration_ms / 1000:.2f}s" + ("（超时强杀）" if timed_out else ""),
        ]
        parts = ["\n".join(head)]
        if stdout.strip():
            body, cut = _window(stdout, _HEAD_LINES, _TAIL_LINES)
            parts.append(f"--- stdout{'（已按窗口截断）' if cut else ''} ---\n{body.rstrip()}")
        if stderr.strip():
            body, cut = _window(stderr, _HEAD_LINES, _TAIL_LINES)
            parts.append(f"--- stderr{'（已按窗口截断）' if cut else ''} ---\n{body.rstrip()}")
        if not stdout.strip() and not stderr.strip():
            parts.append("（命令无输出）")
        return "\n".join(parts)


def _window(text: str, head: int, tail: int) -> Tuple[str, bool]:
    """
    长输出取「前 head 行 + 后 tail 行」。

    对半截断是错的：pytest/编译器的关键信息集中在末尾（失败列表、错误栈），
    但开头的汇总行同样有价值，所以两头都留。
    """
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text, False
    marker = f"...[省略中间 {len(lines) - head - tail} 行]..."
    return "\n".join(lines[:head] + [marker] + lines[-tail:]), True


def build_shell_tool(runner: WorkspaceCommandRunner, output_limit: int = 20000) -> Tool:
    """把执行器包装成 `run_command` 工具（供 ToolRegistry 注册）。"""

    def run_command(kwargs: Dict[str, Any]) -> ToolResult:
        return runner.run(
            kwargs.get("argv"),
            cwd=str(kwargs.get("cwd") or "."),
            timeout_seconds=kwargs.get("timeout_seconds"),
        )

    return Tool(
        name="run_command",
        description=(
            "在工作区目录内执行一条命令，用于编译、跑测试与静态检查（如 pytest / ruff / pyright）。\n"
            "**只接受 argv 数组**，例如 [\"python\", \"-m\", \"pytest\", \"tests/test_x.py\", \"-q\"]；"
            "不接受 shell 字符串，也不支持管道、重定向、&& 与 cd。\n"
            "允许的可执行文件很少（python / pytest / ruff / pyright / pip show|list），"
            "其余会被拒绝；`python -c` 形态也被禁止，请写成脚本文件再运行。\n"
            "返回内容含退出码、stdout 与 stderr（长输出保留前 20 行与后 60 行）。\n"
            "注意：RestrictedPython 沙箱（run_python_code）看不到工作区文件，需要读写项目文件时必须用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令与参数数组，元素逐个传递，不经 shell",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（相对工作区根目录），默认 '.'",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "超时秒数，默认 60，上限 120",
                },
            },
            "required": ["argv"],
        },
        handler=run_command,
        output_limit=output_limit,
    )
