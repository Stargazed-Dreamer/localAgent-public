"""Ticket 02：agent_guide 路由接入 manifest 测试

验证 _load_optional_guide_entries() 双轨策略：
- 优先从 manifest 加载（stock_advisor 通过 manifest 路径，Ticket 07 后）
- 回退旧机制扫 loop_actions.py（无 manifest 的组件仍可通过旧机制加载）
- manifest 加载的组件跳过旧机制，避免重复
"""

from __future__ import annotations

import sys

from server.agent_guide import GUIDE_REGISTRY, _load_optional_guide_entries


def _snapshot_workspace_modules():
    """快照当前 sys.modules 中所有 workspace.* 模块（含 workspace 本身）。

    用于测试 setup/teardown：测试需要临时替换 workspace 包以扫描 tmp_path 下的
    临时组件，但不能永久删除真实 workspace 子模块（否则后续测试 re-import 时
    会产生新的类对象，导致 isinstance 失败——曾引发 test_recorder_consumer_e2e
    的 VLCandidate 身份分裂 bug）。
    """
    snapshot = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name == "workspace" or mod_name.startswith("workspace."):
            snapshot[mod_name] = sys.modules.pop(mod_name)
    return snapshot


def _restore_workspace_modules(snapshot):
    """恢复快照中的 workspace.* 模块。

    先删除测试期间新增的 workspace.* 模块（如 workspace.comp_a），再恢复快照。
    """
    for mod_name in list(sys.modules.keys()):
        if mod_name == "workspace" or mod_name.startswith("workspace."):
            # 测试期间新增的临时模块，删除
            if mod_name not in snapshot:
                del sys.modules[mod_name]
    # 恢复快照
    sys.modules.update(snapshot)


class TestAgentGuideManifestLoading:
    """agent_guide manifest 加载行为测试"""

    def test_stock_advisor_loaded_into_guide_registry(self):
        """stock_advisor 的 GUIDE_REGISTRY_ENTRIES 应被合并到 GUIDE_REGISTRY"""
        # GUIDE_REGISTRY 在模块加载时 update 一次
        assert "recurring.stock_advisor" in GUIDE_REGISTRY, (
            "stock_advisor 条目未合并到 GUIDE_REGISTRY；"
            "可能 _load_optional_guide_entries 未读 manifest"
        )

    def test_stock_advisor_entry_has_required_fields(self):
        """stock_advisor 条目含必要字段"""
        entry = GUIDE_REGISTRY["recurring.stock_advisor"]
        assert entry["skill"] == "stock_advisor"
        assert entry["name"]
        assert entry["skill_file"]
        assert entry["keywords"]

    def test_load_optional_guide_entries_returns_dict(self):
        """_load_optional_guide_entries 返回 dict"""
        result = _load_optional_guide_entries()
        assert isinstance(result, dict)

    def test_load_optional_guide_entries_includes_stock_advisor(self):
        """_load_optional_guide_entries 包含 stock_advisor 条目（通过 manifest 加载）"""
        result = _load_optional_guide_entries()
        assert "recurring.stock_advisor" in result, (
            "stock_advisor 未加载；manifest 加载失败可能导致回归"
        )


