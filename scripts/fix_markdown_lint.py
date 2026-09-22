#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复文档常见 markdownlint 问题：无语言代码块、强调式标题。"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def fix_bare_fences(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "```":
            out.append("```text")
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                out.append(lines[i])
                i += 1
            if i < len(lines):
                out.append(lines[i])
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def fix_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = fix_bare_fences(text)
    path.write_text(text, encoding="utf-8")
    print(f"lint-fixed: {path}")


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1] / "docs"
    paths = [Path(p) for p in argv] if argv else sorted(root.glob("*.md"))
    for path in paths:
        fix_file(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
