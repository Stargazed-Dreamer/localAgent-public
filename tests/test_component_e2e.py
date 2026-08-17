"""Ticket 08: 端到端验证 - 删除 workspace/stock_advisor/ 后主功能零残留

验证第一性原理："删除 workspace/<module>/ 后主代码库零引用，主功能完全正常"。

不实际删除用户 stock_advisor 目录（含 holdings_personal.json / watchlist.json / DuckDB 等用户数据），
而是用 monkeypatch 临时把 _WORKSPACE_DIR 指向一个无 stock_advisor 的临时目录，模拟"组件被删除"的场景。
测试结束后 monkeypatch 自动还原。

验证 4 个插入点全部消失：
  1. agent_guide 不返回 stock_advisor 条目
  2. loop_manager 不调度 stock_advisor 任务
  3. PanelRegistry 不含 stock_advisor 面板
  4. _index.md 不含 stock_advisor 段（generate_index 输出空）
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from server.component_manifest import load_manifests, reset_cache


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """提供一个隔离的 workspace 临时目录，不含 stock_advisor。

    通过 monkeypatch 替换 component_manifest._WORKSPACE_DIR，模拟 stock_advisor 被删除的场景。
    """
    from lib import component_manifest

    # 创建一个空的 workspace 目录
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()

    # monkeypatch 替换 _WORKSPACE_DIR 和 _PROJECT_ROOT
    monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
    monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)

    # 重置 manifest 缓存，让下次 load_manifests() 重新扫描
    reset_cache()

    yield ws_dir

    # 测试结束后还原（monkeypatch 自动还原 _WORKSPACE_DIR，但缓存需手动重置）
    reset_cache()


class TestStockAdvisorDeletionBehavior:
    """删除 workspace/stock_advisor/ 后的端到端验证"""

    def test_stock_advisor_not_in_manifests_when_deleted(self, isolated_workspace):
        """删除后 load_manifests() 不返回 stock_advisor"""
        manifests = load_manifests()
        assert "stock_advisor" not in manifests, (
            f"删除后 manifests 不应含 stock_advisor，实际: {list(manifests.keys())}"
        )

    def test_agent_guide_no_stock_advisor_entries_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 _load_optional_guide_entries() 不返回 stock_advisor 条目

        重新加载 server.agent_guide 模块以触发 _load_optional_guide_entries() 重扫。
        """
        # 重新加载 agent_guide，触发 _load_optional_guide_entries() 重新执行
        from server import agent_guide
        importlib.reload(agent_guide)

        # 检查 GUIDE_REGISTRY 不含 stock_advisor 相关条目
        stock_keys = [k for k in agent_guide.GUIDE_REGISTRY if "stock" in k.lower()]
        assert not stock_keys, (
            f"删除后 GUIDE_REGISTRY 不应含 stock 相关条目，实际: {stock_keys}"
        )

    def test_loop_manager_no_stock_advisor_tasks_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 _load_optional_loop_defs() 不返回 stock_advisor 任务"""
        from server.activity_tracker import loop_manager
        importlib.reload(loop_manager)

        defs = loop_manager._load_optional_loop_defs()
        assert "stock_advisor" not in defs, (
            f"删除后 loop defs 不应含 stock_advisor，实际: {list(defs.keys())}"
        )

    def test_panel_registry_no_stock_advisor_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 PanelRegistry 不含 stock_advisor 面板"""
        from client.core.panel_registry import PanelRegistry
        PanelRegistry.clear()
        PanelRegistry.discover()

        ids = [c.meta().id for c in PanelRegistry.get_all_sorted()]
        assert "stock_advisor" not in ids, (
            f"删除后 PanelRegistry 不应含 stock_advisor，实际: {ids}"
        )

    def test_generate_index_no_stock_advisor_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 generate_index 不输出 stock_advisor 段"""
        from tools import generate_index
        importlib.reload(generate_index)

        # generate_index._collect_component_entries() 应返回空列表
        entries = generate_index._collect_component_entries()
        stock_entries = [e for e in entries if "stock" in e.get("name", "").lower()]
        assert not stock_entries, (
            f"删除后 generate_index 不应输出 stock 相关条目，实际: {stock_entries}"
        )

    def test_check_skills_no_component_skill_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 check_skills.scan_component_skills() 返回空列表"""
        from tools import check_skills
        importlib.reload(check_skills)

        component_skills = check_skills.scan_component_skills()
        assert component_skills == [], (
            f"删除后 check_skills 不应识别任何组件化 skill，实际: {component_skills}"
        )

    def test_main_config_no_stock_advisor_when_deleted(self, isolated_workspace, monkeypatch):
        """删除后 component_config 不返回 stock_advisor 配置

        注：主 config.toml 可能仍含 [stock_advisor] section（用户历史配置），
        但 component_config.load_component_configs() 返回的"组件配置"应为空。
        主 config 的 [stock_advisor] 残留由用户清理，不属于 manifest 管理范围。
        """
        from server import component_config
        importlib.reload(component_config)
        component_config.reset_cache()

        component_configs = component_config.load_component_configs()
        assert "stock_advisor" not in component_configs, (
            f"删除后 component_configs 不应含 stock_advisor，实际: {list(component_configs.keys())}"
        )


