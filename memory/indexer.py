# -*- coding: utf-8 -*-
"""
源码索引器：遍历工作空间 -> 结构切片 -> 写入 Chroma。

增量更新机制：
- 对每个文件计算 SHA1 内容哈希，与向量库元数据中记录的哈希比对；
- 仅对「新增 / 内容变化」的文件重新切片并 upsert；
- 工作空间中已删除的文件，其历史切片按 path 元数据删除；
- 全量重建会先清空集合再索引。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from tools.workspace import WorkspaceSecurity
from .splitter import split_code
from .vector_store import CodeVectorStore

# 目录名级别的硬剪枝（性能优先，任何配置都跳过）
_HARD_SKIP_DIRS: Set[str] = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".pytest_cache", ".mypy_cache",
    # 人工笔记目录：不参与 Agent 记忆 / RAG（见 data/.workbuddy/memory/）
    ".workbuddy",
}


class CodeIndexer:
    def __init__(self, ws: WorkspaceSecurity, cfg: Dict[str, Any],
                 store: CodeVectorStore):
        self.ws = ws
        self.cfg = cfg
        self.store = store
        self.rag_cfg = cfg["rag"]

    # ---------------- 文件扫描 ----------------
    def _ignored(self, relpath: str) -> bool:
        """按 config.ignore 的 glob 模式判断文件是否应忽略。"""
        for pattern in self.rag_cfg.get("ignore", []):
            # 支持 **/x/**、目录名、文件名三类常见写法
            if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(relpath, pattern.strip("/")):
                return True
            parts = relpath.split("/")
            token = pattern.strip("*/")
            if "/" not in pattern and token in parts:
                return True
        return False

    def _scan_files(self) -> List[Path]:
        """列出所有通过过滤规则、可被索引的文件。"""
        include = {e.lower() for e in self.rag_cfg["include_ext"]}
        max_bytes = int(self.rag_cfg["max_file_size_kb"]) * 1024
        files: List[Path] = []
        for path in self.ws.root.rglob("*"):
            if not path.is_file():
                continue
            # 硬剪枝：路径任意一段命中跳过目录
            if any(part in _HARD_SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in include:
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            rel = self.ws.relpath(path)
            if self._ignored(rel):
                continue
            files.append(path)
        return files

    # ---------------- 单文件处理 ----------------
    def _file_hash(self, path: Path) -> str:
        h = hashlib.sha1()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()

    def _index_one(self, path: Path, file_hash: str) -> int:
        """切片并写入单个文件，返回写入切片数。"""
        rel = self.ws.relpath(path)
        try:
            content = path.read_text(
                encoding=WorkspaceSecurity._detect_encoding(path))
        except (OSError, UnicodeDecodeError):
            return 0

        chunks = split_code(
            content, path.suffix,
            chunk_size=int(self.rag_cfg["chunk_size"]),
            chunk_overlap=int(self.rag_cfg["chunk_overlap"]),
            use_tree_sitter=bool(self.rag_cfg.get("use_tree_sitter", True)),
        )
        if not chunks:
            return 0

        ids, docs, metas = [], [], []
        for i, chunk in enumerate(chunks):
            ids.append(f"{rel}#chunk{i}")
            docs.append(f"文件: {rel}\n{chunk.as_doc()}")
            metas.append({
                "path": rel,
                "hash": file_hash,
                "name": chunk.name,
                "kind": chunk.kind,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "language": chunk.language,
            })
        self.store.upsert(ids, docs, metas)
        return len(ids)

    # ---------------- 对外入口 ----------------
    def index(self) -> Dict[str, int]:
        """增量索引：新增/变更文件更新，已删除文件清理。"""
        files = self._scan_files()
        current: Dict[str, Tuple[Path, str]] = {}
        for p in files:
            try:
                current[self.ws.relpath(p)] = (p, self._file_hash(p))
            except OSError:
                continue

        old = self.store.indexed_hashes()
        cur_paths = set(current.keys())
        old_paths = set(old.keys())

        # 当前工作区扫不到文件时不要清空已有切片：常见于激活项目目录已消失，
        # 否则一次误触发的增量索引会把 Agent 的 rag_search 知识库删光。
        if not current and old_paths:
            return {
                "scanned": 0, "added": 0, "updated": 0, "removed": 0,
                "chunks_delta": 0,
                "chunks_total": self.store.count(),
                "skipped_purge": True,
            }

        removed = 0
        for gone in old_paths - cur_paths:
            self.store.delete_by_path(gone)
            removed += 1

        added, updated, total_chunks = 0, 0, 0
        for rel, (path, digest) in current.items():
            if rel not in old:
                total_chunks += self._index_one(path, digest)
                added += 1
            elif old[rel] != digest:
                self.store.delete_by_path(rel)
                total_chunks += self._index_one(path, digest)
                updated += 1

        result = {
            "scanned": len(current), "added": added,
            "updated": updated, "removed": removed,
            "chunks_delta": total_chunks,
            "chunks_total": self.store.count(),
        }
        self._persist_index_meta()
        return result

    def reindex(self) -> Dict[str, int]:
        """全量重建：清空集合后重新索引全部文件。"""
        self.store.reset()
        files = self._scan_files()
        total_chunks = 0
        for path in files:
            try:
                total_chunks += self._index_one(path, self._file_hash(path))
            except Exception:  # noqa: BLE001 - 单文件失败不影响整体索引
                continue
        result = {
            "scanned": len(files), "added": len(files),
            "updated": 0, "removed": 0,
            "chunks_delta": total_chunks,
            "chunks_total": self.store.count(),
        }
        self._persist_index_meta()
        return result

    def _persist_index_meta(self) -> None:
        persist = getattr(self.store, "persist_dir", None) or self.rag_cfg.get("persist_dir")
        if not persist:
            return
        path = Path(persist) / "index_meta.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"indexed_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
