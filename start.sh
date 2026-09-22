#!/usr/bin/env bash
# ============================================================
#  Linux / macOS 一键启动脚本
#  用法:
#    ./start.sh           生产模式启动
#    ./start.sh dev       开发模式启动（热重载）
# ============================================================
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[提示] 未发现 .venv 虚拟环境，建议先执行:"
  echo "       python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
fi

if [ "$1" = "dev" ]; then
  python run.py --env dev
else
  python run.py --env prod
fi
