# -*- coding: utf-8 -*-
"""
代码智能切片模块。

优先使用 tree-sitter 做语法解析，按“函数 / 类 / 方法”等结构单元切片，
召回时可直接定位到代码结构；tree-sitter 不可用时自动降级：
  Python -> 标准库 ast（零依赖，同样精确）；
  其它大括号语言 -> 符号正则 + 大括号配平；
  其余（html/css/json/md 等）-> 带重叠的字符滑动窗口。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# ---------------- 语言探测 ----------------
# 文件后缀 -> tree-sitter 语言名
_EXT_LANG: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
}

# tree-sitter 各语言中需要捕获的结构节点
_TS_BLOCK_TYPES: Dict[str, set] = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "javascript": {"function_declaration", "class_declaration",
                   "method_definition", "generator_function_declaration",
                   "arrow_function"},
    "typescript": {"function_declaration", "class_declaration",
                   "method_definition", "interface_declaration",
                   "enum_declaration", "arrow_function"},
    "tsx": {"function_declaration", "class_declaration", "method_definition",
            "interface_declaration", "enum_declaration", "arrow_function"},
    "java": {"class_declaration", "interface_declaration", "method_declaration",
             "enum_declaration"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "c": {"function_definition", "struct_specifier", "type_definition"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier",
            "type_definition"},
}

# 降级方案：大括号语言的结构起始正则（捕获名用于切片命名）
_BRACE_LANG_PATTERN: Dict[str, re.Pattern] = {
    lang: re.compile(pattern)
    for lang, pattern in {
        "javascript": r"(?:export\s+|default\s+|async\s+)*(?:function|class)\s+([A-Za-z_$][\w$]*)",
        "typescript": r"(?:export\s+|default\s+|async\s+)*(?:function|class|interface|enum)\s+([A-Za-z_$][\w$]*)",
        "tsx": r"(?:export\s+|default\s+|async\s+)*(?:function|class|interface|enum)\s+([A-Za-z_$][\w$]*)",
        "java": r"(?:public|private|protected|static|final|abstract|\s)+(?:class|interface|enum|[A-Za-z0-9_<>\[\]]+\s+[A-Za-z0-9_]+)\s+([A-Za-z_][\w$]*)\s*[({]",
        "go": r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*)\s+(?:struct|interface)",
        "c": r"(?:[A-Za-z_][\w]*\s+)+([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{",
        "cpp": r"(?:[A-Za-z_:][\w:]*\s+)*([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{",
    }.items()
}


@dataclass
class CodeChunk:
    """一个代码切片（向量库中的一条文档）。"""
    name: str            # 结构名（函数/类名）
    kind: str            # function / class / block / text
    start_line: int      # 从 1 开始
    end_line: int
    text: str
    language: str

    def as_doc(self) -> str:
        """带结构头的文本，向量化时保留结构语义。"""
        return f"[{self.language}::{self.kind}::{self.name} L{self.start_line}-{self.end_line}]\n{self.text}"


# 探测 tree-sitter 是否真正可用（调用方传入 use_tree_sitter 开关）
def tree_sitter_available() -> bool:
    try:
        import tree_sitter_languages  # noqa: F401
        from tree_sitter import Parser  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------
def split_code(content: str, suffix: str, chunk_size: int = 1200,
               chunk_overlap: int = 150, use_tree_sitter: bool = True) -> List[CodeChunk]:
    """
    将源码切分为结构块列表。

    :param content: 文件文本
    :param suffix: 文件后缀（如 '.py'）
    :param chunk_size: 目标切片字符数（超出的块会再切；无结构文本按此滑窗）
    :param chunk_overlap: 滑窗重叠字符数
    """
    lang = _EXT_LANG.get(suffix.lower())
    if not lang:
        return _sliding_window(content, "text", chunk_size, chunk_overlap)

    chunks: List[CodeChunk] = []
    if use_tree_sitter and tree_sitter_available() and lang in _TS_BLOCK_TYPES:
        chunks = _split_with_tree_sitter(content, lang, chunk_size)
    elif lang == "python":
        chunks = _split_python_ast(content)
    elif lang in _BRACE_LANG_PATTERN:
        chunks = _split_brace_language(content, lang)

    # 兜底 / 补漏：解析器无产出时退回滑窗
    if not chunks:
        chunks = _sliding_window(content, lang, chunk_size, chunk_overlap)

    # 对超大结构块二次切分，保证单块不会超过 chunk_size 太多
    return _resplit_oversized(chunks, chunk_size, chunk_overlap)


# ------------------------------------------------------------------
# 方案一：tree-sitter
# ------------------------------------------------------------------
def _split_with_tree_sitter(content: str, lang: str, chunk_size: int) -> List[CodeChunk]:
    import tree_sitter_languages
    from tree_sitter import Parser

    parser = Parser()
    parser.set_language(tree_sitter_languages.get_language(lang))
    data = content.encode("utf-8", errors="replace")
    tree = parser.parse(data)
    wanted = _TS_BLOCK_TYPES[lang]
    lines = content.splitlines()

    blocks: List = []

    def walk(node, ancestor_kept: bool) -> None:
        # decorated_definition 整体保留；嵌套方法若祖先类已捕获则不再重复
        keep = (node.type in wanted) and not ancestor_kept
        if keep:
            blocks.append(node)
        for child in node.children:
            walk(child, ancestor_kept or keep)

    walk(tree.root_node, False)

    chunks: List[CodeChunk] = []
    cursor = 0  # 已覆盖到的行（0 基）
    for node in blocks:
        s, e = node.start_point[0], node.end_point[0]
        # 块之前的未覆盖代码（import / 常量等）聚合为模块片段
        if s - cursor >= 2:
            head = "\n".join(lines[cursor:s]).strip()
            if len(head) > 40:
                chunks.append(CodeChunk(
                    "module_head", "block", cursor + 1, s, head, lang))
        name = _ts_node_name(node, lines)
        kind = "class" if "class" in node.type or "interface" in node.type else "function"
        chunks.append(CodeChunk(
            name, kind, s + 1, e + 1,
            "\n".join(lines[s:e + 1]), lang))
        cursor = max(cursor, e + 1)

    # 末尾剩余
    if cursor < len(lines) - 1:
        tail = "\n".join(lines[cursor:]).strip()
        if len(tail) > 40:
            chunks.append(CodeChunk("module_tail", "block", cursor + 1, len(lines), tail, lang))
    return chunks


def _ts_node_name(node, lines: List[str]) -> str:
    """从 AST 节点中尽力提取函数/类名。"""
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return child.text.decode("utf-8", errors="replace")
        # 修饰器包裹的 def/class
        if child.type in ("function_definition", "class_definition"):
            return _ts_node_name(child, lines)
    return node.type


# ------------------------------------------------------------------
# 方案二：Python 标准库 AST（tree-sitter 不可用时的精确降级）
# ------------------------------------------------------------------
def _split_python_ast(content: str) -> List[CodeChunk]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []  # 语法错误文件交给滑窗兜底

    lines = content.splitlines()
    chunks: List[CodeChunk] = []
    cursor = 0
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        s, e = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        if s - cursor >= 2:
            head = "\n".join(lines[cursor:s]).strip()
            if len(head) > 40:
                chunks.append(CodeChunk("module_head", "block", cursor + 1, s, head, "python"))
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        chunks.append(CodeChunk(node.name, kind, s + 1, e + 1,
                                "\n".join(lines[s:e + 1]), "python"))
        cursor = max(cursor, e + 1)
    return chunks


# ------------------------------------------------------------------
# 方案三：大括号语言符号正则 + 括号配平
# ------------------------------------------------------------------
def _split_brace_language(content: str, lang: str) -> List[CodeChunk]:
    pattern = _BRACE_LANG_PATTERN[lang]
    lines = content.splitlines()
    chunks: List[CodeChunk] = []

    starts: List[tuple] = []  # (行号, 名称)
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if m and "{" in line or (m and lang == "go"):
            name = next((g for g in m.groups() if g), m.group(1) if m.groups else "block")
            starts.append((i, name or "block"))

    for idx, (start, name) in enumerate(starts):
        end = (starts[idx + 1][0] - 1) if idx + 1 < len(starts) else len(lines) - 1
        # 括号配平：从起始行开始统计 { }，配平即结束
        depth = 0
        seen = False
        for j in range(start, min(end + 1, len(lines))):
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                seen = True
            if seen and depth <= 0:
                end = j
                break
        text = "\n".join(lines[start:end + 1]).strip()
        if text:
            kind = "class" if re.search(r"class|interface|struct|enum", lines[start]) else "function"
            chunks.append(CodeChunk(name, kind, start + 1, end + 1, text, lang))
    return chunks


# ------------------------------------------------------------------
# 方案四：字符滑动窗口（最终兜底，也用于无结构文件）
# ------------------------------------------------------------------
def _sliding_window(content: str, lang: str, chunk_size: int,
                    overlap: int) -> List[CodeChunk]:
    lines = content.splitlines()
    chunks: List[CodeChunk] = []
    buf: List[str] = []
    size = 0
    start_line = 1
    for i, line in enumerate(lines, start=1):
        buf.append(line)
        size += len(line) + 1
        if size >= chunk_size:
            chunks.append(CodeChunk(f"text_{len(chunks) + 1}", "text",
                                    start_line, i, "\n".join(buf), lang))
            # 保留重叠：从尾部回退 overlap 字符对应的行
            tail, tail_size = [], 0
            for ln in reversed(buf):
                tail.insert(0, ln)
                tail_size += len(ln) + 1
                if tail_size >= overlap:
                    break
            buf, size = tail, tail_size
            start_line = i - len(buf) + 1
    if buf:
        text = "\n".join(buf).strip()
        if text:
            chunks.append(CodeChunk(f"text_{len(chunks) + 1}", "text",
                                    start_line, len(lines), text, lang))
    return chunks


def _resplit_oversized(chunks: List[CodeChunk], chunk_size: int,
                       overlap: int) -> List[CodeChunk]:
    """对字符数远超 chunk_size 的结构块按滑窗二次切分。"""
    result: List[CodeChunk] = []
    limit = chunk_size * 2
    for c in chunks:
        if len(c.text) <= limit:
            result.append(c)
            continue
        sub = _sliding_window(c.text, c.language, chunk_size, overlap)
        for j, s in enumerate(sub):
            result.append(CodeChunk(
                name=f"{c.name}#part{j + 1}", kind=c.kind,
                start_line=c.start_line + s.start_line - 1,
                end_line=c.start_line + s.end_line - 1,
                text=s.text, language=c.language))
    return result
