# -*- coding: utf-8 -*-
"""
文件操作路由（前端文件浏览器使用）。

所有路径同样经过 WorkspaceSecurity 校验；
写 / 编辑 / 搜索直接复用统一工具集中的实现，保证 HTTP 与 Agent 行为一致。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tools.workspace import PathTraversalError

router = APIRouter(prefix="/api/files", tags=["files"])


class WriteRequest(BaseModel):
    path: str
    content: str
    create: bool = True
    overwrite: bool = True


class EditRequest(BaseModel):
    path: str
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    start_line: int | None = None
    end_line: int | None = None
    new_content: str | None = None


@router.get("/list")
def list_dir(request: Request, path: str = "."):
    ws = request.app.state.workspace
    try:
        return {"path": ws.relpath(ws.resolve(path)), "items": ws.list_dir(path)}
    except PathTraversalError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except (NotADirectoryError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/read")
def read_file(request: Request, path: str):
    ws = request.app.state.workspace
    try:
        resolved = ws.resolve(path)
        content = ws.read_text(resolved)
        return {"path": ws.relpath(resolved), "content": content,
                "size": resolved.stat().st_size}
    except PathTraversalError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except (FileNotFoundError, IsADirectoryError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/write")
def write_file(body: WriteRequest, request: Request):
    ws = request.app.state.workspace
    try:
        p = ws.write_text(body.path, body.content, create=True)
        if not body.overwrite and p.exists():
            raise HTTPException(status_code=409, detail="文件已存在且 overwrite=false")
        return {"ok": True, "path": ws.relpath(p), "chars": len(body.content)}
    except PathTraversalError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/edit")
def edit_file(body: EditRequest, request: Request):
    # 直接复用 Agent 同款 edit_file 工具，保证编辑语义与安全校验完全一致
    payload = body.model_dump(exclude_none=True)
    result = request.app.state.tools.execute("edit_file", payload)
    if result.startswith("[工具错误]"):
        raise HTTPException(status_code=400, detail=result)
    return {"ok": True, "message": result}


@router.get("/search")
def search_file(request: Request, pattern: str, regex: bool = False,
                extension: str | None = None, max_results: int = 50):
    result = request.app.state.tools.execute("file_search", {
        "pattern": pattern, "regex": regex,
        "extension": extension, "max_results": max_results,
    })
    if result.startswith("[工具错误]"):
        raise HTTPException(status_code=400, detail=result)
    return {"text": result}
