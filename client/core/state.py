"""客户端状态持久化（基于 QSettings）

存储：窗口大小/位置、最近面板 ID、"显示全部工具"开关等。
"""

from typing import Optional

from PySide6.QtCore import QByteArray, QSize


class AppState:
    """应用状态持久化（单例，依赖 QApplication 已创建）"""
    _instance: Optional["AppState"] = None

    def __init__(self):
        from PySide6.QtCore import QSettings
        self._settings = QSettings("LocalAgent", "Client")

    @classmethod
    def instance(cls) -> "AppState":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # —— 窗口几何 ——

    def save_window_geometry(self, geo: QByteArray) -> None:
        self._settings.setValue("ui/window_geometry", geo)

    def load_window_geometry(self) -> QByteArray:
        v = self._settings.value("ui/window_geometry")
        if isinstance(v, QByteArray):
            return v
        if isinstance(v, str):
            return QByteArray(v.encode("iso-8859-1"))
        return QByteArray()

    def save_window_size(self, size: QSize) -> None:
        self._settings.setValue("ui/window_size", [size.width(), size.height()])

    def load_window_size(self) -> QSize | None:
        v = self._settings.value("ui/window_size")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return QSize(int(v[0]), int(v[1]))
        return None

    # —— 最近面板 ——

    def save_last_panel(self, panel_id: str) -> None:
        self._settings.setValue("ui/last_panel", panel_id)

    def load_last_panel(self) -> str | None:
        v = self._settings.value("ui/last_panel")
        return v if isinstance(v, str) else None

    # —— 工具面板设置 ——

    def save_show_all_tools(self, show: bool) -> None:
        self._settings.setValue("tools/show_all", bool(show))

    def load_show_all_tools(self) -> bool:
        v = self._settings.value("tools/show_all", False)
        return bool(v) if v is not None else False

    # —— Keys 面板设置 ——

    def save_keys_unmasked(self, unmasked: bool) -> None:
        self._settings.setValue("keys/unmasked", bool(unmasked))

    def load_keys_unmasked(self) -> bool:
        v = self._settings.value("keys/unmasked", False)
        return bool(v) if v is not None else False
