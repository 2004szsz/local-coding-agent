# -*- coding: utf-8 -*-
"""
工作区之外的本地文件工具组（前缀 fs_）。

与工作区内的 file_tools 是**两组共存**的工具：工作区内模型继续用
list_dir / read_file / write_file / edit_file（语义不变），工作区外（已授权根内）
用 fs_* 系列。作用域靠「root + 相对该根的路径」二元组显式声明，不靠路径猜。

注册分两个入口（N3 默认关闭在装配期落地）：
    register_fs_read_tools   只读组：fs_roots / fs_list / fs_read / fs_search / fs_stat
    register_fs_write_tools  写入组：fs_write / fs_edit / fs_copy / fs_move
                            / fs_delete / fs_restore（仅当存在可写根时由装配调用）

写入组全部登记为 L4（需人工确认），权限矩阵见 agents/state_loop/permissions.py：
「读自动、可写根上写需要确认、只读根上写被门闩拒绝」。

安全约定（与 docs/local-system-access-plan.md 第 5 节一致）：
- 一切路径经 AccessBroker.resolve，本模块**没有任何自己的 open()/unlink() 旁路**；
- 二进制文件只返回元信息，不把字节吐进上下文；
- 超大文件截断并**明确告知模型「已截断」**；
- fs_delete 永不真删：移入隔离目录，可用 fs_restore 还原。
"""
from __future__ import annotations

import fnmatch
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import Tool, ToolRegistry
from .errors import PatchConflictError
from .fs_access import AccessBroker

__all__ = ["register_fs_read_tools", "register_fs_write_tools",
           "build_fs_tools", "DEFAULT_READ_LIMITS"]

# 搜索时默认跳过的目录名（与工作区 file_search 同一组约定）
_SEARCH_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".pytest_cache", "chroma",
    ".workbuddy", ".trash",
}

#: 二进制探测窗口：前 8KB 出现 NUL 即判定为二进制
_BINARY_PROBE_BYTES = 8192

DEFAULT_READ_LIMITS = {
    "max_read_bytes": 524288,     # fs_read 单文件读取上限 512KB
    "max_read_lines": 2000,       # fs_read 返回行数上限
    "max_list_entries": 2000,     # fs_list 条目上限
    "max_search_results": 50,     # fs_search 命中上限
    "max_search_depth": 4,        # fs_search 目录深度上限
}


def _require(kwargs: Dict[str, Any], key: str) -> Any:
    value = kwargs.get(key)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ValueError(f"缺少必填参数: {key}")
    return value


def _limits(cfg: Optional[Dict[str, Any]]) -> Dict[str, int]:
    out = dict(DEFAULT_READ_LIMITS)
    for key in DEFAULT_READ_LIMITS:
        raw = (cfg or {}).get(key)
        if raw is not None:
            out[key] = max(1, int(raw))
    return out


def _is_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            head = f.read(_BINARY_PROBE_BYTES)
    except OSError:
        return False
    return b"\x00" in head


# ======================================================================
# 只读组
# ======================================================================
def _build_fs_roots(broker: AccessBroker) -> Tool:
    def fs_roots(kwargs: Dict[str, Any]) -> str:
        roots = broker.roots_view()
        if not roots:
            return "当前没有已授权的访问根（在 config.yaml 的 local_access.roots 配置）。"
        lines = [f"{r['name']}: {r['path']} "
                 f"[读:{'是' if r['read'] else '否'} 写:{'是' if r['write'] else '否'}]"
                 for r in roots]
        return "已授权的本地访问根:\n" + "\n".join(lines)

    return Tool(
        name="fs_roots",
        description="列出所有已授权的本地访问根及其读写权限。访问工作区外文件前必须先调用它确认作用域。",
        parameters={"type": "object", "properties": {}},
        handler=fs_roots,
    )


