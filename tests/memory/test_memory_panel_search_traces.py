"""Ticket 08 验收测试：检索追踪页 — 表格 + 详情 + 清理

覆盖 tickets.md Ticket 08 acceptance criteria：
- [x] 实现 SearchTracesPage：筛选条（数量 + 查询 + 重置）+ TableWithDetail + ActionButtonsBar
- [x] 5 列表格：trace_id / 查询 / 时间 / 命中数 / 耗时
- [x] HTTP 调用：GET /memory/search/traces?limit=N + GET /memory/search/traces/{trace_id}
- [x] 清理操作：DELETE /memory/search/traces/cleanup[?days=N]，弹 ConfirmDialog warning
- [x] trace 字段名（candidates/final_results/score/latency_ms）保留英文无括注
- [x] grep QMessageBox.question 在 search_traces_page.py 为 0
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PAGE_PY = PROJECT_ROOT / "client" / "panels" / "memory" / "search_traces_page.py"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def page(qapp):
    """构造 SearchTracesPage 实例（offscreen）。"""
    from client.panels.memory.search_traces_page import SearchTracesPage

    p = SearchTracesPage()
    yield p
    p.deleteLater()


def _mock_list_response() -> dict:
    """构造 GET /memory/search/traces 的 mock 响应"""
    return {
        "traces": [
            {
                "id": 42,
                "ts": "2026-08-18T10:00:00",
                "query": "记忆面板如何接入检索追踪",
                "params": {"top_k": 10},
                "mode": "hybrid",
                "latency_ms": 12.5,
                "bm25_count": 8,
                "vector_count": 15,
                "final_count": 10,
                "error": None,
            },
            {
                "id": 41,
                "ts": "2026-08-18T09:30:00",
                "query": "短查询",
                "params": None,
                "mode": "bm25",
                "latency_ms": 0.8,
                "bm25_count": 5,
                "vector_count": 0,
                "final_count": 5,
                "error": None,
            },
            {
                "id": 40,
                "ts": "2026-08-17T15:00:00",
                "query": "无 latency 数据的 trace",
                "params": {},
                "mode": "vector",
                "latency_ms": None,
                "bm25_count": 0,
                "vector_count": 20,
                "final_count": 20,
                "error": "embedding engine unavailable",
            },
        ],
        "total": 3,
    }


def _mock_detail_response() -> dict:
    """构造 GET /memory/search/traces/{trace_id} 的 mock 详情响应"""
    return {
        "id": 42,
        "ts": "2026-08-18T10:00:00",
        "query": "记忆面板如何接入检索追踪",
        "params": {"top_k": 10, "alpha": 0.7},
        "mode": "hybrid",
        "latency_ms": 12.5,
        "bm25_count": 8,
        "vector_count": 15,
        "final_count": 10,
        "error": None,
        "candidates": [
            {"id": 1, "score": 0.92, "content": "记忆面板架构"},
            {"id": 2, "score": 0.87, "content": "检索追踪设计"},
            {"id": 3, "score": 0.81, "content": "trace 字段"},
        ],
        "final_results": [
            {"id": 1, "score": 0.92, "content": "记忆面板架构"},
            {"id": 2, "score": 0.87, "content": "检索追踪设计"},
        ],
    }


def _mock_cleanup_response(days: int | None = None) -> dict:
    """构造 DELETE /memory/search/traces/cleanup 的 mock 响应"""
    return {
        "status": "ok",
        "result": {
            "deleted": 5,
            "cutoff": "2026-08-11T00:00:00",
        },
    }


# ============================================================================
# Part 1: 源码静态扫描
# ============================================================================


class TestSourceStaticChecks:
    """search_traces_page.py 源码层验证。"""

    def test_no_qmessagebox_question(self):
        source = PAGE_PY.read_text(encoding="utf-8")
        assert "QMessageBox.question" not in source

# ============================================================================
# Part 2: 渲染逻辑
# ============================================================================


class TestListRendering:
    """SearchTracesPage 列表渲染逻辑测试。"""

    def test_instantiation(self, page):
        """SearchTracesPage 可实例化"""
        assert page._table is not None
        assert page._action_bar is not None
        assert page._status_label is not None
        assert page._limit_spin is not None
        assert page._cleanup_days_spin is not None

    def test_list_done_renders_table(self, page):
        """_on_list_done 收到 mock 数据 → 表格行数 = 3"""
        page._on_list_done(_mock_list_response())
        assert page._table._table.rowCount() == 3
        assert "已加载 3 条 trace" in page._status_label.text()

    def test_list_done_precomputes_display_fields(self, page):
        """_on_list_done 应为每行补充 query_trunc + latency_label"""
        page._on_list_done(_mock_list_response())
        row0 = page._all_rows[0]
        assert row0["query_trunc"] == "记忆面板如何接入检索追踪"
        # latency_ms=12.5 → "12.5 ms"
        assert "ms" in row0["latency_label"]
        row1 = page._all_rows[1]
        # latency_ms=0.8 → "800 μs" (< 1 ms)
        assert "μs" in row1["latency_label"]
        row2 = page._all_rows[2]
        # latency_ms=None → 空串
        assert row2["latency_label"] == ""

    def test_list_done_invalid_data_no_crash(self, page):
        """_on_list_done 收到非 dict / 缺 traces / traces 非 list → 不崩溃"""
        page._on_list_done(None)
        page._on_list_done({})
        page._on_list_done({"traces": "not a list"})
        assert "异常" in page._status_label.text() or "等待" in page._status_label.text()

    def test_list_failed_renders_error(self, page):
        """_on_list_failed → 显示错误"""
        page._on_list_failed("connection refused")
        assert "失败" in page._status_label.text()
        assert page._table._table.rowCount() == 0


# ============================================================================
# Part 3: 行选中 + 详情渲染
# ============================================================================


class TestRowActivation:
    """SearchTracesPage 行选中 + 详情渲染测试。"""

    def test_row_activated_sets_current_trace_id(self, page):
        """_on_row_activated(dict) → 设置 _current_trace_id"""
        page._on_row_activated({"id": 42})
        assert page._current_trace_id == 42

    def test_row_activated_invalid_id_no_crash(self, page):
        """_on_row_activated 收到非数字 id → 不崩溃，current_trace_id 为 None"""
        page._on_row_activated({"id": "not a number"})
        assert page._current_trace_id is None
        page._on_row_activated({})
        assert page._current_trace_id is None

    def test_detail_done_renders_markdown(self, page):
        """_on_detail_done 收到 mock detail → 渲染 markdown"""
        page._on_detail_done(_mock_detail_response())
        raw_md = getattr(page._table._detail, "_raw_md", "")
        assert "Trace #42" in raw_md
        assert "candidates" in raw_md
        assert "final_results" in raw_md
        assert "score=0.92" in raw_md

    def test_detail_failed_renders_error(self, page):
        """_on_detail_failed → 详情区显示错误"""
        page._on_detail_failed("timeout")
        raw_md = getattr(page._table._detail, "_raw_md", "")
        assert "失败" in raw_md

    def test_render_trace_detail_md_structure(self):
        """_render_trace_detail_md 输出含元数据 / params / candidates / final_results"""
        from client.panels.memory.search_traces_page import (
            _render_trace_detail_md,
        )
        md = _render_trace_detail_md(_mock_detail_response())
        assert "# Trace #42" in md
        assert "## 元数据" in md
        assert "## params" in md
        assert "## candidates" in md
        assert "## final_results" in md
        assert "top_k" in md  # params 内容
        assert "0.92" in md  # score

    def test_render_trace_detail_md_truncates_long_lists(self):
        """candidates 超过 10 条时只渲染前 10 条 + 「还有 N 条未展示」提示"""
        from client.panels.memory.search_traces_page import (
            _render_trace_detail_md,
        )
        detail = {
            "id": 1,
            "ts": "2026-08-18T10:00:00",
            "query": "test",
            "candidates": [
                {"id": i, "score": 1.0 - i * 0.01, "content": f"item {i}"}
                for i in range(15)
            ],
            "final_results": [],
        }
        md = _render_trace_detail_md(detail)
        assert "还有 5 条未展示" in md
        # 应只渲染 candidates[0..9]，不应含 candidates[10..14] 的 content
        assert "item 9" in md
        assert "item 14" not in md

    def test_render_trace_detail_md_handles_empty_lists(self):
        """candidates / final_results 为空 list → 不出现该 section"""
        from client.panels.memory.search_traces_page import (
            _render_trace_detail_md,
        )
        detail = {
            "id": 1,
            "ts": "2026-08-18T10:00:00",
            "query": "test",
            "candidates": [],
            "final_results": [],
        }
        md = _render_trace_detail_md(detail)
        assert "## candidates" not in md
        assert "## final_results" not in md

    def test_format_latency_units(self):
        """_format_latency 应按数量级返回 μs / ms / s"""
        from client.panels.memory.search_traces_page import _format_latency
        assert "μs" in _format_latency(0.5)  # < 1 ms
        assert "ms" in _format_latency(12.5)  # < 1000 ms
        assert "s" in _format_latency(1500)  # >= 1000 ms
        assert _format_latency(None) == ""
        assert _format_latency("") == ""


# ============================================================================
# Part 4: 清理操作
# ============================================================================


class TestCleanupAction:
    """SearchTracesPage 清理操作测试。"""

    def test_cleanup_message_with_days(self, page):
        """days > 0 时 ConfirmDialog 文案应含「N 天」"""
        page._cleanup_days_spin.setValue(7)
        msg = page._build_cleanup_message()
        assert "7 天" in msg
        assert "不可撤销" in msg

    def test_cleanup_message_zero_uses_backend_config(self, page):
        """days = 0 时文案应说明走后端配置"""
        page._cleanup_days_spin.setValue(0)
        msg = page._build_cleanup_message()
        assert "retention_days" in msg or "后端配置" in msg

    def test_cleanup_clicked_starts_worker_with_days(self, page, monkeypatch):
        """days > 0 → DELETE worker 启动 + params 含 days=N"""
        started = []
        captured_params = []

        from client.core import http_worker

        def fake_start(self):
            started.append(self._path)
            captured_params.append(self._params)
            self.done.emit(_mock_cleanup_response())
            self.finished.emit()

        monkeypatch.setattr(http_worker.HttpWorker, "start", fake_start)
        page._cleanup_days_spin.setValue(14)
        page._on_cleanup_clicked()
        assert any("cleanup" in p for p in started), \
            f"DELETE worker 未启动: {started}"
        assert captured_params[0] == {"days": 14}

    def test_cleanup_clicked_zero_no_days_param(self, page, monkeypatch):
        """days = 0 → DELETE worker 启动 + params 不含 days"""
        started = []
        captured_params = []

        from client.core import http_worker

        def fake_start(self):
            started.append(self._path)
            captured_params.append(self._params)
            self.done.emit(_mock_cleanup_response())
            self.finished.emit()

        monkeypatch.setattr(http_worker.HttpWorker, "start", fake_start)
        page._cleanup_days_spin.setValue(0)
        page._on_cleanup_clicked()
        assert any("cleanup" in p for p in started)
        # days=0 时 params 应为空 dict（不传 days 参数，让后端走 retention_days）
        assert captured_params[0] == {}

    def test_cleanup_done_renders_status_and_refreshes(self, page, monkeypatch):
        """_on_cleanup_done → 更新状态 + 自动 refresh 列表"""
        refresh_called = []
        monkeypatch.setattr(page, "refresh", lambda: refresh_called.append(True))
        page._on_cleanup_done(_mock_cleanup_response())
        assert "清理完成" in page._status_label.text()
        assert "5 条" in page._status_label.text()
        assert refresh_called  # 清理后应自动刷新

    def test_cleanup_failed_renders_error(self, page):
        """_on_cleanup_failed → 显示失败"""
        page._on_cleanup_failed("permission denied")
        assert "清理失败" in page._status_label.text()

    def test_cleanup_uses_confirm_dialog_warning(self, page, monkeypatch):
        """点击清理按钮 → 弹 ConfirmDialog.confirm（risk_level=warning）"""
        # 验证 add_danger_action 已用 warning risk_level
        # 通过 mock ConfirmDialog.confirm 验证调用
        from client.panels.memory import _shared

        captured = []

        def fake_confirm(parent, title, message, risk_level):
            captured.append((title, message, risk_level))
            return False  # 用户取消

        monkeypatch.setattr(_shared.ConfirmDialog, "confirm",
                            staticmethod(fake_confirm))
        # 直接调 _on_cleanup_clicked 模拟用户已确认
        # 但本测试想验证 ConfirmDialog 是被调用的，所以直接调按钮 click
        started = []
        from client.core import http_worker

        def fake_start(self):
            started.append(self._path)

        monkeypatch.setattr(http_worker.HttpWorker, "start", fake_start)
        page._cleanup_days_spin.setValue(7)
        page._cleanup_btn.click()
        # ConfirmDialog 被调用，用户取消 → 不应启动 worker
        assert captured, "ConfirmDialog.confirm 未被调用"
        title, message, risk_level = captured[0]
        assert title == "清理检索追踪"
        assert "7 天" in message
        assert risk_level == "warning"
        assert not started, "用户取消后不应启动 worker"


# ============================================================================
# Part 5: 跳转参数接收
# ============================================================================


class TestJumpParams:
    """SearchTracesPage 跳转参数接收测试。"""

    def test_apply_jump_params_with_limit(self, page):
        """apply_jump_params({"limit": 50}) → 设置 spin=50 + 触发 refresh"""
        with patch.object(page, "refresh") as mock_refresh:
            page.apply_jump_params({"limit": 50})
            assert page._limit_spin.value() == 50
            assert mock_refresh.called

    def test_apply_jump_params_empty_refreshes(self, page):
        """apply_jump_params({}) → 立即 refresh"""
        with patch.object(page, "refresh") as mock_refresh:
            page.apply_jump_params({})
            assert mock_refresh.called

    def test_apply_jump_params_invalid_limit_ignored(self, page):
        """apply_jump_params({"limit": "invalid"}) → 不崩溃，忽略 limit"""
        with patch.object(page, "refresh") as mock_refresh:
            page.apply_jump_params({"limit": "invalid"})
            assert mock_refresh.called
            # spin 保持默认值
            assert page._limit_spin.value() == 20
