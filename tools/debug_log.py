# -*- coding: utf-8 -*-
"""调试模式下把每次工具调用追加到 data/logs/agent.log。未开启时什么都不做。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def debug_enabled() -> bool:
    return os.getenv("AGENT_DEBUG", "").strip().lower() in ("1", "true", "yes")


def log_tool_call(name: str, arguments: object, result: str) -> None:
    if not debug_enabled():
        return
    preview = (result or "").replace("\n", " ")
    if len(preview) > 400:
        preview = preview[:400] + "..."
    try:
        args = json.dumps(arguments, ensure_ascii=False, default=str)
    except TypeError:
        args = str(arguments)
    if len(args) > 500:
        args = args[:500] + "..."
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} "
        f"tool={name} args={args} result={preview}\n"
    )
    path = Path(os.getenv("AGENT_LOG_PATH") or "data/logs/agent.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    print(f"[debug] {name} -> {len(result or '')} 字符")
