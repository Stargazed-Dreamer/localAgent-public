@echo off
chcp 65001 >nul 2>&1
title LocalAgent Server

REM --- LocalAgent backend launcher --------------------------------------------
REM Keep this file ASCII-only with CRLF line endings. cmd.exe on Chinese Windows
REM is extremely sensitive to encoding and line endings, and a broken .bat breaks
REM the whole startup. Before editing, read .agents/skills/bat_writing/SKILL.md.
REM The "chcp 65001" above is required and must not be removed: server/main.py
REM reconfigures its stdio to UTF-8, so the console code page has to match or
REM Chinese log output turns into garbage.
REM See docs/deployment.md section "start.bat" for the do-not-edit note.
REM ---------------------------------------------------------------------------

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

REM UTF-8 stdio for Python logs. main.py reconfigures as well; setting it here
REM makes sure the Python process starts with UTF-8 stderr already in place.
set PYTHONIOENCODING=utf-8

echo [INFO] Starting LocalAgent...
echo [INFO] API: http://127.0.0.1:8766
echo [INFO] Dashboard: http://127.0.0.1:8766/static/index.html
echo [INFO] MCP: http://127.0.0.1:8766/mcp
echo.
.venv\Scripts\python.exe -m server.main
pause
