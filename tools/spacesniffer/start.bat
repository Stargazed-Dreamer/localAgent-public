:: SpaceSniffer 快照浏览器启动脚本
:: 用 localAgent 根目录的 .venv（PySide6 依赖由该 venv 提供）
:: localAgent 根 = 本文件上两级目录（tools\spacesniffer -> localAgent）
:: 用法：双击直接开空窗口；也可以把 .sns 文件拖到本文件上直接加载
@echo off
setlocal
set "LOCALAGENT_ROOT=%~dp0..\.."
set "VENV_PY=%LOCALAGENT_ROOT%\.venv\Scripts\python.exe"
cd /d "%~dp0"
"%VENV_PY%" main.py %*
if errorlevel 1 (
    echo.
    echo [start.bat] 程序异常退出，错误码 %errorlevel%
    pause
)
