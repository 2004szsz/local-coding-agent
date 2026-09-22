# -*- coding: utf-8 -*-
"""
测试用的假 MCP server（stdio 传输，逐行 JSON-RPC）。

存在的意义：MCP 客户端如果不能对着**真实子进程**跑一遍，
「连接 → 发现工具 → 调用 → 通知刷新」这条路就只是纸面设计。
这个 server 刻意实现了两个容易出错的行为：

1. 在 `tools/call` 的响应**之前**插入一条 `notifications/tools/list_changed`——
   客户端必须按 id 匹配响应，把通知当成响应会让后续调用全部错位；
2. 首次调用后工具表发生变化（多出 `echo.extra`），用来验证按需刷新。
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "回显参数（只读）",
        "inputSchema": {"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "make-note",
        "description": "创建一条笔记（有副作用）",
        "inputSchema": {"type": "object",
                        "properties": {"text": {"type": "string"},
                                       "fail": {"type": "boolean"}}},
    },
]

EXTRA_TOOL = {
    "name": "echo.extra",
    "description": "刷新后才会出现的工具（名字里带点，用于验证规范化）",
    "inputSchema": {"type": "object", "properties": {}},
}

STATE = {"calls": 0, "bumped": False}


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request):
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake-mcp", "version": "0.1"},
        }}

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        tools = list(TOOLS) + ([EXTRA_TOOL] if STATE["bumped"] else [])
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        STATE["calls"] += 1

        if name == "echo":
            result = {"content": [{"type": "text",
                                   "text": f"echo: {arguments.get('text', '')}"}]}
        elif name == "make-note":
            if arguments.get("fail"):
                result = {"isError": True,
                          "content": [{"type": "text", "text": "创建被拒绝"}]}
            else:
                result = {"content": [{"type": "text", "text": "已创建笔记"}],
                          "structuredContent": {"id": STATE["calls"]}}
        else:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": f"unknown tool: {name}"}}

        if not STATE["bumped"]:
            STATE["bumped"] = True
            # 关键：通知排在响应之前，客户端必须跳过它继续等 id 匹配的响应
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed",
                  "params": {}})

        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    if request_id is None:
        return None      # 其他通知：忽略
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main():
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
