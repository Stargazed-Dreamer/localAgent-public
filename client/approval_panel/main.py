"""审批面板进程入口。

QApplication 启动 → 单实例锁 → 加载 QSettings → 启动心跳和轮询 → 显示主窗口。
不抢焦点（启动时不激活窗口，任务栏出现即可）。

Ticket 06：
- 创建 TrayIcon，连接信号
- poller.new_arrivals → tray.start_alert + taskbar 闪 5 次
- panel.window_activated → tray.stop_alert + stop taskbar flash
- tray.show_panel_requested → panel.show + activate
- tray.quit_requested → app.quit
- 单实例锁：第二个实例启动时激活已有窗口并退出
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from client.approval_panel.heartbeat import HeartbeatWorker
from client.approval_panel.panel import ApprovalPanel
from client.approval_panel.poller import Poller
from client.approval_panel.settings import get_show_in_tray
from client.approval_panel.taskbar_flash import flash_window_5_times, stop_flash
from client.approval_panel.tray import TrayIcon

# 单实例锁 key（QSharedMemory）
_SINGLE_INSTANCE_KEY = "LocalAgent_ApprovalPanel"


def _acquire_single_instance(app: QApplication) -> bool:
    """尝试获取单实例锁。已有实例运行时返回 False。"""
    from PySide6.QtCore import QSharedMemory

    mem = QSharedMemory(_SINGLE_INSTANCE_KEY, app)
    # attach 一次：如果已存在同名共享内存，说明已有实例运行
    if mem.attach():
        mem.detach()
        return False
    # 创建 1 字节共享内存作为锁
    if not mem.create(1):
        return False
    # mem 绑定到 app 生命周期，退出时自动释放
    app.setProperty("_single_instance_mem", mem)
    return True


def _activate_existing_panel() -> None:
    """第二个实例启动时，尝试激活已有面板窗口。

    通过枚举顶层窗口找到标题为 "LocalAgent 审批面板" 的窗口并前置。
    简单实现：用 Win32 API 枚举窗口（Windows 平台）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        target_hwnd = ctypes.wintypes.HWND()

        # EnumWindows 回调：找到标题匹配的窗口
        def _callback(hwnd, _lparam):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == "LocalAgent 审批面板":
                    nonlocal target_hwnd
                    target_hwnd = hwnd
                    return False  # 停止枚举
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        ctypes.windll.user32.EnumWindows(
            EnumWindowsProc(_callback), 0
        )

        if target_hwnd:
            # 前置窗口
            ctypes.windll.user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(target_hwnd)
    except Exception:
        pass  # 激活失败不影响第二个实例退出


def main() -> int:
    # 不抢焦点：QApplication 前 set Qt::AA_DisableWindowContextHelpButton
    # 启动后窗口通过 WindowDoesNotAcceptFocus 不激活
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("LocalAgent 审批面板")
    app.setOrganizationName("LocalAgent")
    # 关闭最后一个窗口时不退出（托盘模式需要）
    app.setQuitOnLastWindowClosed(False)

    # 单实例锁：已有实例运行时激活已有窗口并退出
    if not _acquire_single_instance(app):
        print("审批面板已在运行，激活已有窗口。", file=sys.stderr)
        _activate_existing_panel()
        return 0

    panel = ApprovalPanel()
    panel.show()

    # 托盘图标（仅 show_in_tray=True 时显示）
    tray = TrayIcon(parent=app)
    if get_show_in_tray():
        tray.show()

    # 心跳 worker
    heartbeat = HeartbeatWorker(parent=app)
    heartbeat.heartbeat_ok.connect(panel.on_heartbeat_ok)
    heartbeat.heartbeat_fail.connect(panel.on_heartbeat_fail)
    heartbeat.start()

    # 轮询 worker
    poller = Poller(parent=app)
    poller.pending_updated.connect(panel.on_pending_updated)
    poller.poll_fail.connect(panel.on_poll_fail)
    # 新审批到达 → 触发托盘闪烁 + 任务栏闪 5 次
    poller.new_arrivals.connect(
        lambda new_ids: _on_new_arrivals(panel, tray, new_ids)
    )
    poller.start()

    # 窗口激活 → 停止托盘闪烁 + 取消任务栏高亮
    panel.window_activated.connect(
        lambda: _on_panel_activated(panel, tray)
    )

    # 托盘菜单：显示面板
    tray.show_panel_requested.connect(
        lambda: _show_panel(panel)
    )
    # 托盘菜单：退出
    tray.quit_requested.connect(app.quit)
    # 托盘菜单：切换 show_in_tray
    tray.show_in_tray_changed.connect(
        lambda checked: _on_show_in_tray_changed(tray, checked)
    )

    # 绑定到 app 生命周期
    app.setProperty("_panel", panel)
    app.setProperty("_tray", tray)
    app.setProperty("_heartbeat", heartbeat)
    app.setProperty("_poller", poller)

    return app.exec()


# ========== 事件处理函数 ==========


def _on_new_arrivals(panel: ApprovalPanel, tray: TrayIcon, new_ids: list) -> None:
    """新审批到达：触发托盘闪烁 + 任务栏闪 5 次。"""
    tray.start_alert()
    # 任务栏闪 5 次（用面板窗口句柄）
    hwnd = int(panel.winId()) if panel.isVisible() else 0
    if hwnd:
        flash_window_5_times(hwnd)


def _on_panel_activated(panel: ApprovalPanel, tray: TrayIcon) -> None:
    """面板被激活：停止托盘闪烁 + 取消任务栏高亮。"""
    tray.stop_alert()
    hwnd = int(panel.winId())
    if hwnd:
        stop_flash(hwnd)


def _show_panel(panel: ApprovalPanel) -> None:
    """从托盘显示面板：show + 激活 + 前置。"""
    panel.show()
    panel.showNormal()  # 从最小化恢复
    panel.raise_()
    panel.activateWindow()


def _on_show_in_tray_changed(tray: TrayIcon, checked: bool) -> None:
    """切换 show_in_tray 设置：显示/隐藏托盘图标。"""
    if checked:
        tray.show()
    else:
        tray.hide()


if __name__ == "__main__":
    raise SystemExit(main())
