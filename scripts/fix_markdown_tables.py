#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 Markdown 表格规范为 markdownlint MD060 compact 风格。"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _parse_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip() for cell in s.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c)


def _separator_cell(orig: str) -> str:
    o = orig.strip()
    left = o.startswith(":")
    right = o.endswith(":")
    if left and right:
        return ":---:"
    if right:
        return "---:"
    if left:
        return ":---"
    return "---"


def _format_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _fix_block(block: list[str]) -> list[str]:
    rows = [_parse_row(line) for line in block]
    if not rows:
        return block
    ncol = max(len(r) for r in rows)
    fixed: list[str] = []
    for i, row in enumerate(rows):
        cells = row + [""] * (ncol - len(row))
        if i == 1 and _is_separator(cells):
            cells = [_separator_cell(c) for c in cells]
        fixed.append(_format_row(cells))
    return fixed


def fix_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _is_table_row(lines[i]):
            block: list[str] = []
            while i < len(lines) and _is_table_row(lines[i]):
                block.append(lines[i])
                i += 1
            out.extend(_fix_block(block))
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main(argv: list[str]) -> int:
    paths = argv or ["docs/agent-runtime-architecture.md"]
    for raw in paths:
        path = Path(raw)
        text = path.read_text(encoding="utf-8")
        path.write_text(fix_markdown_tables(text), encoding="utf-8")
        print(f"fixed tables: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
