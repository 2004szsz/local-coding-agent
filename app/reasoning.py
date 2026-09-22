# -*- coding: utf-8 -*-
"""
思考强度（Reasoning Level）统一目录与 LLM 载荷合并。

前端滑动条、模型注册表、Agent 循环共用同一套档位定义与颜色元数据。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 档位顺序（由低到高，与 ChatGPT 式滑动条一致）
LEVEL_ORDER: List[str] = ["none", "low", "medium", "high", "xhigh", "max"]

LEVEL_META: Dict[str, Dict[str, Any]] = {
    "none": {"label": "关闭", "label_en": "Off", "color": "#94a3b8", "glow": "rgba(148,163,184,.45)"},
    "low": {"label": "低", "label_en": "Low", "color": "#60a5fa", "glow": "rgba(96,165,250,.5)"},
    "medium": {"label": "中", "label_en": "Medium", "color": "#34d399", "glow": "rgba(52,211,153,.5)"},
    "high": {"label": "高", "label_en": "High", "color": "#fbbf24", "glow": "rgba(251,191,36,.55)"},
    "xhigh": {"label": "极高", "label_en": "Extra High", "color": "#f97316", "glow": "rgba(249,115,22,.55)"},
    "max": {"label": "最大", "label_en": "Max", "color": "#ef4444", "glow": "rgba(239,68,68,.55)"},
}

# 厂商默认参数映射（可被模型级 reasoning_param_map 覆盖/合并）
VENDOR_DEFAULT_MAPS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "deepseek": {
        "low": {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        "medium": {"thinking": {"type": "enabled", "budget_tokens": 8192}},
        "high": {"thinking": {"type": "enabled", "budget_tokens": 16384}},
        "xhigh": {"thinking": {"type": "enabled", "budget_tokens": 32768}},
        "max": {"thinking": {"type": "enabled", "budget_tokens": 65536}},
    },
    "openai": {
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "high"},
        "xhigh": {"reasoning_effort": "high"},
        "max": {"reasoning_effort": "high"},
    },
    "qwen": {
        "low": {"enable_thinking": True, "thinking_budget": 2048},
        "medium": {"enable_thinking": True, "thinking_budget": 8192},
        "high": {"enable_thinking": True, "thinking_budget": 16384},
        "xhigh": {"enable_thinking": True, "thinking_budget": 32768},
        "max": {"enable_thinking": True, "thinking_budget": 65536},
    },
    "glm": {
        "low": {"thinking": {"type": "enabled"}},
        "medium": {"thinking": {"type": "enabled"}},
        "high": {"thinking": {"type": "enabled"}},
    },
}


@dataclass(frozen=True)
class VendorReasoningProfile:
    """厂商思考强度能力描述（UI 过滤 + API 映射的单一真相源）。"""

    vendor: str
    native_levels: List[str] = field(default_factory=lambda: list(LEVEL_ORDER))
    max_distinct_level: str = "max"
    note: str = ""

    def effective_levels(self, model_levels: Optional[List[str]]) -> List[str]:
        """模型配置 ∩ 厂商原生档位，保持 LEVEL_ORDER 顺序。"""
        model_set = set(model_levels or LEVEL_ORDER)
        native = set(self.native_levels)
        return [lv for lv in LEVEL_ORDER if lv in model_set and lv in native]


# 厂商能力矩阵：OpenAI 仅区分 low/medium/high，不在 UI 暴露无效的 xhigh/max
VENDOR_PROFILES: Dict[str, VendorReasoningProfile] = {
    "deepseek": VendorReasoningProfile(
        vendor="deepseek",
        native_levels=["none", "low", "medium", "high", "xhigh", "max"],
        max_distinct_level="max",
    ),
    "openai": VendorReasoningProfile(
        vendor="openai",
        native_levels=["none", "low", "medium", "high"],
        max_distinct_level="high",
        note="OpenAI reasoning_effort 仅支持 low/medium/high",
    ),
    "qwen": VendorReasoningProfile(
        vendor="qwen",
        native_levels=["none", "low", "medium", "high", "xhigh", "max"],
        max_distinct_level="max",
    ),
    "glm": VendorReasoningProfile(
        vendor="glm",
        native_levels=["none", "low", "medium", "high"],
        max_distinct_level="high",
        note="GLM 通过 thinking.type 开关，无独立 budget 档位",
    ),
    "custom": VendorReasoningProfile(
        vendor="custom",
        native_levels=list(LEVEL_ORDER),
        max_distinct_level="max",
        note="自定义网关：依赖模型级 reasoning_param_map",
    ),
}


def vendor_profile(vendor: str = "custom") -> VendorReasoningProfile:
    return VENDOR_PROFILES.get(vendor, VENDOR_PROFILES["custom"])


def effective_reasoning_levels(
    model_levels: Optional[List[str]],
    vendor: str = "custom",
) -> List[str]:
    """返回当前模型在指定厂商下可选的思考档位。"""
    return vendor_profile(vendor).effective_levels(model_levels)


def vendor_capability_public(vendor: str = "custom") -> Dict[str, Any]:
    """供 integration_summary / 设置页展示的厂商能力摘要。"""
    profile = vendor_profile(vendor)
    return {
        "vendor": profile.vendor,
        "native_levels": list(profile.native_levels),
        "max_distinct_level": profile.max_distinct_level,
        "note": profile.note,
    }


def level_index(level: str) -> int:
    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return 0


def levels_up_to(max_level: str) -> List[str]:
    """从 none 到 max_level 的连续档位（用于模型「支持上限」滑动条）。"""
    idx = level_index(max_level)
    return LEVEL_ORDER[: idx + 1]


def clamp_level(level: str, allowed: Optional[List[str]] = None) -> str:
    if not allowed:
        return level if level in LEVEL_ORDER else "medium"
    allowed_sorted = [lv for lv in LEVEL_ORDER if lv in allowed]
    if not allowed_sorted:
        return "none"
    if level in allowed_sorted:
        return level
    idx = level_index(level)
    below = [lv for lv in allowed_sorted if level_index(lv) <= idx]
    return below[-1] if below else allowed_sorted[0]


def public_catalog() -> List[Dict[str, Any]]:
    return [
        {"id": lid, **LEVEL_META[lid], "index": i}
        for i, lid in enumerate(LEVEL_ORDER)
    ]


def resolve_param_map(
    level: str,
    vendor: str = "custom",
    model_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """合并厂商默认与模型自定义映射，得到当前档位应注入 LLM 的额外字段。"""
    if level == "none":
        return {}
    vendor_map = VENDOR_DEFAULT_MAPS.get(vendor, {})
    base = copy.deepcopy(vendor_map.get(level, {}))
    if not base:
        # GLM 等厂商未定义低档时，向下一档回退（如 low → medium 映射）
        idx = level_index(level)
        for lv in reversed(LEVEL_ORDER[:idx]):
            if lv in vendor_map:
                base = copy.deepcopy(vendor_map[lv])
                break
    custom = (model_map or {}).get(level) if isinstance(model_map, dict) else None
    if isinstance(custom, dict):
        base.update(custom)
    elif model_map and level in model_map:
        return copy.deepcopy(model_map[level]) if isinstance(model_map[level], dict) else {}
    return base


def merge_into_payload(
    payload: Dict[str, Any],
    level: str,
    *,
    vendor: str = "custom",
    model_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将思考强度参数深度合并进 OpenAI 兼容请求体。"""
    extra = resolve_param_map(level, vendor, model_map)
    if not extra:
        return payload
    result = copy.deepcopy(payload)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            merged = copy.deepcopy(result[key])
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    return result


