# -*- coding: utf-8 -*-
"""
SSE 流式事件类型常量。

所有 Agent 框架（原生 ReAct / 自研 state_loop / AutoGen / ...）的产出
都统一为以下事件，前端只需处理同一套协议，与后端框架彻底解耦。

事件数据均为普通 dict，可直接 json.dumps 后以 SSE data 帧下发：
    {"type": "token", "data": {"content": "你好"}}
"""
from __future__ import annotations

# AI 文本增量（逐字输出）
TOKEN = "token"
# ReAct 思考过程（reasoning/thought 文本）
THOUGHT = "thought"
# 工具调用开始：data = {"id": 调用ID, "name": 工具名, "arguments": {...}}
TOOL_CALL = "tool_call"
# 工具调用结束：data = {"id": 调用ID, "name": 工具名, "output": 文本, "is_error": bool}
TOOL_RESULT = "tool_result"
# 整段回复结束：data = {"finish_reason": "stop"|"tool_calls"}
DONE = "done"
# 出错：data = {"message": 错误描述}
ERROR = "error"
# 一般状态提示（如“正在检索知识库”）
STATUS = "status"

# ---------------- 自研 state_loop 运行时的增量事件 ----------------
# 这些事件是「增量」而非「替换」：旧前端不认识它们时会走 default 分支忽略掉，
# 因此后端可以先上，前端再跟。字段形状与既有事件保持一致，
# 尤其是 tool_call / tool_result 仍用 id/name/arguments/output/is_error。
#
# 任务图建立：data = {"summary": str, "tasks": [{"id","title","status","scope","commands"}]}
PLAN = "plan"
# 任务状态变化：data = {"id": str, "status": str, "attempts": int}
TASK = "task"
# 验收结果：data = {"task", "command", "ok", "exit_code"}
VERIFY = "verify"
# 回滚：data = {"task", "paths": [str], "checkpoint": str}
ROLLBACK = "rollback"
# 子代理：data = {"parent", "children": [id], "results": [str]}
DELEGATE = "delegate"
# 权限请求：data = {"request_id", "kind", "calls": [str], "task"}
# 第一期没有审批 UI，收到它意味着「该操作已被按拒绝处理，不会自动放行」
PERMISSION_REQUEST = "permission_request"


def make_event(event_type: str, data: dict | None = None) -> dict:
    """构造统一事件对象。"""
    return {"type": event_type, "data": data or {}}