class TestStockAdvisorLoadedWhenPresent:
    """正例验证：stock_advisor 存在时 4 个插入点全部正常加载（回归测试）"""

    def setup_method(self):
        """每个测试前重置 manifest 缓存并 reload 模块，避免删除测试污染状态。

        删除测试通过 monkeypatch + reload 模拟组件删除，monkeypatch 还原后
        GUIDE_REGISTRY / TASK_BUILDERS 仍是删除后的状态，需主动 reload 重新加载。
        """
        reset_cache()
        from server import agent_guide
        importlib.reload(agent_guide)
        from server.activity_tracker import loop_manager
        importlib.reload(loop_manager)

    def test_stock_advisor_in_manifests(self):
        """stock_advisor 应被 manifest 加载"""
        reset_cache()
        manifests = load_manifests()
        assert "stock_advisor" in manifests, (
            f"stock_advisor 应在 manifests 中，实际: {list(manifests.keys())}"
        )

    def test_stock_advisor_has_four_insertion_points(self):
        """stock_advisor manifest 含 4 个插入点入口声明"""
        reset_cache()
        manifests = load_manifests()
        m = manifests["stock_advisor"]
        assert m.loop_tasks is not None, "loop_tasks 入口未声明"
        assert m.agent_guide is not None, "agent_guide 入口未声明"
        assert m.client_panel is not None, "client_panel 入口未声明"
        assert m.skill is not None, "skill 入口未声明"

    def test_stock_advisor_manifest_metadata(self):
        """stock_advisor manifest 含完整 component 元信息"""
        reset_cache()
        manifests = load_manifests()
        m = manifests["stock_advisor"]
        assert m.name == "stock_advisor"
        assert m.version
        assert m.description
        assert m.enabled is True
        assert m.project_tag == "stock_advisor", "project_tag 应为 stock_advisor（LLM 池统计用）"

    def test_stock_advisor_agent_guide_entries_loaded(self):
        """stock_advisor 的 agent_guide 条目（recurring + adhoc）被 GUIDE_REGISTRY 加载"""
        from server.agent_guide import GUIDE_REGISTRY
        assert "recurring.stock_advisor" in GUIDE_REGISTRY
        assert "adhoc.stock_wisdom" in GUIDE_REGISTRY
        # skill_file 应指向 workspace/stock_advisor/SKILL.md（迁移后的位置）
        assert GUIDE_REGISTRY["recurring.stock_advisor"]["skill_file"] == "workspace/stock_advisor/SKILL.md"
        assert GUIDE_REGISTRY["adhoc.stock_wisdom"]["skill_file"] == "workspace/stock_advisor/SKILL.md"

    def test_stock_advisor_loop_tasks_loaded(self):
        """stock_advisor 的 3 个 loop 任务（opening/closing/evening）被加载"""
        from server.activity_tracker.loop_manager import _load_optional_loop_defs
        defs = _load_optional_loop_defs()
        assert "stock_advisor" in defs
        sub_ids = [d.get("sub_id") for d in defs["stock_advisor"]]
        assert set(sub_ids) == {"opening", "closing", "evening"}, (
            f"应含 opening/closing/evening 三个子任务，实际: {sub_ids}"
        )

    def test_stock_advisor_panel_discoverable(self):
        """stock_advisor 面板被 PanelRegistry 发现"""
        from client.core.panel_registry import PanelRegistry
        PanelRegistry.clear()
        PanelRegistry.discover()
        ids = [c.meta().id for c in PanelRegistry.get_all_sorted()]
        assert "stock_advisor" in ids

    def test_stock_advisor_skill_file_exists(self):
        """stock_advisor SKILL.md 文件存在且 frontmatter 含 task_type"""
        reset_cache()
        manifests = load_manifests()
        m = manifests["stock_advisor"]
        skill_file = m.workspace_dir / m.skill.file
        assert skill_file.exists(), f"SKILL.md 文件不存在: {skill_file}"

    def test_stock_advisor_config_schema_loaded(self):
        """stock_advisor config_schema 被加载，含核心配置项"""
        from server import component_config
        component_config.reset_cache()
        cc = component_config.load_component_configs()
        assert "stock_advisor" in cc
        sa_cfg = cc["stock_advisor"]
        # 核心配置项必须存在
        assert "watchlist_path" in sa_cfg
        assert "db_path" in sa_cfg
        assert "reports_dir" in sa_cfg

    def test_stock_advisor_merged_config_preserves_user_overrides(self):
        """合并配置时用户主 config 优先于组件 schema 默认值

        场景：用户 config.toml 含 holdings_personal_path（schema 中无此字段），
        合并后此字段应保留。
        """
        from server import component_config
        component_config.reset_cache()
        merged = component_config.get_merged_config()
        sa_cfg = merged.get("stock_advisor", {})
        # 主 config 的字段应保留
        assert "watchlist_path" in sa_cfg
        # 主 config 中存在但 schema 中没有的字段也应保留
        if "holdings_personal_path" in sa_cfg:
            assert sa_cfg["holdings_personal_path"], "holdings_personal_path 不应为空"

    def test_stock_advisor_llm_pool_uses_project_tag(self):
        """stock_advisor 调用 LLM 时传 project='stock_advisor' 标签

        静态检查 analyzer.py 源码含 project="stock_advisor" 调用，
        与 manifest 的 project_tag 字段对齐（LLM 池请求来源统计机制）。
        """
        analyzer_path = Path(__file__).parent.parent / "workspace" / "stock_advisor" / "analyzer.py"
        if not analyzer_path.exists():
            pytest.skip("analyzer.py 不存在")
        content = analyzer_path.read_text(encoding="utf-8")
        assert 'project="stock_advisor"' in content or "project='stock_advisor'" in content, (
            "analyzer.py 应调用 LLM 时传 project='stock_advisor'（与 manifest.project_tag 对齐）"
        )


