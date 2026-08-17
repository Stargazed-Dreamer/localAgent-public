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
    """T01：runner.py 中 agent_guide 调用 URL 应为 /guide（不是 /agent_guide）。"""

    def test_runner_uses_guide_endpoint(self):
        """runner.py 实际 HTTP 调用 URL 应为 /guide（不是 /agent_guide）。

        检查实际调用模式（f-string / 字符串拼接 + URL）而非注释文本。
        T01 修复前的 bug 是 `f"{base_url}/agent_guide"`，修复后是 `f"{base_url}/guide"`。
        """
        runner_path = (
            Path(__file__).resolve().parent.parent
            / "client" / "core" / "agent" / "runner.py"
        )
        src = runner_path.read_text(encoding="utf-8")
        # 实际调用 URL 必须是 /guide（f-string 拼接 base_url）
        assert 'f"{base_url}/guide"' in src or '"/guide"' in src, (
            "runner.py 应使用 /guide 作为 agent_guide 调用 URL"
        )
        # 不应再出现 bug 形态的 URL 拼接（注释里的 "不是 /agent_guide" 文档说明允许）
        assert 'f"{base_url}/agent_guide"' not in src, (
            "runner.py 不应再用 f\"{base_url}/agent_guide\" 拼接 URL（T01 修复的 bug 路径）"
        )


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
        project_root = Path(__file__).resolve().parent.parent
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

    def test_next_action_hint_can_be_set(self):
        """大结果落盘后可填 next_action_hint。"""
        r = ToolResult(
            tool_call_id="big-1",
            content="[preview] first 200 bytes...",
            artifact_path="data/client/artifacts/sess1/big-1.txt",
            next_action_hint=(
                "完整结果已落盘 data/client/artifacts/sess1/big-1.txt，"
                "preview 见 content，需要全文时用 file_read 读取"
            ),
        )
        assert r.variant == ToolResultVariant.SUCCESS
        assert r.artifact_path is not None
        assert r.next_action_hint is not None
        assert "file_read" in r.next_action_hint


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
