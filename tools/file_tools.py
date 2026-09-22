# -*- coding: utf-8 -*-
"""
文件操作工具集（共 4 个）：
    list_dir    列出目录内容（支持递归 / 后缀过滤）
    read_file   读取文件（自动编码检测，支持行范围）
    write_file  写入 / 新建文件
    edit_file   精确局部编辑（字符串替换 或 行号范围替换）

关键词搜索在 tools/search.py。所有路径均经过 WorkspaceSecurity 校验。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import Tool, ToolRegistry
from .errors import PatchConflictError
from .workspace import WorkspaceSecurity

# 搜索时默认跳过的目录名（避免污染结果并提升性能）
_SEARCH_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".pytest_cache", "chroma",
}
_MAX_LIST_ENTRIES = 2000


def _require(kwargs: Dict[str, Any], key: str) -> Any:
    """取必填参数，缺失时抛出对模型友好的错误信息。"""
    value = kwargs.get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ValueError(f"缺少必填参数: {key}")
    return value


def build_file_tools(ws: WorkspaceSecurity) -> List[Tool]:
    """构造绑定到指定工作空间的文件工具实例列表。"""

    # ---------------- 1. list_dir ----------------
    def list_dir(kwargs: Dict[str, Any]) -> str:
        rel = str(kwargs.get("path") or ".")
        recursive = bool(kwargs.get("recursive", False))
        ext_filter = kwargs.get("extension")  # 如 ".py"，可为列表

        def walk(p: Path, depth: int) -> List[str]:
            lines: List[str] = []
            for item in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
                if item.is_dir() and item.name in _SEARCH_SKIP_DIRS:
                    continue
                tag = "DIR " if item.is_dir() else "FILE"
                suffix = "/" if item.is_dir() else ""
                lines.append(f"{'  ' * depth}{tag} {ws.relpath(item)}{suffix}")
                if item.is_dir() and recursive:
                    lines.extend(walk(item, depth + 1))
                if len(lines) > _MAX_LIST_ENTRIES:
                    lines.append("...[条目过多，已截断]")
                    break
            return lines

        root = ws.resolve(rel)
        if not root.is_dir():
            raise NotADirectoryError(f"不是目录: {ws.relpath(root)}")

        entries = ws.list_dir(root)
        if not recursive:
            lines = [
                f"{'DIR ' if e['type'] == 'dir' else 'FILE'} "
                f"{e['relpath']}{'/' if e['type'] == 'dir' else ''}"
                for e in entries
            ]
        else:
            lines = walk(root, 0)

        # 后缀过滤（仅递归模式有意义，扁平模式同样支持）
        if ext_filter:
            exts = {ext_filter} if isinstance(ext_filter, str) else set(ext_filter)
            exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts}
            lines = [ln for ln in lines
                     if ln.startswith("DIR ") or Path(ln.split(" ", 2)[-1]).suffix.lower() in exts]

        header = f"目录 {ws.relpath(root) or '.'} 内容（{len(lines)} 项）:\n"
        return header + ("\n".join(lines) if lines else "(空目录)")

    # ---------------- 2. read_file ----------------
    def read_file(kwargs: Dict[str, Any]) -> str:
        rel = str(_require(kwargs, "path"))
        start_line = kwargs.get("start_line")  # 从 1 开始，含
        end_line = kwargs.get("end_line")      # 含
        p = ws.resolve(rel)
        content = ws.read_text(p)
        lines = content.splitlines()

        s = int(start_line) - 1 if start_line else 0
        e = int(end_line) if end_line else len(lines)
        s = max(0, min(s, len(lines)))
        e = max(s, min(e, len(lines)))

        width = len(str(e or 1))
        numbered = [f"{i + 1:>{width}} | {lines[i]}" for i in range(s, e)]
        rng = f"# 行 {s + 1}-{e}" if (start_line or end_line) else f"# 共 {len(lines)} 行"
        return f"文件 {ws.relpath(p)} {rng}\n" + "\n".join(numbered)

    # ---------------- 3. write_file ----------------
    def write_file(kwargs: Dict[str, Any]) -> str:
        rel = str(_require(kwargs, "path"))
        content = str(kwargs.get("content", ""))
        create = bool(kwargs.get("create", True))  # 默认允许新建
        overwrite = bool(kwargs.get("overwrite", True))
        p = ws.resolve(rel)

        if p.exists() and not overwrite:
            raise FileExistsError(f"文件已存在且 overwrite=false: {ws.relpath(p)}")
        ws.write_text(rel, content, create=True)
        action = "覆盖" if p.exists() else "新建"
        return f"已{action}文件 {ws.relpath(p)}，写入 {len(content)} 字符 / {content.count(chr(10)) + 1} 行。"

    # ---------------- 4. edit_file ----------------
    def edit_file(kwargs: Dict[str, Any]) -> str:
        rel = str(_require(kwargs, "path"))
        p = ws.resolve(rel)
        content = ws.read_text(p)

        # 模式 B：行号范围替换（start_line/end_line 均从 1 开始、含端点）
        start_line = kwargs.get("start_line")
        if start_line is not None:
            end_line = int(kwargs.get("end_line", start_line))
            start_line = int(start_line)
            new_content = str(kwargs.get("new_content", ""))
            lines = content.splitlines(keepends=True)
            if not (1 <= start_line <= len(lines)):
                raise ValueError(f"start_line={start_line} 超出文件范围(1-{len(lines)})")
            end_line = min(max(end_line, start_line), len(lines))
            # 保持新内容结尾换行风格一致
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_lines = lines[:start_line - 1] + [new_content] + lines[end_line:]
            p.write_text("".join(new_lines), encoding="utf-8")
            return (f"已按行号编辑 {ws.relpath(p)}：替换第 {start_line}-{end_line} 行 "
                    f"（共 {end_line - start_line + 1} 行）为 {new_content.count(chr(10))} 行新内容。")

        # 模式 A：精确字符串替换
        old_string = str(_require(kwargs, "old_string"))
        new_string = str(kwargs.get("new_string", ""))
        replace_all = bool(kwargs.get("replace_all", False))
        occurrences = content.count(old_string)
        # 用专门的异常类型表达「补丁定位失败」，而不是普通 ValueError：
        # 上层的失败分类据此决定「回滚到检查点重读文件」而不是「改改参数再试」。
        # 消息文案保持不变（旧框架只看 [工具错误] 前缀）。
        if occurrences == 0:
            raise PatchConflictError(
                "未在文件中找到 old_string，请先 read_file 确认原文（注意缩进/空白）。")
        if occurrences > 1 and not replace_all:
            # 给出出现位置，方便模型补充上下文使其唯一
            line_hits = [
                str(i + 1) for i, ln in enumerate(content.splitlines())
                if old_string.splitlines()[0] in ln
            ][:5]
            raise PatchConflictError(
                f"old_string 在文件中出现 {occurrences} 次（约行 {','.join(line_hits)}），"
                f"非唯一匹配。请补充更多上下文，或显式设置 replace_all=true。"
            )
        content = content.replace(old_string, new_string, -1 if replace_all else 1)
        p.write_text(content, encoding="utf-8")
        count = occurrences if replace_all else 1
        return f"已精确编辑 {ws.relpath(p)}：完成 {count} 处替换。"

    return [
        Tool(
            name="list_dir",
            description=(
                "列出工作空间内某个目录的内容。返回条目带 FILE/DIR 标记与相对路径。"
                "参数 path 为相对工作空间根目录的路径，根目录用 '.'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录相对路径，默认根目录 '.'"},
                    "recursive": {"type": "boolean", "description": "是否递归列出子目录"},
                    "extension": {
                        "type": "string",
                        "description": "可选，按后缀过滤，如 '.py'",
                    },
                },
            },
            handler=list_dir,
        ),
        Tool(
            name="read_file",
            description=(
                "读取工作空间内的文本文件，返回带行号的内容（行号可直接用于 edit_file）。"
                "可通过 start_line/end_line 只读取局部，避免大文件占满上下文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "start_line": {"type": "integer", "description": "可选，起始行（从1开始，含）"},
                    "end_line": {"type": "integer", "description": "可选，结束行（含）"},
                },
                "required": ["path"],
            },
            handler=read_file,
            output_limit=20000,
        ),
        Tool(
            name="write_file",
            description=(
                "将内容整体写入文件（文件不存在则新建，存在则默认覆盖）。"
                "适合新建文件；修改已有文件请优先使用 edit_file 做局部编辑。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件相对路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                    "create": {"type": "boolean", "description": "不存在时是否允许新建，默认 true"},
                    "overwrite": {"type": "boolean", "description": "已存在时是否允许覆盖，默认 true"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
        Tool(
            name="edit_file",
            description=(
                "对已存在文件做精确局部编辑，二选一：\n"
                "1) 字符串替换：提供 old_string（必须与文件内容逐字一致，含缩进）与 new_string，"
                "old_string 必须唯一匹配，多处匹配需设置 replace_all=true；\n"
                "2) 行范围替换：提供 start_line、end_line（从1开始、含端点）与 new_content。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文（逐字一致）"},
                    "new_string": {"type": "string", "description": "替换后的文本"},
                    "replace_all": {"type": "boolean", "description": "old_string 多处出现时是否全部替换"},
                    "start_line": {"type": "integer", "description": "行范围模式：起始行（含）"},
                    "end_line": {"type": "integer", "description": "行范围模式：结束行（含）"},
                    "new_content": {"type": "string", "description": "行范围模式：新内容"},
                },
                "required": ["path"],
            },
            handler=edit_file,
        ),
    ]


def register_file_tools(registry: ToolRegistry, ws: WorkspaceSecurity) -> None:
    """便捷函数：把文件工具批量注册进工具注册表。"""
    for tool in build_file_tools(ws):
        registry.register(tool)
