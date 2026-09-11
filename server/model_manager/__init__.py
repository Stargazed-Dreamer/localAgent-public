"""Model Lifecycle Manager（模型存活管理器）

统一负责后端进程内本地模型（PaddleOCR / 记忆 embedding / guide embedding /
MindForge 搜索引擎）的加载准入、压力逐出与手动控制，动态平衡显存/内存占用。

设计真源：temp/sdd/model-lifecycle-manager/design.md v2
- 管理器是决策权威，模块是执行者（模块入口零改动，加载前调 admit()）
- 主动策略首期仅开 GPU（VRAM）；CPU 监控实现但默认关闭
- 热路径模型（两个 embedding）默认钉住（evictable=False）

用法：
    from server.model_manager import get_model_manager
    manager = get_model_manager()
    manager.registry.register(OcrDriver())     # startup 时注册
    await manager.start()                      # 注册完成后启动监控
"""

from __future__ import annotations

from server.model_manager.manager import ModelLifecycleManager
from server.model_manager.registry import ModelRegistry
from server.model_manager.types import (
    LoadFailureError,
    ModelDriver,
    ModelUnavailableError,
    PressureState,
)

__all__ = [
    "ModelLifecycleManager",
    "ModelRegistry",
    "ModelDriver",
    "ModelUnavailableError",
    "LoadFailureError",
    "PressureState",
    "get_model_manager",
    "reset_model_manager",
]

_instance: ModelLifecycleManager | None = None


def get_model_manager() -> ModelLifecycleManager:
    """进程内单例（懒创建；首次调用读取 [model_manager] 配置）"""
    global _instance
    if _instance is None:
        _instance = ModelLifecycleManager()
    return _instance


def reset_model_manager() -> None:
    """重置单例（仅测试与 shutdown 使用）"""
    global _instance
    _instance = None