class TestAgentGuideManifestPriority:
    """manifest 优先级测试：manifest 加载的组件跳过旧机制"""

    def test_manifest_priority_skips_legacy_for_same_component(self, tmp_path, monkeypatch):
        """manifest 声明的组件不重复走旧机制"""
        # 构造临时 workspace：含 manifest 的组件 + 不含 manifest 的组件
        from lib import component_manifest
        from server import agent_guide

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        # 临时 workspace 包 __init__.py 让 pkgutil 能扫
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")

        # 组件 A：有 manifest，agent_guide 入口指向 entries.py
        comp_a = ws_dir / "comp_a"
        comp_a.mkdir()
        (comp_a / "__init__.py").write_text("", encoding="utf-8")
        (comp_a / "manifest.toml").write_text("""
[component]
name = "comp_a"
version = "0.1.0"
description = "组件 A"

[agent_guide]
file = "entries.py"
entries_var = "GUIDE_REGISTRY_ENTRIES"
""", encoding="utf-8")
        (comp_a / "entries.py").write_text("""
GUIDE_REGISTRY_ENTRIES = {
    "recurring.comp_a": {
        "skill": "comp_a",
        "name": "组件 A (manifest)",
        "skill_file": "workspace/comp_a/SKILL.md",
        "keywords": ["comp_a"],
        "description": "manifest 路径加载",
    }
}
""", encoding="utf-8")
        # 旧机制 loop_actions.py 也声明 comp_a（应被跳过，避免覆盖 manifest 版本）
        (comp_a / "loop_actions.py").write_text("""
GUIDE_REGISTRY_ENTRIES = {
    "recurring.comp_a": {
        "skill": "comp_a_legacy",
        "name": "组件 A (legacy - should be skipped)",
        "skill_file": "legacy.md",
        "keywords": ["legacy"],
        "description": "旧机制 - 不应加载",
    }
}
""", encoding="utf-8")

        # 组件 B：无 manifest，仅 loop_actions.py（应通过旧机制加载）
        comp_b = ws_dir / "comp_b"
        comp_b.mkdir()
        (comp_b / "__init__.py").write_text("", encoding="utf-8")
        (comp_b / "loop_actions.py").write_text("""
GUIDE_REGISTRY_ENTRIES = {
    "recurring.comp_b": {
        "skill": "comp_b",
        "name": "组件 B (legacy)",
        "skill_file": "workspace/comp_b/SKILL.md",
        "keywords": ["comp_b"],
        "description": "旧机制加载",
    }
}
""", encoding="utf-8")

        # monkeypatch workspace 目录
        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        component_manifest.reset_cache()

        # 快照真实 workspace.* 模块，避免被临时替换污染后续测试
        ws_snapshot = _snapshot_workspace_modules()
        # 创建临时 workspace 包，让 pkgutil 扫到临时目录
        sys.modules["workspace"] = type(sys)("workspace")
        sys.modules["workspace"].__path__ = [str(ws_dir)]

        try:
            result = agent_guide._load_optional_guide_entries()
            # comp_a 应来自 manifest（name 是 "组件 A (manifest)" 不是 legacy）
            assert "recurring.comp_a" in result
            assert result["recurring.comp_a"]["name"] == "组件 A (manifest)", (
                "manifest 优先级失败：comp_a 应来自 manifest 而非旧机制"
            )
            # comp_b 应来自旧机制
            assert "recurring.comp_b" in result
            assert result["recurring.comp_b"]["name"] == "组件 B (legacy)"
        finally:
            # 恢复真实 workspace.* 模块（删除测试期间新增的临时模块）
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()


class TestAgentGuideDeletionBehavior:
    """删除 workspace/<module>/ 后 agent_guide 行为"""

    def test_load_without_any_manifest(self, tmp_path, monkeypatch):
        """workspace 目录无任何 manifest.toml 时仍能通过旧机制加载"""
        from lib import component_manifest
        from server import agent_guide

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")
        comp = ws_dir / "legacy_comp"
        comp.mkdir()
        (comp / "__init__.py").write_text("", encoding="utf-8")
        (comp / "loop_actions.py").write_text("""
GUIDE_REGISTRY_ENTRIES = {
    "recurring.legacy_comp": {
        "skill": "legacy_comp",
        "name": "Legacy 组件",
        "skill_file": "workspace/legacy_comp/SKILL.md",
        "keywords": ["legacy"],
        "description": "无 manifest，仅旧机制",
    }
}
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        component_manifest.reset_cache()

        # 快照真实 workspace.* 模块，避免被临时替换污染后续测试
        ws_snapshot = _snapshot_workspace_modules()
        sys.modules["workspace"] = type(sys)("workspace")
        sys.modules["workspace"].__path__ = [str(ws_dir)]

        try:
            result = agent_guide._load_optional_guide_entries()
            assert "recurring.legacy_comp" in result
        finally:
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()
