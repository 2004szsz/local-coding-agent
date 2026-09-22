# -*- coding: utf-8 -*-
"""
检索工具。

两类搜索分开，避免模型把“找字符串”和“找语义”混成一次调用：
- file_search：工作空间内的关键词 / 正则，返回路径与行号
- rag_search：源码向量库语义检索（实现见 rag_tools）
"""
from __future__ import annotations

import re
from typing import Any, Dict

from .base import Tool, ToolRegistry
from .rag_tools import register_rag_tools
from .workspace import WorkspaceSecurity

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".pytest_cache", "chroma",
}
_MAX_HITS = 200


def build_file_search_tool(ws: WorkspaceSecurity) -> Tool:
    """构造绑定到指定工作空间的 file_search。"""

    def file_search(kwargs: Dict[str, Any]) -> str:
        pattern = str(kwargs.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("缺少必填参数: pattern")
        use_regex = bool(kwargs.get("regex", False))
        extension = kwargs.get("extension")
        max_results = int(kwargs.get("max_results", 50))

        compiled = re.compile(pattern, re.IGNORECASE) if use_regex else None
        ext = None
        if extension:
            ext = str(extension).lower()
            ext = ext if ext.startswith(".") else f".{ext}"

        hits: list[str] = []
        limit = min(max_results, _MAX_HITS)
        for path in ws.root.rglob("*"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            if ext and path.suffix.lower() != ext:
                continue
            try:
                text = path.read_text(encoding=WorkspaceSecurity._detect_encoding(path))
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                matched = bool(compiled.search(line)) if compiled else (pattern.lower() in line.lower())
                if not matched:
                    continue
                hits.append(f"{ws.relpath(path)}:{lineno}: {line.strip()[:160]}")
                if len(hits) >= limit:
                    return f"搜索 '{pattern}' 命中 {len(hits)} 处（达上限）:\n" + "\n".join(hits)
        if not hits:
            return f"搜索 '{pattern}' 无命中。"
        return f"搜索 '{pattern}' 命中 {len(hits)} 处:\n" + "\n".join(hits)

    return Tool(
        name="file_search",
        description=(
            "在工作空间内按关键词或正则搜索文件内容，返回 '文件路径:行号: 命中行' 列表。"
            "自动跳过 .git/node_modules/虚拟环境等目录。适合已知符号名或字符串；"
            "不确定实现位置时改用 rag_search。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "关键词或正则表达式"},
                "regex": {"type": "boolean", "description": "pattern 是否为正则表达式"},
                "extension": {"type": "string", "description": "可选，限定文件后缀，如 '.py'"},
                "max_results": {"type": "integer", "description": "返回命中上限，默认 50"},
            },
            "required": ["pattern"],
        },
        handler=file_search,
        output_limit=16000,
    )


def register_search_tools(registry: ToolRegistry, ws: WorkspaceSecurity, store=None) -> None:
    """注册关键词搜索与语义搜索。"""
    registry.register(build_file_search_tool(ws))
    register_rag_tools(registry, store)
