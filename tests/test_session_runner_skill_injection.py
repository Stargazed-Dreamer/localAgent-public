"""chat-panel-v2 Ticket 03 验收测试：SessionRunner skill 注入

覆盖 T03 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] SessionRunner.start 接收 template_skills 参数，把 skills[] 转成 system prompt 段
- [x] skill 段位置 = stable prefix 尾部（每轮都注入，因为是会话级稳定配置）
- [x] skills[] 为空时 stable prefix 不变（向后兼容）
- [x] 缓存复用：第二次调用 _get_stable_prefix 不重新计算

测试 prior art: tests/test_v6_lite_t01.py（SessionRunner 主循环测试模式）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    Message,
    MockLLM,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.template_store import (  # noqa: E402
    _reset_skill_path_map_cache,
    format_skills_prompt,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def reset_skill_cache():
    """每个测试前后重置 skill 路径映射缓存。"""
    _reset_skill_path_map_cache()
    yield
    _reset_skill_path_map_cache()


def _build_runner(
    store: EventStore,
    session_id: str = "test-sess",
    template_skills: tuple[str, ...] = (),
) -> SessionRunner:
    """构造 SessionRunner（用 MockLLM 单轮文本响应）。"""
    config = RunnerConfig(
        session_id=session_id,
        max_iterations=3,
        use_stream=False,  # 用非流式（MockLLM 简单响应）
        template_skills=template_skills,
    )
    deps = RunnerDeps(
        llm_gateway=MockLLM(default_text="你好"),
        event_store=store,
    )
    return SessionRunner(deps=deps, config=config)


# ============================================================================
# 1. RunnerConfig.template_skills 字段
# ============================================================================


class TestRunnerConfigTemplateSkills:
    def test_default_template_skills_is_empty_tuple(self):
        """RunnerConfig 默认 template_skills 是空 tuple。"""
        config = RunnerConfig(session_id="x")
        assert config.template_skills == ()
        assert len(config.template_skills) == 0

    def test_template_skills_accepts_tuple(self):
        """RunnerConfig.template_skills 接受 tuple（frozen dataclass 一致性）。"""
        config = RunnerConfig(
            session_id="x",
            template_skills=("recurring.accounting", "adhoc.deep_research"),
        )
        assert config.template_skills == ("recurring.accounting", "adhoc.deep_research")

    def test_template_skills_frozen(self):
        """RunnerConfig frozen → template_skills 字段不可变。"""
        config = RunnerConfig(session_id="x")
        with pytest.raises((AttributeError, Exception)):
            config.template_skills = ("x",)  # type: ignore[misc]


# ============================================================================
# 2. SessionRunner._get_stable_prefix 注入 skill 段
# ============================================================================


class TestStablePrefixSkillInjection:
    def test_empty_template_skills_no_skill_section(self, store):
        """template_skills 空 → stable prefix 不含 # Active Skills 段。"""
        runner = _build_runner(store, template_skills=())
        prefix = runner._get_stable_prefix()
        assert "# Active Skills" not in prefix
        assert "本会话已激活以下 skill" not in prefix

    def test_nonempty_template_skills_appends_skill_section(self, store):
        """template_skills 非空 → stable prefix 尾部追加 # Active Skills 段。"""
        runner = _build_runner(
            store,
            template_skills=("recurring.accounting",),
        )
        prefix = runner._get_stable_prefix()
        assert "# Active Skills" in prefix
        assert "本会话已激活以下 skill" in prefix
        assert "accounting (workspace/accounting/SKILL.md)" in prefix
        assert "请先读取相关 SKILL.md 了解流程" in prefix

    def test_skill_section_at_tail_of_stable_prefix(self, store):
        """skill 段位于 stable prefix 尾部（在 # Output Contract 段之后）。"""
        runner = _build_runner(
            store,
            template_skills=("recurring.accounting",),
        )
        prefix = runner._get_stable_prefix()
        # # Output Contract 段在 stable prefix 五段式最后
        # skill 段应在其后
        oc_pos = prefix.rfind("# Output Contract")
        skill_pos = prefix.find("# Active Skills")
        assert oc_pos > 0
        assert skill_pos > oc_pos

    def test_multiple_skills_all_listed(self, store):
        """多 skill 都列出（按序号）。"""
        runner = _build_runner(
            store,
            template_skills=(
                "recurring.accounting",
                "adhoc.auto_shutdown",
            ),
        )
        prefix = runner._get_stable_prefix()
        assert "1. accounting" in prefix
        assert "2. auto_shutdown" in prefix

    def test_unresolvable_skill_skipped_in_prefix(self, store):
        """无法解析路径的 task_type 被跳过（不出现在 stable prefix 中）。"""
        runner = _build_runner(
            store,
            template_skills=(
                "recurring.accounting",
                "fake_scope.nonexistent_xyz",
            ),
        )
        prefix = runner._get_stable_prefix()
        # 第一个仍注入
        assert "accounting" in prefix
        # 第二个不注入（找不到路径）
        assert "nonexistent_xyz" not in prefix

    def test_all_unresolvable_no_skill_section(self, store):
        """全部 skill 无法解析 → 不追加 # Active Skills 段。"""
        runner = _build_runner(
            store,
            template_skills=("fake_scope.nonexistent_1",),
        )
        prefix = runner._get_stable_prefix()
        assert "# Active Skills" not in prefix
        assert "本会话已激活以下 skill" not in prefix


