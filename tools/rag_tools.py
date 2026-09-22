# -*- coding: utf-8 -*-
"""
RAG 检索工具：rag_search。

从项目源码知识库中做语义相似性检索，返回结构块及其精确位置，
供 Agent 在修改代码前快速理解项目上下文。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .base import Tool, ToolRegistry

if TYPE_CHECKING:  # 避免运行时循环依赖
    from memory.vector_store import CodeVectorStore


def build_rag_tool(store: Optional["CodeVectorStore"]) -> Tool:
    """构造 rag_search 工具；store 为 None（RAG 未启用/初始化失败）时给出提示。"""

    def rag_search(kwargs: dict) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            raise ValueError("缺少必填参数: query")
        top_k = int(kwargs.get("top_k", 5))
        if store is None:
            return ("[RAG 未就绪] 知识库未启用或初始化失败。请检查 config.yaml 中 rag.enabled "
                    "与 embedding 配置，并在前端点击「重建索引」或 POST /api/rag/index。")

        hits = store.query(query, top_k=top_k)
        if not hits:
            return "知识库中没有检索到相关代码（可尝试先执行索引构建）。"

        blocks = []
        for i, h in enumerate(hits, start=1):
            # 距离越小越相似（cosine distance: 0 最相似）
            blocks.append(
                f"--- 命中 {i} (相似度距离 {h['distance']}) "
                f"{h['path']}::{h['name']} 行{h['start_line']}-{h['end_line']} ---\n"
                f"{h['text']}"
            )
        return (
            f"rag_search 针对 '{query}' 检索到 {len(hits)} 个代码块，"
            f"需要查看完整文件请再用 read_file:\n\n" + "\n\n".join(blocks)
        )

    return Tool(
        name="rag_search",
        description=(
            "对项目源码知识库做语义检索，返回最相关的函数/类/代码块及其文件路径与行号。"
            "当任务涉及理解既有项目结构、查找某个功能实现、复用已有代码时优先调用；"
            "它不返回文件全文，需要全文请配合 read_file。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言描述的检索意图，如“用户登录的token校验逻辑”"},
                "top_k": {"type": "integer", "description": "返回代码块数量，默认 5"},
            },
            "required": ["query"],
        },
        handler=rag_search,
        output_limit=16000,
    )


def register_rag_tools(registry: ToolRegistry,
                       store: "Optional[CodeVectorStore]") -> None:
    registry.register(build_rag_tool(store))
