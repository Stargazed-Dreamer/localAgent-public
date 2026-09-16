"""组件化 Manifest 系统测试

验证 workspace/<module>/manifest.toml 的扫描、解析、缓存行为。
端到端 seam：创建/删除测试组件 manifest，验证 4 个插入点入口声明可被加载器发现。

使用 stock_advisor 作为真实组件 fixture（已通过 Ticket 07 迁移到 manifest）。
"""

from pathlib import Path

from server.component_manifest import (
    AgentGuideEntry,
    BackupEntry,
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
        for cls in [LoopTasksEntry, AgentGuideEntry, ClientPanelEntry, SkillEntry, ConfigSchemaEntry, WatchEntry, BackupEntry]:
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
        assert m.backup is None
        assert m.project_tag is None


class TestBackupEntryParsing:
    """[backup] 段解析行为测试（ADR-0029）"""

    def test_stock_advisor_backup_parsed(self):
        """stock_advisor 真实组件 [backup] 段正确解析为 BackupEntry.paths"""
        manifests = _reload_manifests()
        m = manifests["stock_advisor"]
        assert m.backup is not None, "stock_advisor 未声明 [backup] 段"
        assert isinstance(m.backup, BackupEntry)
        assert m.backup.paths == [
            "watchlist.json",
            "data/stock_advisor.db",
            "data/stock_history.duckdb",
        ]

    def test_accounting_backup_parsed_with_directory(self):
        """accounting [backup] 段含目录路径（以 "/" 结尾）正确解析"""
        manifests = _reload_manifests()
        m = manifests["accounting"]
        assert m.backup is not None, "accounting 未声明 [backup] 段"
        # 含目录路径 "账单/"（用于 backup_workspace.py 打 zip 测试）
        assert "账单/" in m.backup.paths
        assert "data/accounting.db" in m.backup.paths

    def test_backup_defaults_to_none_when_section_absent(self, tmp_path, monkeypatch):
        """manifest 缺 [backup] 段时 m.backup 为 None（不报错）"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        comp = ws_dir / "no_backup"
        comp.mkdir()
        (comp / "manifest.toml").write_text("""
[component]
name = "no_backup"
version = "0.1.0"
description = "无备份声明组件"
""", encoding="utf-8")
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        assert "no_backup" in manifests
        assert manifests["no_backup"].backup is None

    def test_backup_with_empty_paths_returns_empty_list(self, tmp_path, monkeypatch):
        """[backup] 段存在但 paths 缺失时返回空 list（向后兼容）"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        comp = ws_dir / "empty_backup"
        comp.mkdir()
        (comp / "manifest.toml").write_text("""
[component]
name = "empty_backup"
version = "0.1.0"
description = "声明 [backup] 段但 paths 为空"

[backup]
""", encoding="utf-8")
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        m = manifests["empty_backup"]
        assert m.backup is not None
        assert m.backup.paths == []

    def test_backup_decoupled_from_watch(self, tmp_path, monkeypatch):
        """[backup] 段与 [watch] 段语义解耦：两者可独立声明，paths 不共享"""
        from lib import component_manifest
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        comp = ws_dir / "decoupled"
        comp.mkdir()
        (comp / "manifest.toml").write_text("""
[component]
name = "decoupled"
version = "0.1.0"
description = "watch 和 backup 不同"

[watch]
paths = ["scripts/", "config.json"]

[backup]
paths = ["data.db", "user_notes.json"]
""", encoding="utf-8")
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        reset_cache()
        manifests = load_manifests()
        m = manifests["decoupled"]
        assert m.watch is not None
        assert m.backup is not None
        # 两段独立，paths 不交叉
        assert m.watch.paths == ["scripts/", "config.json"]
        assert m.backup.paths == ["data.db", "user_notes.json"]
        assert set(m.watch.paths).isdisjoint(set(m.backup.paths))

    # ---- 6 个扩展组件 [backup] 段解析（2026-08-18 第二轮扩展）----

    def test_yihuan_gacha_backup_parsed(self):
        """yihuan_gacha [backup] 段正确解析：4 个 JSON + raw/ 目录"""
        manifests = _reload_manifests()
        m = manifests["yihuan_gacha"]
        assert m.backup is not None, "yihuan_gacha 未声明 [backup] 段"
        assert m.backup.paths == [
            "yihuan_gacha_database.json",
            "yihuan_gacha_summary.json",
            "yihuan_gacha_final.json",
            "yihuan_gacha_review.json",
            "raw/",
        ]

    def test_endfield_gacha_backup_parsed(self):
        """endfield_gacha [backup] 段正确解析：summary/weapon/char + raw/"""
        manifests = _reload_manifests()
        m = manifests["endfield_gacha"]
        assert m.backup is not None, "endfield_gacha 未声明 [backup] 段"
        assert m.backup.paths == [
            "summary.json",
            "weapon_records.json",
            "char_records.json",
            "raw/",
        ]

    def test_arknights_gacha_backup_parsed_excludes_reference_data(self):
        """arknights_gacha [backup] 段含个人数据但排除 Wiki 参考数据（bwiki/prts）"""
        manifests = _reload_manifests()
        m = manifests["arknights_gacha"]
        assert m.backup is not None, "arknights_gacha 未声明 [backup] 段"
        # 个人数据：gacha_records / summary / pool_registry / raw_full + raw/
        assert "gacha_records.json" in m.backup.paths
        assert "summary.json" in m.backup.paths
        assert "pool_registry.json" in m.backup.paths
        assert "raw_full.json" in m.backup.paths
        assert "raw/" in m.backup.paths
        # Wiki 参考数据不应进入备份（已在 exports.source 发布）
        assert "bwiki_pools.json" not in m.backup.paths
        assert "bwiki_pools_full.json" not in m.backup.paths
        assert "prts_limited_pools.json" not in m.backup.paths

    def test_wuwa_gacha_backup_parsed(self):
        """wuwa_gacha [backup] 段正确解析：database/summary + raw/"""
        manifests = _reload_manifests()
        m = manifests["wuwa_gacha"]
        assert m.backup is not None, "wuwa_gacha 未声明 [backup] 段"
        assert m.backup.paths == [
            "wuwa_gacha_database.json",
            "wuwa_gacha_summary.json",
            "raw/",
        ]

    def test_web_archive_backup_parsed(self):
        """web_archive [backup] 段正确解析：urls*.json + index*.json（随存档源扩展）"""
        manifests = _reload_manifests()
        m = manifests["web_archive"]
        assert m.backup is not None, "web_archive 未声明 [backup] 段"
        assert m.backup.paths == [
            "urls.json",
            "urls_unique.json",
            "urls_custom.json",
            "urls_feishu.json",
            "urls_tencent.json",
            "index.json",
            "index_tencent.json",
            "index_xiaolin.json",
        ]

    def test_gh_mirror_backup_decoupled_from_watch(self):
        """gh_mirror [backup] 段与 [watch] 段解耦：backup 只含 repos.json 不含模板"""
        manifests = _reload_manifests()
        m = manifests["gh_mirror"]
        assert m.watch is not None, "gh_mirror 应有 [watch] 段"
        assert m.backup is not None, "gh_mirror 未声明 [backup] 段"
        # watch 含 repos.json + repos.example.json（模板），backup 只含 repos.json
        assert m.watch.paths == ["repos.json", "repos.example.json"]
        assert m.backup.paths == ["repos.json"]
        # repos.example.json 是模板，不应进入备份
        assert "repos.example.json" not in m.backup.paths
