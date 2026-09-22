# -*- coding: utf-8 -*-
"""
DeepSeek 端到端链路验证驱动器。

走**真实协议路径**：不注入假决策器，模型请求经 HTTP 打到 DeepSeek 官方 API，
再穿过状态机、权限、工具、Journal、验收门，最后回到 SSE 事件流。

验证目标：需求拆解 → 项目规划 → 读代码文件 → 执行终端命令 → 运行检验自测 → 迭代 → 收尾。

环境变量：
    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL   —— 模型接入（不落盘）
    AGENT_FRAMEWORK=state_loop               —— 使用自研状态驱动主循环

产物：data/logs/diagnostics/e2e_{TAG}_result.json（机读）+ e2e_{TAG}_trace.txt（人读）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

WORKSPACE = str(PROJECT_ROOT / "workspaces" / "e2e")
RUN_DIR = str(PROJECT_ROOT / "data" / ".e2e_run")
OUTPUT_DIR = PROJECT_ROOT / "data" / "logs" / "diagnostics"
TAG = os.environ.get("E2E_TAG", "a")
PROMPT = os.environ.get("E2E_PROMPT") or (
    "calc.py 里的 add 函数算错了（返回值不对），请修复它，"
    "然后运行测试确认 4 个用例全部通过。")

#: 每次运行都把待修项目复位，保证两个场景的起点完全一致
BUGGY_CALC = '''# -*- coding: utf-8 -*-
"""用于端到端验证的示例模块：`add` 故意写错（减法）。"""


def add(a, b):
    """返回两数之和。"""
    return a - b          # BUG: 应为 a + b


def mul(a, b):
    """返回两数之积。"""
    return a * b
'''


def reset_workspace() -> None:
    """把 calc.py 复位成有 bug 的版本，并清掉 pycache。"""
    import shutil as _shutil
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(os.path.join(WORKSPACE, "calc.py"), "w", encoding="utf-8") as f:
        f.write(BUGGY_CALC)
    _shutil.rmtree(os.path.join(WORKSPACE, "__pycache__"), ignore_errors=True)

EVENTS: List[Dict[str, Any]] = []
TRACE: List[str] = []
RESULT: Dict[str, Any] = {}


def say(line: str = "") -> None:
    TRACE.append(line)


def record(kind: str, data: Dict[str, Any]) -> None:
    EVENTS.append({"kind": kind, "data": data})


def _short(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _render_event(index: int, kind: str, data: Dict[str, Any]) -> None:
    """把一条 SSE 事件渲染成一行可读轨迹。"""
    if kind == "token":
        return                                    # token 太多，单独汇总
    if kind == "plan":
        say(f"[{index:03d}] plan  摘要={data.get('summary')}")
        for task in data.get("tasks") or []:
            say(f"        - {task.get('id')} {task.get('title')}｜scope={task.get('scope')}"
                f"｜commands={task.get('commands')}")
        return
    if kind == "thought":
        say(f"[{index:03d}] 思考  {_short(data.get('content'), 200)}")
        return
    if kind == "tool_call":
        say(f"[{index:03d}] 调用  {data.get('name')}（{data.get('effect')}）"
            f" args={_short(data.get('arguments'), 300)}")
        return
    if kind == "tool_result":
        flag = "失败" if data.get("is_error") else "成功"
        say(f"[{index:03d}] 结果  {data.get('name')} [{flag}]"
            f" {data.get('duration_ms')}ms kind={data.get('failure_kind') or '-'}"
            f" out={_short(data.get('output'), 240)}")
        return
    if kind == "task":
        say(f"[{index:03d}] 任务  {data.get('id')} → {data.get('status')}"
            f"（attempts={data.get('attempts')}）")
        return
    if kind == "verify":
        say(f"[{index:03d}] 验收  {data.get('command')} ok={data.get('ok')}"
            f" exit={data.get('exit_code')}")
        return
    if kind == "rollback":
        say(f"[{index:03d}] 回滚  任务={data.get('task')} 文件={data.get('paths')}")
        return
    if kind == "permission_request":
        say(f"[{index:03d}] 审批请求  {_short(data, 200)}")
        return
    if kind == "error":
        say(f"[{index:03d}] 错误  {_short(data.get('message'), 300)}")
        return
    if kind == "done":
        say(f"[{index:03d}] 结束  {_short(data, 200)}")
        return
    say(f"[{index:03d}] {kind}  {_short(data, 200)}")


async def main() -> None:
    from app.config import load_config

    reset_workspace()
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(os.path.join(RUN_DIR, "sessions"), exist_ok=True)

    cfg = load_config()
    cfg["server"]["workspace_root"] = WORKSPACE
    cfg["rag"]["enabled"] = False                     # 端到端不掺 RAG，变量更少
    cfg["storage"]["history_db"] = os.path.join(RUN_DIR, "history.db")
    cfg["storage"]["sessions_dir"] = os.path.join(RUN_DIR, "sessions")
    cfg["llm"]["temperature"] = 0.0

    RESULT["llm"] = {
        "base_url": cfg["llm"]["base_url"],
        "model": cfg["llm"]["model"],
        "provider": cfg["llm"]["provider"],
        "temperature": cfg["llm"]["temperature"],
        "max_tokens": cfg["llm"]["max_tokens"],
        "timeout": cfg["llm"]["timeout"],
        "api_key_present": bool(cfg["llm"]["api_key"]),
    }

    from agents.agent import build_runtime

    t0 = time.perf_counter()
    runtime = build_runtime(cfg)
    RESULT["framework"] = runtime.framework_name
    RESULT["tools"] = list(runtime.tools.names())
    RESULT["skills"] = list(runtime.skill_names)
    RESULT["rag_enabled"] = runtime.rag_enabled
    RESULT["build_seconds"] = round(time.perf_counter() - t0, 2)
    say(f"框架={runtime.framework_name}｜技能={runtime.skill_names}")
    say(f"工具={runtime.tools.names()}")
    if runtime.loop_deps is not None:
        limits = runtime.loop_deps.config
        RESULT["limits"] = {
            "max_steps": limits.max_steps,
            "max_model_calls": limits.max_model_calls,
            "max_attempts": limits.max_attempts,
            "max_replan": limits.max_replan,
            "context_char_budget": limits.context_char_budget,
        }
        RESULT["exec_mode"] = str(runtime.loop_deps.mode)
    say(f"执行档={getattr(runtime.loop_deps, 'mode', None)}｜"
        f"预算={getattr(runtime.loop_deps.config, 'context_char_budget', None)}")

    history = [{"role": "user", "content": PROMPT}]
    say(f"用户需求：{PROMPT}")
    say("")

    tokens: List[str] = []
    t1 = time.perf_counter()
    try:
        async for event in runtime.agent.astream_run(history):
            kind = str(event.get("type") or "")
            data = event.get("data") or {}
            record(kind, data)
            _render_event(len(EVENTS), kind, data)
            if kind == "token":
                tokens.append(str(data.get("content") or ""))
    except Exception as e:  # noqa: BLE001
        RESULT["raised"] = f"{type(e).__name__}: {e}"
        say(f"!! 未捕获异常逃出 astream_run: {type(e).__name__}: {e}")

    RESULT["run_seconds"] = round(time.perf_counter() - t1, 2)
    RESULT["event_kinds"] = {k: sum(1 for e in EVENTS if e["kind"] == k)
                             for k in {e["kind"] for e in EVENTS}}
    RESULT["final_text"] = "".join(tokens)

    say("")
    say("=== 收尾文本 ===")
    say(RESULT["final_text"])

    # 断言的原始事实：磁盘与验收命令的真实状态
    calc_path = os.path.join(WORKSPACE, "calc.py")
    with open(calc_path, "r", encoding="utf-8") as f:
        RESULT["calc_after"] = f.read()

    import subprocess
    proc = subprocess.run([sys.executable, "-m", "unittest", "test_calc", "-v"],
                          cwd=WORKSPACE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    RESULT["acceptance_exit_code"] = proc.returncode
    RESULT["acceptance_output"] = (proc.stdout or "") + (proc.stderr or "")

    runtime.shutdown()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / f"e2e_{TAG}_result.json", "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_DIR / f"e2e_{TAG}_events.json", "w", encoding="utf-8") as f:
        json.dump(EVENTS, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_DIR / f"e2e_{TAG}_trace.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(TRACE))


if __name__ == "__main__":
    asyncio.run(main())
