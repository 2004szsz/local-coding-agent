# -*- coding: utf-8 -*-
"""使用统计 API：聚合对话、工具与 Token 指标（不含消息正文）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.usage_tracker import get_usage_tracker

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(request: Request):
    tracker = get_usage_tracker()
    session_count = None
    sessions = getattr(request.app.state, "sessions", None)
    if sessions is not None:
        try:
            session_count = len(sessions.list_sessions())
        except Exception:  # noqa: BLE001
            session_count = None
    return {
        "ok": True,
        **tracker.summary(session_count=session_count),
    }
