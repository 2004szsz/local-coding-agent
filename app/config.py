# -*- coding: utf-8 -*-
"""
配置加载模块。

职责：
1. 读取 config.yaml，并用 settings.json 覆盖路径与监听地址；
2. 用 .env / 进程环境变量覆盖模型密钥（不把 Key 写进仓库）；
3. 与内置默认值合并（配置缺项也能启动）；
4. 将相对路径统一转换为相对于项目根目录的绝对路径；
5. 启动期自动创建必需目录（工作空间、数据目录）。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict

import yaml

# 项目根目录（本文件位于 app/config.py，向上两级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------- 内置默认配置（config.yaml 缺项时兜底） ----------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "reload": False,
        "workspace_root": "./workspaces/demo",
    },
    "llm": {
        "provider": "custom",
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.2,
        "max_tokens": 4096,
        "timeout": 120,
        "stream": True,
        "embedding": {
            "enabled": False,
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout": 60,
            "fallback_hash": True,
        },
    },
    "agent": {
        "framework": "native_react",
        "max_iterations": 12,
    },
    "rag": {
        "enabled": True,
        "persist_dir": "./data/chroma",
        "collection": "code_knowledge",
        "chunk_size": 1200,
        "chunk_overlap": 150,
        "max_file_size_kb": 512,
        "use_tree_sitter": True,
        "include_ext": [".py", ".js", ".ts", ".java", ".go", ".md"],
        "ignore": [
            "**/.git/**",
            "**/__pycache__/**",
            "**/node_modules/**",
            "**/.workbuddy/**",
        ],
    },
    "executor": {"timeout_seconds": 10, "max_result_chars": 20000},
    # 本地访问（工作区之外的本地文件与系统数据）。默认全部关闭：
    # 未授权的根不注册对应工具（N3 默认关闭），详见 docs/local-system-access-plan.md
    "local_access": {
        "enabled": False,
        "roots": [],
        "max_read_bytes": 524288,
        "max_list_entries": 2000,
        "max_search_results": 50,
        "max_search_depth": 4,
        "rate_per_minute": 0,
        "trash_dir": "./data/.trash",
        "audit_log": "./data/logs/fs_access.jsonl",
    },
    "system": {
        "enabled": False,
        "expose": ["overview", "processes", "disks", "network", "env",
                   "battery", "hardware"],
        "allow_actions": [],
        "allow_apps": {},
    },
    # 审批通道：高权限操作暂停等待人工确认的时长（秒）。
    # 超时 = 拒绝（失败关闭，不自动放行）。每次决议落审计日志。
    "permissions": {
        "timeout_seconds": 60,
        "audit_log": "./data/logs/permissions.jsonl",
    },
    "storage": {
        "sessions_dir": "./data/sessions",
        "history_db": "./data/history.db",
        "max_messages_per_session": 500,
        "preferences_path": "./data/memory/preferences.json",
        "preferences_max_items": 500,
        "preferences_max_bytes": 10485760,
    },
    "frameworks": {},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并两个字典，override 中的值覆盖 base；base 不被修改。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _abs_path(value: str) -> str:
    """将配置中的相对路径解析为相对项目根目录的绝对路径。"""
    p = Path(value)
    return str(p if p.is_absolute() else (PROJECT_ROOT / p).resolve())


def _apply_settings(cfg: Dict[str, Any]) -> None:
    """settings.json 覆盖监听地址、工作空间和 history.db 路径。"""
    path = PROJECT_ROOT / "settings.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    cfg["project"] = {
        key: data[key]
        for key in ("name", "version", "description", "language")
        if key in data
    }
    runtime = data.get("runtime") or {}
    if runtime.get("host"):
        cfg["server"]["host"] = runtime["host"]
    if runtime.get("port"):
        cfg["server"]["port"] = int(runtime["port"])
    if runtime.get("workspace_root"):
        cfg["server"]["workspace_root"] = runtime["workspace_root"]
    if runtime.get("history_db"):
        cfg["storage"]["history_db"] = runtime["history_db"]
    vector_store = runtime.get("vector_store")
    if vector_store:
        cfg["rag"]["persist_dir"] = vector_store


def _load_dotenv(path: Path) -> None:
    """读取 .env。已存在的进程环境变量不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _apply_env(cfg: Dict[str, Any]) -> None:
    """密钥与模型地址以环境变量为准，避免写进 yaml。"""
    mapping = {
        "LLM_API_KEY": ("llm", "api_key"),
        "LLM_BASE_URL": ("llm", "base_url"),
        "LLM_MODEL": ("llm", "model"),
        "LLM_PROVIDER": ("llm", "provider"),
        "EMBEDDING_API_KEY": ("embedding", "api_key"),
        "EMBEDDING_BASE_URL": ("embedding", "base_url"),
        "EMBEDDING_MODEL": ("embedding", "model"),
    }
    for env_key, (section, field) in mapping.items():
        value = os.getenv(env_key, "").strip()
        if not value:
            continue
        if section == "embedding":
            cfg["llm"]["embedding"][field] = value
        else:
            cfg[section][field] = value


def load_config(path: str | os.PathLike = "config.yaml") -> Dict[str, Any]:
    """
    加载配置文件。

    :param path: 配置文件路径（默认项目根目录 config.yaml）
    :return: 完整配置字典（路径类字段已转为绝对路径）
    """
    cfg_path = Path(path)
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path

    file_cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}

    cfg = _deep_merge(DEFAULT_CONFIG, file_cfg)
    _apply_settings(cfg)
    _load_dotenv(PROJECT_ROOT / ".env")
    _apply_env(cfg)

    # 设置中心模型注册表覆盖 llm / embedding（优先于 yaml，次于环境变量已写入的字段）
    from app.model_registry import apply_registry_to_config
    cfg = apply_registry_to_config(cfg)

    # 路径绝对化
    cfg["server"]["workspace_root"] = _abs_path(cfg["server"]["workspace_root"])
    cfg["rag"]["persist_dir"] = _abs_path(cfg["rag"]["persist_dir"])
    cfg["storage"]["sessions_dir"] = _abs_path(cfg["storage"]["sessions_dir"])
    cfg["storage"]["history_db"] = _abs_path(cfg["storage"]["history_db"])
    cfg["storage"]["preferences_path"] = _abs_path(
        cfg["storage"].get("preferences_path") or "./data/memory/preferences.json"
    )
    local = cfg.get("local_access") or {}
    if local.get("trash_dir"):
        local["trash_dir"] = _abs_path(str(local["trash_dir"]))
    if local.get("audit_log"):
        local["audit_log"] = _abs_path(str(local["audit_log"]))
    perm = cfg.get("permissions") or {}
    if perm.get("audit_log"):
        perm["audit_log"] = _abs_path(str(perm["audit_log"]))

    # 安全红线：强制仅本机监听（防止误配公网地址）
    host = cfg["server"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"安全限制: server.host 当前为 '{host}'，本应用禁止对外暴露，"
            f"请使用 127.0.0.1。"
        )

    # 运行时项目/能力（data/local_runtime.json）覆盖 workspace、local_access、exec_mode
    from app.local_runtime import apply_runtime_to_config, load_runtime_state
    runtime_state = load_runtime_state()
    apply_runtime_to_config(cfg, runtime_state)
    if runtime_state.projects:
        cfg["server"]["workspace_root"] = _abs_path(cfg["server"]["workspace_root"])

    # 自动创建必需目录
    Path(cfg["server"]["workspace_root"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["storage"]["sessions_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["storage"]["history_db"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["storage"]["preferences_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["rag"]["persist_dir"]).mkdir(parents=True, exist_ok=True)

    return cfg
