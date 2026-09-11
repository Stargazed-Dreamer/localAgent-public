"""Ruff 静态检查测试

对 server/ 和 client/ 目录运行 ruff check，确保代码符合静态检查规则。
规则配置见 pyproject.toml [tool.ruff] 段。

运行方式：
    uv run python -m pytest tests/test_lint.py -v
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _find_ruff() -> str | None:
    """定位 ruff 可执行文件。

    优先级：
      1. 项目 .venv 里的 ruff（与 sys.executable 同目录）
      2. PATH 中的 ruff
    """
    exe_name = "ruff.exe" if sys.platform == "win32" else "ruff"
    # sys.executable 通常是 .venv/Scripts/python.exe，ruff 在同目录
    ruff_in_venv = Path(sys.executable).parent / exe_name
    if ruff_in_venv.exists():
        return str(ruff_in_venv)
    return shutil.which("ruff")


def test_ruff_check_server_client_lib_tests():
    """server/ client/ lib/ tests/ 必须通过 ruff 静态检查。

    规则集：E, W, F, I, UP, B, SIM, C4（详见 pyproject.toml）。
    全局忽略：E501（行长）、SIM102（collapsible-if）、SIM105（suppressible-exception）。
    范围与 pyproject.toml [tool.ruff] extend-exclude 一致（已排除 temp/workspace 等）。
    失败时输出完整 ruff 报告，便于定位问题。

    注：用默认缓存（.ruff_cache/）加速重复跑，不再传 --no-cache。
    """
    ruff = _find_ruff()
    if ruff is None:
        pytest.skip("ruff 未安装；请运行 uv sync 安装 dev 依赖")

    result = subprocess.run(
        [ruff, "check", "server", "client", "lib", "tests"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, (
        f"ruff check 发现问题（规则配置见 pyproject.toml [tool.ruff] 段）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
