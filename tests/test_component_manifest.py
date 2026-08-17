"""组件化 Manifest 系统测试

验证 workspace/<module>/manifest.toml 的扫描、解析、缓存行为。
端到端 seam：创建/删除测试组件 manifest，验证 4 个插入点入口声明可被加载器发现。

使用 stock_advisor 作为真实组件 fixture（已通过 Ticket 07 迁移到 manifest）。
"""

from pathlib import Path

from server.component_manifest import (
    AgentGuideEntry,
    ClientPanelEntry,
    ConfigSchemaEntry,
    LoopTasksEntry,
    Manifest,
    SkillEntry,
    WatchEntry,
    get_component,
    load_manifests,
    reset_cache,
)

PROJECT_ROOT = Path(__file__).parent.parent
STOCK_ADVISOR_DIR = PROJECT_ROOT / "workspace" / "stock_advisor"


def _reload_manifests() -> dict[str, Manifest]:
    """重置缓存并重新扫描 manifest（测试用）"""
    reset_cache()
    return load_manifests()


class TestManifestLoader:
    """Manifest 加载器核心行为测试"""

    def test_load_manifests_returns_dict(self):
        """load_manifests 返回 dict[str, Manifest]"""
        manifests = _reload_manifests()
        assert isinstance(manifests, dict)

    def test_stock_advisor_fixture_exists(self):
        """真实组件 workspace/stock_advisor/ 存在且含 manifest.toml"""
        assert STOCK_ADVISOR_DIR.exists(), f"组件目录不存在: {STOCK_ADVISOR_DIR}"
        assert (STOCK_ADVISOR_DIR / "manifest.toml").exists()

    def test_stock_advisor_loaded(self):
        """stock_advisor 应被加载到 manifests"""
        manifests = _reload_manifests()
        assert "stock_advisor" in manifests, f"stock_advisor 未加载，当前加载: {list(manifests.keys())}"

    def test_manifest_has_component_metadata(self):
        """Manifest 含 component 元信息（name/version/description）"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.name == "stock_advisor"
        assert m.version
        assert m.description
        assert m.enabled is True

    def test_manifest_has_four_insertion_points(self):
        """Manifest 含 4 个插入点入口声明（loop_tasks/agent_guide/client_panel/skill）"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.loop_tasks is not None, "loop_tasks 入口未声明"
        assert m.agent_guide is not None, "agent_guide 入口未声明"
        assert m.client_panel is not None, "client_panel 入口未声明"
        assert m.skill is not None, "skill 入口未声明"

    def test_manifest_has_config_schema(self):
        """Manifest 含 config_schema 入口"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.config_schema is not None

    def test_manifest_optional_fields(self):
        """Manifest 可选字段（project_tag/watch）正确解析"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        # project_tag 可选但 stock_advisor 声明了
        assert m.project_tag == "stock_advisor"
        # watch 可选
        if m.watch:
            assert isinstance(m.watch.paths, list)

    def test_get_component_by_name(self):
        """get_component(name) 按名查询"""
        _reload_manifests()
        m = get_component("stock_advisor")
        assert m is not None
        assert m.name == "stock_advisor"

    def test_get_component_returns_none_for_unknown(self):
        """get_component 对未知组件返回 None"""
        _reload_manifests()
        assert get_component("nonexistent_component") is None

    def test_load_manifests_caches_result(self):
        """load_manifests 缓存结果，第二次调用不重新扫描"""
        m1 = _reload_manifests()
        m2 = load_manifests()  # 不重置缓存
        assert m1 is m2, "load_manifests 未缓存结果"

    def test_workspace_dir_set(self):
        """Manifest 含 workspace_dir 指向 workspace/<name>/"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.workspace_dir == STOCK_ADVISOR_DIR

    def test_insertion_point_entry_fields(self):
        """插入点入口 Entry 含 file 字段（相对路径）"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.loop_tasks.file == "loop_actions.py"
        assert m.loop_tasks.entries_var == "LOOP_TASK_DEFS"
        assert m.agent_guide.file == "loop_actions.py"
        assert m.agent_guide.entries_var == "GUIDE_REGISTRY_ENTRIES"
        assert m.client_panel.file == "panel.py"
        assert m.client_panel.class_name == "StockAdvisorPanel"
        assert m.skill.file == "SKILL.md"
        assert m.skill.task_type == "recurring.stock_advisor"
        assert m.config_schema.file == "config_schema.toml"