# ============================================================================
# 3. 缓存复用：第二次调用不重新计算
# ============================================================================


class TestStablePrefixCache:
    def test_stable_prefix_cached_within_session(self, store):
        """同一 SessionRunner 实例第二次调用 _get_stable_prefix 返回同一字符串。"""
        runner = _build_runner(
            store,
            template_skills=("recurring.accounting",),
        )
        prefix1 = runner._get_stable_prefix()
        prefix2 = runner._get_stable_prefix()
        # 完全相同的字符串（缓存复用）
        assert prefix1 == prefix2

    def test_stable_prefix_cache_initialized_none(self, store):
        """SessionRunner 初始化时 _stable_prefix_cache 为 None（未构建过）。"""
        runner = _build_runner(store)
        assert runner._stable_prefix_cache is None

    def test_stable_prefix_cache_set_after_first_call(self, store):
        """首次调用 _get_stable_prefix 后 _stable_prefix_cache 被 set。"""
        runner = _build_runner(
            store,
            template_skills=("recurring.accounting",),
        )
        runner._get_stable_prefix()
        assert runner._stable_prefix_cache is not None
        assert "# Active Skills" in runner._stable_prefix_cache


# ============================================================================
# 4. 端到端：完整 run() 注入 skill 段到 system prompt
# ============================================================================


class TestEndToEndSkillInjection:
    def test_run_injects_skill_section_into_system_prompt(self, store):
        """run() 完整流程：首轮 system prompt 含 # Active Skills 段。"""
        # 准备会话 + user 消息
        async def _setup():
            await store.create_session(session_id="skill-test")
            await store.append_message(
                "skill-test",
                Message(role="user", content="帮我记账", source="user"),
            )

        asyncio.run(_setup())

        runner = _build_runner(
            store,
            session_id="skill-test",
            template_skills=("recurring.accounting",),
        )
        outcome = asyncio.run(runner.run())

        # 正常完成
        assert outcome.status == "completed"

        # 验证 system prompt 注入了 skill 段
        # 通过 _stable_prefix_cache 检查（runner 已 run 过）
        cached = runner._stable_prefix_cache
        assert cached is not None
        assert "# Active Skills" in cached
        assert "accounting (workspace/accounting/SKILL.md)" in cached

    def test_run_without_template_skills_no_skill_section(self, store):
        """无 template_skills 时 system prompt 不含 # Active Skills 段。"""
        async def _setup():
            await store.create_session(session_id="plain-test")
            await store.append_message(
                "plain-test",
                Message(role="user", content="你好", source="user"),
            )

        asyncio.run(_setup())

        runner = _build_runner(store, session_id="plain-test")
        outcome = asyncio.run(runner.run())

        assert outcome.status == "completed"
        cached = runner._stable_prefix_cache
        assert cached is not None
        assert "# Active Skills" not in cached


# ============================================================================
# 5. format_skills_prompt 与 SessionRunner 集成
# ============================================================================


class TestFormatSkillsPromptIntegration:
    def test_format_skills_prompt_output_matches_runner_injection(self, store):
        """format_skills_prompt 输出 = SessionRunner._get_stable_prefix 尾部追加的 skill 段。"""
        skills = ["recurring.accounting", "adhoc.auto_shutdown"]
        runner = _build_runner(
            store,
            template_skills=tuple(skills),
        )
        prefix = runner._get_stable_prefix()
        expected_skill_section = format_skills_prompt(skills)
        # stable prefix 尾部应包含 format_skills_prompt 的完整输出
        assert prefix.endswith(expected_skill_section)

    def test_skill_section_separated_by_newlines_from_stable_prefix(self, store):
        """skill 段与 stable prefix 之间用 \n\n 分隔。"""
        runner = _build_runner(
            store,
            template_skills=("recurring.accounting",),
        )
        prefix = runner._get_stable_prefix()
        skill_section = format_skills_prompt(["recurring.accounting"])
        # skill 段在 stable prefix 之后，用 \n\n 分隔
        assert prefix.endswith("\n\n" + skill_section)
