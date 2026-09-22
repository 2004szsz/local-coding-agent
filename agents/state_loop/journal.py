# -*- coding: utf-8 -*-
"""
FileJournal：写前字节快照与回滚。

**为什么不用 git**（架构文档 8.6）：
1. 不假设工作区是 git 仓库——本仓库本身就不是；
2. 一旦混进用户自己的未提交改动，「git 状态」就变得不可解释，回滚会连着用户的活一起丢；
3. 我们要的回滚粒度是「某个任务开始之前」，而不是「某个 commit」。

**为什么不落盘**：第一期只把快照留在内存，进程退出即丢。崩溃恢复是第二期的事
（`LoopState` 已按可序列化设计，落盘不需要改结构）。

关键设计点：
- **首次写入的字节才算数**。同一个文件被改两次时，保留的是**第一次**的原始字节——
  那才是「任务开始前」的真实内容。若保留最后一次，回滚会回退到任务中途的状态。
- **`before is None` 表示文件原先不存在**，回滚时应当**删除**它，而不是写空内容。
- 回滚写回前**再走一次 `WorkspaceSecurity.resolve`**，防止检查点里的路径被替换后误写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from tools.workspace import PathTraversalError, WorkspaceSecurity

__all__ = ["FileJournal", "CheckpointInfo"]


@dataclass
class CheckpointInfo:
    """检查点的对外摘要（事件与收尾文本用，不带字节内容）。"""

    checkpoint_id: str
    task_id: str
    reason: str
    paths: tuple[str, ...]

    def one_line(self) -> str:
        return f"{self.checkpoint_id}（{len(self.paths)} 个文件，{self.reason}）"


@dataclass
class FileJournal:
    """写前快照账本。一个任务一个检查点，按 task_id 索引。

    两类条目并存：
    - 工作区条目：键 = 相对工作区路径，回滚经 `WorkspaceSecurity.resolve` 复查；
    - 外部根条目：键 = 「root:rel」，回滚经 `AccessBroker.resolve` 复查——
      「检查点里的路径被换成 junction / 改名后误写回滚」同样防住。
    """

    #: task_id → checkpoint_id
    _by_task: Dict[str, str] = field(default_factory=dict)
    #: checkpoint_id → {相对路径: 写前字节或 None}
    _entries: Dict[str, "dict[str, Optional[bytes]]"] = field(default_factory=dict)
    #: checkpoint_id → {键: (写前字节或 None, 记录时的绝对路径)}
    _external: Dict[str, "dict[str, Tuple[Optional[bytes], str]]"] = field(default_factory=dict)
    _meta: Dict[str, CheckpointInfo] = field(default_factory=dict)
    _counter: int = 0

    # ---------------- 检查点 ----------------
    def checkpoint(self, task_id: str, paths: Iterable[str],
                   workspace: WorkspaceSecurity, reason: str = "task-start") -> str:
        """
        为任务建立检查点，记录 `paths` 中**已存在文件**的当前字节。

        目录型 scope 不递归整棵树——只记 `paths` 里的具体文件；
        之后新增的写入路径由 `remember_write` 补齐。

        幂等：同一任务重复调用返回既有检查点（避免第二次把「已改过的内容」当成原始内容）。
        """
        existing = self._by_task.get(task_id)
        if existing is not None:
            return existing

        self._counter += 1
        checkpoint_id = f"ck{self._counter}"
        entries: Dict[str, Optional[bytes]] = {}

        for rel in paths:
            rel_norm = str(rel).replace("\\", "/")
            try:
                target = workspace.resolve(rel_norm)
            except PathTraversalError:
                continue          # 越界路径不进快照：它本来就不该被写
            if target.is_file():
                entries[rel_norm] = self._read_bytes(target)

        self._by_task[task_id] = checkpoint_id
        self._entries[checkpoint_id] = entries
        self._meta[checkpoint_id] = CheckpointInfo(
            checkpoint_id=checkpoint_id, task_id=task_id, reason=reason,
            paths=tuple(entries.keys()),
        )
        return checkpoint_id

    def remember_write(self, task_id: str, rel_path: str,
                       before: Optional[bytes]) -> None:
        """
        记录一次即将发生的写入。**首次写入的字节胜出**（见模块头说明）。

        调用时机：写工具真正执行之前（见 outcome.run_tool）。
        """
        checkpoint_id = self._by_task.get(task_id)
        if checkpoint_id is None:
            return
        rel_norm = str(rel_path).replace("\\", "/")
        entries = self._entries[checkpoint_id]
        if rel_norm in entries:
            return                    # 已有原始字节，不覆盖
        entries[rel_norm] = before

    def remember_write_external(self, task_id: str, root: str, rel_path: str,
                                before: Optional[bytes], abs_path: str) -> None:
        """
        记录一次即将发生的外部根写入（fs_* 组，L4）。

        键为 `root:rel`——回滚时必须重新解析 root，而不是信任记录里的绝对路径。
        """
        checkpoint_id = self._by_task.get(task_id)
        if checkpoint_id is None:
            return
        key = f"{root}:{str(rel_path).replace(chr(92), '/')}"
        if key not in self._external.get(checkpoint_id, {}):
            self._external.setdefault(checkpoint_id, {})[key] = (before, abs_path)

    # ---------------- 查询 ----------------
    def checkpoint_of(self, task_id: str) -> Optional[str]:
        return self._by_task.get(task_id)

    def info(self, task_id: str) -> Optional[CheckpointInfo]:
        checkpoint_id = self._by_task.get(task_id)
        return self._meta.get(checkpoint_id) if checkpoint_id else None

    def changed_paths(self, task_id: str) -> List[str]:
        """本任务检查点之后**被写入过**的相对路径（按首次写入顺序）。"""
        checkpoint_id = self._by_task.get(task_id)
        if checkpoint_id is None:
            return []
        return list(self._entries.get(checkpoint_id, {}).keys())

    def external_changed_paths(self, task_id: str) -> List[str]:
        """本任务检查点之后被写入过的**外部根**条目（键 = root:rel）。"""
        checkpoint_id = self._by_task.get(task_id)
        if checkpoint_id is None:
            return []
        return list(self._external.get(checkpoint_id, {}).keys())

    def has_writes(self, task_id: str) -> bool:
        """
        本任务是否真的写过盘。

        `decide` 里模型声称「已修改」时，机器用这个判断是否必须进 verify——
        注意它只看**记账事实**，不看模型的话。外部根写入同样算写过盘。
        """
        return bool(self.changed_paths(task_id) or self.external_changed_paths(task_id))

    # ---------------- 回滚 ----------------
    def restore(self, task_id: str, workspace: WorkspaceSecurity,
                broker: Any = None) -> List[str]:
        """
        把任务涉及的文件还原到检查点状态，返回被还原的路径键。

        - 工作区条目：原先存在 → 用原始字节写回；原先不存在 → 删除
        - 外部根条目：**重新经 broker.resolve(root, rel, need_write=True) 解析**，
          用最新解析结果写回（记录时的绝对路径只作信息，不作依据）——
          防止检查点被换路径 / junction 换目标后误写。
        """
        checkpoint_id = self._by_task.get(task_id)
        if checkpoint_id is None:
            return []
        entries = self._entries.get(checkpoint_id, {})
        restored: List[str] = []

        for rel, before in entries.items():
            try:
                target = workspace.resolve(rel)     # 复查：检查点里的路径也可能被换掉
            except PathTraversalError:
                continue
            try:
                if before is None:
                    if target.exists() and target.is_file():
                        target.unlink()
                        restored.append(rel)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(before)
                    restored.append(rel)
            except OSError:
                continue          # 单个文件失败不阻断其余还原

        external = self._external.get(checkpoint_id, {})
        for key, (before, _abs_recorded) in external.items():
            root, sep, rel = key.partition(":")
            if not sep or broker is None:
                continue
            try:
                target = broker.resolve(root, rel, need_write=True)
            except Exception:  # noqa: BLE001 - 越界/权限/根消失都跳过该条目
                continue
            try:
                if before is None:
                    if target.exists() and target.is_file():
                        target.unlink()
                        restored.append(key)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(before)
                    restored.append(key)
            except OSError:
                continue
        return restored

    def forget(self, task_id: str) -> None:
        """任务结束且不再需要回滚能力时释放字节（内存是有限资源）。"""
        checkpoint_id = self._by_task.pop(task_id, None)
        if checkpoint_id is not None:
            self._entries.pop(checkpoint_id, None)
            self._external.pop(checkpoint_id, None)
            self._meta.pop(checkpoint_id, None)

    # ---------------- 工具 ----------------
    @staticmethod
    def read_before(workspace: WorkspaceSecurity, rel_path: str) -> Optional[bytes]:
        """
        读写前字节。文件不存在返回 None（表示「原先不存在」）。

        异常一律吞掉并返回 None：快照失败不应该挡住用户真正要做的写入，
        代价是这一次回滚会退化成「删除该文件」——比抛异常中断整个任务好。
        """
        try:
            target = workspace.resolve(rel_path)
        except (PathTraversalError, OSError, ValueError):
            return None
        return FileJournal._read_bytes(target)

    @staticmethod
    def _read_bytes(target: Path) -> Optional[bytes]:
        try:
            return target.read_bytes()
        except OSError:
            return None
