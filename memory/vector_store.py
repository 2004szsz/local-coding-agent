# -*- coding: utf-8 -*-
"""
Chroma 本地向量库封装。

设计要点：
- Embedding 统一在外部计算后通过 embeddings= 参数写入 / 查询，
  从而绕开 Chroma 对自定义 EmbeddingFunction 的跨版本持久化兼容问题；
- 向量提供两种实现：
    HTTPOpenAIEmbedder：走 OpenAI 兼容 /embeddings（Ollama / GLM 等）；
    HashEmbedder：嵌入服务不可用时的本地确定性降级（可运行、语义弱）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
import httpx

from tools.net import trust_env_for


class HTTPOpenAIEmbedder:
    """通过 OpenAI 兼容接口批量获取文本向量。"""

    name = "http"
    _BATCH = 32

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._cache: Dict[str, List[float]] = {}  # 文本 -> 向量（增量索引去重）

    def ping(self) -> bool:
        """探活：用最短文本请求一次，判断嵌入服务是否可用。"""
        try:
            self.embed(["ping"])
            return True
        except Exception:  # noqa: BLE001
            return False

    def embed(self, texts: List[str]) -> List[List[float]]:
        results: List[Optional[List[float]]] = [None] * len(texts)
        pending_idx: List[int] = []
        pending_text: List[str] = []
        for i, t in enumerate(texts):
            if t in self._cache:
                results[i] = self._cache[t]
            else:
                pending_idx.append(i)
                pending_text.append(t)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        for start in range(0, len(pending_text), self._BATCH):
            batch_texts = pending_text[start:start + self._BATCH]
            resp = httpx.post(
                self.url,
                json={"model": self.model, "input": batch_texts},
                headers=headers, timeout=self.timeout,
                # 嵌入服务通常也是本机 Ollama：本机地址必须绕过系统代理
                trust_env=trust_env_for(self.url),
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            # 按 index 排序，避免服务端乱序
            items.sort(key=lambda d: d.get("index", 0))
            if len(items) != len(batch_texts):
                raise RuntimeError("嵌入服务返回条数与请求不一致")
            for local, item in enumerate(items):
                vec = item["embedding"]
                gi = pending_idx[start + local]
                results[gi] = vec
                self._cache[batch_texts[local]] = vec
        return results  # type: ignore[return-value]

    def describe(self) -> str:
        return f"HTTP 嵌入模型: {self.model}"


class HashEmbedder:
    """
    本地哈希向量（词袋 + 二元组哈希到固定维度）。

    无需任何外部服务，保证 RAG 链路可用；仅做关键词级近似匹配，
    语义能力弱，生产使用请配置真实 embedding 服务。
    """

    name = "hash"
    dim = 256

    def ping(self) -> bool:
        return True

    def describe(self) -> str:
        return "本地哈希向量（降级模式，仅关键词级匹配）"

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fa5]", text.lower())
        # 一元 + 相邻二元，保留一点点词序信息
        grams = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
        for tok in grams:
            digest = hashlib.md5(tok.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class CodeVectorStore:
    """Chroma 向量库 + 可切换 Embedder 的组合封装。"""

    def __init__(self, persist_dir: str, collection_name: str, embedder: Any):
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(path=persist_dir)
        # 不向 Chroma 注册 embedding_function：向量一律外部计算后显式传入
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine", "description": "project source code index"},
        )
        self.embedder = embedder

    def close(self) -> None:
        """释放 Chroma 文件句柄，避免 Windows 下临时目录删不掉。"""
        try:
            system = getattr(self.client, "_system", None)
            if system is not None:
                stop = getattr(system, "stop", None)
                if callable(stop):
                    stop()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 工厂 ----------------
    @classmethod
    def create(cls, cfg: dict) -> "CodeVectorStore":
        """依据配置选择嵌入方案：HTTP 优先，失败按配置降级哈希向量。"""
        emb_cfg = cfg["llm"]["embedding"]
        embedder: Any
        if emb_cfg.get("enabled", True):
            http = HTTPOpenAIEmbedder(
                base_url=emb_cfg["base_url"],
                api_key=emb_cfg["api_key"],
                model=emb_cfg["model"],
                timeout=int(emb_cfg.get("timeout", 60)),
            )
            if http.ping():
                embedder = http
            elif emb_cfg.get("fallback_hash", True):
                print("[RAG] 警告: 嵌入服务不可用，已降级为本地哈希向量。"
                      "可执行 `ollama pull nomic-embed-text` 后重建索引。")
                embedder = HashEmbedder()
            else:
                raise RuntimeError(
                    "嵌入服务不可用且未允许降级（embedding.fallback_hash=false），"
                    "请检查 config.yaml 中 embedding 配置。"
                )
        else:
            embedder = HashEmbedder()
        return cls(cfg["rag"]["persist_dir"], cfg["rag"]["collection"], embedder)

    def reset(self) -> None:
        """删除并重建集合（全量重建时使用）。"""
        name = self.collection.name
        self.client.delete_collection(name)
        self.collection = self.client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"})

    # ---------------- 写入 / 删除 / 查询 ----------------
    def upsert(self, ids: List[str], documents: List[str],
               metadatas: List[dict]) -> None:
        if not ids:
            return
        embeddings = self.embedder.embed(documents)
        self.collection.upsert(
            ids=ids, embeddings=embeddings,
            documents=documents, metadatas=metadatas,
        )

    def delete_by_path(self, relpath: str) -> None:
        try:
            self.collection.delete(where={"path": relpath})
        except Exception:  # noqa: BLE001 - 路径不存在时忽略
            pass

    def indexed_hashes(self) -> Dict[str, str]:
        """返回 {文件相对路径: 内容hash}，供增量索引比对。"""
        try:
            data = self.collection.get(include=["metadatas"])
        except Exception:  # noqa: BLE001
            return {}
        result: Dict[str, str] = {}
        for meta in data.get("metadatas", []) or []:
            if meta and "path" in meta:
                result[meta["path"]] = meta.get("hash", "")
        return result

    def query(self, text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """语义检索，返回 [{path, name, kind, start_line, end_line, text, distance}]。"""
        qvec = self.embedder.embed([text])[0]
        res = self.collection.query(
            query_embeddings=[qvec], n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        hits: List[Dict[str, Any]] = []
        if not res.get("ids") or not res["ids"][0]:
            return hits
        for i, _id in enumerate(res["ids"][0]):
            meta = res["metadatas"][0][i] or {}
            hits.append({
                "path": meta.get("path", ""),
                "name": meta.get("name", ""),
                "kind": meta.get("kind", ""),
                "start_line": meta.get("start_line", 0),
                "end_line": meta.get("end_line", 0),
                "text": res["documents"][0][i],
                "distance": round(float(res["distances"][0][i]), 4),
            })
        return hits

    def count(self) -> int:
        try:
            return self.collection.count()
        except Exception:  # noqa: BLE001
            return 0

    def status(self) -> Dict[str, Any]:
        indexed_at = None
        meta_path = Path(self.persist_dir) / "index_meta.json"
        try:
            if meta_path.is_file():
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                indexed_at = data.get("indexed_at")
        except (OSError, json.JSONDecodeError, TypeError):
            indexed_at = None
        return {
            "chunks": self.count(),
            "embedder": self.embedder.describe(),
            "embedder_mode": getattr(self.embedder, "name", ""),
            "indexed_at": indexed_at,
        }
