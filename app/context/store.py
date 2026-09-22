# -*- coding: utf-8 -*-
"""IDE 上下文内存存储：线程安全快照 + SSE 订阅推送。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

MAX_EDITS = 5
MAX_TERMINAL = 10
MAX_LINTER = 50
MAX_FILE_BYTES = 512 * 1024

LANG_MAP = {
    "js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
    "vue": "vue", "py": "python", "go": "go", "java": "java", "rs": "rust",
    "json": "json", "html": "html", "css": "css", "md": "markdown",
    "yaml": "yaml", "yml": "yaml", "sh": "bash", "sql": "sql",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lang_from_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return LANG_MAP.get(ext, ext or "plaintext")


class ContextStore:
    """维护 8 类 IDE 上下文；对外输出与前端 ContextCapture.capture() 同构。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.seq = 0
        self.open_file: Optional[Dict[str, Any]] = None
        self.selection = ""
        self.recent_edits: List[Dict[str, str]] = []
        self.linter_errors: List[Dict[str, Any]] = []
        self.terminal_lines: List[str] = []
        self.tech_stack: Optional[Dict[str, Any]] = None
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def reset(self) -> None:
        async with self._lock:
            self.open_file = None
            self.selection = ""
            self.recent_edits = []
            self.linter_errors = []
            self.terminal_lines = []
            self.tech_stack = None
            await self._bump()

    async def set_editor(
        self,
        path: str,
        content: str,
        line: int = 1,
        col: int = 1,
        selection: str = "",
    ) -> None:
        async with self._lock:
            safe_path = path or "<未命名>"
            body = content or ""
            if len(body.encode("utf-8")) > MAX_FILE_BYTES:
                body = body[:MAX_FILE_BYTES] + "\n// … 文件过大，后台仅保留前 512KB …"
            self.open_file = {
                "path": safe_path,
                "content": body,
                "language": _lang_from_path(safe_path),
                "cursor": {"line": max(1, line), "col": max(1, col)},
            }
            self.selection = selection or ""
            await self._bump()

    async def set_selection(self, selection: str) -> None:
        async with self._lock:
            self.selection = selection or ""
            await self._bump()

    async def record_edit(self, path: str, summary: str = "", time: str | None = None) -> None:
        async with self._lock:
            self.recent_edits.insert(0, {
                "path": path or "<未知文件>",
                "time": time or _utc_now(),
                "summary": summary or "(无描述)",
            })
            if len(self.recent_edits) > MAX_EDITS:
                self.recent_edits = self.recent_edits[:MAX_EDITS]
            await self._bump()

    async def set_linter_errors(self, errors: List[Dict[str, Any]]) -> None:
        async with self._lock:
            self.linter_errors = list(errors or [])[:MAX_LINTER]
            await self._bump()

    async def set_terminal_output(self, output: str | List[str]) -> None:
        async with self._lock:
            if isinstance(output, list):
                lines = output
            else:
                lines = str(output or "").splitlines()
            self.terminal_lines = [
                ln for ln in lines if ln.strip()
            ][-MAX_TERMINAL:]
            await self._bump()

    async def append_terminal_line(self, line: str) -> None:
        if not line or not str(line).strip():
            return
        async with self._lock:
            self.terminal_lines.append(str(line).rstrip())
            if len(self.terminal_lines) > MAX_TERMINAL:
                self.terminal_lines = self.terminal_lines[-MAX_TERMINAL:]
            await self._bump()

    async def set_tech_stack(self, stack: Dict[str, Any] | None) -> None:
        async with self._lock:
            self.tech_stack = stack
            await self._bump()

    def cursor_snippet(self, radius: int = 12) -> Optional[Dict[str, Any]]:
        f = self.open_file
        if not f or not f.get("content"):
            return None
        all_lines = f["content"].splitlines()
        if not all_lines:
            return None
        line = min(max(f["cursor"]["line"], 1), len(all_lines))
        start = max(1, line - radius)
        end = min(len(all_lines), line + radius)
        width = len(str(end))
        body: List[str] = []
        for i in range(start, end + 1):
            marker = ">>" if i == line else "  "
            num = str(i).rjust(width)
            body.append(f"{marker} {num} | {all_lines[i - 1]}")
        return {
            "path": f["path"],
            "language": f["language"],
            "line": line,
            "col": f["cursor"]["col"],
            "snippet": "\n".join(body),
        }

    def get_snapshot(self) -> Dict[str, Any]:
        open_file = None
        cursor = None
        if self.open_file:
            content = self.open_file.get("content") or ""
            open_file = {
                "path": self.open_file["path"],
                "language": self.open_file.get("language", "plaintext"),
                "content": content,
                "totalLines": len(content.splitlines()) if content else 0,
            }
            cursor = self.cursor_snippet()

        tech = self.tech_stack
        has_tech = bool(
            tech and (tech.get("languages") or tech.get("frameworks"))
        )
        return {
            "openFile": open_file,
            "cursor": cursor,
            "selection": self.selection or None,
            "techStack": tech,
            "recentEdits": list(self.recent_edits),
            "linterErrors": list(self.linter_errors),
            "terminalLines": list(self.terminal_lines),
            "present": {
                "openFile": bool(self.open_file),
                "cursor": bool(self.open_file),
                "selection": bool(self.selection),
                "techStack": has_tech,
                "recentEdits": len(self.recent_edits) > 0,
                "linterErrors": len(self.linter_errors) > 0,
                "terminalLines": len(self.terminal_lines) > 0,
            },
            "seq": self.seq,
        }

    async def _bump(self) -> None:
        self.seq += 1
        snap = self.get_snapshot()
        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait({"seq": self.seq, "snapshot": snap})
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
