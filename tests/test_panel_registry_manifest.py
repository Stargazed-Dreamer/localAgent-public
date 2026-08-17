"""Ticket 04：PanelRegistry 接入 manifest 测试

验证 PanelRegistry.discover() 双轨策略：
- 从 client/panels/ 扫描主代码库面板（向后兼容）
- 从 manifest 声明的 workspace 组件面板加载
- 删除 workspace/<module>/ 后 manifest 消失，面板自动注销
"""

from __future__ import annotations

import sys

from client.core.panel_base import PanelBase
from client.core.panel_registry import PanelRegistry


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


class TestPanelRegistryManifestLoading:
    """PanelRegistry manifest 加载行为测试"""

    def setup_method(self):
        """每个测试前重置 manifest 缓存，避免前序测试 monkeypatch 污染"""
        from server.component_manifest import reset_cache
        reset_cache()
        PanelRegistry.clear()

    def test_discover_finds_stock_advisor_panel(self):
        """discover 应发现 stock_advisor 面板"""
        PanelRegistry.clear()
        PanelRegistry.discover()
        panel = PanelRegistry.get_by_id("stock_advisor")
        assert panel is not None, "stock_advisor 面板未发现"
        assert panel.__name__ == "StockAdvisorPanel"

    def test_discover_returns_count(self):
        """discover 返回新发现的面板数量"""
        PanelRegistry.clear()
        count = PanelRegistry.discover()
        assert count > 0

    def test_stock_advisor_panel_is_panel_base_subclass(self):
        """stock_advisor 面板是 PanelBase 子类"""
        PanelRegistry.clear()
        PanelRegistry.discover()
        panel = PanelRegistry.get_by_id("stock_advisor")
        assert panel is not None
        assert issubclass(panel, PanelBase)

    def test_stock_advisor_panel_meta(self):
        """stock_advisor 面板元数据正确"""
        PanelRegistry.clear()
        PanelRegistry.discover()
        panel = PanelRegistry.get_by_id("stock_advisor")
        meta = panel.meta()
        assert meta.id == "stock_advisor"
        assert meta.title == "股市助手"
        assert meta.category == "main"
        assert meta.order == 40

    def test_get_all_sorted_includes_stock_advisor(self):
        """get_all_sorted 包含 stock_advisor"""
        PanelRegistry.clear()
        PanelRegistry.discover()
        panels = PanelRegistry.get_all_sorted()
        ids = [p.meta().id for p in panels]
        assert "stock_advisor" in ids

    def test_discover_idempotent(self):
        """重复 discover 不重复注册同一面板"""
        PanelRegistry.clear()
        PanelRegistry.discover()
        count1 = len(PanelRegistry._classes)
        PanelRegistry.discover()  # 第二次 discover
        count2 = len(PanelRegistry._classes)
        assert count1 == count2, "重复 discover 导致面板重复注册"


class TestPanelRegistryDeletionBehavior:
    """删除 workspace/<module>/ 后 PanelRegistry 行为"""

    def test_discover_skips_component_without_manifest(self, tmp_path, monkeypatch):
        """manifest 加载失败的组件面板被跳过，不阻断其他面板发现"""
        from lib import component_manifest

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")

        # 组件 A：有 manifest 但 panel.py 无效（类名不匹配）
        comp_a = ws_dir / "comp_a"
        comp_a.mkdir()
        (comp_a / "__init__.py").write_text("", encoding="utf-8")
        (comp_a / "manifest.toml").write_text("""
[component]
name = "comp_a"
version = "0.1.0"
description = "组件 A"

[client_panel]
file = "panel.py"
class = "NonExistentPanel"
category = "tools"
order = 100
""", encoding="utf-8")
        (comp_a / "panel.py").write_text("# 空 panel.py，无任何类定义", encoding="utf-8")

        # 组件 B：有 manifest 且 panel.py 有效
        comp_b = ws_dir / "comp_b"
        comp_b.mkdir()
        (comp_b / "__init__.py").write_text("", encoding="utf-8")
        (comp_b / "manifest.toml").write_text("""
[component]
name = "comp_b"
version = "0.1.0"
description = "组件 B"

[client_panel]
file = "panel.py"
class = "CompBPanel"
category = "tools"
order = 200
""", encoding="utf-8")
        (comp_b / "panel.py").write_text("""
from client.core.panel_base import PanelBase, PanelMeta

class CompBPanel(PanelBase):
    PANEL_META = PanelMeta(
        id="comp_b",
        title="组件 B",
        icon="🧪",
        order=200,
        category="tools",
        requires_backend=False,
    )
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        component_manifest.reset_cache()

        # 快照真实 workspace.* 模块，避免被临时替换污染后续测试
        ws_snapshot = _snapshot_workspace_modules()
        sys.modules["workspace"] = type(sys)("workspace")
        sys.modules["workspace"].__path__ = [str(ws_dir)]

        PanelRegistry.clear()
        # 只从 manifest 加载（不扫 client/panels/，因为临时目录无此包）
        try:
            PanelRegistry._discover_from_manifest()
            # comp_a 跳过，comp_b 加载
            assert PanelRegistry.get_by_id("comp_b") is not None
            assert PanelRegistry.get_by_id("comp_a") is None
        finally:
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()
            PanelRegistry.clear()

    def test_discover_skips_disabled_component(self, tmp_path, monkeypatch):
        """manifest enabled=false 时面板不注册"""
        from lib import component_manifest

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")

        comp = ws_dir / "disabled_comp"
        comp.mkdir()
        (comp / "__init__.py").write_text("", encoding="utf-8")
        (comp / "manifest.toml").write_text("""
[component]
name = "disabled_comp"
version = "0.1.0"
description = "禁用组件"
enabled = false

[client_panel]
file = "panel.py"
class = "DisabledPanel"
category = "tools"
order = 100
""", encoding="utf-8")
        (comp / "panel.py").write_text("""
from client.core.panel_base import PanelBase, PanelMeta

class DisabledPanel(PanelBase):
    PANEL_META = PanelMeta(
        id="disabled_comp",
        title="禁用",
        icon="🚫",
        order=100,
        category="tools",
        requires_backend=False,
    )
""", encoding="utf-8")

        monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
        monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
        component_manifest.reset_cache()

        # 快照真实 workspace.* 模块，避免被临时替换污染后续测试
        ws_snapshot = _snapshot_workspace_modules()
        sys.modules["workspace"] = type(sys)("workspace")
        sys.modules["workspace"].__path__ = [str(ws_dir)]

        PanelRegistry.clear()
        try:
            PanelRegistry._discover_from_manifest()
            assert PanelRegistry.get_by_id("disabled_comp") is None, "enabled=false 组件面板不应注册"
        finally:
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()
            PanelRegistry.clear()
