# -*- coding: utf-8 -*-
"""
聊天路由：SSE 流式对话主通道。

POST /api/chat/stream
    请求: {"session_id": "可选", "message": "用户输入"}
    响应: text/event-stream，data 帧为统一事件 JSON：
        session    会话信息（新建时下发 session_id）
        token      AI 文本增量
        thought    ReAct 思考过程
        tool_call  工具调用开始
        tool_result工具调用结果
        done       本轮结束
        error      错误信息
        saved      助手消息已持久化（含最新会话标题）

即使浏览器中途断开，finally 也会把已生成的内容与工具记录落盘。
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents import events as ev
from app.usage_tracker import get_usage_tracker, merge_usage_dict

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    reasoning_level: Optional[str] = None


def _sse(event: dict) -> str:
    """序列化一个 SSE data 帧（不转义中文，按 UTF-8 输出）。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    state = request.app.state
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")

    # 定位 / 新建会话
    session_id = body.session_id
    if session_id:
        try:
            state.sessions.get_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="会话不存在，请刷新会话列表")
    else:
        session_id = state.sessions.create_session()["id"]

    # 用户消息先落盘，再取出含本轮输入的完整 LLM 历史
    state.sessions.append_message(session_id, "user", message)
    history = state.sessions.get_llm_messages(session_id)
    agent = state.agent

    prev_reasoning = None
    if body.reasoning_level and hasattr(state.llm, "reasoning_level"):
        prev_reasoning = state.llm.reasoning_level
        state.llm.reasoning_level = body.reasoning_level

    answer_parts: list[str] = []
    tool_event_map: dict[str, dict] = {}
    tool_order: list[str] = []
    turn_usage: dict = {}
    turn_latency_ms = 0
    turn_llm_calls = 0
    active_reasoning = (
        body.reasoning_level
        or getattr(state.llm, "reasoning_level", None)
        or "medium"
    )

    async def event_generator():
        nonlocal turn_usage, turn_latency_ms, turn_llm_calls
        try:
            yield _sse(ev.make_event("session", {"session_id": session_id}))

            async for event in agent.astream_run(history):
                etype, edata = event.get("type"), event.get("data", {})

                if etype == ev.TOKEN:
                    answer_parts.append(edata.get("content", ""))
                elif etype == ev.STATUS and edata.get("usage"):
                    turn_llm_calls += 1
                    turn_usage = merge_usage_dict(turn_usage, edata.get("usage") or {})
                    if edata.get("latency_ms") is not None:
                        turn_latency_ms += int(edata["latency_ms"])
                elif etype == ev.DONE:
                    if edata.get("usage"):
                        turn_usage = merge_usage_dict(turn_usage, edata.get("usage") or {})
                    if edata.get("latency_ms") is not None:
                        turn_latency_ms = max(turn_latency_ms, int(edata["latency_ms"]))
                elif etype == ev.TOOL_CALL:
                    record = {
                        "tool_call_id": edata.get("id"),
                        "name": edata.get("name", "unknown"),
                        "arguments": edata.get("arguments", {}),
                        "output": "",
                        "is_error": False,
                    }
                    tool_event_map[record["tool_call_id"]] = record
                    tool_order.append(record["tool_call_id"])
                elif etype == ev.TOOL_RESULT:
                    rec = tool_event_map.get(edata.get("id"))
                    if rec:
                        rec["output"] = edata.get("output", "")
                        rec["is_error"] = edata.get("is_error", False)

                yield _sse(event)
        except GeneratorExit:  # 浏览器主动断开
            raise
        except Exception as e:  # noqa: BLE001 - 兜底，错误也要透传给前端
            yield _sse(ev.make_event(ev.ERROR, {"message": f"{type(e).__name__}: {e}"}))
        finally:
            if prev_reasoning is not None and hasattr(state.llm, "reasoning_level"):
                state.llm.reasoning_level = prev_reasoning
            # 无论正常结束还是中途断开，已生成内容一律持久化
            content = "".join(answer_parts)
            tool_events = [tool_event_map[cid] for cid in tool_order]
            if content.strip() or tool_events:
                state.sessions.append_message(
                    session_id, "assistant", content, tool_events)
            try:
                get_usage_tracker().record_chat_turn(
                    reasoning_level=str(active_reasoning),
                    framework=str(getattr(state, "framework_name", "")),
                    usage=turn_usage,
                    latency_ms=turn_latency_ms,
                    tool_calls=len(tool_order),
                    llm_calls=turn_llm_calls,
                )
            except Exception:  # noqa: BLE001 - 统计失败不阻断对话
                pass

        # 回传保存确认（含可能更新过的标题），供前端刷新侧栏
        session_meta = state.sessions.get_session(session_id)
        yield _sse(ev.make_event("saved", {
            "session_id": session_id,
            "title": session_meta.get("title", "新会话"),
        }))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 禁用反向代理缓冲，保证逐字到达
        },
    )
