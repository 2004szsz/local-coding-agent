@echo off
REM ============================================================
REM  Windows 一键环境初始化（Python 3.13+）
REM  用法: setup.bat
REM ============================================================
chcp 65001 >nul
cd /d %~dp0

echo [1/4] 创建虚拟环境...
if not exist .venv (
  python -m venv .venv
)

echo [2/4] 安装 Python 依赖...
call .venv\Scripts\activate.bat
python -m pip install -U pip
pip install -r requirements.txt

echo [3/4] 检查 .env...
if not exist .env (
  copy /Y .env.example .env
  echo       已从 .env.example 复制 .env
)

echo [4/4] 创建数据目录...
if not exist data\logs mkdir data\logs
if not exist data\chroma mkdir data\chroma
if not exist data\sessions mkdir data\sessions
if not exist workspaces\demo mkdir workspaces\demo

echo.
echo 初始化完成。下一步:
echo   1. 安装 Ollama 并拉取模型:
echo        ollama pull qwen2.5-coder:7b
echo        ollama pull nomic-embed-text
echo   2. 启动服务: start.bat
echo   3. 浏览器打开 http://127.0.0.1:8000
pause
