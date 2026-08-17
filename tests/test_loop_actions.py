"""测试 loop_actions.py 中的 TodosTriggerCheckAction 和 ScreenSummaryTriggerAction

策略：
- TodosTriggerCheckAction：用 tmp_path 模拟 watch_dir + 真实 store（tmp DB）+ monkeypatch add_message
- ScreenSummaryTriggerAction：用 tmp_path 模拟 raw_dir + 写 screen jsonl + monkeypatch add_message
- 用 asyncio.run 包装 async execute，避免依赖 pytest-asyncio
"""

import asyncio
import importlib
import json
import os
import tempfile
import time
from datetime import datetime

import pytest

todos_router_mod = importlib.import_module("server.todos.router")
from server.activity_tracker.loop_actions import (  # noqa: E402
    ScreenSummaryTriggerAction,
    TodosTriggerCheckAction,
)
from server.todos.store import TodosStore  # noqa: E402


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TodosStore(path)
    s.initialize()
    yield s
    s.close()
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def captured_messages(monkeypatch):
    """捕获 add_message 调用，避免真正推送到 user_message 模块"""
    msgs = []
    def _fake_add_message(text: str):
        msgs.append(text)
    monkeypatch.setattr("server.user_message.add_message", _fake_add_message)
    return msgs


def _run(coro):
    """同步运行 async coroutine"""
    return asyncio.run(coro)


# ==================== TodosTriggerCheckAction ====================

class TestTodosTriggerCheckAction:
    def test_no_triggered_todos_returns_skipped(self, store, captured_messages):
        """无 triggered todo → skipped"""
        action = TodosTriggerCheckAction()
        result = _run(action.execute({"config": {"check_interval": 300}}))
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["triggered_count"] == 0

    def test_new_file_triggers(self, store, captured_messages, tmp_path, monkeypatch):
        """watch_dir 下有新文件 → 触发 + 推送消息"""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        store.create_todo({
            "title": "CSV 监控", "type": "triggered",
            "trigger_condition": json.dumps({
                "event": "file_arrived",
                "watch_dir": str(watch_dir),
                "pattern": "*.csv",
            }),
        })
        (watch_dir / "data.csv").write_text("test", encoding="utf-8")

        monkeypatch.setattr(todos_router_mod, "get_todos_store", lambda: store)

        action = TodosTriggerCheckAction()
        result = _run(action.execute({"config": {"check_interval": 300}}))

        assert result["success"] is True
        assert result["triggered_count"] == 1
        assert len(captured_messages) == 1
        assert "CSV 监控" in captured_messages[0]

    def test_no_new_file_no_trigger(self, store, captured_messages, tmp_path, monkeypatch):
        """watch_dir 下无新文件（mtime 超过扫描窗口）→ 不触发"""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        store.create_todo({
            "title": "CSV 监控", "type": "triggered",
            "trigger_condition": json.dumps({
                "event": "file_arrived",
                "watch_dir": str(watch_dir),
                "pattern": "*.csv",
            }),
        })
        old_file = watch_dir / "old.csv"
        old_file.write_text("old", encoding="utf-8")
        old_time = time.time() - 3600
        os.utime(old_file, (old_time, old_time))

        monkeypatch.setattr(todos_router_mod, "get_todos_store", lambda: store)

        action = TodosTriggerCheckAction()
        result = _run(action.execute({"config": {"check_interval": 300}}))

        assert result["success"] is True
        assert result["triggered_count"] == 0
        assert len(captured_messages) == 0


# ==================== ScreenSummaryTriggerAction ====================

