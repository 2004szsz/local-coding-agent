# -*- coding: utf-8 -*-
"""
MCP 客户端：stdio 与 streamable HTTP 两种传输。

**只做客户端。** 本项目是能力消费方；把自身暴露成 MCP Server 会与
「只监听 127.0.0.1 + 工作区锁定」的威胁模型直接冲突，因此不做。

三个容易踩的实现点，都已在代码里处理：

1. **stdio 用后台线程读 stdout**。在 Windows 上对管道做「带超时的 readline」不可靠，
   把读到的行推进 `Queue` 再 `get(timeout=…)`，超时行为才是确定的。
2. **JSON-RPC 必须按 id 匹配响应**。server 会在两次响应之间插入 `notifications/*`，
   把通知当成响应会让后续调用的参数全部错位。
3. **通知不能丢**。`notifications/tools/list_changed` 决定要不要重新拉工具表，
   丢了就再也刷不出来。因此通知单独入队，由 `poll_notifications()` 取走。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from ..errors import McpToolError
from ..net import trust_env_for
from .config import McpServerConfig

__all__ = ["McpClient", "PROTOCOL_VERSION"]

#: 采用 2024-11-05：兼容面最广的一版。server 会在 initialize 响应里回报它实际支持的版本，
#: 我们只记录、不做版本协商失败的降级——协议不匹配时后续调用自然会报错，报错比猜测可靠。
PROTOCOL_VERSION = "2024-11-05"

_CLIENT_INFO = {"name": "local-coding-agent", "version": "1.0"}


class McpClient:
    """单个 MCP server 的连接。**同步阻塞**实现——调用方在 `asyncio.to_thread` 里跑它。"""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self.ready = False
        self.protocol_version = ""
        self.server_info: Dict[str, Any] = {}
        self.error = ""

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_lines: List[str] = []
        self._inbox: "queue.Queue[Optional[str]]" = queue.Queue()
        self._notifications: List[Dict[str, Any]] = []
        self._next_id = 1
        self._session_id = ""
        self._lock = threading.Lock()

    # ---------------- 生命周期 ----------------
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def status(self) -> str:
        return "ready" if self.ready else "down"

    def start(self) -> None:
        """
        建连 + 握手。失败时抛异常，由 `McpManager` 捕获并把该 server 标为 down。

        抛异常而不是静默返回 False：调用方需要知道原因才能记日志。
        """
        if self.config.transport == "stdio":
            self._start_stdio()
        else:
            self._start_http()

        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": dict(_CLIENT_INFO),
        })
        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized", {})
        self.ready = True
        self.error = ""

    def close(self) -> None:
        """关闭连接。**忽略所有关闭期错误**——关闭失败不该影响主流程退出。"""
        self.ready = False
        proc, self._proc = self._proc, None
        if proc is not None:
            # 顺序固定：先终止子进程（读线程会因管道 EOF 自然退出），再关闭管道。
            # 反过来做会在 Windows 上对着「仍有读者阻塞」的管道 close，行为不确定。
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:  # noqa: BLE001
                    pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass          # 关闭管道失败不影响退出
        self._inbox.put(None)          # 唤醒可能在等待的读线程

    # ---------------- 协议 ----------------
    def list_tools(self) -> List[Dict[str, Any]]:
        """`tools/list`。支持分页则拉全（server 会给 nextCursor）。"""
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(20):            # 上限保护：分页异常时不至于死循环
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            page = result.get("tools") or []
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """`tools/call`。`isError=true` 时抛 `McpToolError`（供失败分类识别）。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise McpToolError(f"MCP 工具 {name} 返回错误: {_text_of(result)[:500]}")
        return result

    def poll_notifications(self) -> List[Dict[str, Any]]:
        """
        取走累积的通知（含连接期收到的）。返回后内部列表清空。

        `refresh_if_needed()` 靠它判断是否要重新拉工具表。
        """
        with self._lock:
            items, self._notifications = self._notifications, []
        return items

    # ---------------- 传输：stdio ----------------
    def _start_stdio(self) -> None:
        env = None
        if self.config.env:
            import os

            env = {**os.environ, **self.config.env}
        try:
            self._proc = subprocess.Popen(
                list(self.config.command),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, shell=False,
            )
        except (OSError, ValueError) as e:
            raise McpToolError(f"无法启动 MCP server {self.name}: {e}") from e

        self._reader = threading.Thread(target=self._pump_stdout, name=f"mcp-{self.name}",
                                        daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, name=f"mcp-{self.name}-err",
                         daemon=True).start()

    def _pump_stdout(self) -> None:
        """把 server 的 stdout 逐行推进队列。EOF 时放一个 None 作为终止信号。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._inbox.put(None)
            return
        try:
            for line in proc.stdout:
                self._inbox.put(line)
        except Exception:  # noqa: BLE001 - 管道破裂等，一律当作结束
            pass
        finally:
            self._inbox.put(None)

    def _pump_stderr(self) -> None:
        """留几行 stderr 作为诊断信息。server 崩溃时它是唯一的线索。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr_lines.append(line.rstrip())
                if len(self._stderr_lines) > 20:
                    del self._stderr_lines[0]
        except Exception:  # noqa: BLE001
            pass

    def _write_stdio(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpToolError(f"MCP server {self.name} 未建立标准输入通道")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise McpToolError(f"向 MCP server {self.name} 写入失败: {e}") from e

    # ---------------- 传输：streamable HTTP ----------------
    def _start_http(self) -> None:
        result = self._http_post({
            "jsonrpc": "2.0", "id": self._take_id(), "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": dict(_CLIENT_INFO)},
        })
        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = result.get("serverInfo") or {}
        self._http_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.ready = True

    def _http_post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = httpx.post(self.config.url, json=payload, headers=headers,
                                  timeout=self.config.timeout_seconds,
                                  # 远端 MCP 走代理是对的；本机 MCP 必须绕过（见 tools/net.py）
                                  trust_env=trust_env_for(self.config.url))
        except httpx.HTTPError as e:
            raise McpToolError(f"MCP {self.name} 请求失败: {e}") from e

        session = response.headers.get("Mcp-Session-Id")
        if session:
            self._session_id = session

        if response.status_code >= 400:
            raise McpToolError(
                f"MCP {self.name} 返回 HTTP {response.status_code}: {response.text[:300]}")
        if payload.get("id") is None:
            return {}                  # 通知：按协议无响应体

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return _parse_sse(response.text)
        try:
            return response.json()
        except ValueError as e:
            raise McpToolError(f"MCP {self.name} 返回了非 JSON 响应: {e}") from e

    # ---------------- JSON-RPC ----------------
    def _take_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self.config.transport == "stdio":
            self._write_stdio(payload)
        else:
            self._http_post(payload)

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_id = self._take_id()
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}

        if self.config.transport == "stdio":
            self._write_stdio(payload)
            return self._read_response(request_id)

        response = self._http_post(payload)
        return _unwrap(response)

    def _read_response(self, request_id: int) -> Dict[str, Any]:
        """
        从队列里等到 id 匹配的那条响应。**不匹配的消息一律不当响应**：
        通知进通知队列，其他响应丢弃并记一条 stderr 线索。
        """
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = ("；server stderr 尾部: " + " | ".join(self._stderr_lines[-3:])
                          if self._stderr_lines else "")
                raise McpToolError(
                    f"MCP {self.name} 在 {self.config.timeout_seconds}s 内没有响应{detail}")
            try:
                line = self._inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue

            if line is None:
                detail = ("；server stderr 尾部: " + " | ".join(self._stderr_lines[-3:])
                          if self._stderr_lines else "")
                raise McpToolError(f"MCP {self.name} 连接已关闭{detail}")

            text = line.strip()
            if not text.startswith("{"):
                continue               # server 往 stdout 打了非协议内容，忽略
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue

            if "method" in message and "id" not in message:
                with self._lock:
                    self._notifications.append(message)
                continue
            if message.get("id") != request_id:
                continue               # 别人（超时的旧请求）的响应，丢弃
            return _unwrap(message)


