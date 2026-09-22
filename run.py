# -*- coding: utf-8 -*-
"""
应用启动入口。

用法:
    python run.py                 # 默认读取 config.yaml，生产模式启动
    python run.py --env dev       # 开发模式（热重载，并记录工具调用）
    python run.py --debug         # 只记录工具调用，不热重载
    python run.py --env prod      # 生产模式（关闭热重载与工具日志）
    python run.py --port 9000     # 临时覆盖端口

注意: Windows 下代码执行器使用 multiprocessing 子进程，
      必须保留 `if __name__ == "__main__"` 保护（本文件已具备）。
"""
import argparse
import os
import sys
from pathlib import Path

import uvicorn

from agents.agent import load_agent_spec, resolve_framework
from app.config import load_config

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "logs" / "agent.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地编码智能体启动脚本")
    parser.add_argument("--env", choices=["dev", "prod"], default="prod",
                        help="运行环境: dev 开启热重载和工具日志，prod 关闭（默认 prod）")
    parser.add_argument("--debug", action="store_true",
                        help="记录每次工具调用到 data/logs/agent.log，不强制热重载")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖端口")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    spec = load_agent_spec()
    framework = resolve_framework(spec, cfg)
    debug = bool(args.debug or args.env == "dev")
    if debug:
        os.environ["AGENT_DEBUG"] = "1"
        os.environ["AGENT_LOG_PATH"] = str(LOG_PATH)

    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    reload_enabled = True if args.env == "dev" else bool(cfg["server"].get("reload", False))
    skills = ", ".join(spec.get("skills") or [])

    print("=" * 60)
    print(f"  本地编码智能体启动中  | 环境: {args.env}")
    print(f"  Agent 框架: {framework}")
    print(f"  技能      : {skills}")
    print(f"  LLM 模型  : {cfg['llm']['model']} @ {cfg['llm']['base_url']}")
    print(f"  工作空间  : {cfg['server']['workspace_root']}")
    print(f"  访问地址  : http://{host}:{port}")
    if debug:
        print(f"  工具日志  : {LOG_PATH}")
    print("=" * 60)

    # reload 模式下 uvicorn 会以导入字符串方式重新加载 app
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        reload_excludes=["data/*", "workspaces/*"],
        log_level="info",
    )


if __name__ == "__main__":
    # Windows multiprocessing 安全点：spawn 子进程会重新导入本模块
    sys.exit(main())
