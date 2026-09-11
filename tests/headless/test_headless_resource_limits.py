"""headless-agent-session Ticket 11 验收测试：资源限制默认值 + 配置覆盖

覆盖 Ticket 11 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 验证默认值生效：max_iterations=100, wall_clock_budget_secs=1800,
      judge_max_iterations=10, judge_wall_clock_budget_secs=300
- [x] 验证配置覆盖：config.toml 设小值，任务按配置值限制

B2（spec D2/D3）：max_budget_usd / judge_max_budget_usd 字段已删除，
本测试文件移除所有 budget_usd 相关断言（功能已移除，不再测试）。

策略：monkeypatch SessionFacade 捕获 RunnerConfig，验证 default / override 两种场景。
- 默认值场景：HeadlessConfig() 不指定资源限制 → RunnerConfig 字段 = spec 默认值
- 覆盖场景：parse_headless_config({小值}) → RunnerConfig 字段 = 配置值
- judge 同理：run_judge_agent 的 RunnerConfig 用 judge_* 字段
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionFacade,
)
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_JUDGE_MAX_ITERATIONS,
    DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_WALL_CLOCK_BUDGET_SECS,
    HeadlessConfig,
    parse_headless_config,
    run_judge_agent,
    run_main_agent,
)

# ============================================================================
# Fixtures & helpers
# ============================================================================


def _install_mock_llm_infra(monkeypatch, mock_llm: MockLLM):
    """统一 mock LLMPoolGateway / ToolRegistry / HttpClientToolExecutor。

    复用 test_headless_runner.py 的 mock 模式（同签名兼容）。
    """
    class MockLLMPoolGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url

        async def call(self, request):
            return await mock_llm.call(request)

        async def stream(self, request):
            async for ev in mock_llm.stream(request):
                yield ev

    class MockToolRegistry:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url

        def refresh(self, *, timeout: float = 10.0) -> int:
            return 0

    class MockHttpClientToolExecutor(MockToolExecutor):
        def __init__(self, registry=None, base_url=DEFAULT_BASE_URL, **kwargs):
            super().__init__(**kwargs)

    import client.core.agent as agent_pkg
    monkeypatch.setattr(agent_pkg, "LLMPoolGateway", MockLLMPoolGateway)
    monkeypatch.setattr(agent_pkg, "ToolRegistry", MockToolRegistry)
    monkeypatch.setattr(agent_pkg, "HttpClientToolExecutor", MockHttpClientToolExecutor)


def _install_config_capturing_facade(monkeypatch, captured: list[RunnerConfig]):
    """monkeypatch SessionFacade，把 __init__ 收到的 RunnerConfig 追加到 captured。

    真实 SessionFacade 行为照常（继承 start / 其他方法），仅捕获 config。
    """
    original_init = SessionFacade.__init__

    def capturing_init(self, deps: RunnerDeps, config: RunnerConfig):
        captured.append(config)
        return original_init(self, deps, config)

    monkeypatch.setattr(SessionFacade, "__init__", capturing_init)


# ============================================================================
# Part 1: 默认值传播到 RunnerConfig
# ============================================================================


class TestDefaultValuesPropagated:
    """Ticket 11: HeadlessConfig() 默认值应原样传到 RunnerConfig。"""

    def test_main_agent_defaults_propagated_to_runner_config(self, tmp_db_path, monkeypatch):
        """run_main_agent 用 HeadlessConfig() 默认值时，RunnerConfig 字段 = spec 默认值。"""
        mock_llm = MockLLM(default_text="默认值测试主 agent ok")
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        config = HeadlessConfig(db_path=tmp_db_path)
        # 显式断言 HeadlessConfig 默认值（spec 我替领导拍的板）
        assert config.max_iterations == DEFAULT_MAX_ITERATIONS  # 100
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 1800

        async def run():
            return await run_main_agent(
                trigger_text="测试默认值",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1

        runner_cfg = captured_configs[0]
        # 关键验收点：默认值原样传到 RunnerConfig
        assert runner_cfg.max_iterations == DEFAULT_MAX_ITERATIONS, (
            f"默认 max_iterations 应为 {DEFAULT_MAX_ITERATIONS}, 实际 {runner_cfg.max_iterations}"
        )
        # B2: 删 max_budget_usd 传参，RunnerConfig 无此字段
        assert runner_cfg.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS, (
            f"默认 wall_clock_budget_secs 应为 {DEFAULT_WALL_CLOCK_BUDGET_SECS}, "
            f"实际 {runner_cfg.wall_clock_budget_secs}"
        )

    def test_judge_agent_defaults_propagated_to_runner_config(self, tmp_db_path, monkeypatch):
        """run_judge_agent 用 HeadlessConfig() 默认值时，RunnerConfig 字段 = judge spec 默认值。"""
        mock_llm = MockLLM(
            default_text='{"verdict": "success", "reason": "默认值测试 judge ok"}'
        )
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        config = HeadlessConfig(db_path=tmp_db_path)
        # judge 默认值断言
        assert config.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS  # 10
        # B2: 删 judge_max_budget_usd 字段，不再断言
        assert config.judge_wall_clock_budget_secs == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS  # 300

        async def run():
            return await run_judge_agent(
                main_output=["任务完成"],
                trigger_text="测试 judge 默认值",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1

        runner_cfg = captured_configs[0]
        # 关键验收点：judge 默认值原样传到 RunnerConfig
        assert runner_cfg.max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS, (
            f"judge 默认 max_iterations 应为 {DEFAULT_JUDGE_MAX_ITERATIONS}, "
            f"实际 {runner_cfg.max_iterations}"
        )
        # B2: 删 judge_max_budget_usd 传参，RunnerConfig 无此字段
        assert runner_cfg.wall_clock_budget_secs == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS, (
            f"judge 默认 wall_clock_budget_secs 应为 {DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS}, "
            f"实际 {runner_cfg.wall_clock_budget_secs}"
        )

    def test_default_constants_match_spec(self):
        """默认常量与 spec 一致（防止后续误改默认值）。"""
        assert DEFAULT_MAX_ITERATIONS == 100
        # B2: 删 DEFAULT_MAX_BUDGET_USD 常量
        assert DEFAULT_WALL_CLOCK_BUDGET_SECS == 1800
        assert DEFAULT_JUDGE_MAX_ITERATIONS == 10
        # B2: 删 DEFAULT_JUDGE_MAX_BUDGET_USD 常量
        assert DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS == 300


# ============================================================================
# Part 2: 配置覆盖传播到 RunnerConfig
# ============================================================================


class TestConfigOverridePropagated:
    """Ticket 11: parse_headless_config(小值 dict) 应让 RunnerConfig 用配置值。"""

    def test_main_agent_override_smaller_values_propagated(self, tmp_db_path, monkeypatch):
        """config_dict 设小值（如 max_iterations=10）→ RunnerConfig 用配置值。"""
        mock_llm = MockLLM(default_text="覆盖测试主 agent ok")
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        # config_dict 设小值（模拟 config.toml 收紧资源限制）
        cfg_dict = {
            "trigger_text": "测试覆盖",
            "max_iterations": 10,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 300,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.max_iterations == 10
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == 300

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1

        runner_cfg = captured_configs[0]
        # 关键验收点：配置值覆盖默认值，原样传到 RunnerConfig
        assert runner_cfg.max_iterations == 10, (
            f"覆盖 max_iterations 应为 10, 实际 {runner_cfg.max_iterations}"
        )
        # B2: 删 max_budget_usd 传参，RunnerConfig 无此字段
        assert runner_cfg.wall_clock_budget_secs == 300, (
            f"覆盖 wall_clock_budget_secs 应为 300, 实际 {runner_cfg.wall_clock_budget_secs}"
        )

    def test_judge_agent_override_smaller_values_propagated(self, tmp_db_path, monkeypatch):
        """judge config_dict 设小值 → judge RunnerConfig 用配置值。"""
        mock_llm = MockLLM(
            default_text='{"verdict": "success", "reason": "覆盖测试 judge ok"}'
        )
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        cfg_dict = {
            "trigger_text": "测试 judge 覆盖",
            "judge_max_iterations": 3,
            # B2: 删 judge_max_budget_usd 配置项
            "judge_wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.judge_max_iterations == 3
        # B2: 删 judge_max_budget_usd 字段，不再断言
        assert config.judge_wall_clock_budget_secs == 60

        async def run():
            return await run_judge_agent(
                main_output=["任务完成"],
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1

        runner_cfg = captured_configs[0]
        # 关键验收点：judge 配置值覆盖默认值
        assert runner_cfg.max_iterations == 3, (
            f"judge 覆盖 max_iterations 应为 3, 实际 {runner_cfg.max_iterations}"
        )
        # B2: 删 judge_max_budget_usd 传参，RunnerConfig 无此字段
        assert runner_cfg.wall_clock_budget_secs == 60, (
            f"judge 覆盖 wall_clock_budget_secs 应为 60, 实际 {runner_cfg.wall_clock_budget_secs}"
        )

    def test_main_and_judge_use_independent_config_fields(self, tmp_db_path, monkeypatch):
        """主 agent 和 judge agent 资源限制独立配置（不混用）。

        场景：main max_iterations=20, judge_max_iterations=2
        验证：run_main_agent 的 RunnerConfig.max_iterations=20,
              run_judge_agent 的 RunnerConfig.max_iterations=2
        """
        mock_llm = MockLLM(
            default_text='{"verdict": "success", "reason": "独立配置测试 ok"}'
        )
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        cfg_dict = {
            "trigger_text": "测试独立配置",
            "max_iterations": 20,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 600,
            "judge_max_iterations": 2,
            # B2: 删 judge_max_budget_usd 配置项
            "judge_wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)

        async def run():
            main_result = await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )
            # 主 agent 跑完后再跑 judge（模拟 HeadlessSessionAction 流程）
            judge_result = await run_judge_agent(
                main_output=main_result.last_assistant_messages or ["完成"],
                trigger_text=config.trigger_text,
                config=config,
            )
            return main_result, judge_result

        main_result, judge_result = asyncio.run(run())
        assert main_result.error == ""
        assert judge_result.error == ""
        assert len(captured_configs) == 2  # 一次 main + 一次 judge

        main_cfg = captured_configs[0]
        judge_cfg = captured_configs[1]

        # 主 agent 用 main 字段
        assert main_cfg.max_iterations == 20
        # B2: 删 max_budget_usd 传参，RunnerConfig 无此字段
        assert main_cfg.wall_clock_budget_secs == 600

        # judge 用 judge_* 字段（不是 main 字段）
        assert judge_cfg.max_iterations == 2
        # B2: 删 judge_max_budget_usd 传参，RunnerConfig 无此字段
        assert judge_cfg.wall_clock_budget_secs == 30

    def test_partial_override_keeps_other_defaults(self, tmp_db_path, monkeypatch):
        """部分覆盖：只设 max_iterations，其他字段保持默认值。"""
        mock_llm = MockLLM(default_text="部分覆盖测试 ok")
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        cfg_dict = {
            "trigger_text": "测试部分覆盖",
            "max_iterations": 50,  # 只设这一个
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.max_iterations == 50
        # 未覆盖的字段保持默认
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1

        runner_cfg = captured_configs[0]
        assert runner_cfg.max_iterations == 50  # 覆盖值
        # B2: 删 max_budget_usd 传参，RunnerConfig 无此字段
        assert runner_cfg.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 默认值


# ============================================================================
# Part 3: model_override 透传（spec 要求 judge 用 judge_model 字段）
# ============================================================================


class TestModelOverridePropagated:
    """Ticket 11: model / judge_model 字段透传到 RunnerConfig.model。"""

    def test_main_agent_model_passed_to_runner_config(self, tmp_db_path, monkeypatch):
        """config.model 字段透传到 RunnerConfig.model。"""
        mock_llm = MockLLM(default_text="model 测试 ok")
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        config = HeadlessConfig(
            trigger_text="测试 model",
            model="deepseek-chat",
            db_path=tmp_db_path,
        )

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1
        assert captured_configs[0].model == "deepseek-chat"

    def test_judge_agent_judge_model_passed_to_runner_config(self, tmp_db_path, monkeypatch):
        """config.judge_model 字段透传到 judge RunnerConfig.model。"""
        mock_llm = MockLLM(
            default_text='{"verdict": "success", "reason": "judge model 测试 ok"}'
        )
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        config = HeadlessConfig(
            trigger_text="测试 judge model",
            judge_model="deepseek-chat",
            db_path=tmp_db_path,
        )

        async def run():
            return await run_judge_agent(
                main_output=["完成"],
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1
        assert captured_configs[0].model == "deepseek-chat"  # judge_model 透传

    def test_empty_model_uses_default_tier(self, tmp_db_path, monkeypatch):
        """model="" 时 RunnerConfig.model=""（默认 tier，由 LLMPool 决定）。"""
        mock_llm = MockLLM(default_text="默认 tier 测试 ok")
        _install_mock_llm_infra(monkeypatch, mock_llm)

        captured_configs: list[RunnerConfig] = []
        _install_config_capturing_facade(monkeypatch, captured_configs)

        config = HeadlessConfig(db_path=tmp_db_path)  # model 留空

        async def run():
            return await run_main_agent(
                trigger_text="测试默认 tier",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(captured_configs) == 1
        assert captured_configs[0].model == ""  # 留空 = 默认 tier
