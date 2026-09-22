@echo off
REM ============================================================
REM  Windows 一键启动脚本
REM  用法:
REM    start.bat            生产模式启动
REM    start.bat dev        开发模式启动（热重载）
REM ============================================================
chcp 65001 >nul
cd /d %~dp0

if not exist .venv (
  echo [提示] 未发现 .venv 虚拟环境，正在创建并安装依赖...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -U pip
  pip install -r requirements.txt
)

set PYTHON=.venv\Scripts\python.exe
if not exist %PYTHON% set PYTHON=python

if "%1"=="dev" (
  %PYTHON% run.py --env dev
) else (
  %PYTHON% run.py --env prod
)
pause
