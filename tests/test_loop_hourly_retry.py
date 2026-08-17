"""测试 HourlySummarizeAction 退避重试 + VLQuotaManager 评估指标

策略：
- HourlySummarizeAction：用 tmp_path 模拟 raw_dir/hourly_dir，monkeypatch call_llm
  模拟 quota 失败→重试→成功/全失败两种场景，断言重试次数和占位文件。
  关键：传 catchup_target_ts 固定 last_hour，并写匹配该小时的 windows 记录，
  避免 _filter_by_hour 过滤后为空触发"无活动数据"早退路径。
- VLQuotaManager：用 tmp_path 模拟 quota_dir，调用 should_call/record_call/mark_daily_exhausted
  后断言 decision_counts / grant_outcomes / exhausted_kind / decision_log 等评估指标。
  关键：旧文件向后兼容测试用当前日期写文件（_current_quota_date_str 用 datetime.now()）。

避免依赖真实 LLM、真实截图、真实屏幕抓取。
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from server.activity_tracker.loop_actions import HourlySummarizeAction
from server.activity_tracker.vl_quota import VLQuotaManager


def _run(coro):
    return asyncio.run(coro)


def _today_str() -> str:
    """当前日期 YYYYMMDD（与 VLQuotaManager._current_quota_date_str 对齐，reset_hour=0）"""
    return datetime.now().strftime("%Y%m%d")


# ==================== HourlySummarizeAction 退避重试 ====================

class TestHourlySummarizeRetry:
    """测试 quota/rate_limit 类失败的退避重试逻辑

    覆盖两种场景：
    1. 首次失败 + 第二次成功 → 重试成功
    2. 全部失败 → 写占位文件 + 返回 success=False
    3. 真失败（empty content）→ 直接写占位文件，不重试
    """

    def _make_context(self, tmp_path: Path, max_retries: int = 3, backoff: int = 0):
        """构造 HourlySummarizeAction.execute 所需的 context（backoff=0 加速测试）

        关键：传 catchup_target_ts=14:00 → last_hour=13:00（总结 13:00-14:00），
        并写 hour=13 的 windows 记录，避免 _filter_by_hour 过滤后为空。

        hourly_content_filter_enabled=False：禁用 content_filter 预检（预检会调 call_llm
        询问窗口标题是否含违禁内容），让测试聚焦退避重试逻辑本身。
        """
        raw_dir = tmp_path / "raw"
        hourly_dir = tmp_path / "hourly"
        raw_dir.mkdir(parents=True, exist_ok=True)
        hourly_dir.mkdir(parents=True, exist_ok=True)
        # catchup_target_ts=14:00 → last_hour=13:00，写 13:30 的记录
        today = _today_str()
        target_14 = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
        windows_file = raw_dir / f"windows_{today}.jsonl"
        windows_file.write_text(
            json.dumps({
                "ts": f"{datetime.now().date()}T13:30:00",
                "windows": [{"title": "Test"}],
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return {
            "task_id": "activity_tracker.hourly_summarize",
            "config": {
                "raw_data_dir": str(raw_dir),
                "hourly_data_dir": str(hourly_dir),
                "hourly_summarize_max_retries": max_retries,
                "hourly_summarize_retry_backoff": backoff,
                "hourly_content_filter_enabled": False,  # 禁用预检，避免额外 call_llm
            },
            "source_segment": "activity_tracker",
            "catchup_target_ts": target_14.timestamp(),  # 固定 last_hour=13:00
        }

    def _patch_call_llm(self, monkeypatch, results):
        """让 call_llm 按顺序返回 results 中的结果

        results: list of dict，如 [{"ok": False, "error": "429 Too Many Requests"}, {"ok": True, "content": "..."}]
        """
        call_count = [0]

        def _fake_call_llm(messages, **kwargs):
            idx = min(call_count[0], len(results) - 1)
            call_count[0] += 1
            return results[idx]

        monkeypatch.setattr("server.llm_pool.call_llm", _fake_call_llm)
        return call_count

    def test_retry_then_success(self, tmp_path, monkeypatch):
        """首次 quota 失败，第二次重试成功"""
        ctx = self._make_context(tmp_path, max_retries=3, backoff=0)
        # 成功 content 长度 >= 50 字符，避免触发 detect_filtered_response 的"content 过短"检查
        success_content = "# 13:00 活动总结\n用户在 13:30 切换到 Test 窗口，进行测试相关工作。" * 2
        call_count = self._patch_call_llm(monkeypatch, [
            {"ok": False, "error": "429 Too Many Requests"},  # 首次 quota 失败
            {"ok": True, "content": success_content},  # 第二次成功
        ])
        action = HourlySummarizeAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert call_count[0] == 2  # 调用 2 次（预检已禁用）

    def test_retry_exhausted_writes_placeholder(self, tmp_path, monkeypatch):
        """全部重试失败 → 写占位文件 + success=False"""
        ctx = self._make_context(tmp_path, max_retries=2, backoff=0)
        self._patch_call_llm(monkeypatch, [
            {"ok": False, "error": "429 rate_limited"},
            {"ok": False, "error": "429 rate_limited"},
            {"ok": False, "error": "429 rate_limited"},
        ])
        action = HourlySummarizeAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert "placeholder_file" in result
        assert result["retry_count"] == 3  # 1 次正常 + 2 次重试
        # 占位文件确实写了
        hourly_dir = Path(ctx["config"]["hourly_data_dir"])
        files = list(hourly_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "本时段 LLM 调用失败" in content
        assert "重试 3 次" in content

    def test_real_failure_no_retry(self, tmp_path, monkeypatch):
        """真失败（timeout/HTTP 500）不重试，直接写占位文件

        注意：D6 改动后，empty content 会触发 fallback 重试（见 test_empty_content_fallback）
        此测试改用 timeout 错误验证非 empty content 的真失败不重试
        """
        ctx = self._make_context(tmp_path, max_retries=3, backoff=0)
        call_count = self._patch_call_llm(monkeypatch, [
            {"ok": False, "error": "timeout after 120s"},  # timeout 真失败
        ])
        action = HourlySummarizeAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert call_count[0] == 1  # 只调用 1 次，不重试
        assert "placeholder_file" in result

    def test_empty_content_fallback_success(self, tmp_path, monkeypatch):
        """D6: empty content 时 fallback 到 tier 4 重试成功

        第一次返回 empty content → fallback 到 tier 4 → 成功
        """
        ctx = self._make_context(tmp_path, max_retries=3, backoff=0)
        success_content = "# 13:00 活动总结\n用户在 13:30 切换到 Test 窗口，进行测试相关工作。" * 2
        call_count = self._patch_call_llm(monkeypatch, [
            {"ok": False, "error": "empty content from model"},  # 首次 empty content
            {"ok": True, "content": success_content},  # fallback 成功
        ])
        action = HourlySummarizeAction()
        result = _run(action.execute(ctx))
        assert result["success"] is True
        assert call_count[0] == 2  # 1 次正常 + 1 次 fallback

    def test_empty_content_fallback_also_fails(self, tmp_path, monkeypatch):
        """D6: empty content + fallback 也失败 → 写占位文件

        第一次返回 empty content → fallback 到 tier 4 → 也失败
        """
        ctx = self._make_context(tmp_path, max_retries=3, backoff=0)
        call_count = self._patch_call_llm(monkeypatch, [
            {"ok": False, "error": "empty content from model"},  # 首次 empty content
            {"ok": False, "error": "empty content from fallback model"},  # fallback 也失败
        ])
        action = HourlySummarizeAction()
        result = _run(action.execute(ctx))
        assert result["success"] is False
        assert call_count[0] == 2  # 1 次正常 + 1 次 fallback
        assert "placeholder_file" in result


# ==================== VLQuotaManager 评估指标 ====================

class TestVLQuotaMetrics:
    """测试 VLQuotaManager 评估指标：decision_counts / grant_outcomes / exhausted_kind / decision_log"""

    @pytest.fixture
    def quota_mgr(self, tmp_path):
        """每个测试一个独立的 VLQuotaManager 实例（避免单例污染）"""
        mgr = VLQuotaManager()
        mgr.configure(
            daily_quota=10,
            reset_hour=0,
            min_interval_seconds=0,  # 测试不节流
            quota_dir=str(tmp_path),
        )
        return mgr

    def test_decision_counts_recorded(self, quota_mgr):
        """should_call 后 decision_counts 应记录各类决策"""
        # 焦点变化 → allow_focus
        allow1, _, _ = quota_mgr.should_call({"state": "active", "focus_changed": True})
        assert allow1 is True
        # 节奏放行 → allow_pace
        allow2, _, _ = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow2 is True
        # 配额用完后 → deny_quota_used
        # 把 used_today 撑满（quota=10）
        for _ in range(8):
            quota_mgr.record_call("ok")
        # 现在 used_today=8，仍可放行（remaining>0）
        # 再撑到 10
        for _ in range(2):
            quota_mgr.record_call("ok")
        # 现在 used_today=10，下次应 deny
        allow3, reason3, _ = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow3 is False
        assert "配额已用完" in reason3

        s = quota_mgr.status()
        metrics = s["metrics"]
        assert metrics["decision_counts"]["allow_focus"] == 1
        assert metrics["decision_counts"]["allow_pace"] == 1
        assert metrics["decision_counts"]["deny_quota_used"] == 1
        assert metrics["total_decisions"] == 3
        assert metrics["allow_count"] == 2
        assert metrics["deny_count"] == 1

    def test_grant_outcomes_recorded(self, quota_mgr):
        """record_call 后 grant_outcomes 应记录 vl_status 分布

        Ticket 05: 旧 skipped_quota 硬切为新语义 skipped_idle/skipped_pace/
        skipped_min_interval/skipped_exhausted。本测试用 skipped_exhausted
        （最贴近旧 skipped_quota 的"配额耗尽"语义）验证 grant_outcomes 统计。
        """
        quota_mgr.record_call("ok")
        quota_mgr.record_call("ok")
        quota_mgr.record_call("failed_after_retries")
        quota_mgr.record_call("skipped_exhausted")
        s = quota_mgr.status()
        outcomes = s["metrics"]["grant_outcomes"]
        assert outcomes["ok"] == 2
        assert outcomes["failed_after_retries"] == 1
        assert outcomes["skipped_exhausted"] == 1
        # 旧 skipped_quota 不应再出现（硬切验证）
        assert "skipped_quota" not in outcomes
        # ok_ratio = 2/4 = 0.5
        assert s["metrics"]["ok_ratio"] == 0.5

    def test_exhausted_kind_distinguish(self, quota_mgr):
        """mark_daily_exhausted 区分 quota_exhausted vs upstream_overload"""
        # 上游过载
        quota_mgr.mark_daily_exhausted("rate_limited by ModelScope",
                                         kind="upstream_overload", source="api.modelscope.cn")
        s = quota_mgr.status()
        assert s["daily_exhausted"] is True
        assert s["exhausted_kind"] == "upstream_overload"
        assert s["metrics"]["exhausted_kind"] == "upstream_overload"
        assert s["metrics"]["quota_429_sources"]["api.modelscope.cn"] == 1
        assert s["metrics"]["wasted_minutes_after_exhausted"] >= 0

    def test_decision_log_fifo(self, quota_mgr):
        """decision_log 应有上限（FIFO）"""
        # 触发超过 max 次决策
        for _ in range(250):
            quota_mgr.should_call({"state": "active", "focus_changed": False})
        s = quota_mgr.status()
        assert s["metrics"]["decision_log_size"] == 200  # 上限 200
        assert s["metrics"]["decision_log_max"] == 200

    def test_persist_and_reload(self, tmp_path):
        """评估指标应持久化到文件并能重加载"""
        mgr1 = VLQuotaManager()
        mgr1.configure(daily_quota=5, quota_dir=str(tmp_path))
        mgr1.should_call({"state": "active", "focus_changed": True})
        mgr1.record_call("ok")
        mgr1.mark_daily_exhausted("test", kind="quota_exhausted", source="OpenAI")

        # 新实例从同一目录加载
        mgr2 = VLQuotaManager()
        mgr2.configure(daily_quota=5, quota_dir=str(tmp_path))
        s = mgr2.status()
        assert s["metrics"]["decision_counts"]["allow_focus"] == 1
        assert s["metrics"]["grant_outcomes"]["ok"] == 1
        assert s["metrics"]["exhausted_kind"] == "quota_exhausted"
        assert s["metrics"]["quota_429_sources"]["OpenAI"] == 1

    def test_old_file_backward_compatible(self, tmp_path):
        """旧 quota 文件（无 metrics 字段）应能正常加载，metrics 取空默认值"""
        # 写一个旧格式文件，文件名用当前日期（与 _current_quota_date_str 对齐）
        today = _today_str()
        old_data = {
            "date": today,
            "used_today": 5,
            "by_hour": {"10": 5},
            "skipped_today": 1,
            "daily_exhausted": False,
            "daily_exhausted_at": None,
            "daily_exhausted_reason": None,
            "last_call_ts": None,
            "last_call_epoch": 0.0,
            # 无 decision_counts / grant_outcomes / decision_log / exhausted_kind
        }
        (tmp_path / f"vl_quota_{today}.json").write_text(
            json.dumps(old_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=10, quota_dir=str(tmp_path))
        s = mgr.status()
        assert s["used_today"] == 5
        assert s["metrics"]["decision_counts"] == {}
        assert s["metrics"]["grant_outcomes"] == {}
        assert s["metrics"]["exhausted_kind"] is None
        assert s["metrics"]["quota_429_sources"] == {}
        assert s["metrics"]["total_decisions"] == 0

