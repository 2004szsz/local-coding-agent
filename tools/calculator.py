# -*- coding: utf-8 -*-
"""
安全算术工具。

只接受数字与四则运算，用 AST 求值，不调用 eval，也不进入代码沙箱。
适合数据整理里的比例、差值、行数估算；需要循环或函数时改用 run_python_code。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Dict

from .base import Tool, ToolRegistry

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def evaluate(expression: str) -> str:
    """计算一条算术表达式，返回十进制字符串。非法表达式抛出 ValueError。"""
    text = (expression or "").strip()
    if not text:
        raise ValueError("缺少必填参数: expression")
    if len(text) > 200:
        raise ValueError("表达式过长（上限 200 字符）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e.msg}") from e
    value = _eval_node(tree.body)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 16:
            raise ValueError("指数过大")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_node(node.operand))
    raise ValueError("只允许数字与 + - * / // % ** 和括号")


def build_calculator_tool() -> Tool:
    def calculator(kwargs: Dict[str, Any]) -> str:
        expression = str(kwargs.get("expression") or "")
        return evaluate(expression)

    return Tool(
        name="calculator",
        description="计算算术表达式（仅数字与 + - * / // % **）。不要用它执行 Python。",
        parameters={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "例如 (12 + 3) * 4 / 2"},
            },
            "required": ["expression"],
        },
        handler=calculator,
    )


def register_calculator(registry: ToolRegistry) -> None:
    registry.register(build_calculator_tool())
