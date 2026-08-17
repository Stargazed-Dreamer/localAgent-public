@echo off
chcp 65001 >nul 2>&1
title LocalAgent Server

net session >nul 2>&1
if %errorLevel% == 0 (
    echo [OK] Admin privileges confirmed
    goto :run
)

echo [INFO] Requesting admin privileges...
powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:run
cd /d "%~dp0"

REM Kill any existing backend on port 8766
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8766.*LISTENING"') do (
    echo [INFO] Killing old backend PID %%a ...
    taskkill /PID %%a /F >nul 2>&1
    timeout /t 2 /nobreak >nul
)

REM UTF-8 stdio for Python logs (双保险：main.py 也会 reconfigure，这里确保 Python 启动时 stderr 就是 UTF-8)
set PYTHONIOENCODING=utf-8

echo [INFO] Starting LocalAgent...
echo [INFO] API: http://127.0.0.1:8766
echo [INFO] Dashboard: http://127.0.0.1:8766/static/index.html
echo [INFO] MCP: http://127.0.0.1:8766/mcp
echo.
.venv\Scripts\python.exe -m server.main
pause
