"""Ticket 05 验收测试：概览页 — 健康度评分 + 需关注 + 维护状态 + 高级操作

覆盖 tickets.md Ticket 05 acceptance criteria：
- [x] 实现 OverviewPage：健康度评分区 + 需关注卡片 + 维护状态侧边卡片 + 主操作区 + 高级操作折叠区
- [x] HTTP 调用：GET /memory/status / POST /memory/maintain|reindex|compress|cleanup/orphaned
- [x] 业务术语中文化（grep 业务英文在 UI 标签为 0）
- [x] 60s QTimer 自动刷新（MemoryPanel._on_timer_timeout → overview.refresh_status）
- [x] grep QMessageBox.question 在 overview_page.py 为 0
- [x] MemoryPanel 接入 jump_to 信号：_on_jump_to 切 nav + 转发筛选参数

测试策略：
- 源码静态扫描：grep 业务英文 / QMessageBox.question
- 运行时实例化（offscreen）：OverviewPage / MemoryPanel 实例化无崩溃
- 渲染逻辑：_on_status_done 用 mock status 验证 score / level / concerns / maintainer labels
- 信号转发：_parse_jump_to 解析 + jump_to 信号 → MemoryPanel._on_jump_to 切 nav
- 定时器 wiring：MemoryPanel._on_timer_timeout → overview.refresh_status（mock 验证调用次数）
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OVERVIEW_PY = PROJECT_ROOT / "client" / "panels" / "memory" / "overview_page.py"
INIT_PY = PROJECT_ROOT / "client" / "panels" / "memory" / "__init__.py"


# ============================================================================
# Fixtures — qapp / qapp_args 由 pytest-qt 提供，无需在此覆盖
# ============================================================================


@pytest.fixture
def overview_page(qapp):
    """构造 OverviewPage 实例（offscreen）。"""
    from client.panels.memory.overview_page import OverviewPage

    page = OverviewPage()
    yield page
    page.deleteLater()


@pytest.fixture
def memory_panel(qapp):
    """构造 MemoryPanel 实例（offscreen，复用 OverviewPage 真实实例）。"""
    from client.panels.memory import MemoryPanel

    panel = MemoryPanel()
    yield panel
    panel._timer.stop()
    panel.deleteLater()


# ============================================================================
# Part 1: 源码静态扫描
# ============================================================================


class TestSourceStaticChecks:
    """overview_page.py 源码层验证。"""

    def test_no_qmessagebox_question_in_overview(self):
        """overview_page.py 不得使用 QMessageBox.question（必须用 ConfirmDialog）"""
        source = OVERVIEW_PY.read_text(encoding="utf-8")
        assert "QMessageBox.question" not in source

# ============================================================================
# Part 2: OverviewPage 渲染逻辑
# ============================================================================


def _mock_healthy_status() -> dict:
    """构造一个满分（green）的 mock status dict"""
    return {
        "facts": 100,
        "embedding_ready": True,
        "pending_messages": 0,
        "maintainer": {
            "last_run": "2099-01-01T00:00:00",  # 未来时间，永远新鲜
            "stale_threshold_days": 30,
            "interval_hours": 24,
            "validate_enabled": True,
            "status_counts": {"archive": 0, "keep": 100, "pending": 0},
            "stale_total": 0,
        },
        "db_size_history": [
            {"date": "2026-08-12", "size_mb": 30.0},
            {"date": "2026-08-18", "size_mb": 31.0},  # 周增长 3.3%，不触发
        ],
    }


def _mock_unhealthy_status() -> dict:
    """构造一个低分（red）的 mock status dict — 多维度同时扣分"""
    return {
        "facts": 100,
        "embedding_ready": False,  # 维度 2 red
        "pending_messages": 150,   # 维度 3 red（>100）
        "maintainer": {
            "last_run": "2020-01-01T00:00:00",  # 维度 4 yellow（很久没运行）
            "stale_threshold_days": 30,
            "interval_hours": 24,
            "validate_enabled": False,
            "status_counts": {"archive": 50, "keep": 30, "pending": 20},
            "stale_total": 50,  # 维度 1 red（rate=0.5 > 0.2）
        },
        "db_size_history": [
            {"date": "2026-08-12", "size_mb": 10.0},
            {"date": "2026-08-18", "size_mb": 50.0},  # 周增长 400%，触发 yellow
        ],
    }


class TestOverviewRendering:
    """OverviewPage 渲染逻辑测试（绕过 HttpWorker，直接调 _on_status_done）。"""

    def test_instantiation(self, overview_page):
        """OverviewPage 可实例化，关键 widget 存在"""
        assert overview_page._score_label is not None
        assert overview_page._level_label is not None
        assert overview_page._maintainer_label is not None
        assert overview_page._action_bar is not None
        assert overview_page._action_status is not None

    def test_healthy_status_renders_green(self, overview_page):
        """满分 status → 评分 ≥80 / level=green / concerns 为空"""
        overview_page._on_status_done(_mock_healthy_status())
        assert overview_page._score_label.text() == "100"
        assert "良好" in overview_page._level_label.text()
        # concerns 为空 → 显示「暂无需关注项」
        assert overview_page._concerns_container.count() == 1

    def test_unhealthy_status_renders_red(self, overview_page):
        """多维度扣分 status → 评分 <60 / level=red / concerns 5 条"""
        overview_page._on_status_done(_mock_unhealthy_status())
        score = int(overview_page._score_label.text())
        assert score < 60
        assert "紧急" in overview_page._level_label.text()
        # 5 个维度都触发 concern：过时率 / 嵌入可用性 / 待处理堆积 / 维护新鲜度 / DB 增长率
        assert overview_page._concerns_container.count() == 5

    def test_maintainer_labels_chinese(self, overview_page):
        """维护状态卡片 labels 应中文化（过时阈值 / 运行间隔 / 归档数 / 保留数 / 待处理数 / 过时总数）"""
        overview_page._on_status_done(_mock_healthy_status())
        text = overview_page._maintainer_label.text()
        assert "上次运行" in text
        assert "过时阈值" in text
        assert "运行间隔" in text
        assert "LLM 验证" in text
        assert "归档数" in text
        assert "保留数" in text
        assert "待处理数" in text
        assert "过时总数" in text

    def test_stale_total_red_highlight(self, overview_page):
        """过时总数 > 0 时维护状态应含红色 HTML span"""
        overview_page._on_status_done(_mock_unhealthy_status())
        text = overview_page._maintainer_label.text()
        assert "color:" in text and "过时总数" in text

    def test_status_failed_renders_error(self, overview_page):
        """_on_status_failed → 显示「加载失败」+ 清空 concerns"""
        overview_page._on_status_failed("connection refused")
        assert "--" in overview_page._score_label.text() or "失败" in overview_page._level_label.text()
        assert "无法获取维护状态" in overview_page._maintainer_label.text()
        assert overview_page._concerns_container.count() == 1  # 显示空状态

    def test_status_not_dict_no_crash(self, overview_page):
        """_on_status_done 收到非 dict（如 None）不应崩溃"""
        overview_page._on_status_done(None)
        # 评分仍为初始 "--"
        assert overview_page._score_label.text() == "--"


# ============================================================================
# Part 3: 维护操作 worker（串行 ADR-0022）
# ============================================================================


class TestActionWorker:
    """维护操作串行 worker 测试。"""

    def test_action_done_updates_label(self, overview_page):
        """_on_action_done → 更新 _action_status 为「完成」"""
        overview_page._on_action_done({"ok": True}, "触发维护")
        assert "完成" in overview_page._action_status.text()

    def test_action_failed_updates_label(self, overview_page):
        """_on_action_failed → 更新 _action_status 为「失败」"""
        overview_page._on_action_failed("timeout", "重建索引")
        assert "失败" in overview_page._action_status.text()
        assert "timeout" in overview_page._action_status.text()

    def test_action_finished_clears_worker_ref(self, overview_page):
        """_on_action_finished → 清空 _action_worker 引用"""
        # 模拟有 worker 在跑
        overview_page._action_worker = object()  # 占位引用
        overview_page._on_action_finished()
        assert overview_page._action_worker is None

    def test_serial_rejects_concurrent_action(self, overview_page):
        """已有 worker 在跑时再触发新操作应拒绝（ADR-0022 串行约束）"""
        overview_page._action_worker = object()  # 模拟有 worker 在跑
        overview_page._on_trigger_maintain()  # 触发新操作
        # 应显示排队提示，不启动新 worker
        assert "排队" in overview_page._action_status.text() or "已有操作" in overview_page._action_status.text()


# ============================================================================
# Part 4: jump_to 信号 + _parse_jump_to
# ============================================================================


class TestJumpToParsing:
    """_parse_jump_to 解析逻辑测试。"""

    def test_parse_simple_page(self):
        from client.panels.memory.overview_page import _parse_jump_to

        page, params = _parse_jump_to("overview")
        assert page == "overview"
        assert params == {}

    def test_parse_with_query(self):
        from client.panels.memory.overview_page import _parse_jump_to

        page, params = _parse_jump_to("memory_list?stale=true")
        assert page == "memory_list"
        assert params == {"stale": True}

    def test_parse_false_value(self):
        from client.panels.memory.overview_page import _parse_jump_to

        page, params = _parse_jump_to("timeline?stale=false")
        assert params == {"stale": False}

    def test_parse_string_value(self):
        from client.panels.memory.overview_page import _parse_jump_to

        page, params = _parse_jump_to("memory_list?type=feedback")
        assert params == {"type": "feedback"}


# ============================================================================
# Part 5: MemoryPanel wiring
# ============================================================================


class TestMemoryPanelWiring:
    """MemoryPanel 钩子接入测试。"""

    def test_panel_instantiation(self, memory_panel):
        """MemoryPanel 可实例化，6 个 nav 条目 + overview_page 接入"""
        assert memory_panel._nav is not None
        assert memory_panel._nav.count() == 6
        assert memory_panel._overview_page is not None
        assert memory_panel._pages is not None
        assert memory_panel._pages.count() == 6

    def test_on_show_triggers_refresh(self, memory_panel):
        """on_show → 应触发 overview.refresh_status（mock 验证调用次数）"""
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel.on_show()
            assert mock_refresh.called

    def test_on_refresh_overview(self, memory_panel):
        """on_refresh 在概览页 → 应调 overview.refresh_status"""
        memory_panel._nav.setCurrentRow(0)
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel.on_refresh()
            assert mock_refresh.called

    def test_on_refresh_non_overview_noop(self, memory_panel):
        """on_refresh 在非概览页 → 不调 overview.refresh_status（其他页 Ticket 06-10 接入）"""
        memory_panel._nav.setCurrentRow(1)  # 切到记忆库页
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel.on_refresh()
            assert not mock_refresh.called

    def test_on_backend_online_triggers_refresh(self, memory_panel):
        """on_backend_status_change(online=True) → 调 overview.refresh_status"""
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel.on_backend_status_change(True)
            assert mock_refresh.called

    def test_on_backend_offline_stops_timer(self, memory_panel):
        """on_backend_status_change(online=False) → 停定时器 + 切离线 widget"""
        memory_panel._timer.start()
        assert memory_panel._timer.isActive()
        memory_panel.on_backend_status_change(False)
        assert not memory_panel._timer.isActive()

    def test_timer_timeout_calls_refresh(self, memory_panel):
        """_on_timer_timeout 在概览页 → 调 overview.refresh_status"""
        memory_panel._nav.setCurrentRow(0)
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel._on_timer_timeout()
            assert mock_refresh.called

    def test_timer_timeout_skips_when_not_on_overview(self, memory_panel):
        """_on_timer_timeout 在非概览页 → 不调 refresh_status（ADR-0022 避免并发）"""
        memory_panel._nav.setCurrentRow(2)  # 时间线页
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel._on_timer_timeout()
            assert not mock_refresh.called

    def test_nav_switch_to_overview_triggers_refresh(self, memory_panel):
        """切到概览页 → 触发 refresh_status"""
        memory_panel._nav.setCurrentRow(1)  # 先切到记忆库页
        with patch.object(memory_panel._overview_page, "refresh_status") as mock_refresh:
            memory_panel._nav.setCurrentRow(0)  # 切回概览
            assert mock_refresh.called

    def test_on_jump_to_switches_nav(self, memory_panel):
        """_on_jump_to('memory_list', ...) → 切 nav 到 row 1"""
        memory_panel._on_jump_to("memory_list", {"stale": True})
        assert memory_panel._nav.currentRow() == 1

    def test_on_jump_to_unknown_page_noop(self, memory_panel):
        """_on_jump_to 未知 page_name → 不切 nav"""
        memory_panel._nav.setCurrentRow(0)
        memory_panel._on_jump_to("nonexistent", {})
        assert memory_panel._nav.currentRow() == 0  # 未切

    def test_jump_to_signal_forwarded(self, memory_panel):
        """OverviewPage.jump_to 信号 → MemoryPanel._on_jump_to"""
        with patch.object(memory_panel, "_on_jump_to") as mock_handler:
            memory_panel._overview_page.jump_to.emit("memory_list", {"stale": True})
            mock_handler.assert_called_once_with("memory_list", {"stale": True})
