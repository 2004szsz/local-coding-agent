#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
思考强度全链路本地验证（无需真实 LLM 网络）。

检查项：
  1. 厂商能力矩阵与有效档位
  2. 注册表 → cfg → LLMClient 载荷
  3. UsageTracker 读写
  4. integration_summary 字段
  5. FastAPI /api/stats 与 /api/health（可选）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "OK" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def main() -> int:
    ok = True

    from app.reasoning import (
        effective_reasoning_levels,
        merge_into_payload,
        resolve_param_map,
        vendor_profile,
    )
    from app.model_registry import (
        ModelEntry,
        ModelRegistryState,
        ProviderEntry,
        apply_registry_to_config,
        integration_summary,
    )
    from app.usage_tracker import UsageTracker
    from tools.api_client import LLMClient

    openai_eff = effective_reasoning_levels(
        ["none", "low", "medium", "high", "xhigh", "max"], "openai")
    ok &= check(
        "openai effective levels",
        openai_eff == ["none", "low", "medium", "high"],
        str(openai_eff),
    )

    ds_map = resolve_param_map("max", "deepseek")
    ok &= check(
        "deepseek max budget",
        ds_map.get("thinking", {}).get("budget_tokens") == 65536,
        str(ds_map),
    )

    model = ModelEntry(id="m", name="deepseek-chat", reasoning_levels=["low", "medium", "high"])
    provider = ProviderEntry(
        id="p", name="DS", vendor="deepseek",
        base_url="https://api.deepseek.com/v1", api_key="x", models=[model],
    )
    state = ModelRegistryState(
        active_provider_id="p", active_model_id="m",
        active_reasoning_level="high", providers=[provider],
    )
    cfg = apply_registry_to_config({"llm": {"api_key": "x"}}, state)
    client = LLMClient(cfg)
    payload = client._payload([{"role": "user", "content": "hi"}])
    ok &= check(
        "LLMClient payload merge",
        "thinking" in payload and payload["thinking"]["budget_tokens"] == 16384,
        str(payload.get("thinking")),
    )

    summary = integration_summary(state, cfg)
    agent = summary.get("agent") or {}
    ok &= check(
        "integration_summary preview",
        bool(agent.get("reasoning_param_preview")),
        str(agent.get("reasoning_param_preview")),
    )
    ok &= check(
        "vendor profile note",
        bool(vendor_profile("openai").note),
        vendor_profile("openai").note,
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tracker = UsageTracker(Path(tmp) / "u.json")
        tracker.record_chat_turn(
            reasoning_level="medium",
            usage={"total_tokens": 42},
            latency_ms=100,
            llm_calls=1,
        )
        totals = tracker.summary()["totals"]
        ok &= check("usage tracker", totals["total_tokens"] == 42, str(totals))

    try:
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as client:
            health = client.get("/api/health")
            ok &= check("/api/health", health.status_code == 200)
            if health.status_code == 200:
                body = health.json()
                ok &= check(
                    "health.usage",
                    "usage" in body and "total_tokens" in body["usage"],
                    str(body.get("usage")),
                )
            stats = client.get("/api/stats")
            ok &= check("/api/stats", stats.status_code == 200)
            if stats.status_code == 200:
                ok &= check(
                    "stats.totals",
                    "totals" in stats.json(),
                    str(stats.json().get("totals")),
                )
    except Exception as e:  # noqa: BLE001
        ok &= check("fastapi routes", False, f"{type(e).__name__}: {e}")

    print()
    print("全链路验证:", "通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
