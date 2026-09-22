# -*- coding: utf-8 -*-
"""模块协同规则：读取 config/module-rules.yaml 与 .cursor/rules/module-cohesion.mdc。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.config import PROJECT_ROOT

_RULES_CACHE: Optional[Dict[str, Any]] = None
_MD_CACHE: Optional[Dict[str, Any]] = None


def _rules_yaml_path() -> Path:
    return PROJECT_ROOT / "config" / "module-rules.yaml"


def _markdown_path(rel: str) -> Path:
    return PROJECT_ROOT / rel


def load_module_rules(force: bool = False) -> Dict[str, Any]:
    """加载结构化模块规则（YAML）。"""
    global _RULES_CACHE
    if _RULES_CACHE is not None and not force:
        return _RULES_CACHE

    path = _rules_yaml_path()
    if not path.is_file():
        _RULES_CACHE = {"version": 1, "modules": {}}
        return _RULES_CACHE

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("modules", {})
    _RULES_CACHE = data
    return data


def load_rules_markdown(force: bool = False) -> Dict[str, Any]:
    """加载 Markdown 规则全文及元信息。"""
    global _MD_CACHE
    if _MD_CACHE is not None and not force:
        return _MD_CACHE

    rules = load_module_rules(force=force)
    rel = str(rules.get("rules_markdown") or ".cursor/rules/module-cohesion.mdc")
    path = _markdown_path(rel)
    content = ""
    if path.is_file():
        content = path.read_text(encoding="utf-8")
    _MD_CACHE = {
        "path": rel,
        "exists": path.is_file(),
        "content": content,
        "size": len(content),
    }
    return _MD_CACHE


def extract_rule_sections(module_id: str) -> str:
    """从 Markdown 规则中提取某模块关联的章节正文。"""
    rules = load_module_rules()
    mod = (rules.get("modules") or {}).get(module_id) or {}
    section_names: List[str] = list(mod.get("rule_sections") or [])
    if not section_names:
        return ""

    md = load_rules_markdown()
    content = md.get("content") or ""
    if not content:
        return ""

    parts: List[str] = []
    for name in section_names:
        # 匹配 ### 标题 到下一个同级或更高级标题
        pattern = rf"(###\s+{re.escape(name)}\s*\n)(.*?)(?=\n###\s|\n##\s|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if match:
            parts.append(f"### {name}\n{match.group(2).strip()}")
    return "\n\n".join(parts)


def module_paths(module_id: str) -> List[str]:
    rules = load_module_rules()
    mod = (rules.get("modules") or {}).get(module_id) or {}
    return list(mod.get("paths") or [])


def context_snippet_for_modules(module_ids: Optional[List[str]] = None,
                                max_chars: int = 4000) -> str:
    """为提示词优化 / Agent 上下文拼接模块规则摘要。"""
    rules = load_module_rules()
    modules = rules.get("modules") or {}
    ids = module_ids or list(modules.keys())
    blocks: List[str] = []
    for mid in ids:
        mod = modules.get(mid) or {}
        label = mod.get("label") or mid
        sections = extract_rule_sections(mid)
        paths = ", ".join(mod.get("paths") or [])
        header = f"【{label}】关联路径: {paths}" if paths else f"【{label}】"
        body = sections or "(规则章节未找到，请检查 module-rules.yaml)"
        blocks.append(f"{header}\n{body}")
    text = "\n\n---\n\n".join(blocks)
    if len(text) > max_chars:
        return text[: max_chars - 20] + "\n…(规则已截断)"
    return text
