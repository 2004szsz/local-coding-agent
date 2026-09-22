# -*- coding: utf-8 -*-
"""模块协同协调器：汇总链路状态、执行各模块 adapt 逻辑。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.modules.rules import (
    context_snippet_for_modules,
    extract_rule_sections,
    load_module_rules,
    load_rules_markdown,
    module_paths,
)

logger = logging.getLogger(__name__)

_STATUS_ORDER = ("ready", "pending", "warn", "off", "indexing", "error")


class ModuleCoordinator:
    """基于规则文件协调 fs / rag / agent / access 四链路模块。"""

    def rules_payload(self) -> Dict[str, Any]:
        rules = load_module_rules()
        md = load_rules_markdown()
        modules_out: Dict[str, Any] = {}
        for mid, mod in (rules.get("modules") or {}).items():
            modules_out[mid] = {
                **mod,
                "rule_excerpt": extract_rule_sections(mid),
                "paths": module_paths(mid),
            }
        return {
            "version": rules.get("version", 1),
            "rules_markdown": md.get("path"),
            "markdown_exists": md.get("exists", False),
            "modules": modules_out,
            "context_snippet": context_snippet_for_modules(),
        }

    def module_status(self, app_state: Any) -> Dict[str, Any]:
        rules = load_module_rules()
        modules_cfg = rules.get("modules") or {}
        items: List[Dict[str, Any]] = []
        for mid in ("fs", "rag", "agent", "access"):
            cfg = modules_cfg.get(mid) or {}
            status = self._status_for(mid, app_state)
            items.append({
                "id": mid,
                "label": cfg.get("label") or mid,
                "chain_id": cfg.get("chain_id") or mid,
                "status": status["status"],
                "detail": status["detail"],
                "value": status.get("value", ""),
                "auto_adapt": bool(cfg.get("auto_adapt")),
                "optimize_entry": cfg.get("optimize_entry"),
                "adapt": cfg.get("adapt"),
                "paths": module_paths(mid),
                "clickable": True,
            })
        return {"modules": items, "rules_version": rules.get("version", 1)}

    def adapt(self, module_id: str, app_state: Any) -> Dict[str, Any]:
        handlers = {
            "fs": self._adapt_fs,
            "rag": self._adapt_rag,
            "agent": self._adapt_agent,
            "access": self._adapt_access,
        }
        handler = handlers.get(module_id)
        if handler is None:
            return {"ok": False, "module": module_id, "message": f"未知模块: {module_id}"}
        try:
            return handler(app_state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("module adapt failed: %s", module_id)
            return {"ok": False, "module": module_id, "message": str(exc)}

    # ---------------- 状态探测 ----------------

    def _status_for(self, module_id: str, app_state: Any) -> Dict[str, str]:
        if module_id == "fs":
            return self._fs_status(app_state)
        if module_id == "rag":
            return self._rag_status(app_state)
        if module_id == "agent":
            return self._agent_status(app_state)
        if module_id == "access":
            return self._access_status(app_state)
        return {"status": "off", "detail": "未知模块", "value": "—"}

    def _fs_status(self, app_state: Any) -> Dict[str, str]:
        runtime = getattr(app_state, "runtime", None)
        tools = getattr(runtime, "tools", None) if runtime else None
        names = tools.names() if tools else []
        fs_tools = [n for n in names if n.startswith("fs_")]
        ws_tools = [n for n in names if n in {
            "read_file", "write_file", "list_dir", "search_code",
        }]
        cfg = getattr(app_state, "cfg", {}) or {}
        local_on = bool((cfg.get("local_access") or {}).get("enabled"))
        if runtime and hasattr(runtime, "broker") and runtime.broker:
            local_on = True
        count = len(fs_tools) + len(ws_tools)
        if count > 0 and local_on:
            return {"status": "ready", "detail": "文件工具已挂载", "value": f"{count} 工具"}
        if count > 0:
            return {"status": "warn", "detail": "工作区工具就绪，本机访问待授权", "value": "待授权"}
        return {"status": "off", "detail": "未挂载文件工具", "value": "未挂载"}

    def _rag_status(self, app_state: Any) -> Dict[str, str]:
        daemon = getattr(app_state, "rag_daemon", None)
        if daemon and daemon.indexing:
            return {"status": "indexing", "detail": "后台索引进行中", "value": "索引中"}

        if not getattr(app_state, "rag_enabled", False):
            return {"status": "off", "detail": "RAG 未启用", "value": "未启用"}

        store = getattr(app_state, "vector_store", None)
        if store is None:
            return {"status": "warn", "detail": "向量库未就绪", "value": "未就绪"}

        chunks = 0
        try:
            chunks = int(store.count())
        except Exception:  # noqa: BLE001
            pass

        if chunks > 0:
            return {"status": "ready", "detail": "知识库已就绪", "value": f"{chunks} 块"}

        indexer = getattr(app_state, "indexer", None)
        if indexer is None:
            return {"status": "warn", "detail": "索引器未就绪", "value": "待索引"}

        scan = getattr(indexer, "_scan_files", None)
        if callable(scan):
            try:
                if len(scan()) == 0:
                    return {"status": "warn", "detail": "工作区没有可索引文件", "value": "空工作区"}
            except Exception:  # noqa: BLE001
                pass

        return {"status": "pending", "detail": "知识库待索引", "value": "待索引"}

    def _agent_status(self, app_state: Any) -> Dict[str, str]:
        fw = str(getattr(app_state, "framework_name", "") or "")
        if not fw:
            return {"status": "off", "detail": "Agent 未装配", "value": "—"}
        label = fw.replace("state_loop", "循环").replace("native_react", "ReAct")
        ready = "state_loop" in fw or "react" in fw
        if ready:
            return {"status": "ready", "detail": f"框架 {fw} 已就绪", "value": label}
        return {"status": "warn", "detail": f"框架 {fw}", "value": label}

    def _access_status(self, app_state: Any) -> Dict[str, str]:
        cfg = getattr(app_state, "cfg", {}) or {}
        local = bool((cfg.get("local_access") or {}).get("enabled", False))
        # 与 fs 链路一致：AccessBroker 已挂载即视为本机访问已启用
        # （local_runtime 热重载可能先挂 broker，再写回 cfg）
        runtime = getattr(app_state, "runtime", None)
        if runtime is not None and getattr(runtime, "broker", None):
            local = True
        elif getattr(app_state, "broker", None):
            local = True
        if local:
            return {"status": "ready", "detail": "本机访问已授权", "value": "本机已授权"}
        return {"status": "warn", "detail": "本机访问受限", "value": "受限访问"}

    @staticmethod
    def _access_gate_snapshot() -> Dict[str, str]:
        """从权限表抽取 access 模块关键副作用等级，供前端/适配结果展示。"""
        from agents.state_loop.permissions import TOOL_EFFECTS

        def _code(name: str) -> str:
            effect = TOOL_EFFECTS.get(name)
            return effect.code if effect is not None else "L4"

        return {
            "fs_read": _code("fs_read"),
            "fs_write": _code("fs_write"),
        }

    # ---------------- adapt 动作 ----------------

    def _adapt_fs(self, app_state: Any) -> Dict[str, Any]:
        st = self._fs_status(app_state)
        paths = module_paths("fs")
        return {
            "ok": st["status"] in ("ready", "warn"),
            "module": "fs",
            "action": "verify_fs",
            "status": st["status"],
            "message": st["detail"],
            "paths": paths,
            "optimize_entry": "files",
        }

    def _adapt_rag(self, app_state: Any) -> Dict[str, Any]:
        if not getattr(app_state, "rag_enabled", False):
            return {"ok": False, "module": "rag", "message": "RAG 未启用"}
        indexer = getattr(app_state, "indexer", None)
        if indexer is None:
            return {"ok": False, "module": "rag", "message": "索引器未就绪"}

        daemon = getattr(app_state, "rag_daemon", None)
        if daemon:
            stats = daemon.run_index_now()
        else:
            stats = indexer.index()

        failed = isinstance(stats, dict) and stats.get("ok") is False
        if failed:
            return {
                "ok": False,
                "module": "rag",
                "action": "incremental_index",
                "stats": stats,
                "message": str(stats.get("message") or "索引失败"),
                "optimize_entry": "rag-index",
            }

        return {
            "ok": True,
            "module": "rag",
            "action": "incremental_index",
            "stats": stats,
            "message": (
                f"索引完成：扫描 {stats.get('scanned', 0)}，"
                f"新增 {stats.get('added', 0)}，共 {stats.get('chunks_total', 0)} 切片"
            ),
            "optimize_entry": "rag-index",
        }

    def _adapt_agent(self, app_state: Any) -> Dict[str, Any]:
        st = self._agent_status(app_state)
        snippet = extract_rule_sections("agent")
        return {
            "ok": st["status"] in ("ready", "warn"),
            "module": "agent",
            "action": "verify_agent",
            "status": st["status"],
            "framework": getattr(app_state, "framework_name", ""),
            "message": st["detail"],
            "rule_excerpt": snippet[:2000],
            "optimize_entry": "subagents",
        }

    def _adapt_access(self, app_state: Any) -> Dict[str, Any]:
        st = self._access_status(app_state)
        snippet = extract_rule_sections("access")
        gate = self._access_gate_snapshot()
        ready = st["status"] == "ready"
        message = st["detail"]
        if ready:
            loop = getattr(app_state, "loop_deps", None)
            mode = str(getattr(loop, "mode", "") or "")
            write_policy = (
                "自动（full_access）" if mode == "full_access" else "需确认"
            )
            mode_hint = f"｜执行档 {mode}" if mode else ""
            message = (
                f"{st['detail']}{mode_hint}｜闸门 fs_read={gate['fs_read']}（自动）/"
                f"fs_write={gate['fs_write']}（{write_policy}）"
            )
        return {
            "ok": ready,
            "module": "access",
            "action": "verify_access",
            "status": st["status"],
            "message": message,
            "gate": gate,
            "rule_excerpt": snippet[:2000],
            "paths": module_paths("access"),
            "optimize_entry": "skills",
        }
