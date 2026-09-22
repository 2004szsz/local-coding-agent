# -*- coding: utf-8 -*-
"""会话管理路由：列表 / 新建 / 详情 / 重命名 / 删除。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    title: str = "新会话"


class SessionRename(BaseModel):
    title: str


@router.get("")
def list_sessions(request: Request):
    return {"sessions": request.app.state.sessions.list_sessions()}


@router.post("")
def create_session(body: SessionCreate, request: Request):
    return request.app.state.sessions.create_session(body.title)


@router.get("/{session_id}")
def get_session(session_id: str, request: Request):
    try:
        return request.app.state.sessions.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.patch("/{session_id}")
def rename_session(session_id: str, body: SessionRename, request: Request):
    try:
        return request.app.state.sessions.rename_session(session_id, body.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.delete("")
def clear_sessions(request: Request):
    """清除全部会话历史（设置页「清除全部会话」）。"""
    deleted = request.app.state.sessions.clear_all()
    return {"ok": True, "deleted": deleted}


@router.delete("/{session_id}")
def delete_session(session_id: str, request: Request):
    request.app.state.sessions.delete_session(session_id)
    return {"ok": True}
