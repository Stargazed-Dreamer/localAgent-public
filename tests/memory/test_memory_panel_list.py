"""Ticket 06 验收测试：记忆库页 — 表格 + 筛选 + 详情 + 编辑跳转 + 删除

覆盖 tickets.md Ticket 06 acceptance criteria：
- [x] 实现 MemoryListPage：FilterBar + TableWithDetail + ActionButtonsBar
- [x] 5 列表格：key / 类型 / 来源 / 更新时间 / 过时状态
- [x] HTTP 调用：GET /memory/list + GET /memory/{key} + DELETE /memory/{key}
- [x] 编辑按钮 emit jump_to_write_page(key) 信号
- [x] 删除按钮弹 ConfirmDialog danger 确认后调 DELETE
- [x] 跳转链接接收：apply_jump_params({"stale": True}) → 勾选「仅看过时」+ 刷新列表
- [x] 业务术语中文化（auto→自动 / manual→手动 / stale→过时）
- [x] grep QMessageBox.question 在 memory_list_page.py 为 0
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LIST_PY = PROJECT_ROOT / "client" / "panels" / "memory" / "memory_list_page.py"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def list_page(qapp):
    """构造 MemoryListPage 实例（offscreen）。"""
    from client.panels.memory.memory_list_page import MemoryListPage

    page = MemoryListPage()
    yield page
    page.deleteLater()


def _mock_list_response() -> dict:
    """构造 GET /memory/list 的 mock 响应"""
    return {
        "keys": [
            {
                "key": "user_preference_theme",
                # source 统一为 'auto' | 'manual'（原 fact_source 字段已合并到 source）
                "source": "manual",
                "fact_type": "user",
                "summary": "用户偏好深色主题",
                "value_preview": '{"name": "深色主题"}',
                "updated_at": "2026-08-01 10:00:00",  # 17 天前，< user 阈值 90 → 正常
            },
            {
                "key": "project_arch_old",
                "source": "auto",
                "fact_type": "project",
                "summary": "项目架构（旧版）",
                "value_preview": '{"name": "旧架构"}',
                "updated_at": "2026-07-01 10:00:00",  # 48 天前，> project 阈值 7 → 过时
            },
            {
                "key": "feedback_recent",
                "source": "manual",
                "fact_type": "feedback",
                "summary": "最近反馈",
                "value_preview": "{}",
                "updated_at": "2026-08-15 10:00:00",  # 3 天前，< feedback 阈值 30 → 正常
            },
        ],
        "total": 3,
    }


def _mock_detail_response() -> dict:
    """构造 GET /memory/{key} 的 mock 响应"""
    return {
        "key": "user_preference_theme",
        "source": "manual",
        "fact_type": "user",
        "updated_at": "2026-08-01 10:00:00",
        "occurred_at": "2026-08-01T10:00:00",
        "consumption_contexts": ["recurring.*", "adhoc.*"],
        "trigger_keywords": ["主题", "深色"],
        "memory_age": "17 days ago",
        "staleness_warning": None,
        "trust_recall_hint": "引用前请验证",
        "name": "深色主题",
        "description": "用户偏好使用深色主题",
    }


# ============================================================================
# Part 1: 源码静态扫描
# ============================================================================


class TestSourceStaticChecks:
    """memory_list_page.py 源码层验证。"""

    def test_no_qmessagebox_question(self):
        source = LIST_PY.read_text(encoding="utf-8")
        assert "QMessageBox.question" not in source

# ============================================================================
# Part 2: 渲染 + 筛选逻辑
# ============================================================================


class TestListRendering:
    """MemoryListPage 渲染 + 筛选逻辑测试。"""

    def test_instantiation(self, list_page):
        """MemoryListPage 可实例化"""
        assert list_page._filter_bar is not None
        assert list_page._table is not None
        assert list_page._action_bar is not None
        assert list_page._edit_btn is not None
        assert list_page._status_label is not None

    def test_list_done_renders_table(self, list_page):
        """_on_list_done 收到 mock 数据 → 表格行数 = 3"""
        list_page._on_list_done(_mock_list_response())
        assert list_page._table._table.rowCount() == 3
        assert "已加载 3 条记忆" in list_page._status_label.text()

    def test_list_done_adds_stale_label(self, list_page):
        """_on_list_done 应为每行补充 stale_label + fact_source_label"""
        list_page._on_list_done(_mock_list_response())
        # 第 2 行（project_arch_old）应标记「过时」（48 天 > project 阈值 7）
        row1 = list_page._all_rows[1]
        assert row1["stale_label"] == "过时"
        # 第 1 行（user_preference_theme）应标记「正常」（17 天 < user 阈值 90）
        row0 = list_page._all_rows[0]
        assert row0["stale_label"] == "正常"
        # fact_source 中文化
        assert row0["fact_source_label"] == "手动"
        assert row1["fact_source_label"] == "自动"

    def test_list_done_invalid_data_no_crash(self, list_page):
        """_on_list_done 收到非 dict / 缺 keys 字段 / keys 非 list → 不崩溃"""
        list_page._on_list_done(None)
        list_page._on_list_done({})
        list_page._on_list_done({"keys": "not a list"})
        assert "异常" in list_page._status_label.text() or "等待" in list_page._status_label.text()

    def test_list_failed_renders_error(self, list_page):
        """_on_list_failed → 显示错误"""
        list_page._on_list_failed("connection refused")
        assert "失败" in list_page._status_label.text()
        assert list_page._table._table.rowCount() == 0

    def test_filter_by_fact_type(self, list_page):
        """筛选 fact_type='user' → 只显示 user 类型"""
        list_page._on_list_done(_mock_list_response())
        list_page._apply_filters({"fact_type": "user", "source": "",
                                   "stale_only": False, "keyword": ""})
        assert list_page._table._table.rowCount() == 1

    def test_filter_by_stale_only(self, list_page):
        """筛选 stale_only=True → 只显示过时项"""
        list_page._on_list_done(_mock_list_response())
        list_page._apply_filters({"fact_type": "", "source": "",
                                   "stale_only": True, "keyword": ""})
        # 只有 project_arch_old 过时
        assert list_page._table._table.rowCount() == 1

    def test_filter_by_keyword(self, list_page):
        """筛选 keyword='架构' → 只显示 summary 含「架构」的行"""
        list_page._on_list_done(_mock_list_response())
        list_page._apply_filters({"fact_type": "", "source": "",
                                   "stale_only": False, "keyword": "架构"})
        assert list_page._table._table.rowCount() == 1


# ============================================================================
# Part 3: 行选中 + 详情渲染
# ============================================================================


class TestRowActivation:
    """MemoryListPage 行选中 + 详情渲染测试。"""

    def test_row_activated_sets_current_key(self, list_page):
        """_on_row_activated(dict) → 设置 _current_key + 启用编辑按钮"""
        list_page._on_row_activated({"key": "test_key"})
        assert list_page._current_key == "test_key"
        assert list_page._edit_btn.isEnabled()

    def test_row_activated_empty_key_disables_edit(self, list_page):
        """_on_row_activated 收到无 key 行 → 编辑按钮禁用"""
        list_page._on_row_activated({})
        assert list_page._current_key is None
        assert not list_page._edit_btn.isEnabled()

    def test_detail_done_renders_markdown(self, list_page):
        """_on_detail_done 收到 mock detail → 渲染 markdown"""
        list_page._on_detail_done(_mock_detail_response())
        # 检查 detail widget 的 markdown 内容（set_markdown 存储到 _raw_md）
        raw_md = getattr(list_page._table._detail, "_raw_md", "")
        assert "user_preference_theme" in raw_md
        assert "深色主题" in raw_md

    def test_detail_failed_renders_error(self, list_page):
        """_on_detail_failed → 详情区显示错误"""
        list_page._on_detail_failed("timeout")
        raw_md = getattr(list_page._table._detail, "_raw_md", "")
        assert "失败" in raw_md

    def test_render_detail_md_structure(self, list_page):
        """_render_detail_md 应输出含元数据 / 消费场景 / 触发关键词 / 数据字段的 markdown"""
        md = list_page._render_detail_md(_mock_detail_response())
        assert "# user_preference_theme" in md
        assert "## 元数据" in md
        assert "## 消费场景" in md
        assert "## 触发关键词" in md
        assert "## 数据字段" in md
        assert "recurring.*" in md
        assert "深色主题" in md


# ============================================================================
# Part 4: 编辑 / 删除操作
# ============================================================================


class TestEditDeleteActions:
    """MemoryListPage 编辑 / 删除操作测试。"""

    def test_edit_emits_jump_to_write_page(self, list_page):
        """点击编辑 → emit jump_to_write_page(key)"""
        emitted: list[str] = []
        list_page.jump_to_write_page.connect(lambda k: emitted.append(k))
        list_page._current_key = "test_key"
        list_page._on_edit_clicked()
        # qt 信号默认是同步 emit（AutoConnection 单线程），已立即触发
        assert emitted == ["test_key"]

    def test_edit_no_key_no_emit(self, list_page):
        """无选中 key → 编辑不发信号"""
        emitted: list[str] = []
        list_page.jump_to_write_page.connect(lambda k: emitted.append(k))
        list_page._current_key = None
        list_page._on_edit_clicked()
        assert not emitted

    def test_delete_with_mock_dialog_calls_worker(self, list_page, monkeypatch):
        """删除按钮点击 → ConfirmDialog 确认 → 启动 DELETE worker（不实际调网络）"""
        # mock ConfirmDialog.confirm 返回 True
        from client.panels.memory import _shared

        monkeypatch.setattr(_shared.ConfirmDialog, "confirm",
                            staticmethod(lambda *a, **k: True))
        # mock HttpWorker.start 防止真实网络调用
        started = []
        from client.core import http_worker

        def fake_start(self):
            started.append(self._path)
            # 模拟立即完成：emit done
            self.done.emit({"status": "deleted", "key": "test_key"})
            self.finished.emit()

        monkeypatch.setattr(http_worker.HttpWorker, "start", fake_start)
        # 设置当前选中
        list_page._current_key = "test_key"
        # 模拟有数据加载（refresh_list 会用）
        list_page._on_list_done({"keys": [], "total": 0})

        # 触发删除（add_danger_action 内部已绑 ConfirmDialog，但 _on_delete_clicked 是 callback）
        list_page._on_delete_clicked()

        assert any("test_key" in p for p in started), f"DELETE worker 未启动: {started}"

    def test_apply_jump_params_stale_true(self, list_page):
        """apply_jump_params({"stale": True}) → 勾选「仅看过时」"""
        with patch.object(list_page._filter_bar, "set_filter") as mock_set:
            list_page.apply_jump_params({"stale": True})
            # 应至少调用 set_filter("stale_only", True)
            calls = [(args[0], args[1]) for args, _ in mock_set.call_args_list]
            assert ("stale_only", True) in calls

    def test_apply_jump_params_empty_refreshes(self, list_page):
        """apply_jump_params({}) → 立即 refresh_list"""
        with patch.object(list_page, "refresh_list") as mock_refresh:
            list_page.apply_jump_params({})
            assert mock_refresh.called
