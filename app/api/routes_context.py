# -*- coding: utf-8 -*-
"""
IDE 上下文后台 API：快照拉取、SSE 推送、编辑器/终端/Linter 上报。
前台零 UI；仅 ContextBridge / ContextClient 调用。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/context", tags=["context"])


class EditorState(BaseModel):
    path: str = ""
    content: str = ""
    line: int = Field(default=1, ge=1)
    col: int = Field(default=1, ge=1)
    selection: str = ""


class EditRecord(BaseModel):
    path: str
    summary: str = ""


class LinterPayload(BaseModel):
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class TerminalPayload(BaseModel):
    output: str = ""
    lines: Optional[List[str]] = None
    line: Optional[str] = None


def _daemon(request: Request):
    daemon = getattr(request.app.state, "context_daemon", None)
    if daemon is None:
        raise HTTPException(status_code=503, detail="ContextDaemon 未初始化")
    return daemon


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/snapshot")
async def get_snapshot(request: Request):
    daemon = _daemon(request)
    snap = daemon.store.get_snapshot()
    return {"ok": True, "snapshot": snap, "seq": snap.get("seq", 0)}


@router.get("/stream")
async def context_stream(request: Request):
    daemon = _daemon(request)
    store = daemon.store

    async def event_generator():
        queue = store.subscribe()
        try:
            snap = store.get_snapshot()
            yield _sse({"type": "snapshot", "snapshot": snap, "seq": snap.get("seq", 0)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse({
                        "type": "snapshot",
                        "snapshot": msg["snapshot"],
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


@router.post("/editor")
async def post_editor(body: EditorState, request: Request):
    daemon = _daemon(request)
    await daemon.store.set_editor(
        body.path, body.content, body.line, body.col, body.selection,
    )
    return {"ok": True, "seq": daemon.store.seq}


@router.post("/edit")
async def post_edit(body: EditRecord, request: Request):
    daemon = _daemon(request)
    await daemon.store.record_edit(body.path, body.summary)
    return {"ok": True, "seq": daemon.store.seq}


@router.post("/linter")
async def post_linter(body: LinterPayload, request: Request):
    daemon = _daemon(request)
    await daemon.store.set_linter_errors(body.errors)
    return {"ok": True, "seq": daemon.store.seq}


@router.post("/terminal")
async def post_terminal(body: TerminalPayload, request: Request):
    daemon = _daemon(request)
    if body.line:
        await daemon.store.append_terminal_line(body.line)
    elif body.lines:
        await daemon.store.set_terminal_output(body.lines)
    else:
        await daemon.store.set_terminal_output(body.output)
    return {"ok": True, "seq": daemon.store.seq}
