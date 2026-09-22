# -*- coding: utf-8 -*-
"""
授权根访问网关（本地文件系统访问的**唯一入口**）。

改造命题（docs/local-system-access-plan.md）：把安全边界从「一个工作区根」扩成
「一组可显式授权的根」。本模块就是那个升级后的门闩：

    WorkspaceSecurity   —— 工作区根（保留不动，workspace 内读写继续走它）
    AccessBroker        —— 工作区外的多根门闩（本模块，fs_*/sys_* 工具的唯一通道）

三条不变量在本模块的落点：
    N2 单门闩    任何工作区外的路径解析都必须经过 `resolve()`；
                 规定**不允许任何工具自己 open() 或 subprocess 拼系统命令**。
    N3 默认关闭  未注册的根根本不存在——`resolve` 对未知根直接 PathTraversalError，
                 不存在「注册了但暂时拒绝」的中间态。
    N1 控制流    本模块是纯工具层，不感知 state_loop，任何框架可复用。

安全设计（按踩坑概率排序的实现）：
1. **归属判定放在 resolve() 之后**：junction / 符号链接在 Windows 上无需特权即可创建，
   必须先解析真实路径再判 `commonpath`，否则「链接指向外部目录」直接穿透。
2. **8.3 短名展开**：`C:\\PROGRA~1` 与 `C:\\Program Files` 等价，比较前用
   `GetLongPathNameW` 展开，否则拒绝清单能被短名绕过。
3. **\\?\\ 前缀剥离**：解析前剥离 Win32 长路径前缀，否则字符串比较形同虚设。
4. **拒绝清单命中 = PermissionError**：硬编码、不可配置放宽。宁可少给字段，
   不可事后补救——上下文和日志一旦写进去就收不回。
5. **审计落在门闩内部**：resolve 成功/失败都记账，保证「没经过 broker 就等于没访问」。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .workspace import PathTraversalError, WorkspaceSecurity

__all__ = ["AccessRoot", "AccessBroker", "AccessDeniedError", "DENIED_PREFIXES",
           "DENIED_DIR_NAMES", "DENIED_FILE_NAMES"]

# 复用工作区的编码检测（UTF-8 → GBK → latin-1 三级回退），不另造轮子
_detect_encoding = WorkspaceSecurity._detect_encoding

_IS_WINDOWS = sys.platform == "win32"

# ======================================================================
# 硬编码拒绝清单（不可配置放宽；方案 2.3）
# ======================================================================
#: 目录前缀（切盘符/大小写后比较）。命中即拒，与授权了哪个根无关。
DENIED_PREFIXES: Tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    # POSIX 系统目录（同一威胁模型：个人电脑上不该被模型读的系统区）
    "/etc",
    "/usr/lib",
    "/usr/bin",
    "/usr/sbin",
    "/sbin",
    "/bin",
    "/boot",
    "/System",
    "/Library",
)

#: 路径中**任意一层**目录名命中即拒（大小写不敏感）——凭据与多机管理目录。
DENIED_DIR_NAMES: Tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker",
)

#: 文件名（仅 basename，大小写不敏感）命中即拒——凭据文件。
DELETED_CREDENTIAL_FILES: Tuple[str, ...] = (
    ".env", ".npmrc", ".netrc", ".htpasswd",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "cookies", "login data", "web data", "local state",
)

#: 敏感文件后缀（大小写不敏感）。
DENIED_SUFFIXES: Tuple[str, ...] = (".pem", ".pfx", ".key", ".ppk")


class AccessDeniedError(PermissionError):
    """命中拒绝清单或根权限不足。继承 PermissionError 以兼容旧框架的捕获分支。"""


# ======================================================================
# 8.3 短名展开
# ======================================================================
def _expand_short_name(path: str) -> str:
    """
    把 Windows 8.3 短名展开为长名（`GetLongPathNameW`）。

    失败时**返回原路径**：展开只是加固手段，不是正确性前提——
    非 Windows 或异常场景下退回字符串比较，拒绝清单仍然生效（只是防不住短名）。
    """
    if not _IS_WINDOWS:
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        length = ctypes.windll.kernel32.GetLongPathNameW(path, buf, 1024)
        if 0 < length < 1024:
            return buf[:length]
    except Exception:  # noqa: BLE001 - 加固失败不阻断主路径解析
        pass
    return path


def _strip_verbatim(parts: Iterable[str]) -> str:
    """把 `\\\\?\\C:\\...` / `\\??\\` 前缀剥掉，剩下常规绝对路径。"""
    text = os.sep.join(str(p) for p in parts)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    elif text.startswith("\\??\\"):
        text = text[4:]
    return text


def _normpath_for_cmp(path: Path) -> str:
    """统一用于比较的规范形态：normcase（Windows 切大小写）+ 尾分隔符归一。"""
    return os.path.normcase(str(path)).rstrip("\\/") or os.path.normcase(str(path))


# ======================================================================
# 数据结构
# ======================================================================
@dataclass(frozen=True)
class AccessRoot:
    """一个已授权的本地访问根。模型看到的是 `name`，解析的是 `path`。"""

    name: str
    path: Path
    read: bool = True
    write: bool = False
    tier: str = "granted"       # granted | system（system 不承载文件工具）

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "AccessRoot":
        """从 config.yaml 的 local_access.roots 条目构造。坏条目抛 ValueError。"""
        name = str((item or {}).get("name") or "").strip()
        raw_path = str((item or {}).get("path") or "").strip()
        if not name:
            raise ValueError("本地访问根缺少 name")
        if not raw_path:
            raise ValueError(f"本地访问根 {name} 缺少 path")
        if not Path(raw_path).is_absolute():
            raise ValueError(f"本地访问根 {name} 的 path 必须是绝对路径: {raw_path}")
        return cls(
            name=name,
            path=Path(raw_path),
            read=bool(item.get("read", True)),
            write=bool(item.get("write", False)),
            tier=str(item.get("tier", "granted")),
        )

    def one_line(self) -> str:
        perm = "r" + ("w" if self.write else "-")
        return f"{self.name}[{perm}] {self.path}"


# ======================================================================
# 门闩
# ======================================================================
@dataclass
class AccessBroker:
    """
    多根门闩。resolve 成功 = 该路径被显式授权；resolve 抛错 = 不存在 / 被拒。

    失败语义（遵循方案 3.1）：
        PathTraversalError   越界（未知根、解析后逃出根、空路径）
        AccessDeniedError    根只读 / 拒绝清单命中（继承 PermissionError）
    """

    roots: Dict[str, AccessRoot] = field(default_factory=dict)
    denied_prefixes: Tuple[str, ...] = DENIED_PREFIXES
    denied_dir_names: Tuple[str, ...] = DENIED_DIR_NAMES
    denied_file_names: Tuple[str, ...] = DELETED_CREDENTIAL_FILES
    denied_suffixes: Tuple[str, ...] = DENIED_SUFFIXES
    audit_log: Optional[Path] = None
    #: 删除操作的隔离目录（fs_delete 永不真删，移到这里可还原）
    trash_dir: Optional[Path] = None
    #: 单根每分钟 resolve 上限（0 = 不限）
    rate_per_minute: int = 0

    _hits: Dict[str, List[float]] = field(default_factory=dict, init=False, repr=False)
    #: 审计写失败计数（防审计盘写满时刷屏）
    _audit_failures: int = field(default=0, init=False, repr=False)
    #: from_config 收集的坏根警告（构造后由 from_config 写入）
    warnings: List[str] = field(default_factory=list, init=False, repr=False)

    # ---------------- 构造 ----------------
    @classmethod
    def from_config(cls, section: Optional[Dict[str, Any]],
                    project_root: Path) -> "AccessBroker":
        """
        从 config.yaml 的 local_access 段构造。**坏根跳过并返回警告**，
        与 MCP / RAG 的降级哲学一致：配置问题不得阻断对话。
        """
        warnings: List[str] = []
        roots: Dict[str, AccessRoot] = {}
        for item in (section or {}).get("roots") or []:
            try:
                root = AccessRoot.from_dict(item)
            except (ValueError, TypeError) as e:
                warnings.append(f"本地访问根配置无效，已跳过: {e}")
                continue
            normalized = _normpath_for_cmp(root.path)
            if any(normalized == p or normalized.startswith(p + os.sep)
                   for p in _norm_set(DENIED_PREFIXES)):
                warnings.append(f"本地访问根 {root.name} 位于拒绝清单内，已跳过")
                continue
            roots[root.name] = root

        audit_raw = str((section or {}).get("audit_log") or "")
        audit_log: Optional[Path] = None
        if audit_raw:
            audit_log = Path(audit_raw)
            if not audit_log.is_absolute():
                audit_log = project_root / audit_raw
        trash_raw = str((section or {}).get("trash_dir") or "")
        trash_dir: Optional[Path] = None
        if trash_raw:
            trash_dir = Path(trash_raw)
            if not trash_dir.is_absolute():
                trash_dir = project_root / trash_raw
        broker = cls(
            roots=roots,
            audit_log=audit_log,
            trash_dir=trash_dir,
            rate_per_minute=max(0, int((section or {}).get("rate_per_minute", 0))),
        )
        broker.warnings = warnings
        return broker

    def roots_view(self) -> List[Dict[str, Any]]:
        """给 fs_roots 工具与模型看的授权视图（不含绝对路径的敏感描述之外的信息）。"""
        return [{
            "name": root.name,
            "path": str(root.path),
            "read": root.read,
            "write": root.write,
            "tier": root.tier,
        } for root in self.roots.values()]

    def has_writable_roots(self) -> bool:
        return any(root.write for root in self.roots.values())

    # ---------------- 判定 ----------------
    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        try:
            return os.path.commonpath([str(child), str(parent)]) == str(parent)
        except ValueError:
            return False

    def _denied_reason(self, target: Path) -> Optional[str]:
        """返回拒绝理由，None = 未命中拒绝清单。**只用解析后的最终路径判定。**"""
        cmp = _normpath_for_cmp(target)
        prefixes = _norm_set(self.denied_prefixes)
        for prefix in prefixes:
            if cmp == prefix or cmp.startswith(prefix + os.sep):
                return f"拒绝清单(目录): {prefix}"

        parts = list(target.parts)
        lower_dirs = {os.path.normcase(p) for p in parts}
        for bad in self.denied_dir_names:
            if os.path.normcase(bad) in lower_dirs:
                return f"拒绝清单(敏感目录): {bad}"

        if target.name:
            lower_name = os.path.normcase(target.name)
            if lower_name in {os.path.normcase(b) for b in self.denied_file_names}:
                return f"拒绝清单(凭据文件): {target.name}"
            if any(lower_name.endswith(os.path.normcase(s)) for s in self.denied_suffixes):
                return f"拒绝清单(凭据后缀): {target.name}"

        # 他人用户目录：C:\Users\<他人>\** 一律拒绝（不允许跨用户）
        if len(parts) >= 3 and os.path.normcase(parts[1]) == "users":
            home = Path.home()
            if not self._is_within(target, home):
                return f"拒绝清单(他人用户目录): {parts[1]}\\{parts[2]}"
        return None

    def _check_rate(self, root_name: str) -> None:
        limit = self.rate_per_minute
        if limit <= 0:
            return
        now = time.monotonic()
        window = [t for t in self._hits.get(root_name, []) if now - t < 60.0]
        if len(window) >= limit:
            raise AccessDeniedError(
                f"访问过于频繁: 根 {root_name} 每分钟最多 {limit} 次，请稍后再试")
        window.append(now)
        self._hits[root_name] = window

    # ---------------- 审计 ----------------
    def _audit(self, root_name: str, target: Optional[Path], *,
               action: str, decision: str, reason: str = "") -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "root": root_name,
                "path": str(target) if target is not None else "",
                "action": action,
                "decision": decision,
                "reason": reason,
            }
            with self.audit_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._audit_failures = 0
        except OSError:
            self._audit_failures += 1
            if self._audit_failures <= 1:
                print(f"[fs_access] 警告: 审计日志写入失败: {self.audit_log}")

    # ---------------- 核心解析 ----------------
    def resolve(self, root: str, relpath: str, *, need_write: bool = False) -> Path:
        """
        把「根名 + 相对该根的路径」解析为已授权的绝对路径。

        :param root:       AccessRoot.name（模型显式声明的作用域）
        :param relpath:    相对该根的路径；'' / '.' 表示根本身
        :param need_write: 本次操作是否写盘/删除/移动。根 write=false 时直接拒绝。
        :raises PathTraversalError: 未知根 / 空路径 / 解析后逃出根
        :raises AccessDeniedError:  根只读 / 拒绝清单 / 限流（PermissionError 子类）
        """
        entry = self.roots.get(str(root or "").strip())
        if entry is None:
            known = ", ".join(self.roots) or "(无已授权根)"
            self._audit(root, None, action="resolve", decision="deny",
                        reason="root_not_registered")
            raise PathTraversalError(
                f"安全拦截: 未授权访问根 '{root}'（已授权的根: {known}）")

        text = str(relpath or "").strip()
        if need_write and not entry.write:
            self._audit(entry.name, None, action="write", decision="deny",
                        reason="root_readonly")
            raise AccessDeniedError(
                f"根 {entry.name} 是只读根，禁止写入（写入权限需在配置中显式打开）")

        self._check_rate(entry.name)

        # 空路径 = 根本身；绝对路径视为「绝对地址」，解析后仍须落在根内
        if not text or text in (".", "/", "\\"):
            candidate = entry.path
        else:
            raw = Path(text)
            candidate = raw if raw.is_absolute() else entry.path / raw

        # ① 剥 \\?\ 前缀 → ② 短名展开 → ③ 解析真实路径（junction/symlink）→ ④ 归属判定
        stripped = _strip_verbatim(candidate.parts if candidate.is_absolute()
                                   else (entry.path / candidate).parts)
        expanded = Path(_expand_short_name(stripped))
        resolved = expanded.resolve()

        if not (resolved == entry.path.resolve() or self._is_within(resolved, entry.path.resolve())):
            self._audit(entry.name, resolved, action="resolve", decision="deny",
                        reason="escaped_root")
            raise PathTraversalError(
                f"安全拦截: 路径 '{relpath}'（根 {entry.name}）解析为 '{resolved}'，"
                f"已越出该授权根")

        reason = self._denied_reason(resolved)
        if reason is not None:
            self._audit(entry.name, resolved, action="resolve", decision="deny",
                        reason=reason)
            raise AccessDeniedError(f"安全拦截: {reason}: {resolved}")

        self._audit(entry.name, resolved,
                    action="write" if need_write else "read", decision="allow")
        return resolved

    # ---------------- 文件原语（工具层直接用，不绕过门闩） ----------------
    def read_bytes(self, root: str, relpath: str) -> bytes:
        p = self.resolve(root, relpath)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {relpath}")
        return p.read_bytes()

    def read_text(self, root: str, relpath: str) -> str:
        p = self.resolve(root, relpath)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {relpath}")
        return p.read_text(encoding=_detect_encoding(p))

    def write_text(self, root: str, relpath: str, content: str) -> Path:
        p = self.resolve(root, relpath, need_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def write_bytes(self, root: str, relpath: str, data: bytes) -> Path:
        p = self.resolve(root, relpath, need_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def unlink(self, root: str, relpath: str) -> Path:
        p = self.resolve(root, relpath, need_write=True)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {relpath}")
        p.unlink()
        return p


def _norm_set(prefixes: Iterable[str]) -> List[str]:
    """拒绝清单前缀的规范形态（normcase，按长度从长到短排，先匹配最长前缀）。"""
    normalized = {os.path.normcase(str(p).rstrip("\\/")) for p in prefixes}
    return sorted(normalized, key=len, reverse=True)