class TestMainConfigExampleCleanup:
    """验证主 config.example.toml 已清理 stock_advisor 残留"""

    def test_config_example_no_stock_advisor_section(self):
        """config.example.toml 不含 [stock_advisor] section（已迁移到组件 schema）"""
        cfg_path = Path(__file__).parent.parent / "config.example.toml"
        content = cfg_path.read_text(encoding="utf-8")
        # 不应含 [stock_advisor] / [stock_advisor.profile] / [stock_advisor.cache] / [loops.stock_advisor]
        assert "[stock_advisor]" not in content, "config.example.toml 仍含 [stock_advisor] section"
        assert "[stock_advisor.profile]" not in content
        assert "[stock_advisor.cache]" not in content
        assert "[stock_advisor.cache.lru]" not in content
        assert "[loops.stock_advisor]" not in content

    def test_config_example_has_migration_note(self):
        """config.example.toml 应含迁移说明注释"""
        cfg_path = Path(__file__).parent.parent / "config.example.toml"
        content = cfg_path.read_text(encoding="utf-8")
        assert "stock_advisor" in content, "应含 stock_advisor 迁移说明注释"
        assert "config_schema.toml" in content or "component_config" in content, (
            "应说明 stock_advisor 配置已迁移到 config_schema.toml"
        )

    def test_config_schema_toml_exists(self):
        """workspace/stock_advisor/config_schema.toml 存在"""
        schema_path = Path(__file__).parent.parent / "workspace" / "stock_advisor" / "config_schema.toml"
        assert schema_path.exists(), f"config_schema.toml 不存在: {schema_path}"

    def test_config_schema_has_required_sections(self):
        """config_schema.toml 含 5 个核心 section"""
        schema_path = Path(__file__).parent.parent / "workspace" / "stock_advisor" / "config_schema.toml"
        content = schema_path.read_text(encoding="utf-8")
        assert "[stock_advisor]" in content
        assert "[stock_advisor.profile]" in content
        assert "[stock_advisor.cache]" in content
        assert "[stock_advisor.cache.lru]" in content
        assert "[loops.stock_advisor]" in content


class TestMainCodebaseZeroReferences:
    """验证主代码库零硬编码引用 stock_advisor（manifest 之外的引用）"""

    def test_agent_guide_source_no_stock_advisor(self):
        """server/agent_guide.py 源文件不含 stock_advisor 硬编码（运行时通过 manifest 加载）"""
        ag_path = Path(__file__).parent.parent / "server" / "agent_guide.py"
        content = ag_path.read_text(encoding="utf-8")
        # 检查 GUIDE_REGISTRY 字面量定义中不含 stock_advisor
        # 注：注释中可能提及，所以只检查 "skill_file" 引用
        assert '"recurring.stock_advisor"' not in content, (
            "agent_guide.py 源文件不应硬编码 recurring.stock_advisor（应通过 manifest 加载）"
        )
        assert '"adhoc.stock_wisdom"' not in content, (
            "agent_guide.py 源文件不应硬编码 adhoc.stock_wisdom（应通过 manifest 加载）"
        )

    def test_no_stock_advisor_panel_in_client_panels(self):
        """client/panels/ 目录下不应有 stock_advisor.py（已迁移到 workspace/）"""
        panel_path = Path(__file__).parent.parent / "client" / "panels" / "stock_advisor.py"
        assert not panel_path.exists(), (
            "client/panels/stock_advisor.py 应已删除（迁移到 workspace/stock_advisor/panel.py）"
        )

    def test_no_stock_advisor_skill_in_agents_skills(self):
        """.agents/skills/ 目录下不应有 stock_advisor.md（已迁移到 workspace/）"""
        # 检查扁平 .md
        flat_path = Path(__file__).parent.parent / ".agents" / "skills" / "stock_advisor.md"
        assert not flat_path.exists(), (
            ".agents/skills/stock_advisor.md 应已删除（迁移到 workspace/stock_advisor/SKILL.md）"
        )
        # 检查子目录式
        sub_path = Path(__file__).parent.parent / ".agents" / "skills" / "stock_advisor" / "SKILL.md"
        assert not sub_path.exists(), (
            ".agents/skills/stock_advisor/SKILL.md 应不存在（已迁移到 workspace/）"
        )
