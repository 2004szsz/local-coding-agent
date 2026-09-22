# -*- coding: utf-8 -*-
"""模型注册表：持久化、配置合并、Agent / RAG 解析。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.config import DEFAULT_CONFIG
from app.model_registry import (
    ModelEntry,
    ModelRegistryState,
    ProviderEntry,
    apply_registry_to_config,
    default_registry,
    load_registry,
    resolve_active_chat,
    resolve_rag_embedding,
    save_registry,
)


class ModelRegistryTests(unittest.TestCase):
    def test_default_registry_has_no_ollama(self):
        reg = default_registry()
        vendors = {p.vendor for p in reg.providers}
        self.assertNotIn("ollama", vendors)
        self.assertIn("deepseek", vendors)

    def test_apply_active_chat_to_config(self):
        reg = default_registry()
        provider = reg.providers[0]
        chat = next(m for m in provider.models if m.role == "chat")
        reg.active_provider_id = provider.id
        reg.active_model_id = chat.id
        cfg = apply_registry_to_config(copy.deepcopy(DEFAULT_CONFIG), reg)
        self.assertEqual(cfg["llm"]["base_url"], provider.base_url.rstrip("/"))
        self.assertEqual(cfg["llm"]["model"], chat.name)
        self.assertEqual(cfg["llm"]["registry"]["provider_id"], provider.id)

    def test_rag_follow_active_provider_embedding(self):
        reg = default_registry()
        provider = reg.providers[0]
        emb = ModelEntry(id="emb1", name="text-embedding-test", role="embedding", enabled=True)
        provider.models.append(emb)
        reg.active_provider_id = provider.id
        reg.active_model_id = next(m for m in provider.models if m.role == "chat").id
        reg.rag_use_active_provider = True
        p, m = resolve_rag_embedding(reg)
        self.assertEqual(p.id, provider.id)
        self.assertEqual(m.name, "text-embedding-test")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_registry.json"
            import app.model_registry as mod
            orig = mod.REGISTRY_FILE
            mod.REGISTRY_FILE = path
            try:
                reg = default_registry()
                reg.providers[0].api_key = "sk-test-secret"
                save_registry(reg)
                loaded = load_registry()
                self.assertEqual(loaded.providers[0].api_key, "sk-test-secret")
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["version"], 1)
            finally:
                mod.REGISTRY_FILE = orig

    def test_resolve_skips_disabled(self):
        reg = ModelRegistryState(
            active_provider_id="p1",
            active_model_id="m1",
            providers=[ProviderEntry(
                id="p1", name="P", enabled=False, base_url="https://x/v1",
                models=[ModelEntry(id="m1", name="m", enabled=True)],
            )],
        )
        self.assertEqual(resolve_active_chat(reg), (None, None))


if __name__ == "__main__":
    unittest.main()
