# -*- coding: utf-8 -*-
"""
运行时项目与能力配置（无需改 config.yaml / 重启服务）。

持久化到 data/local_runtime.json，启动时合并进 load_config() 的返回值，
并通过 agents/runtime_reload.reconfigure_runtime() 在运行期热更新工具注册。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import PROJECT_ROOT

RUNTIME_FILE = PROJECT_ROOT / "data" / "local_runtime.json"

# 本地电脑运行模式下的默认能力（仍走审批闸，不自动放行 L4）
DEFAULT_CAPABILITIES: Dict[str, Any] = {
    "run_mode": "sandbox",
    "local_access": True,
    "system": True,
    "allow_actions": ["notify", "open_path", "launch", "screenshot"],
}

RUN_MODES = frozenset({"sandbox", "local"})

WORKSPACE_TOOL_NAMES = frozenset({
    "list_dir", "read_file", "write_file", "edit_file",
    "file_search", "rag_search", "run_command",
})


@dataclass
class ProjectEntry:
    """用户保存的本机项目目录。"""

    id: str
    name: str
    path: str
    write: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "path": self.path, "write": self.write}

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "ProjectEntry":
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise ValueError("项目路径不能为空")
        resolved = str(Path(raw_path).resolve())
        name = str(item.get("name") or "").strip() or Path(resolved).name
        return cls(
            id=str(item.get("id") or uuid.uuid4().hex[:12]),
            name=name,
            path=resolved,
            write=bool(item.get("write", True)),
        )


@dataclass
class LocalRuntimeState:
    """data/local_runtime.json 的内存视图。"""

    projects: List[ProjectEntry] = field(default_factory=list)
    active_project_id: Optional[str] = None
    capabilities: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CAPABILITIES))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "projects": [p.to_dict() for p in self.projects],
            "active_project_id": self.active_project_id,
            "capabilities": self.capabilities,
        }

    def run_mode(self) -> str:
        mode = str((self.capabilities or {}).get("run_mode") or "sandbox").strip().lower()
        return mode if mode in RUN_MODES else "sandbox"

    def set_run_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in RUN_MODES:
            raise ValueError(f"run_mode 必须是 sandbox 或 local，收到: {mode}")
        caps = dict(self.capabilities or DEFAULT_CAPABILITIES)
        caps["run_mode"] = normalized
        if normalized == "local":
            # 切到本地时补齐本机能力开关；真正挂载仍取决于是否已选项目
            caps.setdefault("local_access", True)
            caps.setdefault("system", True)
            if not caps.get("allow_actions"):
                caps["allow_actions"] = list(DEFAULT_CAPABILITIES["allow_actions"])
        self.capabilities = caps

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalRuntimeState":
        projects: List[ProjectEntry] = []
        for item in data.get("projects") or []:
            try:
                projects.append(ProjectEntry.from_dict(item))
            except (ValueError, TypeError):
                continue
        projects = [p for p in projects if Path(p.path).is_dir()]
        caps = dict(DEFAULT_CAPABILITIES)
        raw_caps = data.get("capabilities") or {}
        if isinstance(raw_caps, dict):
            caps.update(raw_caps)
        # 旧数据无 run_mode：有项目视为本地，否则沙箱
        if "run_mode" not in (raw_caps or {}):
            caps["run_mode"] = "local" if projects else "sandbox"
        elif str(caps.get("run_mode") or "").strip().lower() not in RUN_MODES:
            caps["run_mode"] = "local" if projects else "sandbox"
        active = data.get("active_project_id")
        if active and not any(p.id == active for p in projects):
            active = projects[0].id if projects else None
        elif not active and projects:
            active = projects[0].id
        return cls(projects=projects, active_project_id=active, capabilities=caps)

    def active_project(self) -> Optional[ProjectEntry]:
        if not self.active_project_id:
            return self.projects[0] if self.projects else None
        for project in self.projects:
            if project.id == self.active_project_id:
                return project
        return self.projects[0] if self.projects else None

    def find(self, project_id: str) -> Optional[ProjectEntry]:
        for project in self.projects:
            if project.id == project_id:
                return project
        return None


def load_runtime_state(path: Path = RUNTIME_FILE) -> LocalRuntimeState:
    if not path.is_file():
        return LocalRuntimeState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return LocalRuntimeState()
    if not isinstance(data, dict):
        return LocalRuntimeState()
    return LocalRuntimeState.from_dict(data)


def save_runtime_state(state: LocalRuntimeState, path: Path = RUNTIME_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _slug_name(name: str, used: set[str]) -> str:
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.strip()) or "project"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _disable_local_channels(cfg: Dict[str, Any]) -> None:
    """沙箱模式：关掉本机 fs_* / sys_*，工作区保持 yaml 默认根。"""
    local = dict(cfg.get("local_access") or {})
    local["enabled"] = False
    local["roots"] = []
    cfg["local_access"] = local
    system = dict(cfg.get("system") or {})
    system["enabled"] = False
    system["allow_actions"] = []
    cfg["system"] = system


def apply_runtime_to_config(cfg: Dict[str, Any], state: LocalRuntimeState) -> Dict[str, Any]:
    """
    把运行时项目/能力合并进配置字典（不修改 config.yaml 文件）。

    始终应用：
    - agent.exec_mode → 工作台四档执行模式（无选中时保持 yaml）

    run_mode=sandbox：
    - 不覆盖 workspace_root；强制关闭 local_access / system

    run_mode=local 且已有激活项目：
    - workspace_root → 当前激活项目路径
    - local_access.enabled + roots → 全部项目目录
    - system.enabled + allow_actions → capabilities 段

    run_mode=local 但尚未选目录：
    - 与沙箱相同（不挂本机通道），等待「+」选文件夹
    """
    caps = state.capabilities or {}
    exec_mode = str(caps.get("exec_mode") or "").strip()
    if exec_mode:
        agent = dict(cfg.get("agent") or {})
        agent["exec_mode"] = exec_mode
        cfg["agent"] = agent

    if state.run_mode() != "local":
        _disable_local_channels(cfg)
        return cfg

    active = state.active_project()
    if active is None:
        _disable_local_channels(cfg)
        return cfg

    cfg["server"]["workspace_root"] = active.path

    local = dict(cfg.get("local_access") or {})
    local["enabled"] = bool(caps.get("local_access", True))
    used_names: set[str] = set()
    roots: List[Dict[str, Any]] = []
    for project in state.projects:
        root_name = _slug_name(project.name, used_names)
        roots.append({
            "name": root_name,
            "path": project.path,
            "read": True,
            "write": bool(project.write),
            "tier": "granted",
        })
    local["roots"] = roots
    cfg["local_access"] = local

    system = dict(cfg.get("system") or {})
    system["enabled"] = bool(caps.get("system", True))
    actions = caps.get("allow_actions")
    if actions is None:
        actions = DEFAULT_CAPABILITIES["allow_actions"]
    system["allow_actions"] = list(actions)
    cfg["system"] = system
    return cfg


def common_roots() -> List[Dict[str, str]]:
    """文件夹选择器快捷入口（桌面、文档、各盘符等）。"""
    entries: List[Dict[str, str]] = []
    home = Path.home()
    for label, sub in (("桌面", "Desktop"), ("文档", "Documents"), ("下载", "Downloads")):
        path = home / sub
        if path.is_dir():
            entries.append({"label": label, "path": str(path.resolve())})
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            if drive.is_dir():
                entries.append({"label": f"{letter}:", "path": str(drive.resolve())})
    else:
        entries.append({"label": "主目录", "path": str(home.resolve())})
    return entries
