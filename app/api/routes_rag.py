# -*- coding: utf-8 -*-
"""RAG 知识库管理路由：增量索引 / 全量重建 / 状态查询。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/status")
def rag_status(request: Request):
    store = request.app.state.vector_store
    if not request.app.state.rag_enabled or store is None:
        return JSONResponse(
            status_code=503,
            content={"enabled": False,
                     "message": "RAG 未启用或初始化失败，请检查 config.yaml 的 rag/embedding 配置。"},
        )
    status = store.status()
    return {"enabled": True, **status}


@router.post("/index")
async def rag_index(request: Request):
    """增量索引：只处理新增 / 变更 / 删除的文件。"""
    indexer = request.app.state.indexer
    if indexer is None:
        return JSONResponse(status_code=503, content={"ok": False, "message": "RAG 未就绪"})
    stats = await run_in_threadpool(indexer.index)
    return {"ok": True, "mode": "incremental", **stats}


@router.post("/reindex")
async def rag_reindex(request: Request):
    """全量重建：清空向量集合后重新索引。"""
    indexer = request.app.state.indexer
    if indexer is None:
        return JSONResponse(status_code=503, content={"ok": False, "message": "RAG 未就绪"})
    stats = await run_in_threadpool(indexer.reindex)
    return {"ok": True, "mode": "full", **stats}
