@echo off
rem ============================================================
rem  GOLEM Rhino Web Agent —— 一键装环境
rem     1) 建虚拟环境 .venv        2) 装依赖        3) 准备好 .env
rem ============================================================
setlocal
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 找不到 python，请先安装 Python 3.10+ 并加入 PATH
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境 .venv ...
  python -m venv .venv || goto :err
) else (
  echo [1/3] 已存在 .venv，跳过创建
)

echo [2/3] 安装依赖 ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :err

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [3/3] 已从 .env.example 生成 .env
  echo       ^>^> 请打开 .env 填入 DEEPSEEK_API_KEY
) else (
  echo [3/3] 已存在 .env，未覆盖
)

echo.
echo 完成。接下来：
echo   1) Rhino 8 命令行输入 ScriptEditor，运行 start_in_rhino.py
echo      （看到 "listening on 127.0.0.1:9876" 即成功）
echo   2) 双击 start_web.cmd，浏览器打开 http://127.0.0.1:8849
exit /b 0

:err
echo.
echo [ERROR] 安装失败，请检查网络或 Python 版本
exit /b 1
