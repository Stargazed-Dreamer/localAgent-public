"""Ticket 03：loop_manager 接入 manifest 测试

验证 _load_optional_loop_defs() 双轨策略：
- 优先从 manifest 加载（stock_advisor 通过 manifest 路径，Ticket 07 后）
- 回退旧机制扫 loop_actions.py（无 manifest 的组件仍可通过旧机制加载）
- manifest 加载的组件跳过旧机制，避免重复
"""

from __future__ import annotations

import importlib
import sys

from server.activity_tracker import loop_manager


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


class TestLoopManagerManifestLoading:
    """loop_manager manifest 加载行为测试"""

    def setup_method(self):
        """每个测试前重置 manifest 缓存并 reload loop_manager，避免删除测试污染。

        test_component_e2e.py 中的删除测试用 monkeypatch + reload 模拟组件删除，
        monkeypatch 还原后 TASK_BUILDERS 仍是删除后的状态（不含 stock_advisor），
        需主动 reload 重新加载真实 workspace 的 manifest。
        通过 loop_manager.TASK_BUILDERS 访问（而非 from import）以拿到 reload 后的新引用。
        """
        from server.component_manifest import reset_cache
        reset_cache()
        importlib.reload(loop_manager)

    def test_stock_advisor_loaded_into_task_builders(self):
        """stock_advisor 的 LOOP_TASK_DEFS 应被合并到 TASK_BUILDERS"""
        assert "stock_advisor" in loop_manager.TASK_BUILDERS, (
            "stock_advisor 任务定义未合并到 TASK_BUILDERS；"
            "可能 _load_optional_loop_defs 未读 manifest"
        )

    def test_stock_advisor_has_loop_task_def(self):
        """stock_advisor 任务定义含 sub_id 字段"""
        defs = loop_manager.TASK_BUILDERS["stock_advisor"]
        assert isinstance(defs, list)
        assert len(defs) > 0
        assert "sub_id" in defs[0]

    def test_load_optional_loop_defs_returns_dict(self):
        """_load_optional_loop_defs 返回 dict"""
        result = loop_manager._load_optional_loop_defs()
        assert isinstance(result, dict)

    def test_load_optional_loop_defs_includes_stock_advisor(self):
        """_load_optional_loop_defs 包含 stock_advisor（通过 manifest 加载）"""
        result = loop_manager._load_optional_loop_defs()
        assert "stock_advisor" in result, (
            "stock_advisor 未加载；manifest 加载失败可能导致回归"
        )


class TestLoopManagerManifestPriority:
    """manifest 优先级测试：manifest 加载的组件跳过旧机制"""

    def test_manifest_priority_skips_legacy_for_same_component(self, tmp_path, monkeypatch):
        """manifest 声明的组件不重复走旧机制"""
        from lib import component_manifest
        from server.activity_tracker import loop_manager

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")

        # 组件 A：有 manifest，loop_tasks 入口指向 tasks.py
        comp_a = ws_dir / "comp_a"
        comp_a.mkdir()
        (comp_a / "__init__.py").write_text("", encoding="utf-8")
        (comp_a / "manifest.toml").write_text("""
[component]
name = "comp_a"
version = "0.1.0"
description = "组件 A"

[loop_tasks]
file = "tasks.py"
entries_var = "LOOP_TASK_DEFS"
""", encoding="utf-8")
        (comp_a / "tasks.py").write_text("""
LOOP_TASK_DEFS = {
    "comp_a": [
        {"sub_id": "manifest_path", "action": "noop", "trigger": lambda c, tz: None}
    ]
}
""", encoding="utf-8")
        # 旧机制 loop_actions.py 也声明 comp_a（应被跳过）
        (comp_a / "loop_actions.py").write_text("""
LOOP_TASK_DEFS = {
    "comp_a": [
        {"sub_id": "legacy_path_should_be_skipped", "action": "noop", "trigger": lambda c, tz: None}
    ]
}
""", encoding="utf-8")

        # 组件 B：无 manifest，仅 loop_actions.py
        comp_b = ws_dir / "comp_b"
        comp_b.mkdir()
        (comp_b / "__init__.py").write_text("", encoding="utf-8")
        (comp_b / "loop_actions.py").write_text("""
LOOP_TASK_DEFS = {
    "comp_b": [
        {"sub_id": "legacy_b", "action": "noop", "trigger": lambda c, tz: None}
    ]
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
            result = loop_manager._load_optional_loop_defs()
            # comp_a 应来自 manifest（sub_id 是 manifest_path）
            assert "comp_a" in result
            assert result["comp_a"][0]["sub_id"] == "manifest_path", (
                "manifest 优先级失败：comp_a 应来自 manifest 而非旧机制"
            )
            # comp_b 应来自旧机制
            assert "comp_b" in result
            assert result["comp_b"][0]["sub_id"] == "legacy_b"
        finally:
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()


class TestLoopManagerDeletionBehavior:
    """删除 workspace/<module>/ 后 loop_manager 行为"""

    def test_load_without_any_manifest(self, tmp_path, monkeypatch):
        """workspace 目录无任何 manifest.toml 时仍能通过旧机制加载"""
        from lib import component_manifest
        from server.activity_tracker import loop_manager

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "__init__.py").write_text("", encoding="utf-8")
        comp = ws_dir / "legacy_comp"
        comp.mkdir()
        (comp / "__init__.py").write_text("", encoding="utf-8")
        (comp / "loop_actions.py").write_text("""
LOOP_TASK_DEFS = {
    "legacy_comp": [
        {"sub_id": "legacy", "action": "noop", "trigger": lambda c, tz: None}
    ]
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
            result = loop_manager._load_optional_loop_defs()
            assert "legacy_comp" in result
        finally:
            _restore_workspace_modules(ws_snapshot)
            component_manifest.reset_cache()