def _build_fs_list(broker: AccessBroker, limits: Dict[str, int]) -> Tool:
    def fs_list(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(kwargs.get("path") or ".")
        p = broker.resolve(root, rel)
        if not p.is_dir():
            raise NotADirectoryError(f"不是目录: {rel}")

        entries: List[str] = []
        cap = limits["max_list_entries"]
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            display = str(Path(rel) / child.name).replace("\\", "/")
            tag = "DIR " if child.is_dir() else "FILE"
            entries.append(f"{tag} {display}{'/' if child.is_dir() else ''}")
            if len(entries) >= cap:
                entries.append(f"...[条目超过 {cap}，已截断，可用 path 逐级深入]")
                break
        header = f"根 {root} 的 {rel or '.'} 内容（{len(entries)} 项）:\n"
        return header + ("\n".join(entries) if entries else "(空目录)")

    return Tool(
        name="fs_list",
        description=("列出已授权根内的目录内容。path 为相对该根的路径。"
                     "条目超过上限会截断，深入子目录可继续用 path 指定。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名，先看 fs_roots"},
                "path": {"type": "string", "description": "相对该根的目录路径，默认 '.'"},
            },
            "required": ["root"],
        },
        handler=fs_list,
    )


def _build_fs_read(broker: AccessBroker, limits: Dict[str, int]) -> Tool:
    def fs_read(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(_require(kwargs, "path"))
        p = broker.resolve(root, rel)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {rel}")

        size = p.stat().st_size
        truncated = size > limits["max_read_bytes"]
        mode = "rb"
        data = p.read_bytes() if not truncated else p.read_bytes()[:limits["max_read_bytes"]]

        if b"\x00" in data[:_BINARY_PROBE_BYTES]:
            return (f"[二进制文件，不返回内容] {rel}：类型=二进制 "
                    f"大小={size} 字节 修改时间={time.ctime(p.stat().st_mtime)}")

        text = data.decode(WorkspaceEncoding.detect(data))
        lines = text.splitlines()
        start_line = kwargs.get("start_line")
        end_line = kwargs.get("end_line")
        s = int(start_line) - 1 if start_line else 0
        e = int(end_line) if end_line else min(len(lines), limits["max_read_lines"])
        s = max(0, min(s, len(lines)))
        e = max(s, min(e, len(lines)))

        width = len(str(e or 1))
        numbered = [f"{i + 1:>{width}} | {lines[i]}" for i in range(s, e)]
        head = f"# [{root}] {rel} 行 {s + 1}-{e} / 共 {len(lines)} 行"
        if truncated:
            head += f" [文件 {size} 字节超过上限 {limits['max_read_bytes']} 字节，内容已截断！]"
        return head + "\n" + "\n".join(numbered)

    return Tool(
        name="fs_read",
        description=("读取已授权根内的文本文件，返回带行号内容。二进制只返回元信息；"
                     "超大文件会被截断并明确标注。不确定根名时先调 fs_roots。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名"},
                "path": {"type": "string", "description": "相对该根的文件路径"},
                "start_line": {"type": "integer", "description": "可选，起始行（从1开始，含）"},
                "end_line": {"type": "integer", "description": "可选，结束行（含）"},
            },
            "required": ["root", "path"],
        },
        handler=fs_read,
        output_limit=20000,
    )


