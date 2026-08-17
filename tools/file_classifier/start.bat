:: 使用 localAgent 根目录的 .venv 内 Python（onnxruntime/tokenizers/numpy/PySide6 等依赖由该 venv 提供）
:: localAgent 根 = 本文件上两级目录（tools\file_classifier → localAgent）
setlocal
set "LOCALAGENT_ROOT=%~dp0..\.."
set "VENV_PY=%LOCALAGENT_ROOT%\.venv\Scripts\python.exe"
cd /d "%~dp0"
"%VENV_PY%" classifier_gui.py
if errorlevel 1 (
    echo.
    echo [start.bat] 程序异常退出，错误码 %errorlevel%
    pause
)
