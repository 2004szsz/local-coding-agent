# -*- coding: utf-8 -*-
"""把 e2e 事件 JSON 压缩成可读摘要（开发调试用）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIAG = PROJECT_ROOT / "data" / "logs" / "diagnostics"
TAG = os.environ.get("E2E_TAG", "a")

events_path = DIAG / f"e2e_{TAG}_events.json"
result_path = DIAG / f"e2e_{TAG}_result.json"
output_path = DIAG / f"e2e_{TAG}_seq.txt"

ev = json.loads(events_path.read_text(encoding="utf-8"))
out = []
for i, e in enumerate(ev):
    if e["kind"] == "token":
        continue
    out.append(f'{i:03d} {e["kind"]:18s} {json.dumps(e["data"], ensure_ascii=False)[:260]}')
out.append("token_count=" + str(sum(1 for e in ev if e["kind"] == "token")))
r = json.loads(result_path.read_text(encoding="utf-8"))
out.append("")
out.append("acceptance_exit_code=" + str(r.get("acceptance_exit_code")))
out.append("run_seconds=" + str(r.get("run_seconds")))
out.append("calc_after:")
out.append(r.get("calc_after", ""))
out.append("acceptance_output_tail:")
out.append("\n".join((r.get("acceptance_output") or "").splitlines()[-12:]))
output_path.write_text("\n".join(out), encoding="utf-8")
