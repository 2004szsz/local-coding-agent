# -*- coding: utf-8 -*-
"""
RestrictedPython 安全代码执行器。

安全策略（多层纵深）：
1. 编译层：RestrictedPython.compile_restricted 重写字节码，
   访问器（getattr/import/私有属性等）全部走守卫，非法写法编译即报错；
2. 内建白名单：只暴露纯计算内建，**不提供 __import__**，
   因此 os/sys/socket/open/subprocess/eval 等系统能力完全不可达；
3. 属性守卫：safer_getattr 拒绝以下划线开头的属性（杜绝 __globals__ 逃逸等）；
4. 进程隔离：用户代码在独立子进程执行，超时由父进程 terminate 强杀，
   死循环 / 阻塞不会拖垮主服务；
5. 迭代上限：防止巨量 range / 巨型推导式耗尽内存；
6. 输出捕获：stdout / stderr / 异常全部捕获并按配置截断。

注意：这是“降低风险”的本地测试沙箱，不等价于容器级强隔离，禁止执行不可信代码。
"""
from __future__ import annotations

import builtins
import io
import multiprocessing as mp
import operator
import traceback
import warnings
from typing import Any, Dict

from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import (
    guarded_iter_unpack_sequence,
    safer_getattr,
)
from RestrictedPython.PrintCollector import PrintCollector

# RestrictedPython 8.x 中 _inplacevar_ 首参为操作符字符串（如 '+=')
_INPLACE_OPS = {
    "+=": operator.iadd, "-=": operator.isub, "*=": operator.imul,
    "/=": operator.itruediv, "//=": operator.ifloordiv, "%=": operator.imod,
    "**=": operator.ipow, "<<=": operator.ilshift, ">>=": operator.irshift,
    "&=": operator.iand, "|=": operator.ior, "^=": operator.ixor,
    "@=": operator.imatmul,
}

# 单次执行允许的最大迭代次数（防 range(10**12) 这类内存/CPU 炸弹）
_MAX_ITERATIONS = 5_000_000


# ------------------------------------------------------------------
# 以下对象必须定义在模块顶层：Windows 默认 spawn 方式启动子进程时，
# Process target 及其引用的对象需要可被 pickle / 重新导入。
# ------------------------------------------------------------------

# 允许在沙箱中使用的内建名字白名单（纯计算 / 数据结构 / 常用异常）
_ALLOWED_BUILTINS = [
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "chr", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "hash", "hex", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "oct", "ord", "pow",
    "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip",
    "True", "False", "None",
    # 基础异常类型（让用户代码可以正常 try/except/raise）
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "ZeroDivisionError", "ArithmeticError",
    "RuntimeError", "StopIteration", "NotImplementedError", "NameError",
    "OverflowError", "FloatingPointError", "LookupError", "AssertionError",
]


class _GuardedIterator:
    """带迭代次数上限的迭代器包装。"""

    __slots__ = ("_it", "_count")

    def __init__(self, iterator):
        self._it = iterator
        self._count = 0

    def __iter__(self):
        return self

    def __next__(self):
        self._count += 1
        if self._count > _MAX_ITERATIONS:
            raise RuntimeError(f"超过沙箱迭代上限 {_MAX_ITERATIONS:,} 次")
        return next(self._it)


def _guarded_getiter(obj):
    """RestrictedPython _getiter_ 守卫：一切 for/解包都经过迭代上限包装。"""
    return _GuardedIterator(iter(obj))


def _full_write_guard(obj):
    """
    _write_ 守卫：允许对已绑定名字 / 内建容器元素写入。
    （沙箱内无法取得任何系统对象，放开写入不会引入系统能力。）
    """
    return obj


def _inplace_var(op_name: str, x, y):
    """_inplacevar_ 守卫：按操作符名称执行 +=、-= 等纯运算增强赋值。"""
    op = _INPLACE_OPS.get(op_name)
    if op is None:
        raise RuntimeError(f"沙箱不支持的增强赋值操作: {op_name}")
    return op(x, y)


def _build_restricted_globals() -> Dict[str, Any]:
    """构造沙箱全局命名空间（受限内建 + RestrictedPython 守卫钩子）。"""
    safe_builtins: Dict[str, Any] = {}
    for name in _ALLOWED_BUILTINS:
        if hasattr(builtins, name):
            safe_builtins[name] = getattr(builtins, name)
    # 显式保证：即便上游白名单变化，__import__ / open 等系统能力绝不进入沙箱
    for forbidden in ("__import__", "open", "exec", "eval", "compile",
                      "globals", "locals", "input", "breakpoint", "exit", "quit"):
        safe_builtins.pop(forbidden, None)

    return {
        "__builtins__": safe_builtins,
        # RestrictedPython 守卫钩子
        "_getattr_": safer_getattr,
        "_write_": _full_write_guard,
        "_inplacevar_": _inplace_var,
        "_getiter_": _guarded_getiter,
        "_unpack_sequence_": guarded_iter_unpack_sequence,
        # print 编译后为 _print_(_getattr_)._call_print(...)，
        # 输出被 PrintCollector.txt 收集，执行后从中回收
        "_print_": PrintCollector,
        "__name__": "__sandbox__",
    }


