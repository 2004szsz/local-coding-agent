# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from app.usage_tracker import UsageTracker, merge_usage_dict


class UsageTrackerTests(unittest.TestCase):
    def test_merge_usage(self):
        a = {"total_tokens": 10, "reasoning_tokens": 2}
        b = {"total_tokens": 5, "prompt_tokens": 3}
        merged = merge_usage_dict(a, b)
        self.assertEqual(merged["total_tokens"], 15)
        self.assertEqual(merged["reasoning_tokens"], 2)
        self.assertEqual(merged["prompt_tokens"], 3)

    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            tracker = UsageTracker(path)
            tracker.record_chat_turn(
                reasoning_level="medium",
                framework="native_react",
                usage={"total_tokens": 100, "reasoning_tokens": 20},
                latency_ms=500,
                tool_calls=2,
                llm_calls=3,
            )
            summary = tracker.summary(session_count=5)
            self.assertEqual(summary["totals"]["messages"], 2)
            self.assertEqual(summary["totals"]["total_tokens"], 100)
            self.assertEqual(summary["totals"]["tool_calls"], 2)
            self.assertEqual(summary["totals"]["tasks"], 5)
            self.assertEqual(len(summary["by_reasoning_level"]), 1)
            self.assertEqual(summary["by_reasoning_level"][0]["level"], "medium")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["totals"]["llm_calls"], 3)


if __name__ == "__main__":
    unittest.main()
