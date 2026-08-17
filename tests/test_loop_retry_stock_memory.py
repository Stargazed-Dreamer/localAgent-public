"""测试 StockAdvisorAction 和 MemoryMaintainAction 的退避重试逻辑

策略：
- StockAdvisorAction：monkeypatch StockAnalyzer.run_phase 模拟 quota 失败→重试→成功/全失败/真失败不重试
- MemoryMaintainAction：monkeypatch get_memory_manager 模拟 maintainer.run() 抛异常→重试→成功/全失败/真失败不重试

关键：backoff=0 加速测试（asyncio.sleep(0) 不阻塞）。
避免依赖真实 LLM、真实 tushare、真实 memory DB。
"""

import asyncio
import importlib
from unittest.mock import MagicMock

stock_loop = importlib.import_module("workspace.stock_advisor.loop_actions")
memory_loop = importlib.import_module("server.activity_tracker.loop_actions")


def _run(coro):
    return asyncio.run(coro)


# ==================== StockAdvisorAction 退避重试 ====================

class TestStockAdvisorRetry:
    """测试 quota/rate_limit 类失败的退避重试逻辑

    覆盖三种场景：
    1. 首次 quota 失败 + 第二次成功 → 重试成功
    2. 全部 quota 失败 → 重试耗尽，success=False
    3. 真失败（数据源错误）→ 不重试，直接 success=False
    """

    def _make_context(self, max_retries: int = 3, backoff: int = 0, phase: str = "evening"):
        return {
            "task_id": f"stock_advisor.{phase}",
            "config": {
                "tushare_token": "fake_token",
                "watchlist_path": "",
                "max_retries": max_retries,
                "retry_backoff": backoff,
            },
            "source_segment": "stock_advisor",
        }

    def _patch_analyzer(self, monkeypatch, results):
        """让 StockAnalyzer.run_phase 按顺序返回 results

        results: list of dict，如 [{"ok": False, "error": "..."}, {"ok": True, ...}]
        """
        call_count = [0]

        class FakeAnalyzer:
            def __init__(self, *args, **kwargs):
                pass

            def run_phase(self, phase):
                idx = min(call_count[0], len(results) - 1)
                call_count[0] += 1
                return results[idx]

        monkeypatch.setattr(
            "workspace.stock_advisor.analyzer.StockAnalyzer", FakeAnalyzer
        )
        return call_count

    def _patch_inbox(self, monkeypatch):
        """拦截 server.inbox.get_store 避免污染真实 DB，返回 mock store 以便断言"""
        store = MagicMock()
        store.create = MagicMock(return_value={"id": "test_inbox_id"})
        monkeypatch.setattr("server.inbox.get_store", lambda: store)
        return store

    def test_retry_then_success(self, monkeypatch):
        """首次 quota 失败，第二次重试成功"""
        ctx = self._make_context(max_retries=3, backoff=0)
        call_count = self._patch_analyzer(monkeypatch, [
            {"ok": False, "error": "LLM 调用失败: LLM 返回 None（可能无可用 key）"},
            {"ok": True, "report_path": "/tmp/report.md", "wisdom_triggered": [], "data_summary": {}},
        ])
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert result["phase"] == "evening"
        assert call_count[0] == 2

    def test_retry_exhausted_returns_skipped(self, monkeypatch):
        """quota 类失败重试耗尽 → success=False + skipped=True（不计 fail_count）"""
        ctx = self._make_context(max_retries=2, backoff=0)
        call_count = self._patch_analyzer(monkeypatch, [
            {"ok": False, "error": "429 rate_limited"},
            {"ok": False, "error": "429 rate_limited"},
            {"ok": False, "error": "429 rate_limited"},
        ])
        inbox_store = self._patch_inbox(monkeypatch)
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert result["skipped"] is True
        assert result["skip_reason"] == "LLM 配额不足或池未初始化"
        assert call_count[0] == 3  # 1 次正常 + 2 次重试
        assert len(result["retry_errors"]) == 3
        # 应推送 inbox 告警（source/category/title 正确）
        inbox_store.create.assert_called_once()
        payload = inbox_store.create.call_args.args[0]
        assert payload["source"] == "stock_advisor.evening"
        assert payload["category"] == "quota_warning"
        assert "evening" in payload["title"]

    def test_real_failure_no_retry_no_skip(self, monkeypatch):
        """真失败（数据源错误）不重试，不进入 skipped 分支（保持 fail_count 累积）"""
        ctx = self._make_context(max_retries=3, backoff=0)
        call_count = self._patch_analyzer(monkeypatch, [
            {"ok": False, "error": "tushare API 超时: connection timeout"},
        ])
        inbox_store = self._patch_inbox(monkeypatch)
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert result.get("skipped") is not True  # 真失败不进入 skipped 分支
        assert call_count[0] == 1  # 只调用 1 次
        assert len(result["retry_errors"]) == 1
        # 真失败不应推 inbox 告警
        inbox_store.create.assert_not_called()

    def test_quota_exhausted_inbox_payload_content(self, monkeypatch):
        """quota 耗尽时 inbox 告警 payload 内容完整（retry_errors/sync_result 等）"""
        ctx = self._make_context(max_retries=1, backoff=0, phase="opening")
        self._patch_analyzer(monkeypatch, [
            {"ok": False, "error": "LLM 返回 None"},
            {"ok": False, "error": "LLM 返回 None"},
        ])
        inbox_store = self._patch_inbox(monkeypatch)
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["skipped"] is True
        inbox_store.create.assert_called_once()
        payload = inbox_store.create.call_args.args[0]
        assert payload["source"] == "stock_advisor.opening"
        assert payload["category"] == "quota_warning"
        assert "重试 1 次后仍失败" in payload["description"]
        assert payload["payload"]["phase"] == "opening"
        assert len(payload["payload"]["retry_errors"]) == 2

    def test_pool_not_initialized_retries(self, monkeypatch):
        """LLM Pool 未初始化 → 属于 quota 类，应重试"""
        ctx = self._make_context(max_retries=2, backoff=0)
        call_count = self._patch_analyzer(monkeypatch, [
            {"ok": False, "error": "RuntimeError: LLM Pool 未初始化"},
            {"ok": False, "error": "RuntimeError: LLM Pool 未初始化"},
            {"ok": True, "report_path": "/tmp/report.md", "wisdom_triggered": [], "data_summary": {}},
        ])
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert call_count[0] == 3

    def test_exception_treated_as_failure(self, monkeypatch):
        """run_phase 抛异常 → 应被捕获并按 error 分类"""
        ctx = self._make_context(max_retries=2, backoff=0)
        call_count = [0]

        class FakeAnalyzer:
            def __init__(self, *args, **kwargs):
                pass

            def run_phase(self, phase):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("LLM Pool 未初始化，请先调用 init_pool()")
                return {"ok": True, "report_path": "/tmp/r.md", "wisdom_triggered": [], "data_summary": {}}

        monkeypatch.setattr(
            "workspace.stock_advisor.analyzer.StockAnalyzer", FakeAnalyzer
        )
        action = stock_loop.StockAdvisorAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert call_count[0] == 2  # 异常后重试成功