def _build_fs_search(broker: AccessBroker, limits: Dict[str, int]) -> Tool:
    def fs_search(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(kwargs.get("path") or ".")
        pattern = str(_require(kwargs, "pattern"))
        use_regex = bool(kwargs.get("regex", False))
        filename = kwargs.get("filename")        # 可选: fnmatch glob，如 *.py
        compiled = re.compile(pattern, re.IGNORECASE) if use_regex else None
        base = broker.resolve(root, rel)
        if not base.is_dir():
            raise NotADirectoryError(f"不是目录: {rel}")

        cap = min(int(kwargs.get("max_results", 100) or 100), limits["max_search_results"])
        max_depth = limits["max_search_depth"]
        hits: List[str] = []
        queue: List[Tuple[Path, int]] = [(base, 0)]

        while queue and len(hits) < cap:
            current, depth = queue.pop(0)
            if depth > max_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name in _SEARCH_SKIP_DIRS:
                        continue
                    queue.append((child, depth + 1))
                    continue
                if filename and not fnmatch.fnmatch(child.name.lower(),
                                                    str(filename).lower()):
                    continue
                try:
                    data = child.read_bytes()[:limits["max_read_bytes"]]
                except OSError:
                    continue
                if b"\x00" in data[:_BINARY_PROBE_BYTES]:
                    continue
                try:
                    text = data.decode(WorkspaceEncoding.detect(data))
                except Exception:  # noqa: BLE001
                    continue
                display = str(child.relative_to(base)).replace("\\", "/")
                for lineno, line in enumerate(text.splitlines(), start=1):
                    matched = (bool(compiled.search(line)) if compiled
                               else pattern.lower() in line.lower())
                    if not matched:
                        continue
                    hits.append(f"{display}:{lineno}: {line.strip()[:160]}")
                    if len(hits) >= cap:
                        break
        if not hits:
            return f"在根 {root} 的 {rel or '.'} 内搜索 '{pattern}' 无命中。"
        return f"搜索 '{pattern}' 命中 {len(hits)} 处:\n" + "\n".join(hits)

    return Tool(
        name="fs_search",
        description=("在已授权根内按关键词或正则搜索文件内容，返回 '相对路径:行号: 命中行'。"
                     "可用 filename 按文件名 glob 过滤。自动跳过 .git/node_modules 等目录，"
                     "并受搜索深度上限约束。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名"},
                "path": {"type": "string", "description": "搜索起点（相对该根），默认 '.'"},
                "pattern": {"type": "string", "description": "关键词或正则表达式"},
                "regex": {"type": "boolean", "description": "pattern 是否为正则"},
                "filename": {"type": "string", "description": "可选，文件名 glob，如 '*.py'"},
                "max_results": {"type": "integer", "description": "命中上限，默认 50"},
            },
            "required": ["root", "pattern"],
        },
        handler=fs_search,
        output_limit=16000,
    )


def _build_fs_stat(broker: AccessBroker) -> Tool:
    def fs_stat(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(_require(kwargs, "path"))
        p = broker.resolve(root, rel)
        st = p.stat() if p.exists() else None
        if st is None:
            raise FileNotFoundError(f"文件不存在: {rel}")
        size = st.st_size if p.is_file() else 0
        return (f"[{root}] {rel} 类型={'目录' if p.is_dir() else '文件'} "
                f"大小={size} 字节 修改时间={time.ctime(st.st_mtime)}"
                + (" 符号链接" if p.is_symlink() else ""))

    return Tool(
        name="fs_stat",
        description="查看已授权根内路径的元信息（类型/大小/修改时间），不读内容。",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名"},
                "path": {"type": "string", "description": "相对该根的路径"},
            },
            "required": ["root", "path"],
        },
        handler=fs_stat,
    )


# ======================================================================
# 写入组（L4，需人工确认；权限由门闩 + L4 分层强制）
# ======================================================================
def _build_fs_write(broker: AccessBroker) -> Tool:
    def fs_write(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(_require(kwargs, "path"))
        content = str(kwargs.get("content", ""))
        create = bool(kwargs.get("create", True))
        overwrite = bool(kwargs.get("overwrite", True))
        p = broker.resolve(root, rel, need_write=True)
        if p.exists() and not p.is_file():
            raise IsADirectoryError(f"目标路径已被目录占用: {rel}")
        if p.exists() and not overwrite:
            raise FileExistsError(f"文件已存在且 overwrite=false: {rel}")
        if not p.exists() and not create:
            raise FileNotFoundError(f"文件不存在且 create=false: {rel}")
        broker.write_text(root, rel, content)
        return f"已写入根 {root} 的文件 {rel}（{len(content)} 字符）。"

    return Tool(
        name="fs_write",
        description=("在已授权**可写**根内写入/新建文件（整体覆盖）。"
                     "只读根会被拒绝；修改已有文件优先用 fs_edit 做局部编辑。"
                     "该操作需要用户确认后才能执行。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名（必须可写）"},
                "path": {"type": "string", "description": "相对该根的目标文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "create": {"type": "boolean", "description": "不存在时是否允许新建，默认 true"},
                "overwrite": {"type": "boolean", "description": "已存在时是否允许覆盖，默认 true"},
            },
            "required": ["root", "path", "content"],
        },
        handler=fs_write,
    )


