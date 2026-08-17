"""state_manager.py — 文件分类工具的状态持久化管理

实现"分类即已处理"机制：文件/文件夹被分类后标记已处理，后端扫描跳过。
同时持久化列宽、上次源目录、加载的分类配置，GUI 和后端共享同一 state.json。

state.json schema:
{
  "classified_files": {
    "/path/to/file.jpg": {"category": "图片", "timestamp": "2026-07-22T22:50:00"}
  },
  "column_widths": {"col_0": 40, "col_1": 350, ...},
  "last_source_dir": "E:/<data_drive>:\<system_data_root>/<data_drive>:/Downloads",
  "loaded_categories_config": "tools/file_classifier/categories.json"
}
"""

import json
import os
from datetime import datetime
from typing import Optional


class StateManager:
    """文件分类工具状态管理器

    GUI 和后端 <data_drive>:/DownloadscanAction 共享同一 state.json 文件。
    每次写操作立即持久化，确保跨实例/跨进程可见。
    """

    def __init__(self, state_path: str):
        """初始化状态管理器

        Args:
            state_path: state.json 文件路径
        """
        self.state_path = state_path
        self._state = self._load()

    def _load(self) -> dict:
        """从磁盘加载状态，文件不存在时返回默认 schema"""
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                # 损坏的 state.json 回退到默认，避免阻塞启动
                pass
        return self._default_state()

    @staticmethod
    def _default_state() -> dict:
        """返回默认状态 schema"""
        return {
            "classified_files": {},
            "column_widths": {},
            "column_order": [],
            "last_source_dir": "",
            "loaded_categories_config": "",
            "last_sample_path": "",
        }

    def _save(self) -> None:
        """持久化状态到磁盘"""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    # ==================== 分类状态 ====================

    def mark_classified(self, path: str, category: str) -> None:
        """标记文件/文件夹已分类（即已处理）

        Args:
            path: 文件/文件夹绝对路径
            category: 分类名称（如"图片""暂存"）
        """
        self._state["classified_files"][path] = {
            "category": category,
            "timestamp": datetime.now().isoformat(),
        }
        self._save()

    def reset_classification(self, path: str) -> None:
        """重置文件/文件夹的分类状态（让后端重新扫描）

        Args:
            path: 文件/文件夹绝对路径
        """
        self._state["classified_files"].pop(path, None)
        self._save()

    def rename_category_in_classified(self, old_name: str, new_name: str) -> int:
        """重命名 classified_files 中所有引用旧分类名的条目（ticket C：右键重命名分类）

        Args:
            old_name: 旧分类名
            new_name: 新分类名

        Returns:
            被更新的条目数量
        """
        updated = 0
        for meta in self._state["classified_files"].values():
            if meta.get("category") == old_name:
                meta["category"] = new_name
                updated += 1
        if updated:
            self._save()
        return updated

    def is_processed(self, path: str) -> bool:
        """检查文件/文件夹是否已处理（已分类）

        Args:
            path: 文件/文件夹绝对路径

        Returns:
            True 表示已处理，后端扫描应跳过
        """
        return path in self._state["classified_files"]

    def get_classification(self, path: str) -> Optional[dict]:
        """获取文件/文件夹的分类元数据

        Args:
            path: 文件/文件夹绝对路径

        Returns:
            {"category": ..., "timestamp": ...} 或 None（未分类）
        """
        return self._state["classified_files"].get(path)

    # ==================== 列宽记忆 ====================

    def get_column_widths(self) -> dict:
        """获取记忆的列宽配置"""
        return self._state.get("column_widths", {})

    def save_column_widths(self, widths: dict) -> None:
        """保存列宽配置

        Args:
            widths: 列名 → 宽度的映射，如 {"col_0": 40, "col_1": 350}
        """
        self._state["column_widths"] = widths
        self._save()

    # ==================== 列顺序记忆 ====================

    def get_column_order(self) -> list:
        """获取记忆的列视觉顺序（logical index 列表）"""
        return self._state.get("column_order", [])

    def save_column_order(self, order: list) -> None:
        """保存列视觉顺序

        Args:
            order: logical index 列表，如 [0, 2, 1, 3, ...] 表示
                   第0列在位置0、第2列在位置1、第1列在位置2...
        """
        self._state["column_order"] = order
        self._save()

    # ==================== 上次源目录 ====================

    def get_last_source_dir(self) -> str:
        """获取上次打开的源目录路径"""
        return self._state.get("last_source_dir", "")

    def save_last_source_dir(self, directory: str) -> None:
        """保存上次打开的源目录路径

        Args:
            directory: 源目录绝对路径
        """
        self._state["last_source_dir"] = directory
        self._save()

    # ==================== 分类配置路径 ====================

    def get_loaded_categories_config(self) -> str:
        """获取上次加载的分类配置文件路径"""
        return self._state.get("loaded_categories_config", "")

    def save_loaded_categories_config(self, config_path: str) -> None:
        """保存上次加载的分类配置文件路径

        Args:
            config_path: categories.json 路径
        """
        self._state["loaded_categories_config"] = config_path
        self._save()

    # ==================== 上次分类映射路径 ====================

    def get_last_sample_path(self) -> str:
        """获取上次加载/保存的分类映射文件路径"""
        return self._state.get("last_sample_path", "")

    def save_last_sample_path(self, path: str) -> None:
        """保存上次加载/保存的分类映射文件路径

        Args:
            path: 分类映射文件路径
        """
        self._state["last_sample_path"] = path
        self._save()

    # ==================== 暂存分类识别 ====================

    @staticmethod
    def is_staging_category(category: dict) -> bool:
        """判断是否是暂存分类（path 为空）

        暂存分类用于标记"长期放置但不移动"的文件，分类到暂存=已处理但不移动。

        Args:
            category: 分类配置 {"name", "path", "extensions"}

        Returns:
            True 表示是暂存分类（path 为空）
        """
        return not category.get("path", "")
