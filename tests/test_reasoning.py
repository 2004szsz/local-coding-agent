# -*- coding: utf-8 -*-
import unittest

from app.reasoning import (
    clamp_level,
    effective_reasoning_levels,
    emit_llm_call_side_events,
    extract_reasoning_text,
    levels_up_to,
    merge_into_payload,
    normalize_usage,
    resolve_param_map,
    vendor_profile,
)


class ReasoningTests(unittest.TestCase):
    def test_levels_up_to(self):
        self.assertEqual(levels_up_to("high"), ["none", "low", "medium", "high"])

    def test_clamp_to_allowed_max(self):
        self.assertEqual(clamp_level("max", ["none", "low", "medium", "high"]), "high")

    def test_merge_deepseek_thinking(self):
        payload = {"model": "deepseek-reasoner", "messages": []}
        out = merge_into_payload(payload, "medium", vendor="deepseek")
        self.assertIn("thinking", out)

    def test_resolve_custom_overrides_vendor(self):
        custom = {"medium": {"thinking": {"budget_tokens": 999}}}
        m = resolve_param_map("medium", "deepseek", custom)
        self.assertEqual(m["thinking"]["budget_tokens"], 999)

    def test_extract_reasoning_content(self):
        msg = {"role": "assistant", "content": "hi", "reasoning_content": "think first"}
        self.assertEqual(extract_reasoning_text(msg), "think first")

    def test_normalize_usage_reasoning_tokens(self):
        usage = normalize_usage({
            "total_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 42},
        })
        self.assertEqual(usage["total_tokens"], 100)
        self.assertEqual(usage["reasoning_tokens"], 42)

    def test_emit_side_events(self):
        events = emit_llm_call_side_events({
            "reasoning": "chain",
            "usage": {"total_tokens": 10},
            "latency_ms": 50,
            "reasoning_level": "medium",
        }, lambda t, d: {"type": t, "data": d})
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["thought", "status"])
        self.assertEqual(events[0]["data"]["source"], "model")

    def test_openai_effective_levels(self):
        levels = effective_reasoning_levels(
            ["none", "low", "medium", "high", "xhigh", "max"], "openai")
        self.assertEqual(levels, ["none", "low", "medium", "high"])

    def test_glm_low_fallback(self):
        m = resolve_param_map("low", "glm")
        self.assertIn("thinking", m)

    def test_vendor_profile_note(self):
        self.assertIn("reasoning_effort", vendor_profile("openai").note)


if __name__ == "__main__":
    unittest.main()
