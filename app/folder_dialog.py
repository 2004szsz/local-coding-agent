# -*- coding: utf-8 -*-
"""本机系统文件夹选择对话框（服务跑在用户电脑上时弹出原生框）。"""
from __future__ import annotations

from typing import Optional


def pick_directory(title: str = "选择本地项目文件夹") -> Optional[str]:
    """
    弹出系统「选择文件夹」对话框，返回绝对路径；用户取消返回 None。

    依赖本机桌面会话（tkinter）。无头/远程环境会抛 RuntimeError。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境不支持系统文件夹对话框（缺少 tkinter）") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.update_idletasks()
    try:
        chosen = filedialog.askdirectory(
            parent=root,
            title=title,
            mustexist=True,
        )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    text = (chosen or "").strip()
    return text or None
