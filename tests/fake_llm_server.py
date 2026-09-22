# -*- coding: utf-8 -*-
"""
假 OpenAI 兼容模型服务（**仅测试用**）。

为什么需要它：要验证「HTTP → SSE → 状态机 → 工具 → 验收」这条完整链路，
必须有真实的 HTTP 往返；而本地 Ollama 在无模型的机器或 CI 上不可用。

它按请求里的 **tool schema 形状**判断当前是哪个阶段，而不是靠调用序号猜：
- 工具参数里有 `tasks` → 拆解阶段，返回注入的计划
- 工具参数里有 `kind`  → 决策阶段，按脚本顺序返回动作
- 请求里没有 tools     → 收尾文本阶段，返回普通 content

同时记录每个请求，便于断言「tool_choice 确实被强制成 submit_decision」。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

__all__ = ["FakeLLMServer"]


class FakeLLMServer:
    def __init__(self, *, plan: Dict[str, Any],
                 actions: Optional[List[Dict[str, Any]]] = None,
                 final_text: str = "已完成修改并通过验收。",
                 model: str = "fake-model"):
        self.plan = plan
        self.actions = list(actions or [])
        self.final_text = final_text
        self.model = model
        self.requests: List[Dict[str, Any]] = []

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._index = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FakeLLM/1.0"

            def log_message(self, *args):  # 静音，保持测试输出干净
                return

            def _send(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list",
                                     "data": [{"id": server.model, "object": "model"}]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                server.requests.append(body)

                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send(404, {"error": "not found"})
                    return

                tools = body.get("tools") or []
                if not tools:
                    self._send(200, _completion(
                        {"role": "assistant", "content": server.final_text}))
                    return

                properties = (((tools[0].get("function") or {}).get("parameters") or {})
                              .get("properties") or {})
                if "tasks" in properties:
                    arguments = server.plan
                elif "kind" in properties:
                    arguments = server._next_action()
                else:
                    arguments = {}

                call = {
                    "id": f"call_{len(server.requests)}",
                    "type": "function",
                    "function": {"name": "submit_decision",
                                 "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
                self._send(200, _completion(
                    {"role": "assistant", "content": None, "tool_calls": [call]}))

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    # ---------------- 生命周期 ----------------
    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="fake-llm", daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 对内 ----------------
    def _next_action(self) -> Dict[str, Any]:
        if self._index < len(self.actions):
            action = self.actions[self._index]
            self._index += 1
            return action
        return {"kind": "mark_done", "note": "没有更多动作"}

    def decision_requests(self) -> List[Dict[str, Any]]:
        """只取带工具定义的请求（即拆解 / 决策阶段）。"""
        return [item for item in self.requests if item.get("tools")]


def _completion(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
