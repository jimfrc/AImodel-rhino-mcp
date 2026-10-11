@echo off
rem 启动 GOLEM Rhino Web 对话页（自带 MCP 客户端）
cd /d %~dp0
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
.venv\Scripts\python.exe -m webapp.app
