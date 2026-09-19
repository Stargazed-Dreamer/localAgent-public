@echo off
REM 启动独立审批面板进程
REM 面板长期挂在任务栏/托盘，server 检测到面板在线时审批走面板不弹窗
cd /d "%~dp0"
uv run python -m client.approval_panel
