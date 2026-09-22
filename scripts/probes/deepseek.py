# -*- coding: utf-8 -*-
"""
DeepSeek 官方 API 链路探针（分层）。

L0 环境    : 代理变量、DNS 解析、base_url 归一化、密钥形态
L1 网络    : 带 / 不带系统代理环境变量时的连通性
L2 原始 HTTP: 鉴权、参数透传、工具调用、强制 tool_choice、流式 SSE、错误码
L3 项目层  : tools.api_client.LLMClient 的 health / chat / 强制 tool_choice / stream_chat

结果写入 data/logs/diagnostics/probe_ds.txt（人读）与 probe_ds.json（机读）。
密钥只从环境变量读取，不落盘。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "logs" / "diagnostics"
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

KEY = os.environ.get("LLM_API_KEY") or ""
BASE = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL") or "deepseek-chat"
CHAT_URL = BASE + "/chat/completions"

RECORDS: List[Dict[str, Any]] = []


def rec(layer: str, name: str, ok: bool, detail: Any, extra: Any = None) -> None:
    RECORDS.append({
        "layer": layer,
        "name": name,
        "ok": bool(ok),
        "detail": detail if isinstance(detail, str)
                  else json.dumps(detail, ensure_ascii=False, default=str),
        "extra": extra,
    })


def brief(resp: "httpx.Response", limit: int = 400) -> str:
    try:
        body = resp.text
    except Exception:  # noqa: BLE001
        body = "<no body>"
    return f"HTTP {resp.status_code} | {body[:limit]}"


def timed(fn):
    t0 = time.perf_counter()
    try:
        result = fn()
        return result, time.perf_counter() - t0, None
    except Exception as e:  # noqa: BLE001
        return None, time.perf_counter() - t0, f"{type(e).__name__}: {e}"


# =====================================================================
# L0 环境
# =====================================================================
def layer0() -> None:
    env_dump = {k: os.environ.get(k) for k in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    )}
    rec("L0", "代理环境变量", True, env_dump)

    host = urlsplit(BASE).hostname or ""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        rec("L0", f"DNS 解析 {host}", True, ips)
    except Exception as e:  # noqa: BLE001
        rec("L0", f"DNS 解析 {host}", False, f"{type(e).__name__}: {e}")

    rec("L0", "base_url 归一化", True, {
        "raw": os.environ.get("LLM_BASE_URL"),
        "used": BASE,
        "host": host,
        "path": urlsplit(BASE).path or "/",
        "chat_url": CHAT_URL,
    })
    rec("L0", "密钥形态", bool(KEY), {
        "present": bool(KEY),
        "length": len(KEY),
        "prefix": KEY[:6] + "..." if KEY else "",
        "has_whitespace": KEY != KEY.strip(),
    })


# =====================================================================
# L1 网络连通性
# =====================================================================
def layer1() -> None:
    hdrs = {"Authorization": f"Bearer {KEY}"}
    for label, trust in (("带代理环境变量", True), ("绕过代理环境变量", False)):
        resp, cost, err = timed(lambda t=trust: httpx.get(  # type: ignore[misc]
            BASE + "/models", headers=hdrs, timeout=15, trust_env=t))
        if err:
            rec("L1", f"GET /models（{label}）", False, f"{err}（{cost:.2f}s）")
        else:
            rec("L1", f"GET /models（{label}）", resp.status_code == 200,
                f"{brief(resp, 300)}（{cost:.2f}s）")

    # 裸 TCP 到 443，判断是「网络不通」还是「HTTP 层问题」
    host = urlsplit(BASE).hostname or ""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, 443), timeout=10):
            rec("L1", f"TCP 握手 {host}:443", True, f"ok（{time.perf_counter() - t0:.2f}s）")
    except Exception as e:  # noqa: BLE001
        rec("L1", f"TCP 握手 {host}:443", False, f"{type(e).__name__}: {e}")


# =====================================================================
# L2 原始 HTTP 协议
# =====================================================================
def _post(payload: Dict[str, Any], headers: Dict[str, str], **kw) -> "httpx.Response":
    return httpx.post(CHAT_URL, headers=headers, json=payload, timeout=60, **kw)


def layer2() -> None:
    auth = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    minimal = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "temperature": 0.0,
        "max_tokens": 16,
        "stream": False,
    }

    # --- 正常调用 + 参数透传 ---
    resp, cost, err = timed(lambda: _post(minimal, auth, trust_env=True))
    if err:
        rec("L2", "基础对话", False, f"{err}（{cost:.2f}s）")
    else:
        ok = resp.status_code == 200
        detail = brief(resp, 200)
        body = None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            pass
        rec("L2", "基础对话", ok, detail, extra={
            "content": ((body or {}).get("choices") or [{}])[0].get("message", {}).get("content"),
            "usage": (body or {}).get("usage"),
            "model_echo": (body or {}).get("model"),
            "id_prefix": str((body or {}).get("id"))[:12],
        })

    # --- 鉴权：无 key ---
    resp, cost, err = timed(lambda: _post(minimal, {"Content-Type": "application/json"}, trust_env=True))
    rec("L2", "鉴权-缺少密钥", err is None and resp is not None and resp.status_code == 401,
        err or brief(resp, 200))

    # --- 鉴权：错误 key ---
    bad = {"Authorization": "Bearer sk-invalid-key-for-probe", "Content-Type": "application/json"}
    resp, cost, err = timed(lambda: _post(minimal, bad, trust_env=True))
    rec("L2", "鉴权-错误密钥", err is None and resp is not None and resp.status_code == 401,
        err or brief(resp, 200))

    # --- 参数：不存在的模型 ---
    bad_model = dict(minimal, model="deepseek-not-exist-xyz")
    resp, cost, err = timed(lambda: _post(bad_model, auth, trust_env=True))
    rec("L2", "错误处理-未知模型", err is None and resp is not None and resp.status_code == 400,
        err or brief(resp, 200))

    # --- 参数：非法 temperature 类型 ---
    bad_temp = dict(minimal, temperature="hot")
    resp, cost, err = timed(lambda: _post(bad_temp, auth, trust_env=True))
    rec("L2", "错误处理-非法参数类型", err is None and resp is not None and resp.status_code >= 400,
        err or brief(resp, 200))

    # --- 工具调用（auto）---
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }]
    tool_msg = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "杭州今天天气怎么样？请调用工具查询。"}],
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": 128,
        "stream": False,
    }
    resp, cost, err = timed(lambda: _post(tool_msg, auth, trust_env=True))
    if err:
        rec("L2", "工具调用 auto", False, f"{err}（{cost:.2f}s）")
    else:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        msg = ((body.get("choices") or [{}])[0].get("message") or {})
        tcs = msg.get("tool_calls") or []
        rec("L2", "工具调用 auto", resp.status_code == 200 and bool(tcs),
            brief(resp, 300), extra={
                "tool_calls": tcs,
                "arguments_raw": (tcs[0].get("function", {}).get("arguments") if tcs else None),
            })

    # --- 强制 tool_choice（state_loop 的 decompose/decide 依赖它）---
    forced = dict(tool_msg, tool_choice={
        "type": "function", "function": {"name": "get_weather"}})
    resp, cost, err = timed(lambda: _post(forced, auth, trust_env=True))
    if err:
        rec("L2", "强制 tool_choice=对象", False, f"{err}（{cost:.2f}s）")
    else:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        msg = ((body.get("choices") or [{}])[0].get("message") or {})
        tcs = msg.get("tool_calls") or []
        forced_ok = [t.get("function", {}).get("name") for t in tcs]
        rec("L2", "强制 tool_choice=对象", resp.status_code == 200 and forced_ok == ["get_weather"],
            brief(resp, 300), extra={"forced_names": forced_ok,
                                     "content": msg.get("content")})

    # --- 流式 SSE ---
    def do_stream() -> Dict[str, Any]:
        chunks, tool_acc = [], {}
        with httpx.stream("POST", CHAT_URL, headers=auth,
                          json=dict(minimal, stream=True), timeout=60,
                          trust_env=True) as r:
            status = r.status_code
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for ch in obj.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        chunks.append(piece)
                    for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                        tool_acc.setdefault(tc.get("index", 0), [])
                        tool_acc[tc.get("index", 0)].append(tc)
        return {"status": status, "text": "".join(chunks), "frame_count": len(chunks),
                "tool_frames": {k: len(v) for k, v in tool_acc.items()}}

    result, cost, err = timed(do_stream)
    if err:
        rec("L2", "流式 SSE", False, f"{err}（{cost:.2f}s）")
    else:
        rec("L2", "流式 SSE", result["status"] == 200 and bool(result["text"]),
            f"frames={result['frame_count']} text={result['text'][:80]!r}（{cost:.2f}s）")

    # --- 非法 JSON body（httpx 层面直接拒绝，验证客户端侧能报错而不是静默）---
    resp, cost, err = timed(lambda: httpx.post(
        CHAT_URL, headers={**auth, "Content-Type": "application/json"},
        content=b"{not json", timeout=30, trust_env=True))
    rec("L2", "错误处理-畸形请求体", err is None and resp is not None and resp.status_code >= 400,
        err or brief(resp, 200))


# =====================================================================
# L3 项目 LLMClient
# =====================================================================
def layer3() -> None:
    from tools.api_client import LLMClient, LLMError  # noqa: PLC0415

    cfg = {"llm": {
        "provider": "deepseek",
        "base_url": BASE,
        "api_key": KEY,
        "model": MODEL,
        "temperature": 0.0,
        "max_tokens": 64,
        "timeout": 60,
        "stream": True,
    }}
    client = LLMClient(cfg)
    rec("L3", "客户端构造", True, {
        "chat_url": client.chat_url,
        "trust_env（是否走代理）": client.trust_env,
        "provider": client.provider,
        "model": client.model,
    })

    # health()
    h = client.health()
    rec("L3", "health()", h.get("ok") is True, h)

    # chat()
    try:
        msg = client.chat([{"role": "user", "content": "用一句话说明你是什么模型。"}])
        rec("L3", "chat()", bool(msg.get("content")),
            f"role={msg.get('role')} content={(msg.get('content') or '')[:100]!r}",
            extra={"keys": sorted(msg.keys())})
    except Exception as e:  # noqa: BLE001
        rec("L3", "chat()", False, f"{type(e).__name__}: {e}")

    # chat() + 强制 tool_choice（state_loop 决策协议路径）
    tools = [{
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
    try:
        msg = client.chat(
            [{"role": "user", "content": "请提交决策：你想先读文件。next_action 用 tool。"}],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_decision"}})
        tcs = msg.get("tool_calls") or []
        names = [t.get("function", {}).get("name") for t in tcs]
        raw = tcs[0].get("function", {}).get("arguments") if tcs else None
        parsed = None
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                parsed = f"<JSON 解析失败: {e}>"
        rec("L3", "chat()+强制 tool_choice", names == ["submit_decision"],
            f"names={names} content={(msg.get('content') or '')[:80]!r}",
            extra={"arguments_raw": raw, "arguments_parsed": parsed})
    except Exception as e:  # noqa: BLE001
        rec("L3", "chat()+强制 tool_choice", False, f"{type(e).__name__}: {e}")

    # 错误路径：LLMError 是否带上有用信息
    bad_cfg = {"llm": dict(cfg["llm"], api_key="sk-bad")}
    try:
        LLMClient(bad_cfg).chat([{"role": "user", "content": "hi"}])
        rec("L3", "错误路径-坏密钥", False, "没有抛异常（应抛 LLMError）")
    except LLMError as e:
        rec("L3", "错误路径-坏密钥", True, f"LLMError: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001
        rec("L3", "错误路径-坏密钥", False, f"抛了非预期异常 {type(e).__name__}: {e}")

    # stream_chat()
    async def drain() -> List[Dict[str, Any]]:
        out = []
        async for event in client.stream_chat(
                [{"role": "user", "content": "从 1 数到 5，只输出数字。"}]):
            out.append(event)
        return out

    try:
        events = asyncio.run(drain())
        kinds = {}
        text = []
        for e in events:
            kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
            if e.get("type") == "token":
                text.append(e.get("data", {}).get("content", ""))
        rec("L3", "stream_chat()", kinds.get("done", 0) >= 1 and bool("".join(text)),
            f"事件分布={kinds} 文本={''.join(text)[:80]!r}")
    except Exception as e:  # noqa: BLE001
        rec("L3", "stream_chat()", False, f"{type(e).__name__}: {e}")


def main() -> None:
    layer0()
    layer1()
    layer2()
    layer3()

    lines = []
    for r in RECORDS:
        flag = "PASS" if r["ok"] else "FAIL"
        lines.append(f"[{flag}] [{r['layer']}] {r['name']}")
        lines.append(f"        {str(r['detail'])[:400]}")
        if r.get("extra") is not None:
            lines.append(f"        extra={json.dumps(r['extra'], ensure_ascii=False, default=str)[:600]}")
    passed = sum(1 for r in RECORDS if r["ok"])
    lines.append("")
    lines.append(f"TOTAL {passed}/{len(RECORDS)} passed")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "probe_ds.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(OUTPUT_DIR / "probe_ds.json", "w", encoding="utf-8") as f:
        json.dump(RECORDS, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
