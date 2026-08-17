"""L0 采集层 Ticket 06：悬浮条主窗口（FloatingBar，小模式）

设计（01-recorder.md §1）：
- 单行高 40px 的悬浮条，半透明，始终置顶但不遮挡操作
- 显示：录制状态（就绪/录制中/已暂停）/ 计时器 / 事件计数 / 开始停止按钮 / 麦克风开关
- 三状态可见性驱动布局（IDLE/RECORDING/PAUSED），宽度自适应内容
- 可拖拽到任意位置（鼠标按空白区域拖动）

主题（01-recorder.md §共通改造）：
- 接入 lib/ui 主题（main.py 调 apply_theme(app)）
- 删除全部本地 COLOR_* 常量与内联色值
- emoji 全部替换为 lib/ui/icon()
- 解除固定宽度，用 adjustSize() 自适应

Qt 设置：
- Qt.FramelessWindowHint：无标题栏
- Qt.WindowStaysOnTopHint：始终置顶
- Qt.Tool：不在任务栏显示
- setWindowOpacity(0.90)：半透明
- setAttribute WA_TranslucentBackground 配合样式表实现圆角

信号：
- start_requested: 用户点开始按钮
- stop_requested: 用户点停止按钮
- mic_toggled: 用户点麦克风开关按钮（参数：是否启用麦克风）

公开方法：
- set_recording(is_recording): 切换录制状态指示灯 + 按钮文字
- update_duration(seconds): 更新计时器显示
- update_event_count(count): 更新事件计数显示
- update_frame_count(count): 更新帧数显示
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from lib.ui import icon, icon_pixmap, tokens
from lib.ui.theme import set_kind, set_text_role


class FloatingBar(QWidget):
    """录制器悬浮条主窗口（小模式）。

    Args:
        parent: 父窗口（一般 None，独立顶层窗口）
    """

    # 信号
    start_requested = Signal()
    stop_requested = Signal()
    mic_toggled = Signal(bool)
    # 三按钮信号（Ticket 12：PAUSED 状态交互）
    resume_requested = Signal()  # 继续录制
    save_requested = Signal()  # 保存结束
    discard_requested = Signal()  # 丢弃重录

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_recording = False
        self._mic_enabled = True
        self._build_window_flags()
        self._build_ui()
        self._apply_stylesheet()

    def _build_window_flags(self) -> None:
        """设置窗口标志：无标题栏 + 置顶 + Tool 不显示任务栏。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # 半透明（整体透明度，不是背景透明）
        self.setWindowOpacity(0.90)
        # 高度固定 40px（01-recorder §1.2），宽度由内容自适应
        self.setFixedHeight(40)

    def move_to_top_center(self) -> None:
        """移动到屏幕顶部中央（避免默认屏幕中央挡住操作区域）。"""
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        # 水平居中，垂直贴近顶部（留 8px 间距）
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + 8
        self.move(x, y)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # 拖手（01-recorder §1.3：grip-vertical 图标提示可拖动）
        self.lbl_grip = QLabel()
        self.lbl_grip.setPixmap(icon_pixmap("grip-vertical", tokens.TEXT_TERTIARY, 14))
        self.lbl_grip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.lbl_grip)

        # 状态指示灯（图标 + 文本：IDLE/REC/PAUSE）
        # QLabel 设 WA_TransparentForMouseEvents，鼠标事件透传到 FloatingBar 本身，
        # 这样点任何非按钮区域都能拖动窗口（修复"不可拖动"bug）
        self.lbl_status = QLabel("IDLE")
        set_text_role(self.lbl_status, "tertiary")
        self.lbl_status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.lbl_status)

        # 计时器（mono 等宽字体，01-recorder §1.3）
        self.lbl_timer = QLabel("00:00:00")
        set_text_role(self.lbl_timer, "mono")
        self.lbl_timer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.lbl_timer)

        # 事件计数（caption 小字，01-recorder §1.3）
        self.lbl_events = QLabel("events: 0")
        set_text_role(self.lbl_events, "caption")
        self.lbl_events.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.lbl_events)

        # 帧数
        self.lbl_frames = QLabel("frames: 0")
        set_text_role(self.lbl_frames, "caption")
        self.lbl_frames.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.lbl_frames)

        # 弹性间隔（把按钮推到右边）
        layout.addStretch(1)

        # 麦克风开关按钮（checkable，icon + 文本）
        self.btn_mic = QPushButton("麦克风:开")
        self.btn_mic.setCheckable(True)
        self.btn_mic.setChecked(True)
        self.btn_mic.setIcon(icon("mic", tokens.ICON_DEFAULT))
        self.btn_mic.setToolTip("麦克风：开，点击关闭 (Ctrl+M)")
        self.btn_mic.toggled.connect(self._on_mic_toggled)
        layout.addWidget(self.btn_mic)

        # 开始/停止按钮（同一个按钮，文字根据状态切换）
        self.btn_start_stop = QPushButton("开始录制")
        self.btn_start_stop.setIcon(icon("record", tokens.ICON_REC))
        self.btn_start_stop.setToolTip("开始录制 (Ctrl+R)")
        self.btn_start_stop.clicked.connect(self._on_start_stop_clicked)
        layout.addWidget(self.btn_start_stop)

        # 三按钮（PAUSED 状态显示，初始隐藏；Ticket 12 D028）
        self.btn_resume = QPushButton("继续录制")
        self.btn_resume.setIcon(icon("play", tokens.ICON_ACTIVE))
        self.btn_resume.setToolTip("继续录制")
        self.btn_resume.clicked.connect(self._on_resume_clicked)
        self.btn_resume.hide()
        layout.addWidget(self.btn_resume)

        self.btn_save = QPushButton("保存结束")
        self.btn_save.setIcon(icon("save", tokens.ICON_ACTIVE))
        self.btn_save.setToolTip("保存并结束录制")
        set_kind(self.btn_save, "primary")
        self.btn_save.clicked.connect(self._on_save_clicked)
        self.btn_save.hide()
        layout.addWidget(self.btn_save)

        self.btn_discard = QPushButton("丢弃重录")
        self.btn_discard.setIcon(icon("trash", tokens.ICON_DANGER))
        self.btn_discard.setToolTip("丢弃本次录制（不可恢复）")
        set_kind(self.btn_discard, "danger")
        self.btn_discard.clicked.connect(self._on_discard_clicked)
        self.btn_discard.hide()
        layout.addWidget(self.btn_discard)

        # 宽度由内容自适应（01-recorder §1.4：禁止固定 px 宽度）
        self.adjustSize()

    def _apply_stylesheet(self) -> None:
        """仅设置 frameless 窗口背景（圆角 + 边框），控件样式由全局 QSS 负责。"""
        self.setObjectName("FloatingBar")
        # FramelessWindowHint 需要本地样式表设置圆角背景，
        # 颜色引用 tokens（不硬编码 hex）
        self.setStyleSheet(f"""
            QWidget#FloatingBar {{
                background-color: {tokens.BG_PANEL};
                border: 1px solid {tokens.BORDER};
                border-radius: {tokens.RADIUS_LG}px;
            }}
        """)

    # ========== 事件处理 ==========

    def _on_start_stop_clicked(self) -> None:
        """开始/停止按钮点击：根据当前状态发对应信号。"""
        if self._is_recording:
            self.stop_requested.emit()
        else:
            self.start_requested.emit()

    def _on_resume_clicked(self) -> None:
        """继续录制按钮点击：发 resume_requested 信号。"""
        self.resume_requested.emit()

    def _on_save_clicked(self) -> None:
        """保存结束按钮点击：发 save_requested 信号。"""
        self.save_requested.emit()

    def _on_discard_clicked(self) -> None:
        """丢弃重录按钮点击：弹确认对话框，确认后发 discard_requested 信号。

        取消则不发信号，保持 PAUSED 状态（D030：丢弃因不可逆需确认）。
        """
        if self._confirm_discard():
            self.discard_requested.emit()

    def _confirm_discard(self) -> bool:
        """弹出丢弃确认对话框，返回用户是否确认。

        抽成单独方法便于测试 monkeypatch / override，避免真实弹窗。
        按规范 01-recorder §1.5：danger 按钮「丢弃」，默认按钮「取消」。
        """
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("丢弃录制？")
        msg.setText("将丢弃本次录制，此操作不可恢复。")
        # 自定义中文按钮：丢弃（danger）+ 取消（默认）
        discard_btn = msg.addButton("丢弃", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = msg.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        return msg.clickedButton() is discard_btn

    def _on_mic_toggled(self, checked: bool) -> None:
        """麦克风开关按钮切换。"""
        self._mic_enabled = checked
        self.btn_mic.setText("麦克风:" + ("开" if checked else "关"))
        self.btn_mic.setIcon(icon("mic" if checked else "mic-off", tokens.ICON_DEFAULT))
        self.btn_mic.setToolTip(
            f"麦克风：{'开' if checked else '关'}，点击{'关闭' if checked else '开启'} (Ctrl+M)"
        )
        self.mic_toggled.emit(checked)

    # ========== 鼠标拖拽 ==========

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """鼠标按下：记录拖拽起点（点空白区域才拖，点控件不拖）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            # 只在按下点没有命中子控件时才拖动
            child = self.childAt(event.position().toPoint())
            if child is None:
                self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
            else:
                super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """鼠标移动：拖拽窗口。"""
        if hasattr(self, "_drag_start") and self._drag_start is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_start)
                event.accept()
            else:
                super().mouseMoveEvent(event)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """鼠标释放：清除拖拽起点。"""
        self._drag_start = None
        super().mouseReleaseEvent(event)

    # ========== 公开方法（由 RecorderApp 调用更新 UI） ==========

    def set_recording(self, is_recording: bool) -> None:
        """切换录制状态：更新指示灯颜色 + 按钮文字。

        RECORDING/IDLE 状态显示 btn_start_stop 和 btn_mic，隐藏三按钮。
        """
        self._is_recording = is_recording
        # 确保三按钮隐藏（从 PAUSED 切回时干净）
        self.btn_resume.hide()
        self.btn_save.hide()
        self.btn_discard.hide()
        self.btn_start_stop.show()
        self.btn_mic.show()  # 从 PAUSED 切回时恢复麦克风按钮显示
        if is_recording:
            self.lbl_status.setText("REC")
            set_text_role(self.lbl_status, "accent")
            self.btn_start_stop.setText("停止录制")
            self.btn_start_stop.setIcon(icon("square", tokens.ICON_REC))
            self.btn_start_stop.setToolTip("停止录制 (Ctrl+R)")
        else:
            self.lbl_status.setText("IDLE")
            set_text_role(self.lbl_status, "tertiary")
            self.btn_start_stop.setText("开始录制")
            self.btn_start_stop.setIcon(icon("record", tokens.ICON_REC))
            self.btn_start_stop.setToolTip("开始录制 (Ctrl+R)")
        # 宽度自适应（01-recorder §1.4）
        self.adjustSize()

    def set_paused(self, is_paused: bool) -> None:
        """切换暂停状态：隐藏停止按钮和麦克风按钮，显示三按钮（Ticket 12 D028）。

        PAUSED 状态下麦克风开关无意义（暂停时不录音频），隐藏 btn_mic 节省空间。

        Args:
            is_paused: True=进入 PAUSED（显示三按钮）；False=退出 PAUSED（隐藏三按钮）
        """
        if is_paused:
            self._is_recording = True  # 仍在录制会话中（仅暂停）
            self.lbl_status.setText("PAUSE")
            set_text_role(self.lbl_status, "warning")
            self.btn_start_stop.hide()
            self.btn_mic.hide()  # 暂停时麦克风开关无意义，隐藏节省空间
            self.btn_resume.show()
            self.btn_save.show()
            self.btn_discard.show()
            # 宽度自适应（01-recorder §1.4：不再用固定 680px）
            self.adjustSize()
        else:
            # 退出 PAUSED：隐藏三按钮，恢复 btn_start_stop 和 btn_mic 显示
            self.btn_resume.hide()
            self.btn_save.hide()
            self.btn_discard.hide()
            self.btn_start_stop.show()
            self.btn_mic.show()
            # 宽度自适应（01-recorder §1.4：不再用固定 520px）
            self.adjustSize()

    def update_duration(self, seconds: int) -> None:
        """更新计时器显示。"""
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.lbl_timer.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def update_event_count(self, count: int) -> None:
        """更新事件计数显示。"""
        self.lbl_events.setText(f"events: {count}")

    def update_frame_count(self, count: int) -> None:
        """更新帧数显示。"""
        self.lbl_frames.setText(f"frames: {count}")

    def set_mic_enabled(self, enabled: bool) -> None:
        """设置麦克风开关状态（不触发 toggled 信号，避免循环）。"""
        self._mic_enabled = enabled
        # blockSignals 避免触发 mic_toggled 信号
        self.btn_mic.blockSignals(True)
        self.btn_mic.setChecked(enabled)
        self.btn_mic.setText("麦克风:" + ("开" if enabled else "关"))
        self.btn_mic.setIcon(icon("mic" if enabled else "mic-off", tokens.ICON_DEFAULT))
        self.btn_mic.blockSignals(False)

    # 截图瞬间用 setWindowOpacity 替代 hide/show：
    # - opacity=0.0 时窗口完全透明（不进截图），但 Qt 透明度只影响绘制，
    #   不影响事件接收——窗口仍可点击（看不到按钮但位置不变，截图只几十毫秒）
    # - 相比 hide/show：无窗口重建闪烁，重显示更快，终止按钮位置稳定可点
    _NORMAL_OPACITY = 0.90
    _CAPTURE_OPACITY = 0.0

    def set_overlay_visible(self, visible: bool) -> None:
        """截图隐藏用：visible=False 完全透明（不进截图），visible=True 恢复半透明。

        用 setWindowOpacity 替代 hide/show，避免窗口重建导致闪烁和按钮位置抖动。
        截图本身只几十毫秒，用户在此瞬间不会点按钮；截图完立即恢复可见。
        """
        self.setWindowOpacity(self._NORMAL_OPACITY if visible else self._CAPTURE_OPACITY)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def mic_enabled(self) -> bool:
        return self._mic_enabled
