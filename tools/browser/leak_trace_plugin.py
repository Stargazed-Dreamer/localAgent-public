"""pytest 诊断插件：逐用例 teardown 后统计调试浏览器真实 tab 数，超过基线即记录泄漏用例。

背景（2026-09-03）：e2e 测试曾每轮泄漏 4-5 个孤儿 tab（window.open popup 无
Page 引用 + close_session 只关自己页）。该插件把"每个用例结束后浏览器还剩几个
tab"变成可断言的输出，用来定位是哪个用例在泄漏——单纯看 pytest 通过率看不到
tab 泄漏，因为测试断言的是接口响应，不是浏览器进程里的残留。

用法（项目根目录）：
    env PYTHONPATH="<项目根>/tools/browser" uv run pytest \
        -p leak_trace_plugin tests/browser/... -q

结果写入 <项目根>/temp/leak_trace.log（可用环境变量 LEAK_TRACE_LOG 覆盖）。
基线（默认 2 = 用户原有 chrome://newtab 等常驻 tab）可用 LEAK_TRACE_BASELINE 覆盖。

前提：后端 8766 存活（插件通过 GET /browser/tabs 数 tab）；调用失败时记 tabs=-1
（后端瞬时不可用不算泄漏，跳过本用例的判定）。
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG_PATH = Path(os.environ.get("LEAK_TRACE_LOG", _REPO_ROOT / "temp" / "leak_trace.log"))
BASELINE = int(os.environ.get("LEAK_TRACE_BASELINE", "2"))

_LOG: object | None = None  # typing: file handle，惰性打开


def _count_tabs() -> int:
    try:
        r = httpx.get("http://127.0.0.1:8766/browser/tabs", timeout=10)
        return len(r.json()["tabs"])
    except Exception:
        return -1


@pytest.fixture(autouse=True)
def _trace_tabs(request: pytest.FixtureRequest):
    global _LOG
    if _LOG is None:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOG = open(_LOG_PATH, "w", encoding="utf-8")
        _LOG.write(f"=== leak trace start, baseline={BASELINE} ===\n")  # type: ignore[union-attr]
        _LOG.flush()  # type: ignore[union-attr]
    yield
    n = _count_tabs()
    if n > BASELINE:
        _LOG.write(f"LEAK after {request.node.nodeid}: tabs={n}\n")  # type: ignore[union-attr]
        _LOG.flush()  # type: ignore[union-attr]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _LOG
    if _LOG is not None:
        _LOG.close()  # type: ignore[union-attr]
        _LOG = None
