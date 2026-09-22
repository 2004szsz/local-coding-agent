# -*- coding: utf-8 -*-
"""DeepSeek 模型能力边界探针：模型名解析、参数兼容性、流式工具调用。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "logs" / "diagnostics"
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

KEY = os.environ.get("LLM_API_KEY") or ""
BASE = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
CHAT_URL = BASE + "/chat/completions"

RECORDS: List[Dict[str, Any]] = []


def rec(name: str, ok: bool, detail: Any, extra: Any = None) -> None:
    RECORDS.append({"name": name, "ok": bool(ok),
                    "detail": detail if isinstance(detail, str)
                              else json.dumps(detail, ensure_ascii=False, default=str),
                    "extra": extra})


AUTH = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "提交本轮决策",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "next_action": {"type": "string", "enum": ["tool", "respond", "replan"]},
            },
            "required": ["thought", "next_action"],
        },
    },
}]

CANDIDATES = ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro",
              "deepseek-reasoner", "deepseek-coder", "deepseek-v3"]


def probe_models() -> None:
    for model in CANDIDATES:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "回复 OK"}],
            "temperature": 0.0,
            "max_tokens": 8,
            "stream": False,
        }
        t0 = time.perf_counter()
        try:
            r = httpx.post(CHAT_URL, headers=AUTH, json=payload, timeout=60, trust_env=True)
        except Exception as e:  # noqa: BLE001
            rec(f"模型名 {model}", False, f"{type(e).__name__}: {e}")
            continue
        cost = time.perf_counter() - t0
        served, err_msg = None, None
        try:
            body = r.json()
            served = body.get("model")
            err_msg = (body.get("error") or {}).get("message")
        except Exception:  # noqa: BLE001
            pass
        rec(f"模型名 {model}", r.status_code == 200,
            f"HTTP {r.status_code} served={served} err={str(err_msg)[:150]}（{cost:.2f}s）")


def probe_params(model: str) -> None:
    """参数兼容性：temperature / 缺失 max_tokens / 大 max_tokens / 额外字段。"""
    base_msg = [{"role": "user", "content": "回复 OK"}]

    cases = {
        "temperature=0": {"model": model, "messages": base_msg, "temperature": 0.0,
                          "max_tokens": 8, "stream": False},
        "temperature=2.0": {"model": model, "messages": base_msg, "temperature": 2.0,
                            "max_tokens": 8, "stream": False},
        "temperature=2.5（越界）": {"model": model, "messages": base_msg, "temperature": 2.5,
                                    "max_tokens": 8, "stream": False},
        "省略 max_tokens": {"model": model, "messages": base_msg, "temperature": 0.0,
                            "stream": False},
        "未知字段（模拟旧代码）": {"model": model, "messages": base_msg, "temperature": 0.0,
                                   "max_tokens": 8, "stream": False, "legacy_flag": True},
        "system+user 双消息": {"model": model, "temperature": 0.0, "max_tokens": 8,
                              "stream": False,
                              "messages": [{"role": "system", "content": "你是编码助手"},
                                           {"role": "user", "content": "回复 OK"}]},
        "空 content 消息": {"model": model, "temperature": 0.0, "max_tokens": 8,
                            "stream": False,
                            "messages": [{"role": "user", "content": ""}]},
        "中文工具名": {"model": model, "temperature": 0.0, "max_tokens": 32, "stream": False,
                       "messages": [{"role": "user", "content": "提交决策"}],
                       "tools": [{"type": "function", "function": {
                           "name": "提交决策", "description": "x",
                           "parameters": {"type": "object", "properties": {}}}}],
                       "tool_choice": "auto"},
    }
    for label, payload in cases.items():
        try:
            r = httpx.post(CHAT_URL, headers=AUTH, json=payload, timeout=60, trust_env=True)
            detail = f"HTTP {r.status_code} | {r.text[:160]}"
        except Exception as e:  # noqa: BLE001
            r, detail = None, f"{type(e).__name__}: {e}"
        # 这里只记录状态，不用 ok 判定（各案期望不同）
        rec(f"参数 [{model}] {label}", True, detail)


def probe_tools(model: str) -> None:
    """工具能力：强制 tool_choice、工具+流式。"""
    # 强制 tool_choice
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "请先读取文件，next_action 用 tool"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "submit_decision"}},
        "temperature": 0.0,
        "max_tokens": 200,
        "stream": False,
    }
    try:
        r = httpx.post(CHAT_URL, headers=AUTH, json=payload, timeout=60, trust_env=True)
        body = r.json() if r.status_code == 200 else {}
        choice = ((body.get("choices") or [{}])[0])
        msg = choice.get("message") or {}
        tcs = msg.get("tool_calls") or []
        args_raw = tcs[0].get("function", {}).get("arguments") if tcs else None
        parsed_ok = False
        if args_raw:
            try:
                json.loads(args_raw)
                parsed_ok = True
            except json.JSONDecodeError:
                parsed_ok = False
        rec(f"工具 [{model}] 强制 tool_choice", r.status_code == 200 and bool(tcs),
            f"HTTP {r.status_code} names={[t.get('function', {}).get('name') for t in tcs]}",
            extra={"finish_reason": choice.get("finish_reason"),
                   "arguments_raw": args_raw, "arguments_json_valid": parsed_ok})
    except Exception as e:  # noqa: BLE001
        rec(f"工具 [{model}] 强制 tool_choice", False, f"{type(e).__name__}: {e}")

    # 工具 + 流式（state_loop/stream_chat 的路径）
    try:
        frames = 0
        acc: Dict[int, Dict[str, Any]] = {}
        finish = None
        with httpx.stream("POST", CHAT_URL, headers=AUTH,
                          json=dict(payload, stream=True), timeout=60,
                          trust_env=True) as r:
            status = r.status_code
            for raw in r.iter_lines():
                if not raw or not raw.strip().startswith("data:"):
                    continue
                data = raw.strip()[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in obj.get("choices") or []:
                    finish = ch.get("finish_reason") or finish
                    for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                        frames += 1
                        slot = acc.setdefault(tc.get("index", 0),
                                              {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        norm = [{"index": k, **v} for k, v in sorted(acc.items())]
        ok_json = all(True for _ in norm)
        for item in norm:
            try:
                json.loads(item["args"] or "{}")
            except json.JSONDecodeError:
                ok_json = False
        rec(f"工具 [{model}] 流式 tool_calls", status == 200 and frames >= 1,
            f"HTTP {status} 分片数={frames} finish_reason={finish}",
            extra={"assembled": norm, "all_args_json_valid": ok_json})
    except Exception as e:  # noqa: BLE001
        rec(f"工具 [{model}] 流式 tool_calls", False, f"{type(e).__name__}: {e}")


def probe_stream_errors() -> None:
    """流式下的错误路径：坏模型在流式请求中如何报错。"""
    try:
        with httpx.stream("POST", CHAT_URL, headers=AUTH, timeout=30, trust_env=True,
                          json={"model": "deepseek-nope", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]}) as r:
            body = r.read().decode("utf-8", errors="replace")
        rec("流式-未知模型报错", r.status_code >= 400,
            f"HTTP {r.status_code} | {body[:200]}")
    except Exception as e:  # noqa: BLE001
        rec("流式-未知模型报错", False, f"{type(e).__name__}: {e}")

    # 流式 SSE 是否以 [DONE] 结尾
    try:
        saw_done = False
        lines = 0
        with httpx.stream("POST", CHAT_URL, headers=AUTH, timeout=60, trust_env=True,
                          json={"model": "deepseek-chat", "stream": True,
                                "max_tokens": 16, "temperature": 0.0,
                                "messages": [{"role": "user", "content": "数到3"}]}) as r:
            for raw in r.iter_lines():
                lines += 1
                if raw.strip() == "data: [DONE]":
                    saw_done = True
        rec("流式-SSE 结束标记", saw_done, f"总行数={lines} saw_[DONE]={saw_done}")
    except Exception as e:  # noqa: BLE001
        rec("流式-SSE 结束标记", False, f"{type(e).__name__}: {e}")


def probe_usage_in_stream() -> None:
    """流式最后一帧是否带 usage（影响 token 计量）。"""
    try:
        frames_with_usage = 0
        last = None
        with httpx.stream("POST", CHAT_URL, headers=AUTH, timeout=60, trust_env=True,
                          json={"model": "deepseek-chat", "stream": True, "max_tokens": 16,
                                "temperature": 0.0,
                                "messages": [{"role": "user", "content": "数到3"}]}) as r:
            for raw in r.iter_lines():
                if not raw.strip().startswith("data:"):
                    continue
                data = raw.strip()[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    frames_with_usage += 1
                last = obj
        rec("流式-usage 帧", frames_with_usage >= 1,
            f"带 usage 的帧数={frames_with_usage}",
            extra={"last_frame": last})
    except Exception as e:  # noqa: BLE001
        rec("流式-usage 帧", False, f"{type(e).__name__}: {e}")


def main() -> None:
    probe_models()
    probe_params("deepseek-chat")
    probe_tools("deepseek-chat")
    if len([r for r in RECORDS if "deepseek-v4-pro" in r["name"] and "模型名" in r["name"]
            and r["ok"]]) > 0:
        probe_tools("deepseek-v4-pro")
    probe_stream_errors()
    probe_usage_in_stream()

    lines = []
    for r in RECORDS:
        lines.append(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}")
        lines.append(f"        {str(r['detail'])[:400]}")
        if r.get("extra") is not None:
            lines.append(f"        extra={json.dumps(r['extra'], ensure_ascii=False, default=str)[:700]}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "probe_models.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
