# -*- coding: utf-8 -*-
"""
记忆抽象。

Agent 记忆只有三段：
- 短期：会话历史（HistoryStore / history.db），实现 Memory。
- 源码知识库：CodeVectorStore（Chroma upsert/query），**不**实现 Memory。
- 跨会话用户偏好：PreferenceStore（JSON），**不**进 RAG / history.db。

`data/.workbuddy/memory/` 是人工笔记，**不参与**上述任何一段，设置页不展示。
需要 Memory 协议时用 RagMemory 薄适配，不要把向量库伪装成会话记忆。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List


class Memory(ABC):
    """记忆后端协议。"""

    kind: str = "base"

    @abstractmethod
    def remember(self, key: str, value: Any) -> None:
        """写入一条记忆。key 的含义由实现决定（会话 id 或文档路径）。"""

    @abstractmethod
    def recall(self, key: str) -> Any:
        """按 key 取回。不存在时抛出 KeyError。"""


class RagMemory(Memory):
    """把 CodeVectorStore 的 upsert / query 适配成 Memory。"""

    kind = "rag"

    def __init__(self, store: Any):
        self.store = store

    def remember(self, key: str, value: Any) -> None:
        if isinstance(value, dict):
            docs: List[str] = value.get("documents") or [str(value.get("text") or "")]
            metas = value.get("metadatas") or [{"path": key}]
            ids = value.get("ids") or [f"{key}#{i}" for i in range(len(docs))]
            self.store.upsert(ids, docs, metas)
            return
        self.store.upsert([key], [str(value)], [{"path": key}])

    def recall(self, key: str) -> Any:
        hits = self.store.query(key)
        if not hits:
            raise KeyError(key)
        return hits
