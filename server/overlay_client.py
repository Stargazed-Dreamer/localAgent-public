"""GUI子进程 - PySide6覆盖层、确认窗口、红框标注

PySide6必须在主线程运行QApplication，但FastAPI已占据主线程。
解决方案：在独立子进程中运行PySide6 GUI，通过multiprocessing.Queue通信。

架构（spec 决策 12/13/14/17/18/19）：
- OverlayWidget：持久横条（左圆点 + 文案 + 倒计时 + 红色按钮），无临时覆盖层
- StartConfirmDialog：normal 只有允许/拒绝；watchdog 含 QSpinBox(1-999h) + 允许关机复选框(默认勾选)
- OverlayClient 主进程侧 _tick_loop：1 秒读 SessionManager.status() 推 IPC tick
- OverlayClient 主进程侧 _recv_loop：处理子进程 release_request（按钮点击）+ 弹窗结果分发
"""

import base64
import logging
import multiprocessing
import threading
import time

from server.config import get_screen_config
from server.screen.session import SessionEvent, get_session_manager

logger = logging.getLogger("localagent.gui")


def _invalidate_health_cache() -> None:
    """Keep task authorization changes immediately visible through /health."""
    from server.core.health import invalidate_health_cache

    invalidate_health_cache()


def _create_start_confirm_dialog():
    """Create the user-visible takeover dialog on the calling GUI thread.

    spec 决策 17/19：agent 指定 mode，用户只能允许/拒绝，不能改模式。
    - normal 模式：只有 task_label + hotkey_label + feedback + 拒绝/允许
    - watchdog 模式：额外显示 QSpinBox(1-999h, 默认10) + 允许关机 QCheckBox(默认勾选)
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QSpinBox,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    from lib.ui import tokens
    from lib.ui.theme import set_kind, set_text_role

    class StartConfirmDialog(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog
            )
            self.setWindowTitle("Agent请求接管操作")
            self.setMinimumWidth(tokens.DIALOG_MIN_WIDTH)
            self.result_data = {
                "status": "cancelled",
                "reason": "",
                "task_authorization": False,
                "requested_mode": "normal",
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }

            layout = QVBoxLayout(self)

            title_label = QLabel("Agent请求接管键鼠操作")
            set_text_role(title_label, "heading")
            layout.addWidget(title_label)

            self.task_label = QLabel()
            self.task_label.setWordWrap(True)
            layout.addWidget(self.task_label)

            self.hotkey_label = QLabel()
            set_text_role(self.hotkey_label, "secondary")
            layout.addWidget(self.hotkey_label)

            # watchdog 模式专属控件（默认隐藏，setup 时按 requested_mode 显示）
            self.watchdog_group = QWidget()
            watchdog_layout = QVBoxLayout(self.watchdog_group)
            watchdog_layout.setContentsMargins(0, 4, 0, 4)

            duration_row = QHBoxLayout()
            duration_label = QLabel("看门狗时长(小时):")
            set_text_role(duration_label, "secondary")
            duration_row.addWidget(duration_label)
            self.duration_spinbox = QSpinBox()
            self.duration_spinbox.setRange(1, 999)
            self.duration_spinbox.setValue(10)
            self.duration_spinbox.setToolTip(
                "看门狗模式持续时长（1-999 小时）；到期后自动降级到普通执行模式"
            )
            duration_row.addWidget(self.duration_spinbox)
            duration_row.addStretch()
            watchdog_layout.addLayout(duration_row)

            self.shutdown_checkbox = QCheckBox("允许关机")
            self.shutdown_checkbox.setChecked(True)  # spec 决策 19：默认勾选
            self.shutdown_checkbox.setToolTip(
                "勾选后允许 agent 在本任务中调用关机；不勾选则关机端点返回 403"
            )
            watchdog_layout.addWidget(self.shutdown_checkbox)

            self.watchdog_help = QLabel(
                "看门狗模式：不因空闲撤销，到期降级到普通模式；危险操作仍拦截"
            )
            self.watchdog_help.setWordWrap(True)
            set_text_role(self.watchdog_help, "tertiary")
            watchdog_layout.addWidget(self.watchdog_help)

            self.watchdog_group.setVisible(False)
            layout.addWidget(self.watchdog_group)

            feedback_label = QLabel("反馈(可选):")
            set_text_role(feedback_label, "secondary")
            layout.addWidget(feedback_label)
            self.feedback_edit = QTextEdit()
            self.feedback_edit.setMaximumHeight(80)
            self.feedback_edit.setPlaceholderText(
                "如:同意，但请小心操作 / 拒绝，我现在在用电脑..."
            )
            layout.addWidget(self.feedback_edit)

            btn_layout = QHBoxLayout()
            cancel_btn = QPushButton("拒绝")
            set_kind(cancel_btn, "ghost")
            cancel_btn.setToolTip("拒绝本次接管请求")
            cancel_btn.clicked.connect(lambda: self._finish("cancelled"))
            btn_layout.addStretch()
            btn_layout.addWidget(cancel_btn)

            confirm_btn = QPushButton("允许接管")
            set_kind(confirm_btn, "primary")
            confirm_btn.setToolTip("允许本次接管")
            confirm_btn.setDefault(True)
            confirm_btn.clicked.connect(lambda: self._finish("confirmed"))
            btn_layout.addWidget(confirm_btn)
            layout.addLayout(btn_layout)

        def setup(
            self,
            task_description: str,
            hotkey_hint: str,
            allow_task_authorization: bool = True,
            requested_mode: str = "normal",
        ):
            """初始化弹窗并显示。

            Args:
                requested_mode: agent 请求的模式（"normal" | "watchdog"）。
                    用户只能允许/拒绝，不能改模式（spec 决策 17）。
                allow_task_authorization: 兼容旧签名，新设计忽略（授权总由 SessionManager 管）
            """
            self.result_data = {
                "status": "cancelled",
                "reason": "",
                "task_authorization": False,
                "requested_mode": requested_mode,
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
            self.feedback_edit.clear()
            self.duration_spinbox.setValue(10)
            self.shutdown_checkbox.setChecked(True)
            self.watchdog_group.setVisible(requested_mode == "watchdog")
            self.task_label.setText(f"任务: {task_description}")
            self.hotkey_label.setText(f"过程中可按 {hotkey_hint} 紧急停止")

        def _finish(self, status: str):
            requested_mode = self.result_data.get("requested_mode", "normal")
            is_watchdog = requested_mode == "watchdog"
            confirmed = status == "confirmed"
            self.result_data = {
                "status": status,
                "reason": self.feedback_edit.toPlainText().strip(),
                # 决策 17：用户只能允许/拒绝，authorized_mode 总 = requested_mode
                "task_authorization": confirmed,
                "requested_mode": requested_mode,
                # confirmed 时 authorized_mode = requested_mode；否则 normal（无授权）
                "authorized_mode": requested_mode if confirmed else "normal",
                "max_duration_hours": (
                    int(self.duration_spinbox.value()) if (confirmed and is_watchdog) else None
                ),
                "shutdown_permitted": (
                    bool(self.shutdown_checkbox.isChecked()) if (confirmed and is_watchdog) else False
                ),
            }
            self.hide()

    return StartConfirmDialog()


# ========== 子进程端：PySide6 GUI ==========

def _gui_subprocess(recv_queue: multiprocessing.Queue,
                    send_queue: multiprocessing.Queue):
    """PySide6 GUI子进程主函数"""
    import ctypes
    import ctypes.wintypes
    import os
    import signal
    import sys
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QFrame,
            QHBoxLayout,
            QLabel,
            QPushButton,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        send_queue.put({"type": "import_error", "error": f"PySide6 导入失败: {exc}"})
        return

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    from lib.ui.theme import apply_theme
    apply_theme(app)

    # ---- 父进程存活检测（防孤儿：主进程异常退出时子进程自动关闭）----
    _parent_pid = os.getppid()

    def _is_parent_alive():
        """检查父进程是否存活（Windows API）"""
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, _parent_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        exit_code = ctypes.wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE

    # ---- SIGINT handler：让 Ctrl+C 能退出 GUI（PySide6 默认吞掉 SIGINT）----
    signal.signal(signal.SIGINT, lambda *_: app.quit())

    # ---- 覆盖层窗口（spec 决策 12/14/18）----
    class OverlayWidget(QFrame):
        """持久横条：左圆点 + 文案 + 倒计时 + 红色按钮。

        颜色（决策 12/14）：
        - normal 正常（0-10min）：浅蓝圆点 + 浅蓝背景 + MM:SS
        - normal 告警（10-30min）：黄色圆点 + 黄色背景 + MM:SS
        - watchdog 全程：浅蓝圆点 + 浅蓝背景 + HH:MM:SS
        """

        def __init__(self):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowTransparentForInput  # 不拦截鼠标
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

            self._mode = "normal"
            self._phase = "normal"
            self._remaining_seconds = 0
            self._task_description = ""

            layout = QHBoxLayout(self)
            layout.setContentsMargins(15, 8, 15, 8)

            # 左：颜色圆点
            self.dot_label = QLabel()
            self.dot_label.setFixedSize(14, 14)
            self.dot_label.setStyleSheet("background-color: #2196F3; border-radius: 7px;")
            layout.addWidget(self.dot_label)

            # 左：任务文案
            self.label = QLabel()
            self.label.setStyleSheet("color: #000; font-size: 14px; font-weight: bold;")
            layout.addWidget(self.label, 1)  # stretch=1 占据中间空间

            # 右：倒计时
            self.countdown_label = QLabel()
            self.countdown_label.setStyleSheet("color: #000; font-size: 14px; font-weight: bold;")
            layout.addWidget(self.countdown_label)

            # 最右：红色停止按钮（决策 18：文字="Ctrl+`" + 点击撤销 + 始终红色）
            self.stop_btn = QPushButton("Ctrl+`")
            self.stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #f44336; color: white;
                    padding: 4px 12px; font-size: 12px;
                    border-radius: 4px; border: none;
                }
                QPushButton:hover { background-color: #da190b; }
            """)
            self.stop_btn.setToolTip("点击撤销授权（等同 Ctrl+` 紧急停止）")
            self.stop_btn.clicked.connect(self._on_stop_clicked)
            layout.addWidget(self.stop_btn)

            self._apply_phase_style()
            self._update_display()

        def _on_stop_clicked(self):
            """按钮点击 → 通知主进程撤销授权（决策 18）"""
            send_queue.put({"type": "release_request", "reason": "user_button"})

        def _apply_phase_style(self):
            """根据 phase 切换圆点 + 背景颜色"""
            if self._phase == "warning":
                dot_color = "#FFC107"  # 黄色
                bg = "rgba(255, 193, 7, 0.9)"
            else:
                dot_color = "#2196F3"  # 浅蓝
                bg = "rgba(135, 206, 235, 0.9)"
            self.dot_label.setStyleSheet(
                f"background-color: {dot_color}; border-radius: 7px;"
            )
            self.setStyleSheet(
                f"QFrame {{ background-color: {bg}; border-radius: 6px; }}"
            )

        def set_state(self, mode: str, phase: str, remaining_seconds: int, task_description: str):
            """更新 overlay 状态（由主进程 tick 消息驱动）"""
            self._mode = mode
            self._phase = phase
            self._remaining_seconds = max(0, int(remaining_seconds))
            self._task_description = task_description or ""
            self._apply_phase_style()
            self._update_display()

        def _update_display(self):
            """根据 mode/phase 更新文案 + 倒计时格式"""
            desc = self._task_description or "Agent操作中"
            self.label.setText(desc)
            # 倒计时格式：watchdog 用 HH:MM:SS；normal 用 MM:SS
            if self._mode == "watchdog":
                h = self._remaining_seconds // 3600
                m = (self._remaining_seconds % 3600) // 60
                s = self._remaining_seconds % 60
                self.countdown_label.setText(f"剩余 {h:02d}:{m:02d}:{s:02d}")
            else:
                m = self._remaining_seconds // 60
                s = self._remaining_seconds % 60
                self.countdown_label.setText(f"剩余 {m:02d}:{s:02d}")

        def show_at_top(self):
            screen = app.primaryScreen()
            if screen:
                geo = screen.geometry()
                self.resize(geo.width() - 20, 40)
                self.move(10, 5)
            self.show()

        def show_at_bottom(self):
            screen = app.primaryScreen()
            if screen:
                geo = screen.geometry()
                self.resize(geo.width() - 20, 40)
                self.move(10, geo.height() - 50)
            self.show()

    overlay = OverlayWidget()

    # ---- 确认窗口 ----
    class ConfirmDialog(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.Dialog
            )
            self.setWindowTitle("Agent操作确认")
            self.setMinimumWidth(500)
            self.result_data = {"status": "cancelled", "same_coords_skip": False}

            layout = QVBoxLayout(self)

            # 截图预览
            self.screenshot_label = QLabel()
            self.screenshot_label.setMaximumHeight(350)
            self.screenshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.screenshot_label.setStyleSheet("border: 1px solid #ccc; background: #f0f0f0;")
            layout.addWidget(self.screenshot_label)

            # 操作描述
            self.action_label = QLabel()
            self.action_label.setStyleSheet("font-size: 14px; padding: 8px;")
            layout.addWidget(self.action_label)

            # 跳过确认复选框
            self.skip_checkbox = QCheckBox("未来相同操作坐标不变时自动执行")
            self.skip_checkbox.setStyleSheet("padding: 4px;")
            layout.addWidget(self.skip_checkbox)

            # 用户反馈输入框(取消时可输入原因)
            feedback_label = QLabel("反馈(可选,取消时建议填写原因):")
            feedback_label.setStyleSheet("font-size: 12px; color: #666; padding: 2px;")
            layout.addWidget(feedback_label)
            self.feedback_edit = QTextEdit()
            self.feedback_edit.setMaximumHeight(80)
            self.feedback_edit.setPlaceholderText("如:坐标偏移、点错了、应该点另一个按钮...")
            self.feedback_edit.setStyleSheet("font-size: 13px; padding: 4px;")
            layout.addWidget(self.feedback_edit)

            # 按钮
            btn_layout = QHBoxLayout()
            self.confirm_btn = QPushButton("确认执行")
            self.confirm_btn.setStyleSheet("""
                QPushButton { background-color: #4CAF50; color: white; padding: 8px 24px; font-size: 14px; border-radius: 4px; }
                QPushButton:hover { background-color: #45a049; }
            """)
            self.confirm_btn.clicked.connect(self._on_confirm)
            btn_layout.addStretch()
            btn_layout.addWidget(self.confirm_btn)

            self.cancel_btn = QPushButton("取消")
            self.cancel_btn.setStyleSheet("""
                QPushButton { background-color: #f44336; color: white; padding: 8px 24px; font-size: 14px; border-radius: 4px; }
                QPushButton:hover { background-color: #da190b; }
            """)
            self.cancel_btn.clicked.connect(self._on_cancel)
            btn_layout.addWidget(self.cancel_btn)
            layout.addLayout(btn_layout)

        def setup(self, screenshot_b64: str, action: str,
                  x: int | None = None, y: int | None = None,
                  draw_x: int | None = None, draw_y: int | None = None,
                  text: str | None = None, keys: list | None = None):
            """设置确认窗口内容。

            x, y: 显示给用户的原始坐标
            draw_x, draw_y: 截图内画点坐标（双屏修正后）。None 时用 x, y。
            """
            self.result_data = {"status": "cancelled", "same_coords_skip": False, "reason": ""}
            self.skip_checkbox.setChecked(False)
            self.feedback_edit.clear()

            # 显示截图（带L形标记）
            if screenshot_b64:
                try:
                    img_bytes = base64.b64decode(screenshot_b64)
                    qimg = QImage()
                    qimg.loadFromData(img_bytes)
                    if not qimg.isNull():
                        # 在截图上画 L 形角标 + 中心红点
                        dx = draw_x if draw_x is not None else x
                        dy = draw_y if draw_y is not None else y
                        if dx is not None and dy is not None:
                            painter = QPainter(qimg)
                            pen = QPen(QColor(255, 0, 0), 2)
                            painter.setPen(pen)
                            arm = 20  # L形臂长
                            gap = 4   # L形离中心间隙
                            o = arm + gap
                            t = gap
                            # 四个 L 形角标（开口朝向中心，不遮挡点击中心）
                            # 左上
                            painter.drawLine(dx - o, dy - o, dx - o, dy - t)
                            painter.drawLine(dx - o, dy - o, dx - t, dy - o)
                            # 右上
                            painter.drawLine(dx + o, dy - o, dx + o, dy - t)
                            painter.drawLine(dx + o, dy - o, dx + t, dy - o)
                            # 左下
                            painter.drawLine(dx - o, dy + o, dx - o, dy + t)
                            painter.drawLine(dx - o, dy + o, dx - t, dy + o)
                            # 右下
                            painter.drawLine(dx + o, dy + o, dx + o, dy + t)
                            painter.drawLine(dx + o, dy + o, dx + t, dy + o)
                            # 中心小红点
                            painter.setBrush(QColor(255, 0, 0))
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.drawEllipse(dx - 3, dy - 3, 6, 6)
                            painter.end()

                        pixmap = QPixmap.fromImage(qimg)
                        scaled = pixmap.scaled(
                            self.screenshot_label.maximumWidth() - 20,
                            self.screenshot_label.maximumHeight() - 20,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        self.screenshot_label.setPixmap(scaled)
                except Exception as e:
                    self.screenshot_label.setText(f"截图加载失败: {e}")

            # 操作描述（显示原始坐标，非截图内坐标）
            desc = f"操作: {action}"
            if x is not None and y is not None:
                desc += f"  坐标: ({x}, {y})"
            if text:
                desc += f"\n文本: {text}"
            if keys:
                desc += f"\n快捷键: {'+'.join(keys)}"
            self.action_label.setText(desc)

        def _on_confirm(self):
            self.result_data = {
                "status": "confirmed",
                "same_coords_skip": self.skip_checkbox.isChecked(),
                "reason": self.feedback_edit.toPlainText().strip(),
            }
            self.hide()

        def _on_cancel(self):
            self.result_data = {
                "status": "cancelled",
                "same_coords_skip": False,
                "reason": self.feedback_edit.toPlainText().strip(),
            }
            self.hide()

    confirm_dialog = ConfirmDialog()

    # ---- 开始确认弹窗 ----
    start_confirm = _create_start_confirm_dialog()

    # ---- 消息处理定时器 ----
    _parent_check_counter = [0]

    def process_messages():
        """定期检查消息队列"""
        # 每 ~2 秒检查父进程是否存活（主进程异常退出时子进程自动关闭）
        _parent_check_counter[0] += 1
        if _parent_check_counter[0] >= 20:
            _parent_check_counter[0] = 0
            if not _is_parent_alive():
                logger.warning("父进程已退出，GUI子进程自动关闭")
                app.quit()
                return

        try:
            while not recv_queue.empty():
                try:
                    msg = recv_queue.get_nowait()
                except Exception:
                    break

                cmd = msg.get("cmd")

                if cmd == "overlay_show":
                    pos = msg.get("position", "top")
                    if pos == "bottom":
                        overlay.show_at_bottom()
                    else:
                        overlay.show_at_top()

                elif cmd == "overlay_hide":
                    overlay.hide()

                elif cmd == "tick":
                    # 主进程 1 秒推送的状态（T23）
                    overlay.set_state(
                        mode=msg.get("mode", "normal"),
                        phase=msg.get("phase", "normal"),
                        remaining_seconds=msg.get("remaining_seconds", 0),
                        task_description=msg.get("task_description", ""),
                    )

                elif cmd == "confirm_action":
                    confirm_dialog.setup(
                        screenshot_b64=msg.get("screenshot_b64"),
                        action=msg.get("action", ""),
                        x=msg.get("x"),
                        y=msg.get("y"),
                        draw_x=msg.get("draw_x"),
                        draw_y=msg.get("draw_y"),
                        text=msg.get("text"),
                        keys=msg.get("keys"),
                    )
                    confirm_dialog.show()
                    confirm_dialog.raise_()

                elif cmd == "confirm_start":
                    start_confirm.setup(
                        task_description=msg.get("task_description", ""),
                        hotkey_hint=msg.get("hotkey_hint", "Ctrl+`"),
                        allow_task_authorization=msg.get("allow_task_authorization", True),
                        requested_mode=msg.get("requested_mode", "normal"),
                    )
                    start_confirm.show()
                    start_confirm.raise_()

                elif cmd == "shutdown":
                    app.quit()
                    return

        except Exception as e:
            logger.debug(f"GUI消息处理错误: {e}")

        # 检查确认窗口是否已关闭（用户已操作）
        if confirm_dialog.isVisible() is False and confirm_dialog.result_data:
            send_queue.put({"type": "confirm_action_result", **confirm_dialog.result_data})
            confirm_dialog.result_data = None

        if start_confirm.isVisible() is False and start_confirm.result_data:
            send_queue.put({"type": "confirm_start_result", **start_confirm.result_data})
            start_confirm.result_data = None

    timer = QTimer()
    timer.timeout.connect(process_messages)
    timer.start(100)  # 100ms检查一次

    logger.info("GUI子进程已启动")
    app.exec()
    logger.info("GUI子进程已退出")


# ========== 主进程端：GUI客户端 ==========

class OverlayClient:
    """主进程端 overlay 客户端，通过 Queue 与子进程通信（仅负责渲染，不负责授权）。

    职责（spec 决策 12/13）：
    - IPC 通信（recv_queue → 子进程，send_queue ← 子进程）
    - 订阅 SessionManager 事件，驱动 overlay 显示/隐藏
    - _tick_loop：1 秒读 SessionManager.status() 推 IPC tick（决策 4 方案 A）
    - _recv_loop：处理子进程 release_request + 弹窗结果分发
    - 弹窗接口（confirm_action / confirm_start / ensure_takeover_approved）

    不持有授权状态（SessionManager 单例负责）。
    """

    def __init__(self):
        self._recv_queue = multiprocessing.Queue()
        self._send_queue = multiprocessing.Queue()
        self._process: multiprocessing.Process | None = None
        self._overlay_visible = False
        self._started = False
        # T23: 1 秒 tick 循环（推 IPC tick 到子进程刷新倒计时）
        self._tick_stop_event = threading.Event()
        self._tick_thread: threading.Thread | None = None
        # 接收循环：处理子进程 release_request + 弹窗结果分发
        self._recv_stop_event = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._pending_results: dict[str, dict] = {}
        self._results_lock = threading.Lock()
        # 会话管理由 SessionManager 单例负责；OverlayClient 仅订阅事件渲染 overlay
        self._session_unsubscribe = get_session_manager().subscribe(
            self._on_session_event
        )

    def start(self):
        """启动GUI子进程 + tick/recv 线程"""
        if self._started:
            return
        self._process = multiprocessing.Process(
            target=_gui_subprocess,
            args=(self._recv_queue, self._send_queue),
            daemon=True,
        )
        self._process.start()
        self._started = True
        # 启动 tick 循环（1 秒推 IPC tick）
        self._tick_stop_event.clear()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, daemon=True, name="overlay-tick"
        )
        self._tick_thread.start()
        # 启动 recv 循环（处理 release_request + 弹窗结果）
        self._recv_stop_event.clear()
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="overlay-recv"
        )
        self._recv_thread.start()
        logger.info("GUI子进程已启动")

    def shutdown(self):
        """关闭GUI子进程 + tick/recv 线程"""
        # 停 tick/recv 线程
        self._tick_stop_event.set()
        self._recv_stop_event.set()
        for t in (self._tick_thread, self._recv_thread):
            if t and t.is_alive():
                t.join(timeout=1.0)
        self._tick_thread = None
        self._recv_thread = None
        # 退订 SessionManager 事件
        if self._session_unsubscribe is not None:
            try:
                self._session_unsubscribe()
            except Exception:
                pass
            self._session_unsubscribe = None
        _invalidate_health_cache()
        if self._process and self._process.is_alive():
            try:
                self._recv_queue.put({"cmd": "shutdown"}, timeout=2)
            except Exception:
                pass
            self._process.terminate()
            self._process.join(timeout=3)
        self._started = False
        self._overlay_visible = False

    @property
    def overlay_visible(self) -> bool:
        return self._overlay_visible

    # ========== SessionManager 事件回调 ==========

    def _on_session_event(self, event: SessionEvent) -> None:
        """SessionManager 事件回调：驱动 overlay 显示/隐藏。

        状态刷新（mode/phase/remaining_seconds）由 _tick_loop 每秒推送，本方法只管 show/hide。
        """
        etype = event.type
        state = event.state or {}
        if etype == "grant":
            # 授权成功 → 显示 overlay（状态由 _tick_loop 推送）
            self.show_overlay()
        elif etype in ("release", "expired"):
            # 撤销/过期 → 隐藏 overlay（决策 2/13：降级后横条消失）
            self.hide_overlay()
        elif etype == "transition":
            # watchdog→normal 降级 → overlay 保持显示（tick 会刷新新状态）
            if state.get("active"):
                self.show_overlay()
            else:
                self.hide_overlay()
        elif etype == "warning":
            # normal 告警阶段 → 无操作（tick 刷颜色）
            pass
        _invalidate_health_cache()

    # ========== T23: 1 秒 tick 循环 ==========

    def _tick_loop(self):
        """1 秒 tick 循环：读 SessionManager.status() 推 IPC tick 到子进程。

        决策 4 方案 A：主进程 OverlayClient 侧独立 1 秒 Timer，SessionManager worker 保持 5 秒。
        """
        while not self._tick_stop_event.wait(1.0):
            if not self._started or not self._overlay_visible:
                continue
            try:
                status = get_session_manager().status()
                self._recv_queue.put({
                    "cmd": "tick",
                    "mode": status.get("mode", "no_permission"),
                    "phase": status.get("phase", "inactive"),
                    "remaining_seconds": status.get("remaining_seconds", 0),
                    "task_description": status.get("task_description", ""),
                })
            except Exception as e:
                logger.debug(f"tick 循环异常: {e}")

    # ========== 接收循环：处理子进程消息 ==========

    def _recv_loop(self):
        """持续接收子进程消息，分发到 _pending_results 或处理 release_request。

        - release_request：用户点击横条停止按钮 → 调 SessionManager.release
        - confirm_action_result / confirm_start_result：弹窗结果 → 存入 _pending_results
        - import_error：PySide6 导入失败 → 存入 _pending_results
        """
        while not self._recv_stop_event.wait(0.1):
            try:
                msg = self._send_queue.get(timeout=0.1)
            except Exception:
                continue
            msg_type = msg.get("type")
            if msg_type == "release_request":
                try:
                    get_session_manager().release(msg.get("reason", "user_button"))
                except Exception as e:
                    logger.warning(f"release_request 处理失败: {e}")
            elif msg_type in ("confirm_action_result", "confirm_start_result", "import_error"):
                with self._results_lock:
                    self._pending_results[msg_type] = msg

    def _pop_result(self, msg_type: str, timeout: float) -> dict | None:
        """等待指定类型的结果消息。优先返回 import_error。"""
        start = time.time()
        while time.time() - start < timeout:
            with self._results_lock:
                # 优先检查 import_error（子进程启动失败）
                err = self._pending_results.pop("import_error", None)
                if err:
                    return err
                msg = self._pending_results.pop(msg_type, None)
            if msg:
                return msg
            if self._process and not self._process.is_alive():
                return None
            time.sleep(0.1)
        return None

    # ========== Overlay 渲染 ==========

    def show_overlay(self, position: str = "top"):
        """显示覆盖层（持久，无自动隐藏）。

        状态（mode/phase/remaining_seconds/task_description）由 _tick_loop 每秒推送。
        """
        if not self._started:
            self.start()
        self._recv_queue.put({
            "cmd": "overlay_show",
            "position": position,
        })
        self._overlay_visible = True

    def hide_overlay(self):
        """隐藏覆盖层（仅视觉隐藏，不撤销任务授权）。"""
        if not self._started:
            self._overlay_visible = False
            return
        self._recv_queue.put({"cmd": "overlay_hide"})
        self._overlay_visible = False

    # ========== 弹窗接口 ==========

    def confirm_action(self, screenshot_b64: str, action: str,
                       x: int | None = None, y: int | None = None,
                       text: str | None = None, keys: list | None = None,
                       draw_x: int | None = None, draw_y: int | None = None) -> dict:
        """弹出操作确认窗口，等待用户响应。

        x, y: 显示给用户的原始坐标（pyautogui 坐标）
        draw_x, draw_y: 截图内画点坐标（双屏修正后）。None 时回退到 x, y。
        """
        if not self._started:
            self.start()

        # 清空旧的 confirm_action_result
        with self._results_lock:
            self._pending_results.pop("confirm_action_result", None)
            self._pending_results.pop("import_error", None)

        self._recv_queue.put({
            "cmd": "confirm_action",
            "screenshot_b64": screenshot_b64,
            "action": action,
            "x": x, "y": y,
            "draw_x": draw_x if draw_x is not None else x,
            "draw_y": draw_y if draw_y is not None else y,
            "text": text,
            "keys": keys,
        })

        timeout = get_screen_config().get("confirm_timeout_seconds", 180)
        result = self._pop_result("confirm_action_result", timeout)
        if result is None:
            return {"status": "cancelled", "same_coords_skip": False, "reason": ""}
        if result.get("type") == "import_error":
            return {
                "status": "error",
                "same_coords_skip": False,
                "reason": result.get("error", "PySide6 导入失败"),
            }
        return {
            "status": result.get("status", "cancelled"),
            "same_coords_skip": result.get("same_coords_skip", False),
            "reason": result.get("reason", ""),
        }

    def confirm_start(self, task_description: str,
                      hotkey_hint: str = "Ctrl+`",
                      allow_task_authorization: bool = True,
                      requested_mode: str = "normal") -> dict:
        """弹出开始确认窗口（watchdog 模式含 QSpinBox + 允许关机复选框）。"""
        if not self._started:
            self.start()

        with self._results_lock:
            self._pending_results.pop("confirm_start_result", None)
            self._pending_results.pop("import_error", None)

        self._recv_queue.put({
            "cmd": "confirm_start",
            "task_description": task_description,
            "hotkey_hint": hotkey_hint,
            "allow_task_authorization": allow_task_authorization,
            "requested_mode": requested_mode,
        })

        timeout = get_screen_config().get("confirm_timeout_seconds", 180)
        result = self._pop_result("confirm_start_result", timeout)
        if result is None:
            return {
                "status": "cancelled",
                "reason": "",
                "task_authorization": False,
                "requested_mode": requested_mode,
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
        if result.get("type") == "import_error":
            return {
                "status": "error",
                "reason": result.get("error", "PySide6 导入失败"),
                "task_authorization": False,
                "requested_mode": requested_mode,
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
        return {
            "status": result.get("status", "cancelled"),
            "reason": result.get("reason", ""),
            "task_authorization": result.get("task_authorization", False),
            "requested_mode": result.get("requested_mode", requested_mode),
            "max_duration_hours": result.get("max_duration_hours"),
            "shutdown_permitted": result.get("shutdown_permitted", False),
        }

    def ensure_takeover_approved(
        self,
        task_description: str,
        *,
        timeout: int = 30,
        timeout_action: str = "cancel",
        hotkey_hint: str = "Ctrl+`",
        allow_task_authorization: bool = True,
        force_prompt: bool = False,
        source: str = "agent",
        requested_mode: str = "normal",
    ) -> dict:
        """顶栏未显示时弹"接管确认"窗口，询问用户是否允许 agent 接管键鼠操作。

        Args:
            requested_mode: agent 请求的模式（"normal" | "watchdog"）。
                用户只能允许/拒绝，不能改模式（决策 17）。

        Returns:
            {
                "status": "skipped" | "confirmed" | "cancelled" | "error",
                "reason": str,
                "task_authorization": bool,
                "requested_mode": str,
                "max_duration_hours": int | None,  # watchdog 时用户输入的时长
                "shutdown_permitted": bool,         # watchdog 时用户勾选的关机权限
            }
            - "skipped"：顶栏已显示，无需弹窗，直接放行
            - "confirmed"：用户点"允许接管" 或 超时 timeout_action=proceed
            - "cancelled"：用户点"拒绝" 或 超时 timeout_action=cancel
            - "error"：GUI 子进程异常（fail-closed）
        """
        # 1. 已有任务授权 → 直接放行
        session = get_session_manager()
        if session.is_active():
            # 保留当前会话模式（避免 agent 重调 request 时把 watchdog 误降级为 normal）
            current_mode = session.status().get("mode", "normal")
            return {
                "status": "skipped",
                "reason": "",
                "task_authorization": True,
                "requested_mode": requested_mode,
                "authorized_mode": current_mode,
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
        if self._overlay_visible and not force_prompt:
            return {
                "status": "skipped",
                "reason": "",
                "task_authorization": False,
                "requested_mode": requested_mode,
                "authorized_mode": "normal",
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }

        if not self._started:
            self.start()

        # 2. 清空旧结果
        with self._results_lock:
            self._pending_results.pop("confirm_start_result", None)
            self._pending_results.pop("import_error", None)

        # 3. 发送弹窗命令
        self._recv_queue.put({
            "cmd": "confirm_start",
            "task_description": task_description,
            "hotkey_hint": hotkey_hint,
            "allow_task_authorization": allow_task_authorization,
            "requested_mode": requested_mode,
        })

        # 4. 等待响应（timeout 秒）
        result = self._pop_result("confirm_start_result", timeout)
        if result is None:
            # 5. 超时——按 timeout_action 决定
            if timeout_action == "proceed":
                return {
                    "status": "confirmed",
                    "reason": "timeout_auto_proceed",
                    "task_authorization": False,
                    "requested_mode": requested_mode,
                    "authorized_mode": "normal",
                    "max_duration_hours": None,
                    "shutdown_permitted": False,
                }
            return {
                "status": "cancelled",
                "reason": "timeout_auto_cancel",
                "task_authorization": False,
                "requested_mode": requested_mode,
                "authorized_mode": "normal",
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
        if result.get("type") == "import_error":
            return {
                "status": "error",
                "reason": result.get("error", "PySide6 导入失败"),
                "task_authorization": False,
                "requested_mode": requested_mode,
                "authorized_mode": "normal",
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }
        return {
            "status": result.get("status", "cancelled"),
            "reason": result.get("reason", ""),
            "task_authorization": result.get("task_authorization", False),
            "requested_mode": result.get("requested_mode", requested_mode),
            "authorized_mode": result.get("authorized_mode", "normal"),
            "max_duration_hours": result.get("max_duration_hours"),
            "shutdown_permitted": result.get("shutdown_permitted", False),
        }


# 全局单例
overlay_client = OverlayClient()
