# -*- coding: utf-8 -*-
"""思考强度全链路集成：注册表 → 配置 → LLM 载荷 → integration 摘要。"""
import unittest

from app.model_registry import (
    ModelEntry,
    ModelRegistryState,
    ProviderEntry,
    apply_registry_to_config,
    integration_summary,
)
from app.reasoning import effective_reasoning_levels, merge_into_payload
from tools.api_client import LLMClient


class ReasoningIntegrationTests(unittest.TestCase):
    def _state(self, vendor: str, model_levels: list[str]) -> ModelRegistryState:
        model = ModelEntry(
            id="m1",
            name="gpt-test",
            reasoning_levels=model_levels,
            default_reasoning_level="high",
        )
        provider = ProviderEntry(
            id="p1",
            name="Test",
            vendor=vendor,
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            models=[model],
        )
        return ModelRegistryState(
            active_provider_id="p1",
            active_model_id="m1",
            active_reasoning_level="max",
            providers=[provider],
        )

    def test_openai_filters_xhigh_from_effective_levels(self):
        state = self._state("openai", ["none", "low", "medium", "high", "xhigh", "max"])
        cfg = apply_registry_to_config({"llm": {}}, state)
        allowed = cfg["llm"]["registry"]["reasoning_levels"]
        self.assertNotIn("xhigh", allowed)
        self.assertNotIn("max", allowed)
        self.assertEqual(cfg["llm"]["reasoning_level"], "high")

    def test_deepseek_keeps_max_level(self):
        state = self._state("deepseek", ["low", "medium", "high", "xhigh", "max"])
        state.active_reasoning_level = "max"
        cfg = apply_registry_to_config({"llm": {}}, state)
        self.assertEqual(cfg["llm"]["reasoning_level"], "max")
        payload = LLMClient(cfg).reasoning_extra()
        self.assertEqual(payload["thinking"]["budget_tokens"], 65536)

    def test_integration_summary_param_preview(self):
        state = self._state("deepseek", ["low", "medium", "high"])
        cfg = apply_registry_to_config({"llm": {}}, state)
        summary = integration_summary(state, cfg)
        agent = summary["agent"]
        self.assertIn("reasoning_param_preview", agent)
        self.assertIn("thinking", agent["reasoning_param_preview"])
        self.assertEqual(agent["reasoning_vendor"]["vendor"], "deepseek")

    def test_merge_payload_chain(self):
        payload = merge_into_payload(
            {"model": "x", "messages": []},
            "medium",
            vendor="qwen",
        )
        self.assertTrue(payload["enable_thinking"])
        self.assertEqual(payload["thinking_budget"], 8192)

    def test_effective_levels_intersection(self):
        levels = effective_reasoning_levels(
            ["none", "low", "medium", "high", "xhigh"],
            "openai",
        )
        self.assertEqual(levels, ["none", "low", "medium", "high"])


if __name__ == "__main__":
    unittest.main()
