# -*- coding: utf-8 -*-
"""提示词模板模型族匹配（与前端 templates.js 规则保持一致）。"""
from __future__ import annotations

from typing import Any, Dict

DEFAULT_HYPERPARAMS = {"temperature": 0.1, "top_p": 0.9}

FAMILY_KEYWORDS: Dict[str, list[str]] = {
    "deepseek": ["deepseek", "deep-seek", "深度求索"],
    "seed": ["seed", "doubao", "bytedance", "skylark", "豆包"],
    "glm": ["glm", "chatglm", "zhipu", "bigmodel", "智谱"],
    "claude": ["claude", "anthropic", "opus", "sonnet", "haiku"],
    "gpt": ["gpt", "openai", "chatgpt", "o1-", "o3-", "o4-", "gpt-"],
}

FAMILY_LABELS: Dict[str, str] = {
    "seed": "Seed（豆包/字节）系列",
    "glm": "GLM（智谱）系列",
    "claude": "Claude（Anthropic）系列",
    "gpt": "GPT（OpenAI）系列",
    "deepseek": "DeepSeek 系列",
    "general": "通用模型（兜底）",
}

MATCH_ORDER = ("deepseek", "seed", "glm", "claude", "gpt")


def match_model(model_name: str) -> str:
    """根据模型名关键词匹配模板族；无命中回落 general。"""
    if not model_name or not isinstance(model_name, str):
        return "general"
    name = model_name.lower()
    for family in MATCH_ORDER:
        for kw in FAMILY_KEYWORDS.get(family, []):
            if kw in name:
                return family
    return "general"


def build_match_status(
    model_name: str,
    *,
    provider: str = "",
    source: str = "auto",
) -> Dict[str, Any]:
    """构建供 API / SSE 推送的匹配状态快照。"""
    model = (model_name or "").strip()
    family = match_model(model) if model else "general"
    label = FAMILY_LABELS.get(family, FAMILY_LABELS["general"])
    if not model:
        return {
            "status": "pending",
            "model": "",
            "provider": provider or "",
            "family": "general",
            "label": label,
            "hyperparams": dict(DEFAULT_HYPERPARAMS),
            "source": source,
            "message": "等待 Agent 模型配置",
            "matched_text": "模板后台匹配中…",
        }
    matched_text = f"已匹配：{label}（{model}）"
    return {
        "status": "matched",
        "model": model,
        "provider": provider or "",
        "family": family,
        "label": label,
        "hyperparams": dict(DEFAULT_HYPERPARAMS),
        "source": source,
        "message": matched_text,
        "matched_text": matched_text,
    }