def _build_fs_edit(broker: AccessBroker) -> Tool:
    def fs_edit(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(_require(kwargs, "path"))
        old_string = str(_require(kwargs, "old_string"))
        new_string = str(kwargs.get("new_string", ""))
        replace_all = bool(kwargs.get("replace_all", False))

        p = broker.resolve(root, rel, need_write=True)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {rel}")
        content = broker.read_text(root, rel)
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise PatchConflictError(
                "未在文件中找到 old_string，请先 fs_read 确认原文（注意缩进/空白）。")
        if occurrences > 1 and not replace_all:
            raise PatchConflictError(
                f"old_string 出现 {occurrences} 次，非唯一匹配。"
                f"请补充更多上下文，或显式设置 replace_all=true。")
        content = content.replace(old_string, new_string, -1 if replace_all else 1)
        broker.write_text(root, rel, content)
        count = occurrences if replace_all else 1
        return f"已精确编辑根 {root} 的 {rel}：完成 {count} 处替换。"

    return Tool(
        name="fs_edit",
        description=("对已授权**可写**根内的已有文件做精确局部编辑：old_string 必须与原文"
                     "逐字一致（含缩进）且唯一匹配，多处匹配需 replace_all=true。"
                     "该操作需要用户确认后才能执行。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名（必须可写）"},
                "path": {"type": "string", "description": "相对该根的文件路径"},
                "old_string": {"type": "string", "description": "要被替换的原文（逐字一致）"},
                "new_string": {"type": "string", "description": "替换后的文本"},
                "replace_all": {"type": "boolean", "description": "多处出现时是否全部替换"},
            },
            "required": ["root", "path", "old_string"],
        },
        handler=fs_edit,
    )


def _build_fs_copy_move(broker: AccessBroker, move: bool) -> Tool:
    name = "fs_move" if move else "fs_copy"
    verb = "移动" if move else "复制"

    def handler(kwargs: Dict[str, Any]) -> str:
        src_root = str(_require(kwargs, "src_root"))
        src_path = str(_require(kwargs, "src_path"))
        dst_root = str(_require(kwargs, "dst_root"))
        dst_path = str(_require(kwargs, "dst_path"))
        # 读源只需根可读；写目标必须根可写。目标解析失败 = 拒绝整次操作。
        src = broker.resolve(src_root, src_path)
        dst = broker.resolve(dst_root, dst_path, need_write=True)
        if not src.exists():
            raise FileNotFoundError(f"源不存在: {src_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(src), str(dst))
        else:
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        return f"已{verb} {src_root}:{src_path} → {dst_root}:{dst_path}。"

    return Tool(
        name=name,
        description=(f"在已授权根之间{verb}文件或目录。目标根必须可写。"
                     "该操作需要用户确认后才能执行。"),
        parameters={
            "type": "object",
            "properties": {
                "src_root": {"type": "string", "description": "源根名"},
                "src_path": {"type": "string", "description": "相对源根的路径"},
                "dst_root": {"type": "string", "description": "目标根名（必须可写）"},
                "dst_path": {"type": "string", "description": "相对目标根的路径"},
            },
            "required": ["src_root", "src_path", "dst_root", "dst_path"],
        },
        handler=handler,
    )


def _trash_dir_of(broker: AccessBroker) -> Path:
    if broker.trash_dir is None:
        raise ValueError("未配置 trash_dir，无法执行删除（需要 config.yaml 的 local_access.trash_dir）")
    broker.trash_dir.mkdir(parents=True, exist_ok=True)
    return broker.trash_dir


def _build_fs_delete(broker: AccessBroker) -> Tool:
    def fs_delete(kwargs: Dict[str, Any]) -> str:
        root = str(_require(kwargs, "root"))
        rel = str(_require(kwargs, "path"))
        p = broker.resolve(root, rel, need_write=True)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {rel}")
        ts = time.strftime("%Y%m%d_%H%M%S")
        trash = _trash_dir_of(broker)
        target_dir = trash / ts
        meta = {
            "ts": ts,
            "root": root,
            "rel": rel.replace("\\", "/"),
            "abs": str(p),
            "name": p.name,
            "is_dir": p.is_dir(),
            "deleted_at": time.time(),
        }
        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            shutil.move(str(p), str(target_dir / (p.name or "root")))
        except OSError as e:
            raise OSError(f"移入隔离目录失败: {e}") from e
        (target_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return (f"已把 {root}:{rel} 移入隔离目录（未真正删除）。"
                f"如需还原请调用 fs_restore(root='{root}', ts='{ts}')。")

    return Tool(
        name="fs_delete",
        description=("删除已授权可写根内的文件/目录——**永不真删**，只移入隔离目录，"
                     "可用 fs_restore 还原。该操作需要用户确认后才能执行。"),
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名（必须可写）"},
                "path": {"type": "string", "description": "相对该根的文件/目录路径"},
            },
            "required": ["root", "path"],
        },
        handler=fs_delete,
    )


