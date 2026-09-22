# -*- coding: utf-8 -*-
"""
工作空间安全模块（安全强制组件）。

所有文件类工具、以及任何需要落盘的操作，都必须经过本模块的路径校验，
确保目标路径被严格锁定在工作空间根目录之内。

防护清单：
1. 相对路径穿越：a/../../etc/passwd   -> resolve 后判定越界，拒绝
2. 绝对路径逃逸：C:\\Windows / /etc     -> resolve 后不在根目录，拒绝
3. 符号链接逃逸：link -> 外部目录       -> resolve 会解析真实路径，越界拒绝
4. 盘符/UNC 路径                        -> 同上，按解析后真实路径判定
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List


class PathTraversalError(Exception):
    """路径越界（试图访问工作空间之外的位置）时抛出。"""


class WorkspaceSecurity:
    """工作空间根目录锁定器与安全文件操作门面。"""

    def __init__(self, root: str | os.PathLike):
        # resolve() 会规范化 .. 并解析符号链接，得到真实绝对路径
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------- 核心校验 ----------------
    def resolve(self, target: str | os.PathLike) -> Path:
        """
        将用户/模型提供的路径解析为工作空间内的真实绝对路径。

        :param target: 相对工作空间的路径，或绝对路径（仍须位于根目录内）
        :raises PathTraversalError: 解析后路径越界
        """
        if target is None or str(target).strip() == "":
            raise PathTraversalError("路径不能为空")

        raw = Path(str(target))
        # 绝对路径直接解析；相对路径视为相对于工作空间根目录
        candidate = (raw if raw.is_absolute() else (self.root / raw)).resolve()

        # Python 3.9+: Path.is_relative_to；以解析后的真实路径比较，杜绝 symlink 逃逸
        if not (candidate == self.root or self._is_within(candidate, self.root)):
            raise PathTraversalError(
                f"安全拦截: 路径 '{target}' 解析为 '{candidate}'，已超出工作空间根目录"
            )
        return candidate

    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        """使用 commonpath 做最终判定（对盘符/根边界更严谨）。"""
        try:
            return os.path.commonpath([str(child), str(parent)]) == str(parent)
        except ValueError:
            # 不同盘符（Windows）等情况下 commonpath 抛 ValueError，必然越界
            return False

    def relpath(self, target: Path) -> str:
        """获取相对于根目录的展示路径（统一用 / 分隔，便于跨端显示）。"""
        try:
            return str(Path(target).resolve().relative_to(self.root)).replace(os.sep, "/")
        except ValueError:
            return str(target)

    # ---------------- 安全文件操作门面 ----------------
    def read_text(self, target: str | os.PathLike) -> str:
        p = self.resolve(target)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {self.relpath(p)}")
        if not p.is_file():
            raise IsADirectoryError(f"目标不是文件: {self.relpath(p)}")
        return p.read_text(encoding=self._detect_encoding(p))

    def write_text(self, target: str | os.PathLike, content: str,
                   create: bool = False) -> Path:
        p = self.resolve(target)
        if p.exists() and not p.is_file():
            raise IsADirectoryError(f"目标路径已被目录占用: {self.relpath(p)}")
        if not create and not p.exists():
            raise FileNotFoundError(
                f"文件不存在: {self.relpath(p)}（如需新建请显式声明 create=true）"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def list_dir(self, target: str | os.PathLike = ".") -> List[dict]:
        """列出目录内容，返回 [{name, type, relpath, size, mtime}]。"""
        p = self.resolve(target)
        if not p.is_dir():
            raise NotADirectoryError(f"不是目录: {self.relpath(p)}")
        items: List[dict] = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            stat = child.stat()
            items.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "relpath": self.relpath(child),
                "size": stat.st_size if child.is_file() else 0,
                "mtime": stat.st_mtime,
            })
        return items

    @staticmethod
    def _detect_encoding(p: Path) -> str:
        """
        简易编码检测：优先 UTF-8（含 BOM），失败回退 GBK（中文系统常见导出编码），
        再失败回退 latin-1（不会抛异常）。
        """
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        for enc in ("utf-8", "gbk"):
            try:
                raw.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
        return "latin-1"
