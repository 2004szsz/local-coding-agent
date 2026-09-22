# -*- coding: utf-8 -*-
"""
手写原生轻量版 ReAct Agent。

特点：
- 零额外框架依赖（只使用统一 LLMClient + ToolRegistry），资源占用最低、启动最快；
- 优先使用模型原生 Function Call；当模型不支持 / 未触发工具调用时，
  提供「文本动作协议」兜底：模型输出 JSON 代码块即可驱动工具；
- 最终回复分片输出，前端同样获得逐字流式体验。

文本动作协议（兜底），模型可输出：
    ```json
    {"name": "read_file", "arguments": {"path": "main.py"}}
    ```
    或 Action: {"name": "...", "arguments": {...}}
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from tools.api_client import LLMError

from . import events as ev
from .base import AgentDependencies, BaseAgent
from .gate import authorize_tool, loop_confirm_handler, loop_mode, loop_workspace

# 文本动作协议的两种识别正则
_ACTION_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_ACTION_LINE = re.compile(r"(?:Action|工具调用)\s*[:：]\s*(\{.*?\})\s*(?:\n|$)", re.DOTALL)


def _extract_text_action(content: str) -> Optional[Dict[str, Any]]:
    """从模型自由文本中解析出一个工具动作字典。"""
    for pattern in (_ACTION_BLOCK, _ACTION_LINE):
        match = pattern.search(content)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        # 兼容多种字段命名
        name = data.get("name") or data.get("tool") or data.get("action")
        args = data.get("arguments", data.get("input", data.get("args", {})))
        if name:
            return {"name": str(name), "arguments": args if isinstance(args, (dict, str)) else {}}
    return None


async def _pseudo_stream(text: str):
    """把非流式得到的最终回复切成小片产出，模拟逐字效果。"""
    if not text:
        return
    step = max(2, len(text) // 250)
    for i in range(0, len(text), step):
        yield text[i:i + step]
        await asyncio.sleep(0.01)


class NativeReActAgent(BaseAgent):
    framework_name = "native_react"

    async def _run_gated(self, call_id: str, name: str, arguments: Any
                         ) -> Tuple[str, List[Dict[str, Any]]]:
        """执行前过审批闸。返回 (正文, 需要先发给前端的事件)。"""
        deps = self.deps
        decision = await authorize_tool(
            name, arguments,
            registry=deps.tools,
            workspace=loop_workspace(deps),
            confirm_handler=loop_confirm_handler(deps),
            mode=loop_mode(deps),
            call_id=call_id,
        )
        events = list(decision.events)
        if not decision.allowed:
            return decision.output, events
        output = await asyncio.to_thread(deps.tools.execute, name, arguments)
        return output, events

    async def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        deps = self.deps
        messages = self.build_messages(history)
        tools_spec = deps.tools.openai_tools_specs()

        for _ in range(deps.max_iterations + 1):
            # 同步 LLM 调用放到线程，避免阻塞事件循环
            try:
                response = await asyncio.to_thread(deps.llm.chat, messages, tools_spec)
            except LLMError as e:
                yield ev.make_event(ev.ERROR, {"message": str(e)})
                return

            from app.reasoning import emit_llm_call_side_events
            for side in emit_llm_call_side_events(
                    getattr(deps.llm, "last_call_meta", {}), ev.make_event):
                yield side

            content = response.get("content") or ""
            native_calls = response.get("tool_calls") or []

            # -------- 路径 1：原生 Function Call --------
            if native_calls:
                assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
                normalized: List[Dict[str, Any]] = []
                for tc in native_calls:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = raw_args
                    call = {
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "arguments": args,
                    }
                    normalized.append(call)
                    yield ev.make_event(ev.TOOL_CALL, {
                        "id": call["id"], "name": call["name"], "arguments": call["arguments"]})

                assistant_msg["tool_calls"] = [{
                    "id": c["id"], "type": "function",
                    "function": {"name": c["name"],
                                 "arguments": c["arguments"] if isinstance(c["arguments"], str)
                                 else json.dumps(c["arguments"], ensure_ascii=False)},
                } for c in normalized]
                messages.append(assistant_msg)

                for call in normalized:
                    output, extra = await self._run_gated(
                        call["id"], call["name"], call["arguments"])
                    for event in extra:
                        yield event
                    yield ev.make_event(ev.TOOL_RESULT, {
                        "id": call["id"], "name": call["name"], "output": output,
                        "is_error": output.startswith("[工具错误]"),
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "name": call["name"], "content": output,
                    })
                continue

            # -------- 路径 2：文本动作协议兜底 --------
            text_action = _extract_text_action(content)
            if text_action:
                # 动作之前的文字视为思考过程
                thought = _ACTION_BLOCK.split(content)[0].strip()
                if thought:
                    yield ev.make_event(ev.THOUGHT, {"content": thought[:800]})
                messages.append({"role": "assistant", "content": content})

                call_id = f"call_{uuid.uuid4().hex[:8]}"
                yield ev.make_event(ev.TOOL_CALL, {
                    "id": call_id, "name": text_action["name"],
                    "arguments": text_action["arguments"]})
                output, extra = await self._run_gated(
                    call_id, text_action["name"], text_action["arguments"])
                for event in extra:
                    yield event
                yield ev.make_event(ev.TOOL_RESULT, {
                    "id": call_id, "name": text_action["name"], "output": output,
                    "is_error": output.startswith("[工具错误]"),
                })
                messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "name": text_action["name"], "content": output,
                })
                continue

            # -------- 路径 3：最终自然语言回复（分片流式） --------
            async for piece in _pseudo_stream(content):
                yield ev.make_event(ev.TOKEN, {"content": piece})
            yield ev.make_event(ev.DONE, {"finish_reason": "stop"})
            return

        # 超过最大轮数仍在调用工具
        yield ev.make_event(ev.ERROR, {
            "message": f"已达到最大工具调用轮数（{deps.max_iterations} 轮）仍未完成。"})
