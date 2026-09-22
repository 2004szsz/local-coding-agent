# -*- coding: utf-8 -*-
"""
统一 LLM 客户端。

对所有框架屏蔽底层模型差异：只要是 OpenAI 兼容的 /chat/completions 接口即可，
已适配 Ollama（http://127.0.0.1:11434/v1）、智谱 GLM（/api/paas/v4）、
OpenAI 及各类兼容网关。

能力：
- chat()        同步非流式调用（供轻量 ReAct 等同步框架使用）
- stream_chat() 异步流式调用（供 Agent 主循环 / FastAPI SSE 使用），
                 逐 token 产出，并正确累积分片下发的 tool_calls
- health()      连通性探活
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import httpx

from agents import events as ev

from .net import trust_env_for


class LLMError(Exception):
    """LLM 调用异常（网络 / HTTP / 协议错误）。"""


class LLMClient:
    def __init__(self, cfg: Dict[str, Any]):
        llm_cfg = cfg["llm"]
        self.base_url = llm_cfg["base_url"].rstrip("/")
        self.chat_url = self.base_url + "/chat/completions"
        self.api_key = llm_cfg.get("api_key", "ollama")
        self.model = llm_cfg["model"]
        self.temperature = float(llm_cfg.get("temperature", 0.2))
        self.max_tokens = int(llm_cfg.get("max_tokens", 4096))
        self.timeout = float(llm_cfg.get("timeout", 120))
        self.provider = llm_cfg.get("provider", "custom")
        reg = llm_cfg.get("registry") or {}
        self._reasoning_level = llm_cfg.get("reasoning_level") or reg.get("reasoning_level") or "medium"
        self._reasoning_vendor = reg.get("vendor") or self.provider
        self._reasoning_param_map = reg.get("reasoning_param_map") or {}
        self._reasoning_levels = reg.get("reasoning_levels") or ["none"]
        self.last_call_meta: Dict[str, Any] = {}
        # 本机模型（默认 Ollama 跑在 127.0.0.1）**必须绕过系统代理**：
        # 否则请求会被送进代理并回 502，表现为「Ollama 开着却连不上」。见 tools/net.py。
        self.trust_env = trust_env_for(self.base_url)

    @property
    def reasoning_level(self) -> str:
        return self._reasoning_level

    @reasoning_level.setter
    def reasoning_level(self, value: str) -> None:
        from app.reasoning import clamp_level
        self._reasoning_level = clamp_level(value or "medium", self._reasoning_levels)

    def sync_registry(self, llm_cfg: Dict[str, Any]) -> None:
        """热切换模型后同步思考强度上下文。"""
        reg = llm_cfg.get("registry") or {}
        self._reasoning_level = llm_cfg.get("reasoning_level") or reg.get("reasoning_level") or "medium"
        self._reasoning_vendor = reg.get("vendor") or llm_cfg.get("provider", self.provider)
        self._reasoning_param_map = reg.get("reasoning_param_map") or {}
        self._reasoning_levels = reg.get("reasoning_levels") or ["none"]

    def reasoning_extra(self) -> Dict[str, Any]:
        """当前思考强度对应的厂商/API 额外参数字段（供可选框架复用）。"""
        from app.reasoning import resolve_param_map
        return resolve_param_map(
            self._reasoning_level,
            self._reasoning_vendor,
            self._reasoning_param_map,
        )

    def _record_call_meta(self, message: Dict[str, Any], usage_raw: Any,
                          t0: float) -> None:
        from app.reasoning import extract_reasoning_text, normalize_usage
        self.last_call_meta = {
            "reasoning": extract_reasoning_text(message),
            "usage": normalize_usage(usage_raw),
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "reasoning_level": self._reasoning_level,
        }

    # ---------------- 公共 ----------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 stream: bool = False,
                 tool_choice: Optional[Union[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if tools:
            # 原生 Function Call：工具 schema 直接下发给模型
            payload["tools"] = tools
            # 默认 "auto"；传 {"type":"function","function":{"name":...}} 可强制指定（见 force_tool）
            payload["tool_choice"] = tool_choice or "auto"
        from app.reasoning import merge_into_payload
        return merge_into_payload(
            payload,
            self._reasoning_level,
            vendor=self._reasoning_vendor,
            model_map=self._reasoning_param_map,
        )

    def health(self) -> Dict[str, Any]:
        """探活：GET /models，返回 {ok, detail}。"""
        try:
            resp = httpx.get(self.base_url + "/models",
                             headers=self._headers(), timeout=10,
                             trust_env=self.trust_env)
            if resp.status_code == 200:
                return {"ok": True, "detail": f"{self.provider} 连接正常，模型 {self.model}"}
            return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    # ---------------- 同步非流式 ----------------
    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: Optional[Union[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        非流式调用。

        :param tool_choice: 传 "auto"（默认）由模型自行决定是否调用工具；
                            传 {"type":"function","function":{"name":X}} 则**强制**模型只产出该函数调用，
                            用于结构化决策协议（见 agents/state_loop/decisions.py）。
        :return: OpenAI 风格 message dict，形如
                 {"role": "assistant", "content": "...",
                  "tool_calls": [{"id":..., "function": {"name","arguments(JSON字符串)"}}]}
        """
        t0 = time.perf_counter()
        try:
            resp = httpx.post(self.chat_url, headers=self._headers(),
                              json=self._payload(messages, tools, stream=False,
                                                 tool_choice=tool_choice),
                              timeout=self.timeout,
                              trust_env=self.trust_env)
        except httpx.HTTPError as e:
            raise LLMError(f"请求 LLM 失败: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"LLM 返回 HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 返回中没有 choices: {json.dumps(data, ensure_ascii=False)[:300]}")
        message = choices[0].get("message") or {}
        self._record_call_meta(message, data.get("usage"), t0)
        return message

    # ---------------- 异步流式 ----------------
    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        异步流式调用，产出归一化事件：
            {"type": "token", "content": 文本增量}
            {"type": "tool_calls", "tool_calls": [归一化工具调用]}
            {"type": "done", "finish_reason": str}
            {"type": "error", "message": str}

        归一化工具调用结构：
            {"id", "name", "arguments": {dict}（解析失败时为原始字符串）}
        """
        payload = self._payload(messages, tools, stream=True)
        # tool_calls 分片累积：服务端可能分多次下发同一 index 的参数片段
        acc: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        usage_raw: Any = None
        t0 = time.perf_counter()

        try:
            async with httpx.AsyncClient(timeout=self.timeout,
                                         trust_env=self.trust_env) as client:
                async with client.stream("POST", self.chat_url,
                                         headers=self._headers(),
                                         json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        yield ev.make_event(
                            ev.ERROR,
                            {"message": f"LLM 返回 HTTP {resp.status_code}: {body[:500]}"})
                        return

                    async for raw_line in resp.aiter_lines():
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue  # 容错：跳过非 JSON 心跳帧

                        if obj.get("usage"):
                            usage_raw = obj.get("usage")

                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}

                        for rkey in ("reasoning_content", "reasoning"):
                            rpiece = delta.get(rkey)
                            if rpiece:
                                yield ev.make_event(ev.THOUGHT, {
                                    "content": rpiece,
                                    "source": "model",
                                })

                        piece = delta.get("content")
                        if piece:
                            yield ev.make_event(ev.TOKEN, {"content": piece})

                        # 工具调用分片累积
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
        except httpx.HTTPError as e:
            yield ev.make_event(ev.ERROR, {"message": f"流式请求 LLM 失败: {e}"})
            return

        # 流结束：解析完整工具调用
        if acc:
            normalized = []
            for idx in sorted(acc):
                slot = acc[idx]
                raw_args = slot["arguments"] or "{}"
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError:
                    arguments = raw_args  # 保留原文，由上层做容错处理
                normalized.append({
                    "id": slot["id"] or f"call_{idx}",
                    "name": slot["name"],
                    "arguments": arguments,
                })
            yield ev.make_event(ev.TOOL_CALL, {"tool_calls": normalized})

        done_data: Dict[str, Any] = {"finish_reason": finish_reason}
        from app.reasoning import normalize_usage
        usage = normalize_usage(usage_raw)
        if usage:
            done_data["usage"] = usage
        done_data["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        done_data["reasoning_level"] = self._reasoning_level
        yield ev.make_event(ev.DONE, done_data)
