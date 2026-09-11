"""Model Lifecycle Manager — 驱动注册表（design §4）"""

from __future__ import annotations

import logging

from server.model_manager.types import ModelDriver

logger = logging.getLogger("localagent.model_manager.registry")


class ModelRegistry:
    """model_id -> ModelDriver 的注册表

    注册发生在后端 startup（管理器在全部注册后才启动监控循环，
    避免逐出未知模型，见 design §13）。
    """

    def __init__(self):
        self._drivers: dict[str, ModelDriver] = {}

    def register(self, driver: ModelDriver) -> None:
        """注册驱动；重复注册同名模型抛 ValueError（配置错误必须显式暴露）"""
        if driver.model_id in self._drivers:
            raise ValueError(f"model_id 重复注册: {driver.model_id}")
        self._drivers[driver.model_id] = driver
        logger.info(
            f"模型驱动已注册: {driver.model_id} resource={driver.resource} "
            f"footprint≈{driver.footprint_mb}MB evictable={driver.evictable}"
        )

    def get(self, model_id: str) -> ModelDriver | None:
        return self._drivers.get(model_id)

    def all(self) -> list[ModelDriver]:
        return list(self._drivers.values())

    def of_resource(self, resource: str) -> list[ModelDriver]:
        return [d for d in self._drivers.values() if d.resource == resource]

    def __len__(self) -> int:
        return len(self._drivers)