# ======================================================================
# 协议辅助
# ======================================================================
def _unwrap(message: Dict[str, Any]) -> Dict[str, Any]:
    """把 JSON-RPC 响应拆成 result，或把 error 转成异常。"""
    if not isinstance(message, dict):
        raise McpToolError(f"MCP 响应不是对象: {type(message).__name__}")
    error = message.get("error")
    if error:
        if isinstance(error, dict):
            raise McpToolError(
                f"MCP 错误 {error.get('code')}: {error.get('message')}"
                + (f"（data: {str(error.get('data'))[:200]}）" if error.get("data") else ""))
        raise McpToolError(f"MCP 错误: {error}")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def _parse_sse(text: str) -> Dict[str, Any]:
    """
    解析 SSE 响应体，取第一条带 `id` 的 JSON-RPC 响应。

    streamable HTTP 允许服务端用 SSE 回单个响应，因此这里不是「流式消费」，
    而是「把响应体当文本拆帧」。
    """
    for block in text.split("\n\n"):
        payload_lines = [line[5:].strip() for line in block.splitlines()
                         if line.startswith("data:")]
        if not payload_lines:
            continue
        try:
            message = json.loads("\n".join(payload_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and "id" in message:
            return _unwrap(message)
    raise McpToolError("MCP 的 SSE 响应里没有找到 JSON-RPC 结果")


def _text_of(result: Dict[str, Any]) -> str:
    chunks = []
    for item in (result.get("content") or []):
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)