def _sandbox_worker(code: str, queue: "mp.Queue") -> None:
    """子进程入口：在受限环境中编译并执行代码，通过 queue 回传结果。"""
    # PrintCollector 缺失读取时会产生 SyntaxWarning，沙箱场景按预期抑制
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    stderr_buf = io.StringIO()

    # 1) 受限编译（compile_restricted_exec 在 7.x / 8.x 中均可用，
    #    返回 CompileResult(code, errors, warnings, used_names)）
    try:
        compiled = compile_restricted_exec(code, "<sandbox>")
    except SyntaxError as e:
        queue.put({"stdout": "", "stderr": "",
                   "error": f"语法错误: {e.msg} (行 {e.lineno})"})
        return

    if compiled.errors:
        # 编译期安全错误（如访问下划线属性）或语法错误
        msgs = "; ".join(str(e) for e in compiled.errors)
        queue.put({"stdout": "", "stderr": "", "error": f"编译被拒绝: {msgs}"})
        return
    if compiled.code is None:
        queue.put({"stdout": "", "stderr": "", "error": "编译失败：未生成可执行代码"})
        return

    # 2) 受限执行
    sandbox_globals = _build_restricted_globals()
    try:
        exec(compiled.code, sandbox_globals)  # noqa: S102 - 已通过 RestrictedPython 受限编译
        # print 输出由 PrintCollector 收集（编译产物中保存为全局 _print 实例）
        collector = sandbox_globals.get("_print")
        stdout_text = "".join(getattr(collector, "txt", [])) if collector else ""
        queue.put({"stdout": stdout_text, "stderr": "", "error": None})
    except BaseException as e:  # noqa: BLE001 - 沙箱内任何异常都要回传而非崩溃子进程
        # import/open 等在执行期触发的安全拦截也会在这里被捕获（如 __import__ not found）
        stderr_buf.write(traceback.format_exc())
        collector = sandbox_globals.get("_print")
        stdout_text = "".join(getattr(collector, "txt", [])) if collector else ""
        queue.put({
            "stdout": stdout_text,
            "stderr": stderr_buf.getvalue(),
            "error": f"{type(e).__name__}: {e}",
        })


class RestrictedPythonExecutor:
    """对外使用的执行器：负责进程调度、超时控制与结果截断。"""

    def __init__(self, timeout_seconds: int = 10, max_result_chars: int = 20000):
        self.timeout = max(1, int(timeout_seconds))
        self.max_result_chars = max_result_chars

    def run(self, code: str) -> Dict[str, Any]:
        """
        执行一段 Python 代码。

        :return: {"stdout": str, "stderr": str, "error": str|None,
                  "timed_out": bool, "truncated": bool}
        """
        # spawn 上下文：子进程不继承父进程内存中的任何句柄/状态，隔离更彻底
        ctx = mp.get_context("spawn")
        queue: mp.Queue = ctx.Queue()
        proc = ctx.Process(target=_sandbox_worker, args=(code, queue), daemon=True)

        proc.start()
        proc.join(self.timeout)

        result: Dict[str, Any]
        if proc.is_alive():
            # 超时：强杀整个子进程树（daemon 子进程随父退出）
            proc.terminate()
            proc.join(2)
            if proc.is_alive():  # pragma: no cover - terminate 仍失败时的最后手段
                proc.kill()
                proc.join(1)
            result = {
                "stdout": "",
                "stderr": "",
                "error": f"执行超时（超过 {self.timeout} 秒），子进程已被强制终止。",
                "timed_out": True,
            }
        else:
            try:
                result = queue.get_nowait()
            except Exception:  # noqa: BLE001 - 子进程异常退出时队列可能为空
                result = {
                    "stdout": "",
                    "stderr": "",
                    "error": f"沙箱子进程异常退出，退出码 {proc.exitcode}。",
                }
            result.setdefault("timed_out", False)

        # 清空队列，避免缓冲区线程残留
        try:
            queue.close()
            queue.join_thread()
        except Exception:  # noqa: BLE001
            pass

        # 结果截断
        truncated = False
        for key in ("stdout", "stderr"):
            if len(result.get(key) or "") > self.max_result_chars:
                result[key] = result[key][:self.max_result_chars] + "\n...[输出过长已截断]"
                truncated = True
        result["truncated"] = truncated
        return result


def build_execution_tool(executor: "RestrictedPythonExecutor"):
    """构造 run_python_code 工具（供 ToolRegistry 注册）。"""
    from .base import Tool  # 局部导入，保持 executor 模块可独立被子进程加载

    def run_python_code(kwargs: dict) -> str:
        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("缺少必填参数: code（需要执行的 Python 源码字符串）")

        result = executor.run(code)
        status = "超时" if result.get("timed_out") else (
            "失败" if result.get("error") else "成功")
        lines = [f"代码执行{status}（RestrictedPython 沙箱，已禁止 import 与系统调用）:"]
        if result.get("stdout"):
            lines.append("[stdout]\n" + result["stdout"].rstrip())
        if result.get("stderr"):
            lines.append("[stderr]\n" + result["stderr"].rstrip())
        if result.get("error"):
            lines.append("[error] " + result["error"])
        if not result.get("stdout") and not result.get("error") and not result.get("stderr"):
            lines.append("（无输出，代码已执行完毕）")
        return "\n".join(lines)

    return Tool(
        name="run_python_code",
        description=(
            "在本地 RestrictedPython 隔离沙箱中执行 Python 代码用于验证（如单元测试、算法验证）。"
            "禁止 import，不能访问文件/网络/操作系统，仅支持纯 Python 计算；"
            "有执行超时限制，结果包含 stdout/stderr/异常信息。"
            "注意：该沙箱无法看到工作空间中的文件，仅用于计算验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 源码"},
            },
            "required": ["code"],
        },
        handler=run_python_code,
        output_limit=12000,
    )
