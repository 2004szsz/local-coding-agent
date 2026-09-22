# -*- coding: utf-8 -*-
"""
提示词模板后台匹配 API：状态拉取 + SSE 推送。
前台仅订阅，不做模型探测或手动族选择。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/prompt-match", tags=["prompt-match"])


def _daemon(request: Request):
    daemon = getattr(request.app.state, "prompt_match_daemon", None)
    if daemon is None:
        raise HTTPException(status_code=503, detail="PromptMatchDaemon 未初始化")
    return daemon


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/status")
async def get_status(request: Request):
    daemon = _daemon(request)
    status = daemon.store.get_status()
    return {"ok": True, "status": status, "seq": daemon.store.seq}


@router.get("/stream")
async def match_stream(request: Request):
    daemon = _daemon(request)
    store = daemon.store

    async def event_generator():
        queue = store.subscribe()
        try:
            status = store.get_status()
            yield _sse({"type": "status", "status": status, "seq": store.seq})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse({
                        "type": "status",
                        "status": msg["status"],
                        "seq": msg["seq"],
                    })
                except asyncio.TimeoutError:
                    yield _sse({"type": "heartbeat"})
        finally:
            store.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