class TestScreenSummaryTriggerAction:
    def test_no_screen_data_skipped(self, tmp_path, captured_messages):
        """当日无 screen jsonl → skipped"""
        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))
        assert result["success"] is True
        assert result["skipped"] is True
        assert "无 screen" in result["skip_reason"]

    def test_below_threshold_skipped(self, tmp_path, captured_messages):
        """记录数和字符数都低于阈值 → skipped"""
        date_str = datetime.now().strftime("%Y%m%d")
        screen_path = tmp_path / f"screen_{date_str}.jsonl"
        with open(screen_path, "w", encoding="utf-8") as f:
            for _ in range(5):
                f.write(json.dumps({"ts": datetime.now().isoformat(), "answer": "short"}) + "\n")

        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["record_count"] == 5
        assert len(captured_messages) == 0

    def test_meets_threshold_triggers(self, tmp_path, captured_messages):
        """达阈值 → 触发 + 推送消息 + 写标记文件"""
        date_str = datetime.now().strftime("%Y%m%d")
        screen_path = tmp_path / f"screen_{date_str}.jsonl"
        with open(screen_path, "w", encoding="utf-8") as f:
            for i in range(25):
                f.write(json.dumps({"ts": datetime.now().isoformat(), "answer": f"activity {i}"}) + "\n")

        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))
        assert result["success"] is True
        assert result["triggered"] is True
        assert result["record_count"] == 25
        assert len(captured_messages) == 1
        marker = tmp_path / f".screen_summary_triggered_{date_str}"
        assert marker.exists()

    def test_marker_file_prevents_repeat(self, tmp_path, captured_messages):
        """标记文件已存在 → 不重复触发"""
        date_str = datetime.now().strftime("%Y%m%d")
        screen_path = tmp_path / f"screen_{date_str}.jsonl"
        with open(screen_path, "w", encoding="utf-8") as f:
            for _ in range(25):
                f.write(json.dumps({"answer": "x"}) + "\n")
        marker = tmp_path / f".screen_summary_triggered_{date_str}"
        marker.write_text("already triggered", encoding="utf-8")

        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))
        assert result["success"] is True
        assert result["skipped"] is True
        assert "已触发过" in result["skip_reason"]
        assert len(captured_messages) == 0


# ==================== D3-A: _format_screens_for_prompt OCR 兜底文字消费 ====================

class TestFormatScreensOcrFallback:
    """D3-A: OCR 兜底文字应被 _format_screens_for_prompt 消费，不再丢弃"""

    def test_ocr_fallback_text_included(self):
        """ocr_fallback_used=True 且有 answer → 输出包含 answer 文字

        Ticket 05: vl_status 用新语义 skipped_pace（替代旧 skipped_quota），
        验证 _format_screens_for_prompt 不依赖具体 skipped_* 子类型。
        """
        from server.activity_tracker.loop_actions import _format_screens_for_prompt
        records = [
            {
                "ts": "2026-07-30T10:00:00",
                "vl_status": "skipped_pace",
                "answer": "[OCR兜底] 用户正在浏览网页",
                "ocr_fallback_used": True,
            }
        ]
        result = _format_screens_for_prompt(records)
        assert "[OCR兜底]" in result
        assert "VL数据缺失" not in result

    def test_no_ocr_fallback_shows_missing(self):
        """ocr_fallback_used=False 且 status != ok → 输出 VL数据缺失

        Ticket 05: vl_status 用新语义 skipped_pace（替代旧 skipped_quota）。
        """
        from server.activity_tracker.loop_actions import _format_screens_for_prompt
        records = [
            {
                "ts": "2026-07-30T10:00:00",
                "vl_status": "skipped_pace",
                "answer": None,
                "ocr_fallback_used": False,
            }
        ]
        result = _format_screens_for_prompt(records)
        assert "VL数据缺失" in result
        assert "skipped_pace" in result

    def test_ok_status_shows_answer(self):
        """status=ok 且有 answer → 正常输出 answer"""
        from server.activity_tracker.loop_actions import _format_screens_for_prompt
        records = [
            {
                "ts": "2026-07-30T10:00:00",
                "vl_status": "ok",
                "answer": "用户正在写代码",
                "ocr_fallback_used": False,
            }
        ]
        result = _format_screens_for_prompt(records)
        assert "用户正在写代码" in result
        assert "VL数据缺失" not in result

    def test_mixed_records(self):
        """混合记录：ok + ocr_fallback + 缺失"""
        from server.activity_tracker.loop_actions import _format_screens_for_prompt
        records = [
            {"ts": "2026-07-30T10:00:00", "vl_status": "ok", "answer": "正常描述", "ocr_fallback_used": False},
            {"ts": "2026-07-30T10:05:00", "vl_status": "skipped_pace", "answer": "[OCR兜底] OCR文字", "ocr_fallback_used": True},
            {"ts": "2026-07-30T10:10:00", "vl_status": "skipped_high_load", "answer": None, "ocr_fallback_used": False},
        ]
        result = _format_screens_for_prompt(records)
        assert "正常描述" in result
        assert "[OCR兜底] OCR文字" in result
        assert "VL数据缺失: skipped_high_load" in result