class TestManifestDeletionBehavior:
    """删除 workspace/<module>/ 后 manifest 加载行为"""

    def test_load_manifests_handles_missing_workspace_dir(self, tmp_path, monkeypatch):
        """workspace 目录不存在时 load_manifests 返回空 dict 不报错"""
        # mock 项目根目录指向临时目录（无 workspace/）
        from lib import component_manifest
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", tmp_path / "workspace")
        reset_cache()
        manifests = load_manifests()
        assert manifests == {}

    def test_load_manifests_skips_dir_without_manifest(self, tmp_path, monkeypatch):
        """workspace/<dir>/ 无 manifest.toml 时跳过该目录"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        # 创建一个无 manifest.toml 的子目录
        (ws_dir / "no_manifest_component").mkdir()
        (ws_dir / "no_manifest_component" / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        assert "no_manifest_component" not in manifests

    def test_load_manifests_skips_invalid_manifest(self, tmp_path, monkeypatch):
        """manifest.toml 解析失败时跳过该组件不阻断其他组件"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()

        # 组件 A：无效 toml
        comp_a = ws_dir / "comp_a"
        comp_a.mkdir()
        (comp_a / "manifest.toml").write_text("invalid toml content = = =", encoding="utf-8")

        # 组件 B：有效 toml
        comp_b = ws_dir / "comp_b"
        comp_b.mkdir()
        (comp_b / "manifest.toml").write_text("""
[component]
name = "comp_b"
version = "0.1.0"
description = "组件 B"
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        # comp_a 被跳过，comp_b 正常加载
        assert "comp_b" in manifests
        assert "comp_a" not in manifests

    def test_load_manifests_skips_disabled_component(self, tmp_path, monkeypatch):
        """manifest enabled = false 时仍加载（供 config_schema 引用），但 enabled 字段为 False"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        comp = ws_dir / "disabled_comp"
        comp.mkdir()
        (comp / "manifest.toml").write_text("""
[component]
name = "disabled_comp"
version = "0.1.0"
description = "禁用组件"
enabled = false
""", encoding="utf-8")
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        # enabled=false 仍加载（软禁用，配置仍可见）
        assert "disabled_comp" in manifests
        assert manifests["disabled_comp"].enabled is False


class TestManifestDataStructure:
    """Manifest 数据结构测试"""

    def test_manifest_is_dataclass(self):
        """Manifest 是 dataclass"""
        import dataclasses
        assert dataclasses.is_dataclass(Manifest)

    def test_entry_types_are_dataclasses(self):
        """各插入点 Entry 是 dataclass"""
        import dataclasses
        for cls in [LoopTasksEntry, AgentGuideEntry, ClientPanelEntry, SkillEntry, ConfigSchemaEntry, WatchEntry]:
            assert dataclasses.is_dataclass(cls), f"{cls.__name__} 不是 dataclass"

    def test_manifest_default_enabled_is_true(self):
        """Manifest enabled 默认为 True"""
        m = Manifest(name="x", version="0.1.0", description="d", workspace_dir=Path("/tmp"))
        assert m.enabled is True

    def test_manifest_optional_entries_default_none(self):
        """Manifest 可选插入点默认 None"""
        m = Manifest(name="x", version="0.1.0", description="d", workspace_dir=Path("/tmp"))
        assert m.loop_tasks is None
        assert m.agent_guide is None
        assert m.client_panel is None
        assert m.skill is None
        assert m.config_schema is None
        assert m.watch is None
        assert m.project_tag is None
