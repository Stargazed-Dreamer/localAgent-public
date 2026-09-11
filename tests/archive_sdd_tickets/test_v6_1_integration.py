"""v6.1 记忆/提示词/工具改造 — 端到端集成测试（T33）

覆盖 spec §7 4 个核心场景：
  1. agent_guide URL 修复（runner 内 URL 是 /guide 不是 /agent_guide）
  2. ToolResult 5 变体 + 内置工具执行（file_read 经 BuiltinToolExecutor 调用成功）
  3. 大结果 next_action_hint 字段存在（L0 落盘配套字段）
  4. memory staleness 注入（project 类型超阈值 → staleness_warning 非空）

设计原则（按 docs/dev-workflow.md "测试修复铁律"）：
- 不用 @pytest.mark.skip 绕过、不 try/except 吞 crash、不放宽断言
- 直接调被测代码（BuiltinToolExecutor / _enrich_with_staleness / ToolResult 构造）
- mock 只用于隔离外部依赖（文件系统 / HTTP），业务逻辑走真实路径

异步测试策略（与 test_v6_lite_t01.py 一致）：用 asyncio.run 不用 pytest-asyncio。
"""
import asyncio
from pathlib import Path

from client.core.agent.builtin_tool_executor import BuiltinToolExecutor
from client.core.agent.types import (
    ToolCall,
    ToolResult,
    ToolResultVariant,
)

# ============================================================================
# 场景 1：agent_guide URL 修复验证
# ============================================================================