def _build_fs_restore(broker: AccessBroker) -> Tool:
    def fs_restore(kwargs: Dict[str, Any]) -> str:
        ts = str(_require(kwargs, "ts"))
        trash = _trash_dir_of(broker)
        target_dir = trash / ts
        meta_file = target_dir / "meta.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"未找到隔离记录: {ts}")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        # 还原前再走一次门闩复查：检查点/记录里的路径可能已被替换
        dest = broker.resolve(str(meta.get("root")), str(meta.get("rel")), need_write=True)
        if dest.exists():
            raise FileExistsError(f"目标已存在，拒绝覆盖还原: {meta.get('rel')}")
        moved = target_dir / (meta.get("name") or "root")
        if not moved.exists():
            raise FileNotFoundError(f"隔离文件已丢失: {ts}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(moved), str(dest))
        shutil.rmtree(target_dir, ignore_errors=True)
        return f"已还原 {meta.get('root')}:{meta.get('rel')}。"

    return Tool(
        name="fs_restore",
        description="把 fs_delete 移入隔离目录的内容还原到原位置（由 ts 指定）。",
        parameters={
            "type": "object",
            "properties": {
                "ts": {"type": "string", "description": "fs_delete 返回的时间戳"},
            },
            "required": ["ts"],
        },
        handler=fs_restore,
    )


# ======================================================================
# 编码检测入口（复用 WorkspaceSecurity 的三级回退，不 import 两次）
# ======================================================================
class WorkspaceEncoding:
    """fs 工具用的编码探测（UTF-8 → GBK → latin-1）。"""

    @staticmethod
    def detect(data: bytes) -> str:
        if data.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        for enc in ("utf-8", "gbk"):
            try:
                data.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
        return "latin-1"


# ======================================================================
# 注册入口
# ======================================================================
def register_fs_read_tools(registry: ToolRegistry, broker: AccessBroker,
                           cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """注册只读组。local_access.enabled=false 时装配层根本不会调用本函数。"""
    limits = _limits(cfg)
    tools = [
        _build_fs_roots(broker),
        _build_fs_list(broker, limits),
        _build_fs_read(broker, limits),
        _build_fs_search(broker, limits),
        _build_fs_stat(broker),
    ]
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]


def register_fs_write_tools(registry: ToolRegistry, broker: AccessBroker,
                            cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    注册写入组。**只在至少存在一个可写根时由装配层调用**——
    「根只读 → 写工具根本不存在」是 N3 默认关闭的组成部分。
    """
    tools = [
        _build_fs_write(broker),
        _build_fs_edit(broker),
        _build_fs_copy_move(broker, move=False),
        _build_fs_copy_move(broker, move=True),
        _build_fs_delete(broker),
        _build_fs_restore(broker),
    ]
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]


def build_fs_tools(broker: AccessBroker,
                   cfg: Optional[Dict[str, Any]] = None) -> List[Tool]:
    """一次性构造全部 fs 工具（测试与自定义装配用）。"""
    registry = ToolRegistry()
    register_fs_read_tools(registry, broker, cfg)
    if broker.has_writable_roots():
        register_fs_write_tools(registry, broker, cfg)
    return [registry.get(n) for n in registry.names()]