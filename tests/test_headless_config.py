"""headless-agent-session Ticket 05 验收测试：config 段支持 + config.example.toml

覆盖 Ticket 05 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] LoopManager.load_tasks() 解析 [loops.headless_session] 段
- [x] HeadlessSessionAction 配置项解析（trigger_text / skill / one_shot / 资源限制 / judge 配置）
- [x] 默认值（spec 默认值）：max_iterations=100, wall_clock_budget_secs=1800
- [x] config.example.toml 新增 [loops.headless_session] 模板
- [x] data/config_descriptions.json 新增配置项描述
- [x] 单元测试：配置解析（完整配置 / 部分配置用默认值 / 空配置）
- [x] 单元测试：验证默认值生效
- [x] 单元测试：LoopManager.load_tasks() 加载 headless_session 段

B2（spec D2/D3）：max_budget_usd / judge_max_budget_usd 字段已删除，
本测试文件移除所有 budget_usd 相关断言（功能已移除，不再测试）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_JUDGE_MAX_ITERATIONS,
    DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_WALL_CLOCK_BUDGET_SECS,
    HeadlessConfig,
    parse_headless_config,
)
from server.activity_tracker.loop_manager import (  # noqa: E402
    LoopManager,
    NoopAction,
    reset_manager,
)

# ============================================================================
# 1. parse_headless_config 完整覆盖
# ============================================================================


class TestParseHeadlessConfigComplete:
    """Ticket 05: 配置解析完整覆盖（完整 / 部分 / 空）。"""

    def test_complete_config_all_fields_specified(self):
        """完整配置：所有字段都指定，无默认值生效。"""
        cfg_dict = {
            "trigger_text": "查 dmca-backup 日志",
            "skill": "adhoc.log_check",
            "one_shot": True,
            "max_iterations": 50,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 900,
            "model": "deepseek-chat",
            "judge_model": "deepseek-chat",
            "judge_max_iterations": 5,
            # B2: 删 judge_max_budget_usd 配置项
            "judge_wall_clock_budget_secs": 120,
            "base_url": "http://127.0.0.1:9999",
            "db_path": "/tmp/test_agent.db",
        }
        cfg = parse_headless_config(cfg_dict)
        assert cfg.trigger_text == "查 dmca-backup 日志"
        assert cfg.skill == "adhoc.log_check"
        assert cfg.one_shot is True
        assert cfg.max_iterations == 50
        # B2: 删 max_budget_usd 字段
        assert cfg.wall_clock_budget_secs == 900
        assert cfg.model == "deepseek-chat"
        assert cfg.judge_model == "deepseek-chat"
        assert cfg.judge_max_iterations == 5
        # B2: 删 judge_max_budget_usd 字段
        assert cfg.judge_wall_clock_budget_secs == 120
        assert cfg.base_url == "http://127.0.0.1:9999"
        assert cfg.db_path == "/tmp/test_agent.db"

    def test_empty_config_all_defaults(self):
        """空配置：所有字段用默认值（spec 默认值）。"""
        cfg = parse_headless_config({})
        assert cfg.trigger_text == ""
        assert cfg.skill == ""
        assert cfg.one_shot is False
        # spec 默认值（用户反馈放宽后）
        assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS  # 100
        # B2: 删 max_budget_usd 字段
        assert cfg.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 1800
        assert cfg.model == ""
        # judge 默认值
        assert cfg.judge_model == ""
        assert cfg.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS  # 10
        # B2: 删 judge_max_budget_usd 字段
        assert cfg.judge_wall_clock_budget_secs == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS  # 300
        # 其他默认值
        assert cfg.base_url == DEFAULT_BASE_URL
        assert cfg.db_path == ""

    def test_partial_config_uses_defaults_for_missing_fields(self):
        """部分配置：缺失字段用默认值。"""
        cfg = parse_headless_config({
            "trigger_text": "周期性股票分析",
            "cron": "0 9 * * *",  # 这个字段不进 HeadlessConfig（由 LoopManager trigger 解析）
            "one_shot": False,
        })
        # 指定的字段
        assert cfg.trigger_text == "周期性股票分析"
        assert cfg.one_shot is False
        # 未指定的字段用默认值
        assert cfg.skill == ""
        assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS
        # B2: 删 max_budget_usd 字段
        assert cfg.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS
        assert cfg.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS
        # B2: 删 judge_max_budget_usd 字段

    def test_skill_only_config(self):
        """只配 skill（trigger_text 留空）：典型 skill 绑定场景。"""
        cfg = parse_headless_config({
            "skill": "recurring.stock_analysis",
            "cron": "0 9 * * *",
        })
        assert cfg.trigger_text == ""
        assert cfg.skill == "recurring.stock_analysis"
        assert cfg.one_shot is False  # 默认

    def test_one_shot_with_trigger_text(self):
        """一次性任务：trigger_text + one_shot=true。"""
        cfg = parse_headless_config({
            "trigger_text": "1 小时后查日志",
            "one_shot": True,
            "cron": "0 * * * *",
        })
        assert cfg.trigger_text == "1 小时后查日志"
        assert cfg.one_shot is True

    def test_resource_limits_override(self):
        """资源限制覆盖：用户放宽/收紧 max_iterations / wall_clock。"""
        cfg = parse_headless_config({
            "max_iterations": 200,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 3600,
            "judge_max_iterations": 20,
            # B2: 删 judge_max_budget_usd 配置项
        })
        assert cfg.max_iterations == 200
        # B2: 删 max_budget_usd 字段
        assert cfg.wall_clock_budget_secs == 3600
        assert cfg.judge_max_iterations == 20
        # B2: 删 judge_max_budget_usd 字段


# ============================================================================
# 2. 默认值与 spec 一致性
# ============================================================================


class TestDefaultValues:
    """Ticket 05: 验证默认值与 spec 一致（用户反馈放宽后）。"""

    def test_main_agent_defaults_match_spec(self):
        """主 agent 资源限制默认值与 spec 一致（用户反馈放宽）。"""
        cfg = HeadlessConfig()
        assert cfg.max_iterations == 100  # chat 会话 50 翻倍
        # B2: 删 max_budget_usd 字段
        assert cfg.wall_clock_budget_secs == 1800  # 30 分钟

    def test_judge_agent_defaults_match_spec(self):
        """judge agent 资源限制默认值与 spec 一致（判定任务简单）。"""
        cfg = HeadlessConfig()
        assert cfg.judge_max_iterations == 10
        # B2: 删 judge_max_budget_usd 字段
        assert cfg.judge_wall_clock_budget_secs == 300  # 5 分钟

    def test_default_base_url(self):
        """默认 base_url 指向后端 8766 端口。"""
        cfg = HeadlessConfig()
        assert cfg.base_url == "http://127.0.0.1:8766"

    def test_default_one_shot_is_false(self):
        """默认 one_shot=False（周期性任务），一次性任务需显式设 true。"""
        cfg = HeadlessConfig()
        assert cfg.one_shot is False


# ============================================================================
# 3. LoopManager.load_tasks() 集成测试
# ============================================================================


class TestLoopManagerLoadsHeadlessSession:
    """Ticket 05: LoopManager.load_tasks() 解析 [loops.headless_session] 段。"""

    @pytest.fixture
    def isolated_manager(self, monkeypatch, tmp_path):
        """隔离的 LoopManager：state 文件指向临时目录。"""
        state_file = tmp_path / "tasks.json"
        monkeypatch.setattr(
            "server.activity_tracker.loop_manager.STATE_FILE", state_file
        )
        from unittest.mock import MagicMock
        monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
        yield
        reset_manager()

    def test_load_tasks_loads_headless_session_segment(self, isolated_manager):
        """config 含 [loops.headless_session] 段时，load_tasks 加载该任务。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    "cron": "0 9 * * *",
                    "trigger_text": "测试任务",
                },
            },
        }
        mgr = LoopManager(config)
        # 注册 headless_session action（用 NoopAction 代替真实 HeadlessSessionAction）
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        assert "headless_session.run" in mgr.tasks
        task = mgr.tasks["headless_session.run"]
        assert task.enabled is True
        assert task.action.action_type == "noop"
        assert task.source_segment == "headless_session"

    def test_load_tasks_headless_session_disabled(self, isolated_manager):
        """[loops.headless_session] enabled=False 时任务 disabled。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": False,
                    "cron": "0 9 * * *",
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        assert "headless_session.run" in mgr.tasks
        assert mgr.tasks["headless_session.run"].enabled is False

    def test_load_tasks_headless_session_default_cron(self, isolated_manager):
        """[loops.headless_session] 不指定 cron 时用默认 "0 9 * * *"。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    # 不指定 cron
                    "trigger_text": "测试",
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        task = mgr.tasks["headless_session.run"]
        # trigger 是 CronTrigger，repr 含 cron 表达式
        trigger_repr = repr(task.trigger)
        assert "0 9 * * *" in trigger_repr

    def test_load_tasks_headless_session_custom_cron(self, isolated_manager):
        """[loops.headless_session] 自定义 cron 生效。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    "cron": "*/30 * * * *",  # 每 30 分钟
                    "trigger_text": "高频任务",
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        task = mgr.tasks["headless_session.run"]
        trigger_repr = repr(task.trigger)
        assert "*/30 * * * *" in trigger_repr

    def test_load_tasks_headless_session_config_passed_to_action(self, isolated_manager):
        """task.config 含完整 config 段（action 从中读 trigger_text / one_shot 等）。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    "cron": "0 9 * * *",
                    "trigger_text": "查日志",
                    "one_shot": True,
                    "max_iterations": 50,
                    "judge_model": "deepseek-chat",
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        task = mgr.tasks["headless_session.run"]
        # task.config 是 segment 的完整 cfg dict
        assert task.config["trigger_text"] == "查日志"
        assert task.config["one_shot"] is True
        assert task.config["max_iterations"] == 50
        assert task.config["judge_model"] == "deepseek-chat"

    def test_load_tasks_headless_session_fail_threshold(self, isolated_manager):
        """[loops.headless_session] fail_threshold 生效（默认 5，可覆盖）。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    "cron": "0 9 * * *",
                    "fail_threshold": 3,
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("headless_session", NoopAction())
        mgr.load_tasks()

        task = mgr.tasks["headless_session.run"]
        assert task.fail_threshold == 3

    def test_load_tasks_headless_session_action_not_registered_skipped(self, isolated_manager):
        """headless_session action 未注册时跳过（不报错）。"""
        config = {
            "enabled": True,
            "tasks": {
                "headless_session": {
                    "enabled": True,
                    "cron": "0 9 * * *",
                },
            },
        }
        mgr = LoopManager(config)
        # 不注册 headless_session action
        mgr.load_tasks()

        # 任务未加载（action 未注册）
        assert "headless_session.run" not in mgr.tasks


# ============================================================================
# 4. config.example.toml 与 config_descriptions.json 同步检查
# ============================================================================


class TestConfigFilesSync:
    """Ticket 05: 验证 config.example.toml 和 config_descriptions.json 含 headless_session 配置。"""

    def test_config_example_toml_contains_headless_session(self):
        """config.example.toml 含 [loops.headless_session] 段。"""
        config_path = PROJECT_ROOT / "config.example.toml"
        content = config_path.read_text(encoding="utf-8")
        assert "[loops.headless_session]" in content
        # 关键字段都应出现（B2: 删 max_budget_usd / judge_max_budget_usd）
        for field in [
            "trigger_text",
            "skill",
            "one_shot",
            "max_iterations",
            "wall_clock_budget_secs",
            "judge_model",
            "judge_max_iterations",
            "judge_wall_clock_budget_secs",
        ]:
            assert field in content, f"config.example.toml 缺字段: {field}"

    def test_config_descriptions_json_contains_headless_session(self):
        """data/config_descriptions.json 含 headless_session 配置项描述。"""
        desc_path = PROJECT_ROOT / "data" / "config_descriptions.json"
        data = json.loads(desc_path.read_text(encoding="utf-8"))
        # 关键配置项都应有描述（B2: 删 max_budget_usd / judge_max_budget_usd 描述）
        required_keys = [
            "loops.headless_session",
            "loops.headless_session.enabled",
            "loops.headless_session.cron",
            "loops.headless_session.trigger_text",
            "loops.headless_session.skill",
            "loops.headless_session.one_shot",
            "loops.headless_session.max_iterations",
            "loops.headless_session.wall_clock_budget_secs",
            "loops.headless_session.judge_model",
            "loops.headless_session.judge_max_iterations",
            "loops.headless_session.judge_wall_clock_budget_secs",
        ]
        for key in required_keys:
            assert key in data, f"config_descriptions.json 缺 key: {key}"
            assert "desc" in data[key], f"{key} 缺 desc 字段"
            assert data[key]["desc"], f"{key} 的 desc 为空"
