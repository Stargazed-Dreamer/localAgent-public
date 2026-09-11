"""组件化 Manifest 系统 - 兼容性重导出层（ADR-0027）

实际逻辑已迁移到 lib/component_manifest.py。此文件保留向后兼容现有
`from server.component_manifest import ...` 调用（35 处 import 跨 server/tests/tools）。

新代码请直接从 lib.component_manifest 导入。client 已改为从 lib 导入
（client/core/panel_registry.py），不再依赖 server 模块。

ADR-0027 决策：物理隔离 + 单一真源。server 和 client 都从 lib 读 manifest，
消除 client→server 直接依赖，改善 release profile 分层。

--- 可变模块级变量的动态解析（重要） ---

`_PROJECT_ROOT` / `_WORKSPACE_DIR` / `_manifests_cache` / `_cache_lock` 是 lib 模块的
可变模块级变量，测试通过 monkeypatch.setattr(lib.component_manifest, "_WORKSPACE_DIR", ...)
动态替换。若用静态 `from lib.component_manifest import _WORKSPACE_DIR` re-export，
shim 命名空间会留下 import 时的 Path 对象引用，monkeypatch lib 后 shim 仍是旧值，
导致 server/agent_guide.py 和 server/activity_tracker/loop_manager.py 的 legacy 回退
扫描读到过期路径（曾引发 6 个 manifest 优先级/删除行为测试失败）。

故这 4 个变量不静态 re-export，改用 PEP 562 模块级 __getattr__ 动态解析到 lib 当前值。
其他导出（dataclasses、load_manifests 等函数）是稳定引用，静态 re-export 无此问题——
函数体在 lib 中定义，引用 lib 的全局变量，monkeypatch lib 后函数调用自动生效。
"""

import lib.component_manifest as _lib_cm
from lib.component_manifest import (  # noqa: F401
    # dataclasses（不可变，静态 re-export 安全）
    AgentGuideEntry,
    BackupEntry,
    ClientPanelEntry,
    ConfigSchemaEntry,
    HealthCheckEntry,
    LlmUseCaseEntry,
    LoopTasksEntry,
    Manifest,
    ReleaseEntry,
    SkillEntry,
    WatchEntry,
    # 加载/查询函数（函数体在 lib 中定义，引用 lib 全局，monkeypatch lib 生效）
    _parse_manifest,
    _scan_workspace,
    get_component,
    get_components_by_capability,
    load_manifests,
    reset_cache,
)

# 可变模块级变量：测试 monkeypatch lib 后需读到当前值，不能用静态 re-export 的快照。
# __getattr__ 在属性不在模块 __dict__ 时调用，每次访问都从 lib 读最新值。
_DYNAMIC_ATTRS = frozenset({
    "_PROJECT_ROOT",
    "_WORKSPACE_DIR",
    "_manifests_cache",
    "_cache_lock",
})


def __getattr__(name: str):
    """动态解析可变模块级变量到 lib 当前值。

    触发场景：`from server.component_manifest import _WORKSPACE_DIR` 或
    `server.component_manifest._WORKSPACE_DIR` 访问时，因未静态 re-export，
    走 __getattr__ 读 lib 当前值，确保 monkeypatch lib 后 shim 同步生效。
    """
    if name in _DYNAMIC_ATTRS:
        return getattr(_lib_cm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """让 dir(server.component_manifest) 包含动态属性，便于 IDE 补全和调试。"""
    module_attrs = list(globals().keys())
    return sorted(set(module_attrs) | set(_DYNAMIC_ATTRS))