# 模型内部推理文本在前端 thought 面板的最大长度
THOUGHT_MAX_CHARS = 8000


def extract_reasoning_text(message: Optional[Dict[str, Any]]) -> str:
    """从 OpenAI 兼容 message 中提取模型内部推理文本（DeepSeek/Qwen 等）。"""
    if not message:
        return ""
    for key in ("reasoning_content", "reasoning", "thinking_content"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    thinking = message.get("thinking")
    if isinstance(thinking, dict):
        for sub in ("content", "text"):
            val = thinking.get(sub)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def normalize_usage(raw: Any) -> Dict[str, Any]:
    """归一化 usage 字段，便于前端展示与统计。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        if key in raw and raw[key] is not None:
            out[key] = raw[key]
    details = raw.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = details["reasoning_tokens"]
    return out


def format_usage_status(meta: Dict[str, Any]) -> str:
    """生成简短的 token / 耗时状态行。"""
    usage = meta.get("usage") or {}
    parts: List[str] = []
    if usage.get("total_tokens") is not None:
        parts.append(f"tokens {usage['total_tokens']}")
    if usage.get("reasoning_tokens") is not None:
        parts.append(f"推理 {usage['reasoning_tokens']}")
    if meta.get("latency_ms") is not None:
        parts.append(f"{meta['latency_ms']}ms")
    level = meta.get("reasoning_level")
    if level and level != "none":
        label = LEVEL_META.get(level, {}).get("label", level)
        parts.append(f"强度 {label}")
    return " · ".join(parts)


def emit_llm_call_side_events(
    meta: Optional[Dict[str, Any]],
    make_event_fn,
) -> List[Dict[str, Any]]:
    """
    根据 LLMClient.last_call_meta 构造 thought / status 事件列表。

    :param make_event_fn: 与 agents.events.make_event 相同签名
    """
    meta = meta or {}
    events: List[Dict[str, Any]] = []
    reasoning = meta.get("reasoning") or ""
    if reasoning:
        events.append(make_event_fn("thought", {
            "content": reasoning[:THOUGHT_MAX_CHARS],
            "source": "model",
        }))
    status = format_usage_status(meta)
    if status:
        payload: Dict[str, Any] = {"message": status}
        if meta.get("usage"):
            payload["usage"] = meta["usage"]
        if meta.get("latency_ms") is not None:
            payload["latency_ms"] = meta["latency_ms"]
        if meta.get("reasoning_level"):
            payload["reasoning_level"] = meta["reasoning_level"]
        events.append(make_event_fn("status", payload))
    return events
