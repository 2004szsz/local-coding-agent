# -*- coding: utf-8 -*-
"""模块协同：规则加载、状态协调、RAG 后台自动索引。"""
from app.modules.coordinator import ModuleCoordinator
from app.modules.rag_daemon import RagAutoIndexDaemon
from app.modules.rules import load_module_rules, load_rules_markdown

__all__ = [
    "ModuleCoordinator",
    "RagAutoIndexDaemon",
    "load_module_rules",
    "load_rules_markdown",
]