# ==================== MemoryMaintainAction 退避重试 ====================

class TestMemoryMaintainRetry:
    """测试 maintainer.run() 抛异常时的退避重试

    覆盖场景：
    1. 首次抛 Pool 未初始化异常 + 第二次成功 → 重试成功
    2. 全部抛 Pool 未初始化 → 重试耗尽，success=False
    3. 非 quota 异常（DB 错误）→ 不重试
    4. 正常返回（含 broke_on_429=True）→ 不重试，success=True
    """

    def _make_context(self, max_retries: int = 2, backoff: int = 0):
        return {
            "task_id": "memory.maintain",
            "config": {
                "max_retries": max_retries,
                "retry_backoff": backoff,
            },
            "source_segment": "memory",
        }

    def _patch_manager(self, monkeypatch, run_behaviors):
        """让 maintainer.run() 按 run_behaviors 顺序执行

        run_behaviors: list，元素为：
            - dict → maintainer.run() 返回该 dict（正常返回）
            - Exception 实例 → maintainer.run() 抛该异常
        """
        call_count = [0]
        maintainer = MagicMock()

        async def _fake_run():
            idx = min(call_count[0], len(run_behaviors) - 1)
            call_count[0] += 1
            behavior = run_behaviors[idx]
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

        maintainer.run = _fake_run

        mgr = MagicMock()
        mgr._initialized = True
        mgr.maintainer = maintainer

        monkeypatch.setattr(
            "server.memory.manager.get_memory_manager", lambda: mgr
        )
        # 也 patch MemoryMaintainAction.execute 内部的 import
        monkeypatch.setattr(
            "server.activity_tracker.loop_actions.get_memory_manager",
            lambda: mgr,
            raising=False,
        )
        return call_count

    def test_retry_then_success(self, monkeypatch):
        """首次 Pool 未初始化异常，第二次成功"""
        ctx = self._make_context(max_retries=2, backoff=0)
        call_count = self._patch_manager(monkeypatch, [
            RuntimeError("LLM Pool 未初始化，请先调用 init_pool()"),
            {"aging": {"stale_count": 0}, "validate": {"validated": 5, "keep": 3, "update": 1, "archive": 1}, "last_run": "2026-07-24T01:00:00"},
        ])
        action = memory_loop.MemoryMaintainAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert result["validated"] == 5
        assert call_count[0] == 2

    def test_retry_exhausted_returns_failure(self, monkeypatch):
        """全部重试抛 Pool 未初始化 → success=False"""
        ctx = self._make_context(max_retries=2, backoff=0)
        call_count = self._patch_manager(monkeypatch, [
            RuntimeError("LLM Pool 未初始化"),
            RuntimeError("LLM Pool 未初始化"),
            RuntimeError("LLM Pool 未初始化"),
        ])
        action = memory_loop.MemoryMaintainAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert call_count[0] == 3  # 1 + 2 retries
        assert len(result["retry_errors"]) == 3

    def test_db_error_no_retry(self, monkeypatch):
        """非 quota 异常（DB 错误）→ 不重试"""
        ctx = self._make_context(max_retries=3, backoff=0)
        call_count = self._patch_manager(monkeypatch, [
            Exception("sqlite3.DatabaseError: database is locked"),
        ])
        action = memory_loop.MemoryMaintainAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert call_count[0] == 1  # 不重试

    def test_broke_on_429_no_retry(self, monkeypatch):
        """正常返回（含 broke_on_429=True）→ 不重试，success=True"""
        ctx = self._make_context(max_retries=3, backoff=0)
        call_count = self._patch_manager(monkeypatch, [
            {
                "aging": {"stale_count": 136},
                "validate": {
                    "validated": 36, "keep": 34, "update": 1, "archive": 1,
                    "skipped_429": 9, "broke_on_429": True,
                },
                "last_run": "2026-07-24T00:00:00",
            },
        ])
        action = memory_loop.MemoryMaintainAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert result["broke_on_429"] is True
        assert result["skipped_429"] == 9
        assert call_count[0] == 1  # 不重试
