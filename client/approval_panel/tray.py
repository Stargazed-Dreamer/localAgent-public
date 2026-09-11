"""托盘图标 + 闪烁提醒。

行为：
- 新审批到达 → start_alert()：托盘图标 500ms 切换（普通/警示），持续闪烁
- 面板被激活 → stop_alert()：停止闪烁，恢复普通图标

托盘菜单：
- 显示面板（双击托盘图标也触发）
- 切换"最小化到托盘"（show_in_tray）
- 退出

注意：QSystemTrayIcon 需要在 QApplication 存在后才能创建。
"""
from __future__ import annotations

from typing import cast

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from client.approval_panel import settings as panel_settings

_FLASH_INTERVAL_MS = 500  # spec: 500ms 切换


class TrayIcon(QObject):
    """托盘图标 + 闪烁提醒控制器。

    信号：
        show_panel_requested()       — 用户想显示面板（双击托盘/菜单触发）
        quit_requested()             — 用户从托盘菜单退出
        show_in_tray_changed(bool)   — 用户切换了 show_in_tray 设置
    """

    show_panel_requested = Signal()
    quit_requested = Signal()
    show_in_tray_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._app = cast(QApplication, QApplication.instance())
        self._normal_icon = self._load_normal_icon()
        self._alert_icon = self._load_alert_icon()

        self._tray = QSystemTrayIcon(self._normal_icon, parent=self)
        self._tray.setToolTip("LocalAgent 审批面板")
        self._build_menu()
        self._tray.activated.connect(self._on_activated)

        # 闪烁定时器
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(_FLASH_INTERVAL_MS)
        self._flash_timer.timeout.connect(self._toggle_flash_icon)
        self._is_alerting = False
        self._flash_state = False  # False=normal, True=alert

    # ========== 生命周期 ==========

    def show(self) -> None:
        """显示托盘图标（仅当 show_in_tray=True 或系统支持时）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    # ========== 提醒控制 ==========

    def start_alert(self) -> None:
        """开始闪烁（新审批到达）。"""
        if not self._tray.isVisible():
            return
        self._is_alerting = True
        if not self._flash_timer.isActive():
            self._flash_timer.start()

    def stop_alert(self) -> None:
        """停止闪烁，恢复普通图标（面板被激活）。"""
        self._is_alerting = False
        if self._flash_timer.isActive():
            self._flash_timer.stop()
        self._flash_state = False
        self._tray.setIcon(self._normal_icon)

    # ========== 内部回调 ==========

    def _on_activated(self, reason) -> None:
        # 双击托盘图标 → 显示面板
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_panel_requested.emit()

    def _toggle_flash_icon(self) -> None:
        """500ms 切换图标。"""
        self._flash_state = not self._flash_state
        self._tray.setIcon(self._alert_icon if self._flash_state else self._normal_icon)

    def _build_menu(self) -> None:
        menu = QMenu()

        act_show = QAction("显示面板", menu)
        act_show.triggered.connect(self.show_panel_requested.emit)
        menu.addAction(act_show)

        menu.addSeparator()

        self._act_tray = QAction("最小化到托盘", menu, checkable=True)
        self._act_tray.setChecked(panel_settings.get_show_in_tray())
        self._act_tray.triggered.connect(self._on_toggle_tray)
        menu.addAction(self._act_tray)

        menu.addSeparator()

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(act_quit)

        self._tray.setContextMenu(menu)

    def _on_toggle_tray(self, checked: bool) -> None:
        panel_settings.set_show_in_tray(checked)
        self.show_in_tray_changed.emit(checked)

    # ========== 图标加载 ==========

    def _load_normal_icon(self) -> QIcon:
        """普通图标（绿色圆点）。"""
        style = self._app.style() if self._app else None
        if style is not None:
            return style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        return QIcon()

    def _load_alert_icon(self) -> QIcon:
        """警示图标（黄色警告）。"""
        style = self._app.style() if self._app else None
        if style is not None:
            return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        return QIcon()

    # ========== 公共方法 ==========

    @property
    def is_visible(self) -> bool:
        return self._tray.isVisible()
