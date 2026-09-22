# -*- coding: utf-8 -*-
"""临时诊断：SSE 流中 POST 决议是否送达主循环。跑完即删。"""
import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from tests.fake_llm_server import FakeLLMServer

tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)
ws = root / "ws"; ext = root / "ext"
ws.mkdir(); ext.mkdir()
(ws / "check_ok.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")

PLAN = {"summary": "写外部文件", "tasks": [{
    "title": "向根 ext 写入 result.txt",
    "detail": "fs_write", "scope": ["check_ok.py"],
    "commands": [["python", "check_ok.py"]], "must_read": []}]}
llm = FakeLLMServer(plan=PLAN, actions=[{"kind": "tool_batch", "calls": [
    {"name": "fs_write", "arguments": {"root": "ext", "path": "result.txt",
                                      "content": "approved"}}]}])
llm.start()

cfg = copy.deepcopy(DEFAULT_CONFIG)
cfg["server"]["workspace_root"] = str(ws)
cfg["llm"].update({"base_url": llm.base_url, "api_key": "k", "model": "fake", "timeout": 30})
cfg["llm"]["embedding"]["enabled"] = False
cfg["rag"]["enabled"] = False
cfg["storage"]["history_db"] = str(root / "h.db")
cfg["storage"]["sessions_dir"] = str(root / "sessions")
cfg["local_access"]["enabled"] = True
cfg["local_access"]["roots"] = [{"name": "ext", "path": str(ext), "read": True, "write": True}]
cfg["local_access"]["audit_log"] = ""
cfg["permissions"]["timeout_seconds"] = 20
cfg["permissions"]["audit_log"] = str(root / "perm.jsonl")

os.environ["AGENT_FRAMEWORK"] = "state_loop"
import app.main as main_module
main_module.load_config = lambda *a, **k: cfg

client = TestClient(main_module.app).__enter__()
results = {}

def post_decision(rid):
    t0 = time.time()
    pending = client.get("/api/permissions/pending")
    results["pending_body"] = pending.text
    resp = client.post(f"/api/permissions/{rid}", json={"decision": "allow"})
    results["post_status"] = resp.status_code
    results["post_body"] = resp.text
    results["post_elapsed"] = time.time() - t0

with client.stream("POST", "/api/chat/stream", json={"message": "写外部文件"}) as resp:
    kinds = []
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        ev = json.loads(line[5:].strip())
        kinds.append(ev["type"])
        if ev["type"] == "permission_request":
            rid = ev["data"]["request_id"]
            print("收到 permission_request:", rid)
            t = threading.Thread(target=post_decision, args=(rid,), daemon=True)
            t.start()
            t.join(timeout=10)
            print("POST 结果:", results)
print("事件序列:", kinds)
print("文件存在:", (ext / "result.txt").exists())
audit = Path(cfg["permissions"]["audit_log"])
print("审计存在:", audit.exists())
if audit.exists():
    print("审计内容:", audit.read_text(encoding="utf-8"))
tmp.cleanup()