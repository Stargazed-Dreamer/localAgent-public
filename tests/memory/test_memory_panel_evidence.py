"""Ticket 09 验收测试：证据账本页 — 表格 + 详情 + 清理

覆盖 tickets.md Ticket 09 acceptance criteria：
- [x] 实现 EvidencePage：筛选条（数量 + 查询 + 重置）+ TableWithDetail + ActionButtonsBar
- [x] 5 列表格：evidence_id / 类型 / 时间 / 冲突数 / 匹配方式
- [x] HTTP 调用：GET /memory/evidence/recent?limit=N + GET /memory/evidence/{evidence_id}
- [x] 清理操作：DELETE /memory/evidence/cleanup[?days=N]，弹 ConfirmDialog warning
- [x] evidence 字段名（conflicting_ids/source_type/matched_by）保留英文无括注
- [x] grep QMessageBox.question 在 evidence_page.py 为 0
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PAGE_PY = PROJECT_ROOT / "client" / "panels" / "memory" / "evidence_page.py"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def page(qapp):
    """构造 EvidencePage 实例（offscreen）。"""
    from client.panels.memory.evidence_page import EvidencePage

    p = EvidencePage()
    yield p
    p.deleteLater()


def _mock_list_response() -> dict:
    """构造 GET /memory/evidence/recent 的 mock 响应"""
    return {
        "evidences": [
            {
                "id": 100,
                "query": "记忆面板架构",
                "search_trace_id": 42,
                "source_type": "message",
                "source_id": 1,
                "content_preview": "记忆面板分 6 页 + 共享组件 ...",
                "timestamp": "2026-08-18T10:00:00",
                "retrieval_score": 0.92,
                "source_tag": "recent",
                "confidence": 0.8,
                "corroboration_count": 2,
                "conflicting_ids": [5, 7],
                "matched_by": "both",
                "created_at": "2026-08-18T10:00:01",
            },
            {
                "id": 99,
                "query": "检索追踪设计",
                "search_trace_id": 41,
                "source_type": "fact",
                "source_id": 2,
                "content_preview": "trace 含 candidates + final_results",
                "timestamp": "2026-08-18T09:30:00",
                "retrieval_score": 0.87,
                "source_tag": "semantic",
                "confidence": 0.7,
                "corroboration_count": 0,
                "conflicting_ids": [],
                "matched_by": "vector",
                "created_at": "2026-08-18T09:30:01",
            },
            {
                "id": 98,
                "query": "无 conflicting_ids 字段",
                "search_trace_id": 40,
                "source_type": "summary",
                "source_id": 3,
                "content_preview": "压缩摘要 ...",
                "timestamp": "2026-08-17T15:00:00",
                "retrieval_score": 0.65,
                "source_tag": "summary",
                "confidence": 0.5,
                "corroboration_count": 0,
                # 无 conflicting_ids 字段
                "matched_by": "summary",
                "created_at": "2026-08-17T15:00:01",
            },
        ],
        "total": 3,
    }


def _mock_detail_response() -> dict:
    """构造 GET /memory/evidence/{evidence_id} 的 mock 详情响应"""
    return {
        "id": 100,
        "query": "记忆面板架构",
        "search_trace_id": 42,
        "source_type": "message",
        "source_id": 1,
        "content_preview": "记忆面板分 6 页 + 共享组件 ...",
        "timestamp": "2026-08-18T10:00:00",
        "retrieval_score": 0.92,
        "source_tag": "recent",
        "confidence": 0.8,
        "corroboration_count": 2,
        "conflicting_ids": [5, 7, 12],
        "matched_by": "both",
        "created_at": "2026-08-18T10:00:01",
    }


def _mock_cleanup_response() -> dict:
    """构造 DELETE /memory/evidence/cleanup 的 mock 响应"""
    return {
        "status": "ok",
        "result": {
            "deleted": 3,
            "cutoff": "2026-08-04T00:00:00",
        },
    }


# ============================================================================
# Part 1: 源码静态扫描
# ============================================================================


class TestSourceStaticChecks:
    """evidence_page.py 源码层验证。"""

    def test_no_qmessagebox_question(self):
        source = PAGE_PY.read_text(encoding="utf-8")
        assert "QMessageBox.question" not in source

# ============================================================================
# Part 2: 渲染逻辑
# ============================================================================


class TestListRendering:
    """EvidencePage 列表渲染逻辑测试。"""

    def test_instantiation(self, page):
        """EvidencePage 可实例化"""
        assert page._table is not None
        assert page._action_bar is not None
        assert page._status_label is not None
        assert page._limit_spin is not None
        assert page._cleanup_days_spin is not None

    def test_list_done_renders_table(self, page):
        """_on_list_done 收到 mock 数据 → 表格行数 = 3"""
        page._on_list_done(_mock_list_response())
        assert page._table._table.rowCount() == 3
        assert "已加载 3 条证据" in page._status_label.text()

    def test_list_done_precomputes_conflicting_count(self, page):
        """_on_list_done 应为每行补充 conflicting_count 字段"""
        page._on_list_done(_mock_list_response())
        # row0: conflicting_ids=[5,7] → 2
        assert page._all_rows[0]["conflicting_count"] == 2
        # row1: conflicting_ids=[] → 0
        assert page._all_rows[1]["conflicting_count"] == 0
        # row2: 无 conflicting_ids 字段 → 0
        assert page._all_rows[2]["conflicting_count"] == 0

    def test_list_done_invalid_data_no_crash(self, page):
        """_on_list_done 收到非 dict / 缺 evidences / evidences 非 list → 不崩溃"""
        page._on_list_done(None)
        page._on_list_done({})
        page._on_list_done({"evidences": "not a list"})
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
    """EvidencePage 行选中 + 详情渲染测试。"""

    def test_row_activated_sets_current_evidence_id(self, page):
        """_on_row_activated(dict) → 设置 _current_evidence_id"""
        page._on_row_activated({"id": 100})
        assert page._current_evidence_id == 100

    def test_row_activated_invalid_id_no_crash(self, page):
        """_on_row_activated 收到非数字 id → 不崩溃，current_evidence_id 为 None"""
        page._on_row_activated({"id": "not a number"})
        assert page._current_evidence_id is None
        page._on_row_activated({})
        assert page._current_evidence_id is None

    def test_detail_done_renders_markdown(self, page):
        """_on_detail_done 收到 mock detail → 渲染 markdown"""
        page._on_detail_done(_mock_detail_response())
        raw_md = getattr(page._table._detail, "_raw_md", "")
        assert "Evidence #100" in raw_md
        assert "conflicting_ids" in raw_md
        assert "5" in raw_md  # conflicting_ids 内容

    def test_detail_failed_renders_error(self, page):
        """_on_detail_failed → 详情区显示错误"""
        page._on_detail_failed("timeout")
        raw_md = getattr(page._table._detail, "_raw_md", "")
        assert "失败" in raw_md

    def test_render_evidence_detail_md_structure(self):
        """_render_evidence_detail_md 输出含元数据 / content_preview / conflicting_ids"""
        from client.panels.memory.evidence_page import (
            _render_evidence_detail_md,
        )
        md = _render_evidence_detail_md(_mock_detail_response())
        assert "# Evidence #100" in md
        assert "## 元数据" in md
        assert "## content_preview" in md
        assert "## conflicting_ids" in md
        assert "source_type" in md
        assert "matched_by" in md
        assert "retrieval_score" in md

    def test_render_evidence_detail_md_empty_conflicting(self):
        """conflicting_ids 为空 list → 显示「无冲突证据」"""
        from client.panels.memory.evidence_page import (
            _render_evidence_detail_md,
        )
        detail = {
            "id": 1,
            "query": "test",
            "source_type": "message",
            "conflicting_ids": [],
            "created_at": "2026-08-18T10:00:00",
        }
        md = _render_evidence_detail_md(detail)
        assert "无冲突证据" in md

    def test_render_evidence_detail_md_no_conflicting_field(self):
        """conflicting_ids 字段缺失 → 不出现该 section"""
        from client.panels.memory.evidence_page import (
            _render_evidence_detail_md,
        )
        detail = {
            "id": 1,
            "query": "test",
            "source_type": "message",
            "created_at": "2026-08-18T10:00:00",
        }
        md = _render_evidence_detail_md(detail)
        assert "conflicting_ids" not in md

    def test_render_evidence_detail_md_truncates_long_preview(self):
        """content_preview 超过 500 字符时截断"""
        from client.panels.memory.evidence_page import (
            _render_evidence_detail_md,
        )
        long_preview = "x" * 1000
        detail = {
            "id": 1,
            "query": "test",
            "source_type": "message",
            "content_preview": long_preview,
            "conflicting_ids": [],
            "created_at": "2026-08-18T10:00:00",
        }
        md = _render_evidence_detail_md(detail)
        # 截断后 content_preview 不超过 500
        assert "..." in md
        # 应截断为 497 + "..."
        assert long_preview not in md


# ============================================================================
# Part 4: 清理操作
# ============================================================================


class TestCleanupAction:
    """EvidencePage 清理操作测试。"""

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
        assert "evidence_audit_keep_days" in msg or "后端配置" in msg

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
        # days=0 时 params 应为空 dict（不传 days 参数）
        assert captured_params[0] == {}

    def test_cleanup_done_renders_status_and_refreshes(self, page, monkeypatch):
        """_on_cleanup_done → 更新状态 + 自动 refresh 列表"""
        refresh_called = []
        monkeypatch.setattr(page, "refresh", lambda: refresh_called.append(True))
        page._on_cleanup_done(_mock_cleanup_response())
        assert "清理完成" in page._status_label.text()
        assert "3 条" in page._status_label.text()
        assert refresh_called

    def test_cleanup_failed_renders_error(self, page):
        """_on_cleanup_failed → 显示失败"""
        page._on_cleanup_failed("permission denied")
        assert "清理失败" in page._status_label.text()

    def test_cleanup_uses_confirm_dialog_warning(self, page, monkeypatch):
        """点击清理按钮 → 弹 ConfirmDialog.confirm（risk_level=warning）"""
        from client.panels.memory import _shared

        captured = []

        def fake_confirm(parent, title, message, risk_level):
            captured.append((title, message, risk_level))
            return False  # 用户取消

        monkeypatch.setattr(_shared.ConfirmDialog, "confirm",
                            staticmethod(fake_confirm))
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
        assert title == "清理证据账本"
        assert "7 天" in message
        assert risk_level == "warning"
        assert not started, "用户取消后不应启动 worker"


# ============================================================================
# Part 5: 跳转参数接收
# ============================================================================


class TestJumpParams:
    """EvidencePage 跳转参数接收测试。"""

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
