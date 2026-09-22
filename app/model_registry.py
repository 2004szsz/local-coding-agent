# -*- coding: utf-8 -*-
"""
自定义模型供应商注册表。

持久化到 data/model_registry.json，供设置中心 CRUD 与运行时热切换。
Agent 主循环与 RAG 嵌入均走 OpenAI 兼容协议（/chat/completions、/embeddings）。

不包含 Ollama 预设；用户可自行添加任意 OpenAI 兼容网关。
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import PROJECT_ROOT
from tools.net import trust_env_for

REGISTRY_FILE = PROJECT_ROOT / "data" / "model_registry.json"
REGISTRY_VERSION = 1

# Agent 循环当前仅对接 OpenAI Chat Completions
AGENT_SUPPORTED_FORMATS = frozenset({"openai_chat"})

API_FORMAT_LABELS = {
    "openai_chat": "OpenAI Chat Completions",
    "openai_responses": "OpenAI Responses",
    "anthropic_messages": "Anthropic Messages",
    "google_generate": "Google Generative Language",
}

VENDOR_PRESETS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_format": "openai_chat",
        "models": [
            {"name": "gpt-4.1", "context_window": 128000, "max_output_tokens": 16384},
            {"name": "gpt-4.1-mini", "context_window": 128000, "max_output_tokens": 16384},
            {"name": "text-embedding-3-small", "role": "embedding"},
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_format": "openai_chat",
        "models": [
            {"name": "deepseek-chat", "context_window": 64000, "max_output_tokens": 8192},
            {"name": "deepseek-reasoner", "context_window": 64000, "max_output_tokens": 8192,
             "reasoning_levels": ["low", "medium", "high"]},
        ],
    },
    "anthropic": {
        "name": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com/v1",
        "api_format": "anthropic_messages",
        "models": [
            {"name": "claude-sonnet-4-20250514", "context_window": 200000, "max_output_tokens": 8192},
            {"name": "claude-opus-4-20250514", "context_window": 200000, "max_output_tokens": 8192},
        ],
    },
    "qwen": {
        "name": "通义千问 (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_format": "openai_chat",
        "models": [
            {"name": "qwen-plus", "context_window": 131072, "max_output_tokens": 8192},
            {"name": "qwen-max", "context_window": 131072, "max_output_tokens": 8192},
            {"name": "text-embedding-v3", "role": "embedding"},
        ],
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_format": "openai_chat",
        "models": [
            {"name": "glm-4-plus", "context_window": 128000, "max_output_tokens": 4096},
            {"name": "glm-4-flash", "context_window": 128000, "max_output_tokens": 4096},
            {"name": "embedding-3", "role": "embedding"},
        ],
    },
    "gemini": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_format": "openai_chat",
        "models": [
            {"name": "gemini-2.5-pro", "context_window": 1048576, "max_output_tokens": 8192},
            {"name": "gemini-2.5-flash", "context_window": 1048576, "max_output_tokens": 8192},
        ],
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "api_format": "openai_chat",
        "models": [
            {"name": "moonshot-v1-8k", "context_window": 8192, "max_output_tokens": 4096},
            {"name": "moonshot-v1-32k", "context_window": 32768, "max_output_tokens": 4096},
        ],
    },
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _default_capabilities(**overrides: Any) -> Dict[str, bool]:
    base = {
        "tools": True,
        "vision": False,
        "structured_output": False,
        "web_search": False,
        "system_message": True,
    }
    base.update(overrides)
    return base


@dataclass
class ModelEntry:
    id: str
    name: str
    enabled: bool = True
    role: str = "chat"  # chat | embedding
    context_window: int = 128000
    max_output_tokens: int = 4096
    temperature: float = 0.2
    input_types: List[str] = field(default_factory=lambda: ["text"])
    capabilities: Dict[str, bool] = field(default_factory=_default_capabilities)
    reasoning_levels: List[str] = field(default_factory=lambda: ["none", "low", "medium", "high"])
    reasoning_param_map: Dict[str, Any] = field(default_factory=dict)
    default_reasoning_level: str = "medium"
    smart_config: bool = False

    def to_dict(self, *, mask: bool = False) -> Dict[str, Any]:
        data = asdict(self)
        data["agent_compatible"] = True  # filled by registry when serializing
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelEntry":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: data[k] for k in known if k in data}
        if "capabilities" not in kwargs:
            kwargs["capabilities"] = _default_capabilities()
        return cls(**kwargs)


@dataclass
class ProviderEntry:
    id: str
    name: str
    vendor: str = "custom"
    enabled: bool = True
    base_url: str = ""
    api_format: str = "openai_chat"
    api_key: str = ""
    models: List[ModelEntry] = field(default_factory=list)
    notes: str = ""

    def find_model(self, model_id: str) -> Optional[ModelEntry]:
        for m in self.models:
            if m.id == model_id:
                return m
        return None

    def to_public_dict(self) -> Dict[str, Any]:
        key = self.api_key or ""
        preview = ""
        if len(key) > 8:
            preview = key[:3] + "…" + key[-4:]
        elif key:
            preview = "••••"
        return {
            "id": self.id,
            "name": self.name,
            "vendor": self.vendor,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "api_format": self.api_format,
            "api_format_label": API_FORMAT_LABELS.get(self.api_format, self.api_format),
            "agent_compatible": self.api_format in AGENT_SUPPORTED_FORMATS,
            "api_key_set": bool(key),
            "api_key_preview": preview,
            "notes": self.notes,
            "models": [self._model_public(m) for m in self.models],
        }

    def _model_public(self, m: ModelEntry) -> Dict[str, Any]:
        d = m.to_dict()
        d["agent_compatible"] = self.api_format in AGENT_SUPPORTED_FORMATS and m.role == "chat"
        d["tags"] = _model_tags(m)
        return d


def _model_tags(m: ModelEntry) -> List[str]:
    tags: List[str] = []
    if m.role == "embedding":
        tags.append("嵌入")
    if m.context_window >= 1_000_000:
        tags.append("1M")
    elif m.context_window >= 100_000:
        tags.append(f"{m.context_window // 1000}K")
    caps = m.capabilities or {}
    if caps.get("vision"):
        tags.append("视觉")
    if caps.get("tools"):
        tags.append("工具")
    if len(m.reasoning_levels or []) > 1 or (m.reasoning_levels and m.reasoning_levels[0] != "none"):
        tags.append("推理")
    return tags


@dataclass
class ModelRegistryState:
    version: int = REGISTRY_VERSION
    active_provider_id: Optional[str] = None
    active_model_id: Optional[str] = None
    rag_use_active_provider: bool = True
    rag_provider_id: Optional[str] = None
    rag_model_id: Optional[str] = None
    active_reasoning_level: str = "medium"
    providers: List[ProviderEntry] = field(default_factory=list)

    def find_provider(self, provider_id: str) -> Optional[ProviderEntry]:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    def to_public_dict(self) -> Dict[str, Any]:
        from app.reasoning import public_catalog

        return {
            "version": self.version,
            "active": {
                "provider_id": self.active_provider_id,
                "model_id": self.active_model_id,
                "reasoning_level": self.active_reasoning_level,
            },
            "reasoning_catalog": public_catalog(),
            "rag": {
                "use_active_provider": self.rag_use_active_provider,
                "provider_id": self.rag_provider_id,
                "model_id": self.rag_model_id,
            },
            "providers": [p.to_public_dict() for p in self.providers],
            "presets": [
                {"vendor": k, "name": v["name"], "base_url": v["base_url"],
                 "api_format": v["api_format"]}
                for k, v in VENDOR_PRESETS.items()
            ],
            "api_formats": [
                {"id": k, "label": v, "agent_compatible": k in AGENT_SUPPORTED_FORMATS}
                for k, v in API_FORMAT_LABELS.items()
            ],
        }


def _model_from_preset(raw: Dict[str, Any]) -> ModelEntry:
    role = raw.get("role", "chat")
    caps = _default_capabilities(vision=bool(raw.get("vision")))
    if role == "embedding":
        caps = _default_capabilities(tools=False, system_message=False)
    return ModelEntry(
        id=_new_id(),
        name=raw["name"],
        enabled=True,
        role=role,
        context_window=int(raw.get("context_window", 8192 if role == "embedding" else 128000)),
        max_output_tokens=int(raw.get("max_output_tokens", 4096)),
        reasoning_levels=list(raw.get("reasoning_levels", ["none", "low", "medium", "high"])),
        default_reasoning_level=str(raw.get("default_reasoning_level", "medium")),
        capabilities=caps,
    )


def _provider_from_preset(vendor: str) -> ProviderEntry:
    preset = VENDOR_PRESETS[vendor]
    return ProviderEntry(
        id=vendor if vendor not in ("custom",) else _new_id(),
        name=preset["name"],
        vendor=vendor,
        enabled=True,
        base_url=preset["base_url"],
        api_format=preset.get("api_format", "openai_chat"),
        models=[_model_from_preset(m) for m in preset.get("models", [])],
    )


def default_registry() -> ModelRegistryState:
    """首次启动的种子数据（无 Ollama）。"""
    providers = [
        _provider_from_preset("deepseek"),
        _provider_from_preset("openai"),
        _provider_from_preset("qwen"),
    ]
    active_p = providers[0]
    active_m = next((m for m in active_p.models if m.role == "chat"), active_p.models[0])
    return ModelRegistryState(
        active_provider_id=active_p.id,
        active_model_id=active_m.id,
        active_reasoning_level="medium",
        rag_use_active_provider=True,
        providers=providers,
    )


def load_registry() -> ModelRegistryState:
    if not REGISTRY_FILE.is_file():
        state = default_registry()
        save_registry(state)
        return state
    data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    providers = []
    for p in data.get("providers") or []:
        models = [ModelEntry.from_dict(m) for m in p.get("models") or []]
        providers.append(ProviderEntry(
            id=p["id"],
            name=p["name"],
            vendor=p.get("vendor", "custom"),
            enabled=bool(p.get("enabled", True)),
            base_url=p.get("base_url", ""),
            api_format=p.get("api_format", "openai_chat"),
            api_key=p.get("api_key", ""),
            notes=p.get("notes", ""),
            models=models,
        ))
    return ModelRegistryState(
        version=int(data.get("version", REGISTRY_VERSION)),
        active_provider_id=data.get("active_provider_id") or (data.get("active") or {}).get("provider_id"),
        active_model_id=data.get("active_model_id") or (data.get("active") or {}).get("model_id"),
        active_reasoning_level=str(
            data.get("active_reasoning_level")
            or (data.get("active") or {}).get("reasoning_level")
            or "medium"),
        rag_use_active_provider=bool(data.get("rag_use_active_provider",
                                               (data.get("rag") or {}).get("use_active_provider", True))),
        rag_provider_id=data.get("rag_provider_id") or (data.get("rag") or {}).get("provider_id"),
        rag_model_id=data.get("rag_model_id") or (data.get("rag") or {}).get("model_id"),
        providers=providers,
    )


def save_registry(state: ModelRegistryState) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": state.version,
        "active_provider_id": state.active_provider_id,
        "active_model_id": state.active_model_id,
        "active_reasoning_level": state.active_reasoning_level,
        "rag_use_active_provider": state.rag_use_active_provider,
        "rag_provider_id": state.rag_provider_id,
        "rag_model_id": state.rag_model_id,
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "vendor": p.vendor,
                "enabled": p.enabled,
                "base_url": p.base_url,
                "api_format": p.api_format,
                "api_key": p.api_key,
                "notes": p.notes,
                "models": [asdict(m) for m in p.models],
            }
            for p in state.providers
        ],
    }
    REGISTRY_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_active_chat(state: ModelRegistryState) -> Tuple[Optional[ProviderEntry], Optional[ModelEntry]]:
    if not state.active_provider_id or not state.active_model_id:
        return None, None
    provider = state.find_provider(state.active_provider_id)
    if not provider or not provider.enabled:
        return None, None
    model = provider.find_model(state.active_model_id)
    if not model or not model.enabled or model.role != "chat":
        return None, None
    if provider.api_format not in AGENT_SUPPORTED_FORMATS:
        return None, None
    return provider, model


def resolve_rag_embedding(
    state: ModelRegistryState,
    fallback_embed_model: str = "",
) -> Tuple[Optional[ProviderEntry], Optional[ModelEntry]]:
    """解析 RAG 嵌入端点：可跟随 Agent 供应商，或绑定独立嵌入模型。"""
    if state.rag_use_active_provider:
        provider, _ = resolve_active_chat(state)
        if not provider:
            return None, None
        emb = next((m for m in provider.models if m.enabled and m.role == "embedding"), None)
        if emb:
            return provider, emb
        if fallback_embed_model:
            return provider, ModelEntry(
                id="__fallback__",
                name=fallback_embed_model,
                role="embedding",
                enabled=True,
                capabilities=_default_capabilities(tools=False, system_message=False),
            )
        return None, None
    if not state.rag_provider_id or not state.rag_model_id:
        return None, None
    provider = state.find_provider(state.rag_provider_id)
    if not provider or not provider.enabled:
        return None, None
    model = provider.find_model(state.rag_model_id)
    if not model or not model.enabled:
        return None, None
    return provider, model


def apply_registry_to_config(cfg: Dict[str, Any], state: Optional[ModelRegistryState] = None) -> Dict[str, Any]:
    """
    将注册表中的活跃模型写入 cfg['llm']（及 embedding 段）。
    无有效选择时保留 config.yaml / 环境变量中的值。
    """
    reg = state or load_registry()
    cfg = copy.deepcopy(cfg)

    provider, model = resolve_active_chat(reg)
    if provider and model:
        cfg["llm"]["provider"] = provider.vendor
        cfg["llm"]["base_url"] = provider.base_url.rstrip("/")
        cfg["llm"]["api_key"] = provider.api_key or cfg["llm"].get("api_key", "")
        cfg["llm"]["model"] = model.name
        cfg["llm"]["temperature"] = float(model.temperature)
        cfg["llm"]["max_tokens"] = int(model.max_output_tokens)
        from app.reasoning import clamp_level, effective_reasoning_levels

        allowed = effective_reasoning_levels(
            model.reasoning_levels or ["none"], provider.vendor)
        level = clamp_level(reg.active_reasoning_level or model.default_reasoning_level, allowed)
        cfg["llm"]["registry"] = {
            "provider_id": provider.id,
            "model_id": model.id,
            "vendor": provider.vendor,
            "api_format": provider.api_format,
            "context_window": model.context_window,
            "capabilities": model.capabilities,
            "reasoning_levels": allowed,
            "reasoning_levels_model": list(model.reasoning_levels or []),
            "reasoning_param_map": model.reasoning_param_map,
            "reasoning_level": level,
            "default_reasoning_level": model.default_reasoning_level,
            "reasoning_vendor": provider.vendor,
        }
        cfg["llm"]["reasoning_level"] = level

    fallback_emb = (cfg.get("llm") or {}).get("embedding", {}).get("model", "")
    emb_provider, emb_model = resolve_rag_embedding(reg, fallback_embed_model=fallback_emb)
    if emb_provider and emb_model:
        emb = cfg["llm"].get("embedding") or {}
        emb["enabled"] = True
        emb["base_url"] = emb_provider.base_url.rstrip("/")
        emb["api_key"] = emb_provider.api_key or emb.get("api_key", "")
        emb["model"] = emb_model.name
        cfg["llm"]["embedding"] = emb

    return cfg


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key or 'no-key'}",
        "Content-Type": "application/json",
    }


def test_provider_connection(provider: ProviderEntry) -> Dict[str, Any]:
    """探活：GET /models。"""
    base = (provider.base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "detail": "Base URL 未配置"}
    if provider.api_format not in AGENT_SUPPORTED_FORMATS:
        return {
            "ok": False,
            "detail": f"API 格式 {API_FORMAT_LABELS.get(provider.api_format, provider.api_format)} "
                      f"暂不支持 Agent 循环，请改用 OpenAI Chat Completions 或兼容网关",
        }
    trust = trust_env_for(base)
    try:
        resp = httpx.get(base + "/models", headers=_headers(provider.api_key),
                         timeout=12, trust_env=trust)
        if resp.status_code == 200:
            return {"ok": True, "detail": "连接成功", "status_code": resp.status_code}
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "status_code": resp.status_code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def test_model_chat(provider: ProviderEntry, model: ModelEntry) -> Dict[str, Any]:
    """最小对话探针，验证模型 ID、Key 与思考强度载荷是否被网关接受。"""
    base = (provider.base_url or "").rstrip("/")
    if provider.api_format not in AGENT_SUPPORTED_FORMATS:
        return {"ok": False, "detail": "当前 API 格式不支持对话探针"}
    from app.reasoning import (
        clamp_level,
        effective_reasoning_levels,
        merge_into_payload,
        resolve_param_map,
    )

    allowed = effective_reasoning_levels(model.reasoning_levels, provider.vendor)
    level = clamp_level(model.default_reasoning_level or "medium", allowed)
    url = base + "/chat/completions"
    payload = merge_into_payload({
        "model": model.name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }, level, vendor=provider.vendor, model_map=model.reasoning_param_map)
    trust = trust_env_for(base)
    try:
        resp = httpx.post(url, headers=_headers(provider.api_key), json=payload,
                          timeout=30, trust_env=trust)
        if resp.status_code == 200:
            body = resp.json()
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            return {
                "ok": True,
                "detail": "模型响应正常",
                "sample": str(content)[:120],
                "reasoning_level": level,
                "reasoning_levels_effective": allowed,
                "reasoning_payload": resolve_param_map(
                    level, provider.vendor, model.reasoning_param_map),
            }
        return {
            "ok": False,
            "detail": f"HTTP {resp.status_code}: {resp.text[:300]}",
            "reasoning_level": level,
            "reasoning_payload": resolve_param_map(
                level, provider.vendor, model.reasoning_param_map),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def add_provider_from_preset(state: ModelRegistryState, vendor: str,
                             name: Optional[str] = None) -> ProviderEntry:
    if vendor in VENDOR_PRESETS:
        entry = _provider_from_preset(vendor)
        if name:
            entry.name = name.strip()
        # 避免与已有 id 冲突
        if state.find_provider(entry.id):
            entry.id = f"{vendor}-{_new_id()[:6]}"
    else:
        entry = ProviderEntry(
            id=_new_id(),
            name=(name or "自定义供应商").strip(),
            vendor="custom",
            base_url="",
            api_format="openai_chat",
            models=[],
        )
    state.providers.append(entry)
    return entry


def integration_summary(state: ModelRegistryState, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """供前端展示 Agent / RAG 衔接状态。"""
    provider, model = resolve_active_chat(state)
    emb_p, emb_m = resolve_rag_embedding(
        state, fallback_embed_model=(cfg.get("llm") or {}).get("embedding", {}).get("model", ""))
    loop = (cfg.get("llm") or {}).get("registry") or {}
    from app.reasoning import (
        LEVEL_META,
        effective_reasoning_levels,
        resolve_param_map,
        vendor_capability_public,
    )

    vendor = provider.vendor if provider else "custom"
    model_levels = model.reasoning_levels if model else []
    effective = effective_reasoning_levels(model_levels, vendor)
    rl = state.active_reasoning_level or loop.get("reasoning_level") or "medium"
    meta = LEVEL_META.get(rl, LEVEL_META["medium"])
    param_preview = resolve_param_map(
        rl, vendor, model.reasoning_param_map if model else None)
    return {
        "agent": {
            "ready": bool(provider and model),
            "provider": provider.name if provider else None,
            "model": model.name if model else None,
            "vendor": vendor,
            "api_format": provider.api_format if provider else None,
            "context_window": model.context_window if model else None,
            "capabilities": model.capabilities if model else {},
            "reasoning_level": rl,
            "reasoning_label": meta.get("label"),
            "reasoning_color": meta.get("color"),
            "reasoning_levels": effective,
            "reasoning_levels_model": list(model_levels),
            "reasoning_vendor": vendor_capability_public(vendor),
            "reasoning_param_preview": param_preview,
            "framework_note": "state_loop 通过 OpenAI 兼容接口调用模型并驱动工具循环",
        },
        "rag": {
            "ready": bool(emb_p and emb_m),
            "provider": emb_p.name if emb_p else None,
            "model": emb_m.name if emb_m else None,
            "use_active_provider": state.rag_use_active_provider,
            "endpoint": (cfg.get("llm") or {}).get("embedding", {}).get("base_url"),
        },
        "active_registry": loop,
    }
