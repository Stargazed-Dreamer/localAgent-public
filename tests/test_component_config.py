"""Ticket 05：config_schema 运行时合并测试

验证 component_config 模块的加载、合并、缓存行为：
- 加载 manifest 声明的 config_schema.toml
- 配置项以 [component_name] 为 namespace 合并
- 删除 workspace/<module>/ 后配置自动清除
- enabled=false 时配置仍合并（供其他组件引用）
"""

from __future__ import annotations

from server.component_config import (
    get_component_config,
    get_merged_config,
    load_component_configs,
    reset_cache,
)


def _reload_configs() -> dict[str, dict]:
    """重置缓存并重新加载组件配置（测试用）"""
    # 同时重置 manifest 缓存，避免前序测试的 monkeypatch 污染
    from server.component_manifest import reset_cache as reset_manifest_cache
    reset_manifest_cache()
    reset_cache()
    return load_component_configs()


class TestComponentConfigLoading:
    """component_config 加载行为测试"""

    def setup_method(self):
        """每个测试前重置 manifest 缓存，避免前序测试 monkeypatch 污染"""
        from server.component_manifest import reset_cache as reset_manifest_cache
        reset_manifest_cache()
        reset_cache()

    def test_load_component_configs_returns_dict(self):
        """load_component_configs 返回 dict"""
        configs = _reload_configs()
        assert isinstance(configs, dict)

    def test_stock_advisor_config_loaded(self):
        """stock_advisor 的 config_schema 应被加载"""
        configs = _reload_configs()
        assert "stock_advisor" in configs, f"stock_advisor 配置未加载，当前: {list(configs.keys())}"

    def test_stock_advisor_config_has_fields(self):
        """stock_advisor 配置含核心字段"""
        configs = _reload_configs()
        cfg = configs["stock_advisor"]
        assert isinstance(cfg, dict)
        # config_schema.toml 声明了 watchlist_path / db_path / reports_dir
        assert "watchlist_path" in cfg or "db_path" in cfg or "reports_dir" in cfg

    def test_get_component_config_by_name(self):
        """get_component_config 按名查询"""
        _reload_configs()
        cfg = get_component_config("stock_advisor")
        assert cfg is not None
        assert isinstance(cfg, dict)

    def test_get_component_config_returns_none_for_unknown(self):
        """get_component_config 对未知组件返回 None"""
        _reload_configs()
        assert get_component_config("nonexistent") is None

    def test_load_component_configs_caches_result(self):
        """load_component_configs 缓存结果"""
        c1 = _reload_configs()
        c2 = load_component_configs()  # 不重置缓存
        assert c1 is c2, "load_component_configs 未缓存结果"


class TestComponentConfigMerge:
    """component_config 合并行为测试"""

    def test_get_merged_config_includes_main_config(self):
        """get_merged_config 含主 config.toml 配置"""
        _reload_configs()
        merged = get_merged_config()
        # 主 config.toml 应有某些字段（如 llm 或 server）
        assert isinstance(merged, dict)
        # 不应丢失主配置
        assert len(merged) > 0

    def test_get_merged_config_includes_component_config(self):
        """get_merged_config 含组件配置"""
        _reload_configs()
        merged = get_merged_config()
        assert "stock_advisor" in merged, "合并配置不含 stock_advisor"

    def test_get_merged_config_component_namespaced(self):
        """组件配置以 [component_name] 为 namespace 合并到顶层"""
        _reload_configs()
        merged = get_merged_config()
        # stock_advisor 配置应在顶层 key
        assert "stock_advisor" in merged
        # 值应是 dict
        assert isinstance(merged["stock_advisor"], dict)


class TestComponentConfigDeletionBehavior:
    """删除 workspace/<module>/ 后 component_config 行为"""

    def test_load_configs_handles_missing_workspace(self, tmp_path, monkeypatch):
        """workspace 目录不存在时返回空 dict 不报错"""
        from lib import component_manifest
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", tmp_path / "workspace")
        component_manifest.reset_cache()
        reset_cache()
        configs = load_component_configs()
        assert configs == {}

    def test_load_configs_skips_invalid_schema(self, tmp_path, monkeypatch):
        """config_schema 解析失败时跳过该组件不阻断其他组件"""
        from lib import component_manifest

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()

        # 组件 A：无效 toml
        comp_a = ws_dir / "comp_a"
        comp_a.mkdir()
        (comp_a / "manifest.toml").write_text("""
[component]
name = "comp_a"
version = "0.1.0"
description = "组件 A"

[config_schema]
file = "config_schema.toml"
""", encoding="utf-8")
        (comp_a / "config_schema.toml").write_text("invalid toml = = =", encoding="utf-8")

        # 组件 B：有效 toml
        comp_b = ws_dir / "comp_b"
        comp_b.mkdir()
        (comp_b / "manifest.toml").write_text("""
[component]
name = "comp_b"
version = "0.1.0"
description = "组件 B"

[config_schema]
file = "config_schema.toml"
""", encoding="utf-8")
        (comp_b / "config_schema.toml").write_text("""
[comp_b]
enabled = true
field = "value"
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        component_manifest.reset_cache()
        reset_cache()
        configs = load_component_configs()
        # comp_a 跳过，comp_b 加载
        assert "comp_b" in configs
        assert "comp_a" not in configs

    def test_load_configs_includes_disabled_component(self, tmp_path, monkeypatch):
        """enabled=false 时配置仍合并（供其他组件引用）"""
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

[config_schema]
file = "config_schema.toml"
""", encoding="utf-8")
        (comp / "config_schema.toml").write_text("""
[disabled_comp]
field = "value"
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        component_manifest.reset_cache()
        reset_cache()
        configs = load_component_configs()
        # enabled=false 仍加载配置
        assert "disabled_comp" in configs