class TestAgentGuideURLFixed:
    """T01：runner 的 agent_guide 调用应请求 /guide 端点（不是 /agent_guide）。

    行为化改写（原为源码字符串 grep）：mock requests.get 捕获实际请求 URL，
    验证 `_call_agent_guide_http` 打到 /guide 路径（T01 修复前是 /agent_guide，
    会导致 404 → 静默降级）。
    """

    def test_runner_uses_guide_endpoint(self, monkeypatch):
        """_call_agent_guide_http 实际请求 {base}/guide 且携带 task 参数。"""
        import client.core.agent.runner as runner_mod

        captured = {}

        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"task_type": "dev.test", "first_action": "do it"}

        def _fake_get(url, params=None, timeout=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResp()

        monkeypatch.setattr(runner_mod.requests, "get", _fake_get)

        result = runner_mod._call_agent_guide_http(task="写个测试", base_url="http://fake.test:8766")

        assert result == {"task_type": "dev.test", "first_action": "do it"}
        # 关键回归：URL 必须是 /guide（不是 /agent_guide）
        assert captured["url"] == "http://fake.test:8766/guide", (
            f"agent_guide 调用必须请求 /guide 端点，实际: {captured['url']}"
        )
        assert captured["params"] == {"task": "写个测试"}

    def test_runner_guide_failure_returns_none(self, monkeypatch):
        """请求失败（如 T01 修复前的 404）→ 返回 None 走降级，不抛异常。"""
        import requests as _requests

        import client.core.agent.runner as runner_mod

        def _fake_get(url, params=None, timeout=None, headers=None):
            raise _requests.ConnectionError("connection refused")

        monkeypatch.setattr(runner_mod.requests, "get", _fake_get)

        assert runner_mod._call_agent_guide_http(task="x", base_url="http://fake.test") is None


# ============================================================================
# 场景 2：ToolResult 5 变体 + 内置工具执行
# ============================================================================

class TestToolResultVariantsAndBuiltin:
    """T03+T20：ToolResult 5 变体 + BuiltinToolExecutor 路由。"""

    def test_five_variants_construct_correctly(self):
        """5 变体均可直接构造，is_error property 向后兼容。"""
        # SUCCESS
        ok = ToolResult(
            tool_call_id="t1",
            content="ok",
            variant=ToolResultVariant.SUCCESS,
        )
        assert ok.variant == ToolResultVariant.SUCCESS
        assert ok.is_error is False

        # ERROR
        err = ToolResult(
            tool_call_id="t2",
            content="boom",
            variant=ToolResultVariant.ERROR,
            error_type="not_found",
        )
        assert err.variant == ToolResultVariant.ERROR
        assert err.is_error is True
        assert err.error_type == "not_found"

        # USER_DENIED
        denied = ToolResult(
            tool_call_id="t3",
            content="user said no",
            variant=ToolResultVariant.USER_DENIED,
        )
        assert denied.variant == ToolResultVariant.USER_DENIED
        assert denied.is_error is True

        # APPROVAL_REQUIRED
        appr = ToolResult(
            tool_call_id="t4",
            content="need approval",
            variant=ToolResultVariant.APPROVAL_REQUIRED,
            approval_id="appr-1",
        )
        assert appr.variant == ToolResultVariant.APPROVAL_REQUIRED
        assert appr.is_error is True
        assert appr.approval_id == "appr-1"

        # TIMEOUT
        tmo = ToolResult(
            tool_call_id="t5",
            content="timed out",
            variant=ToolResultVariant.TIMEOUT,
            timeout_seconds=30.0,
        )
        assert tmo.variant == ToolResultVariant.TIMEOUT
        assert tmo.is_error is True
        assert tmo.timeout_seconds == 30.0

    def test_backward_compat_is_error_true_syncs_to_error_variant(self):
        """旧代码 is_error=True 自动同步到 variant=ERROR（向后兼容）。"""
        legacy = ToolResult(
            tool_call_id="legacy",
            content="old style",
            is_error=True,
        )
        assert legacy.variant == ToolResultVariant.ERROR
        assert legacy.is_error is True

    def test_builtin_file_read_executes_locally(self, tmp_path):
        """BuiltinToolExecutor 调用 file_read 不经 HTTP，返回带行号内容。

        file_read 路径安全 4 重校验限制在项目根目录内（spec D8.1），
        所以测试文件必须写到项目内 temp/ 下，不能用 pytest tmp_path（在用户 temp 目录）。
        """
        # 用项目内 temp/html/ 目录（已被 .gitignore 排除，安全）
        project_root = Path(__file__).resolve().parents[2]
        test_dir = project_root / "temp" / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "_v6_1_sample.txt"
        try:
            test_file.write_text("hello\nworld\n", encoding="utf-8")

            executor = BuiltinToolExecutor(base_url="http://127.0.0.1:8766")
            call = ToolCall(
                id="call-1",
                name="file_read",
                args={"file_path": str(test_file)},
                args_raw='{"file_path": "' + str(test_file).replace("\\", "\\\\") + '"}',
            )
            result = asyncio.run(executor.execute(call))

            assert result.variant == ToolResultVariant.SUCCESS
            assert result.is_error is False
            # file_read 返回带行号内容（{n}→{content}）
            assert "hello" in result.content
            assert "world" in result.content
        finally:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass

    def test_builtin_unknown_tool_returns_error_not_found(self):
        """调用不存在的内置工具 → variant=ERROR, error_type=not_found（非抛异常）。"""
        executor = BuiltinToolExecutor(base_url="http://127.0.0.1:8766")
        call = ToolCall(
            id="call-x",
            name="nonexistent_tool_xyz",
            args={},
            args_raw="{}",
        )
        result = asyncio.run(executor.execute(call))
        assert result.variant == ToolResultVariant.ERROR
        assert result.error_type == "not_found"
        assert result.is_error is True


# ============================================================================
# 场景 3：大结果 next_action_hint 字段
# ============================================================================

class TestNextActionHintField:
    """T05：ToolResult 含 next_action_hint 字段（L0 落盘后填充）。"""

    def test_next_action_hint_default_none(self):
        """默认 next_action_hint=None（普通结果不填）。"""
        r = ToolResult(tool_call_id="t", content="x")
        assert r.next_action_hint is None

# ============================================================================
# 场景 4：memory staleness 注入
# ============================================================================

class TestMemoryStalenessEnrichment:
    """T31+T32：_enrich_with_staleness 给 memory 返回结果追加字段。"""

    def test_stale_project_fact_gets_warning(self):
        """project 类型超 7 天阈值 → staleness_warning 非空。"""
        # 构造 30 天前更新的 project fact
        from datetime import datetime, timedelta

        from server.memory.router import _enrich_with_staleness
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        fact = {
            "key": "test_stale_project",
            "fact_type": "project",
            "updated_at": old,
        }
        _enrich_with_staleness(fact)
        assert fact["staleness_warning"] is not None
        assert "过时" in fact["staleness_warning"] or "可能过时" in fact["staleness_warning"]
        assert fact["memory_age"] is not None
        # trust_recall_hint 常驻
        assert "trust_recall_hint" in fact

    def test_fresh_user_fact_no_warning(self):
        """user 类型 10 天 → 未超 90 天阈值，staleness_warning=None。"""
        from datetime import datetime, timedelta

        from server.memory.router import _enrich_with_staleness

        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        fact = {
            "key": "test_fresh_user",
            "fact_type": "user",
            "updated_at": recent,
        }
        _enrich_with_staleness(fact)
        assert fact["staleness_warning"] is None
        # memory_age 仍然填（相对时间提示）
        assert fact["memory_age"] is not None

    def test_null_fact_type_no_warning(self):
        """fact_type=NULL → 不算 staleness_warning（君子协议，不强制）。"""
        from datetime import datetime, timedelta

        from server.memory.router import _enrich_with_staleness

        old = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        fact = {
            "key": "test_null_type",
            "fact_type": None,
            "updated_at": old,
        }
        _enrich_with_staleness(fact)
        # NULL fact_type 不算 staleness
        assert fact["staleness_warning"] is None

    def test_list_enrichment_idempotent(self):
        """list 输入逐条追加；已有 staleness_warning 时跳过（幂等）。"""
        from datetime import datetime, timedelta

        from server.memory.router import _enrich_with_staleness

        old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        items = [
            {"key": "a", "fact_type": "project", "updated_at": old},
            {"key": "b", "fact_type": "experience", "updated_at": old},  # 14 天阈值，60 天超
        ]
        _enrich_with_staleness(items)
        assert items[0]["staleness_warning"] is not None
        assert items[1]["staleness_warning"] is not None

        # 二次调用幂等（已有 staleness_warning 字段 → 跳过）
        before_b_warning = items[1]["staleness_warning"]
        _enrich_with_staleness(items)
        assert items[1]["staleness_warning"] == before_b_warning
