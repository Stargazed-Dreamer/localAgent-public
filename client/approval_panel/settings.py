"""QSettings 读写：show_in_tray + window_geometry。

独立面板进程自己的配置，不写入 config.toml（决策 8）。
注册表位置：HKEY_CURRENT_USER\\Software\\LocalAgent\\ApprovalPanel
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

_SETTINGS_ORG = "LocalAgent"
_SETTINGS_APP = "ApprovalPanel"

# 默认值
DEFAULT_SHOW_IN_TRAY = True


def get_settings() -> QSettings:
    """获取 QSettings 实例（HKCU\\Software\\LocalAgent\\ApprovalPanel）。"""
    return QSettings(QSettings.Scope.UserScope, _SETTINGS_ORG, _SETTINGS_APP)


def get_show_in_tray() -> bool:
    """是否展示在托盘（关窗时最小化到托盘而非退出）。"""
    s = get_settings()
    return bool(s.value("show_in_tray", DEFAULT_SHOW_IN_TRAY, type=bool))


def set_show_in_tray(value: bool) -> None:
    s = get_settings()
    s.setValue("show_in_tray", value)


def get_window_geometry() -> QByteArray | None:
    """读取上次窗口位置/大小。"""
    s = get_settings()
    geo = s.value("window_geometry")
    if isinstance(geo, (QByteArray, bytes)):
        return QByteArray(geo) if isinstance(geo, bytes) else geo
    return None


def save_window_geometry(geo: QByteArray) -> None:
    s = get_settings()
    s.setValue("window_geometry", geo)
