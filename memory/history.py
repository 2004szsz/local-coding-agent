# -*- coding: utf-8 -*-
"""
短期记忆：SQLite 会话历史。

库文件默认在 data/history.db（由 settings.json / config.yaml 决定），
不放进源码目录。首次启动时把 data/sessions/*.json 导入一次，已存在的 id 跳过。

对外方法与原先的 JSON SessionStore 一致，HTTP 路由不用改调用方式。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Memory


class SessionNotFoundError(KeyError):
    pass


def _safe_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not session_id or safe != session_id:
        raise ValueError("非法 session_id")
    return safe


class HistoryStore(Memory):
    """会话级短期记忆。remember/recall 的 key 是 session_id。"""

    kind = "history"

    def __init__(self, db_path: str, max_messages: int = 500,
                 legacy_dir: str | None = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_messages = max_messages
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        if legacy_dir:
            self._import_legacy(Path(legacy_dir))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_events TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, created_at);
            """
        )
        self._conn.commit()

    def _import_legacy(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session_id = _safe_id(str(data["id"]))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists:
                    continue
                self._conn.execute(
                    "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        data.get("title") or "新会话",
                        float(data.get("created_at") or time.time()),
                        float(data.get("updated_at") or time.time()),
                    ),
                )
                for msg in data.get("messages") or []:
                    self._conn.execute(
                        """INSERT INTO messages
                           (id, session_id, role, content, tool_events, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            str(msg.get("id") or uuid.uuid4().hex),
                            session_id,
                            msg.get("role") or "user",
                            msg.get("content") or "",
                            json.dumps(msg.get("tool_events") or [], ensure_ascii=False),
                            float(msg.get("created_at") or time.time()),
                        ),
                    )
                self._conn.commit()

    def remember(self, key: str, value: Any) -> None:
        """value 为 {'role', 'content', 'tool_events'}。"""
        if not isinstance(value, dict):
            raise TypeError("history.remember 需要消息 dict")
        self.append_message(
            key,
            str(value.get("role") or "user"),
            str(value.get("content") or ""),
            value.get("tool_events") or [],
        )

    def recall(self, key: str) -> Dict[str, Any]:
        return self.get_session(key)

    def stats(self) -> Dict[str, Any]:
        """会话库占用与条数，供设置页展示。"""
        with self._lock:
            sessions = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            messages = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix) if suffix else self.path
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                continue
        return {
            "db_path": str(self.path),
            "db_name": self.path.name,
            "db_bytes": size,
            "session_count": int(sessions or 0),
            "message_count": int(messages or 0),
        }

    def clear_all(self) -> int:
        """删除全部会话及消息，返回删除的会话数。"""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            self._conn.execute("DELETE FROM sessions")
            self._conn.commit()
        return int(n or 0)

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
                FROM sessions s
                ORDER BY s.updated_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": row["message_count"],
            }
            for row in rows
        ]

    def create_session(self, title: str = "新会话") -> Dict[str, Any]:
        now = time.time()
        session_id = uuid.uuid4().hex
        title = title or "新会话"
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, now, now),
            )
            self._conn.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        session_id = _safe_id(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise SessionNotFoundError(f"会话不存在: {session_id}")
            messages = self._conn.execute(
                """SELECT id, role, content, tool_events, created_at
                   FROM messages WHERE session_id = ? ORDER BY created_at, rowid""",
                (session_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"],
                    "tool_events": json.loads(m["tool_events"] or "[]"),
                    "created_at": m["created_at"],
                }
                for m in messages
            ],
        }

    def delete_session(self, session_id: str) -> None:
        session_id = _safe_id(session_id)
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.commit()

    def rename_session(self, session_id: str, title: str) -> Dict[str, Any]:
        session_id = _safe_id(session_id)
        title = (title or "").strip()[:80] or "新会话"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, time.time(), session_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                raise SessionNotFoundError(f"会话不存在: {session_id}")
        return self.get_session(session_id)

    def append_message(self, session_id: str, role: str, content: str,
                       tool_events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        session_id = _safe_id(session_id)
        now = time.time()
        message = {
            "id": uuid.uuid4().hex,
            "role": role,
            "content": content or "",
            "tool_events": tool_events or [],
            "created_at": now,
        }
        with self._lock:
            row = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise SessionNotFoundError(f"会话不存在: {session_id}")
            self._conn.execute(
                """INSERT INTO messages
                   (id, session_id, role, content, tool_events, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    message["id"], session_id, role, message["content"],
                    json.dumps(message["tool_events"], ensure_ascii=False), now,
                ),
            )
            title = row["title"]
            if role == "user" and title in (None, "", "新会话"):
                title = (content or "新会话").strip().replace("\n", " ")[:30]
            self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
            extra = self._conn.execute(
                """SELECT id FROM messages WHERE session_id = ?
                   ORDER BY created_at DESC, rowid DESC""",
                (session_id,),
            ).fetchall()
            if len(extra) > self.max_messages:
                drop = [r["id"] for r in extra[self.max_messages:]]
                self._conn.executemany(
                    "DELETE FROM messages WHERE id = ?", [(i,) for i in drop]
                )
            self._conn.commit()
        return message

    def get_llm_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """提取供 LLM 使用的 OpenAI 消息格式（含 tool 角色回放）。"""
        session = self.get_session(session_id)
        history: List[Dict[str, Any]] = []
        for msg in session["messages"]:
            if msg["role"] == "user":
                history.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                for ev in msg.get("tool_events", []):
                    if ev.get("tool_call_id"):
                        history.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": ev["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": ev["name"],
                                    "arguments": json.dumps(
                                        ev.get("arguments", {}), ensure_ascii=False
                                    ),
                                },
                            }],
                        })
                        history.append({
                            "role": "tool",
                            "tool_call_id": ev["tool_call_id"],
                            "content": ev.get("output", ""),
                        })
                history.append({"role": "assistant", "content": msg["content"]})
        return history


# 旧名字，避免外部脚本还在找 SessionStore
SessionStore = HistoryStore
