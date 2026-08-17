"""L0 采集层 Ticket 06：新建录制配置对话框（ConfigDialog）

设计（01-recorder.md §2）：
- QDialog 模态弹窗，新建录制时弹出
- 字段：
  - 录制范围：QRadioButton 组（全屏 / 某屏 / 某窗口）
  - 显示器选择：QComboBox（range_type=monitor 时启用）
  - 窗口选择：QComboBox（range_type=window 时启用，列表来自 list_available_windows）
  - 录制模式：QRadioButton 组（详细 / 粗略 / 自定义）
  - 自定义参数（折叠）：截图间隔 / burst 间隔 / 最大时长
  - 音频：QCheckBox
  - UIA：QCheckBox（L0 阶段 stub，未来控制是否写 UIA 树到文件）
- 取消按钮返回 None，开始录制按钮返回 RecordingConfig

主题（01-recorder.md §共通改造）：
- 接入 lib/ui 主题（main.py 调 apply_theme(app)）
- 删除全部本地 COLOR_* 常量与内联色值
- 全中文按钮（无 OK/Cancel 英文）
- 高度 ≤ 700px（01-recorder §2.2）

测试策略：
- windows_provider 注入：测试时不调真实 list_available_windows，注入 fake 返回固定窗口列表
- 不依赖真实 Qt 事件循环（用 QTest 模拟点击 + dialog.show() + 直接调 get_config）
"""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lib.ui import icon, tokens
from lib.ui.theme import set_kind, set_text_role
from workspace.recorder.tools.config import (
    RecordingConfig,
    list_available_microphones,
    list_available_monitors,
    list_available_windows,
)


class ConfigDialog(QDialog):
    """新建录制配置对话框。

    Args:
        parent: 父窗口
        windows_provider: 窗口列表提供者（无参 → list[dict]），默认用 list_available_windows。
            测试时可注入 fake 返回固定窗口列表，避免调真实 win32 API。
        monitors_provider: 显示器列表提供者（无参 → list[dict]），默认用 list_available_monitors。
            测试时可注入 fake 返回固定显示器列表，避免调真实 mss。
        microphones_provider: 麦克风列表提供者（无参 → list[dict]），默认用 list_available_microphones。
            测试时可注入 fake 返回固定麦克风列表，避免调真实 sounddevice。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        windows_provider: Callable[[], list[dict]] | None = None,
        monitors_provider: Callable[[], list[dict]] | None = None,
        microphones_provider: Callable[[], list[dict]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建录制")
        self.setModal(True)
        self._windows_provider = windows_provider or list_available_windows
        self._monitors_provider = monitors_provider or list_available_monitors
        self._microphones_provider = microphones_provider or list_available_microphones
        self._windows_cache: list[dict] = []
        self._monitors_cache: list[dict] = []
        self._microphones_cache: list[dict] = []
        self._build_ui()
        self._refresh_monitors()
        self._refresh_microphones()
        self._refresh_windows()

    # ========== UI 构建 ==========

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        # ── 录制范围 ──
        range_group = QGroupBox("录制范围")
        range_layout = QVBoxLayout(range_group)
        range_layout.setSpacing(6)
        self.rb_fullscreen = QRadioButton("整个屏幕（所有显示器）")
        self.rb_monitor = QRadioButton("指定屏幕")
        self.rb_window = QRadioButton("指定窗口")
        self.rb_fullscreen.setChecked(True)

        # 显示器选择行（mode=monitor 时启用）
        monitor_row = QHBoxLayout()
        monitor_row.setSpacing(8)
        self.cb_monitor = QComboBox()
        self.cb_monitor.setEnabled(False)
        self.btn_refresh_monitors = QToolButton()
        self.btn_refresh_monitors.setIcon(icon("refresh", tokens.ICON_DEFAULT))
        self.btn_refresh_monitors.setToolTip("刷新显示器列表")
        self.btn_refresh_monitors.setEnabled(False)
        self.btn_refresh_monitors.clicked.connect(self._refresh_monitors)
        monitor_row.addWidget(self.cb_monitor, 1)
        monitor_row.addWidget(self.btn_refresh_monitors)
        monitor_row_widget = QWidget()
        monitor_row_widget.setLayout(monitor_row)
        monitor_row_widget.setEnabled(False)
        self._monitor_row_widget = monitor_row_widget

        # 窗口选择行（mode=window 时启用）
        window_row = QHBoxLayout()
        window_row.setSpacing(8)
        self.cb_window = QComboBox()
        self.cb_window.setEnabled(False)
        self.btn_refresh_windows = QToolButton()
        self.btn_refresh_windows.setIcon(icon("refresh", tokens.ICON_DEFAULT))
        self.btn_refresh_windows.setToolTip("刷新窗口列表")
        self.btn_refresh_windows.setEnabled(False)
        self.btn_refresh_windows.clicked.connect(self._refresh_windows)
        window_row.addWidget(self.cb_window, 1)
        window_row.addWidget(self.btn_refresh_windows)
        window_row_widget = QWidget()
        window_row_widget.setLayout(window_row)
        window_row_widget.setEnabled(False)
        self._window_row_widget = window_row_widget

        range_layout.addWidget(self.rb_fullscreen)
        range_layout.addWidget(self.rb_monitor)
        range_layout.addWidget(self._monitor_row_widget)
        range_layout.addWidget(self.rb_window)
        range_layout.addWidget(self._window_row_widget)

        # 范围 radio 切换时启用/禁用对应行
        self.rb_fullscreen.toggled.connect(self._on_range_changed)
        self.rb_monitor.toggled.connect(self._on_range_changed)
        self.rb_window.toggled.connect(self._on_range_changed)

        layout.addWidget(range_group)

        # ── 录制模式 ──
        mode_group = QGroupBox("录制模式")
        mode_layout = QVBoxLayout(mode_group)
        mode_layout.setSpacing(6)
        self.rb_detailed = QRadioButton("详细  定时截图 + UIA + 音频")
        self.rb_coarse = QRadioButton("粗略  仅键鼠 + 窗口标题 + 音频")
        self.rb_custom = QRadioButton("自定义")
        self.rb_detailed.setChecked(True)

        mode_layout.addWidget(self.rb_detailed)
        mode_layout.addWidget(self.rb_coarse)
        mode_layout.addWidget(self.rb_custom)

        # 自定义参数（mode=custom 时展开，默认折叠隐藏）
        self.custom_box = QGroupBox("自定义参数")
        custom_form = QFormLayout(self.custom_box)
        self.edit_capture_interval = QLineEdit("0.5")
        self.edit_capture_interval.setPlaceholderText("秒，如 0.5 / 2.0")
        self.edit_burst_intervals = QLineEdit("0.05,0.10,0.15")
        self.edit_burst_intervals.setPlaceholderText("逗号分隔，如 0.05,0.10,0.15")
        self.edit_max_duration = QLineEdit("1800")
        self.edit_max_duration.setPlaceholderText("秒，默认 1800（30 分钟）")
        custom_form.addRow("截图间隔 (s):", self.edit_capture_interval)
        custom_form.addRow("Burst 时刻 (s):", self.edit_burst_intervals)
        custom_form.addRow("最大时长 (s):", self.edit_max_duration)
        self.custom_box.setEnabled(False)
        self.custom_box.setVisible(False)  # 默认折叠（01-recorder §2.3）
        mode_layout.addWidget(self.custom_box)

        self.rb_detailed.toggled.connect(self._on_mode_changed)
        self.rb_coarse.toggled.connect(self._on_mode_changed)
        self.rb_custom.toggled.connect(self._on_mode_changed)

        layout.addWidget(mode_group)

        # ── 采集选项 ──
        opts_group = QGroupBox("采集选项")
        opts_layout = QVBoxLayout(opts_group)
        opts_layout.setSpacing(6)
        self.chk_audio = QCheckBox("录制麦克风音频")
        self.chk_audio.setChecked(True)
        self.chk_audio.toggled.connect(self._on_audio_toggled)
        opts_layout.addWidget(self.chk_audio)

        # 麦克风选择行（audio_enabled 时启用）
        mic_row = QHBoxLayout()
        mic_row.setSpacing(8)
        self.cb_mic = QComboBox()
        self.btn_refresh_mic = QToolButton()
        self.btn_refresh_mic.setIcon(icon("refresh", tokens.ICON_DEFAULT))
        self.btn_refresh_mic.setToolTip("刷新麦克风列表")
        self.btn_refresh_mic.clicked.connect(self._refresh_microphones)
        mic_row.addWidget(self.cb_mic, 1)
        mic_row.addWidget(self.btn_refresh_mic)
        mic_row_widget = QWidget()
        mic_row_widget.setLayout(mic_row)
        self._mic_row_widget = mic_row_widget
        opts_layout.addWidget(self._mic_row_widget)

        self.chk_clipboard = QCheckBox("采集剪贴板变化（文本/图片，密码框自动隐藏）")
        self.chk_clipboard.setChecked(False)
        opts_layout.addWidget(self.chk_clipboard)

        self.chk_uia = QCheckBox("采集 UIA 树（L0 暂未实现，预留字段）")
        self.chk_uia.setChecked(True)
        self.chk_uia.setEnabled(False)  # L0 stub：默认勾选但 disabled
        opts_layout.addWidget(self.chk_uia)
        layout.addWidget(opts_group)

        # ── 截图去重 ──（D035 / D036，压缩为一行 + ⓘ）
        dedup_group = QGroupBox("截图去重")
        dedup_layout = QHBoxLayout(dedup_group)
        dedup_layout.setSpacing(8)
        dedup_label = QLabel("相似阈值")
        set_text_role(dedup_label, "secondary")
        dedup_layout.addWidget(dedup_label)
        self.slider_phash = QSlider(Qt.Orientation.Horizontal)
        self.slider_phash.setRange(0, 16)
        self.slider_phash.setValue(5)
        self.slider_phash.setToolTip("0 = 禁用去重；值越小越严格")
        self.lbl_phash_value = QLabel("5")
        set_text_role(self.lbl_phash_value, "mono")
        self.lbl_phash_value.setMinimumWidth(24)
        self.slider_phash.valueChanged.connect(
            lambda v: self.lbl_phash_value.setText(str(v))
        )
        dedup_layout.addWidget(self.slider_phash, 1)
        dedup_layout.addWidget(self.lbl_phash_value)
        # ⓘ help 按钮：pHash 原理进 WhatsThis（components §10）
        self.btn_phash_help = QToolButton()
        self.btn_phash_help.setIcon(icon("info", tokens.ICON_DEFAULT))
        self.btn_phash_help.setToolTip("pHash 去重原理")
        self.btn_phash_help.setWhatsThis(
            "pHash 相邻帧去重阈值（汉明距离）。\n"
            "0 = 禁用去重；值越小越严格（仅跳过几乎相同的帧）。\n"
            "默认 5：64-bit hash 中允许 5 bit 不同（≈7.8% 差异）。"
        )
        self.btn_phash_help.clicked.connect(self._show_phash_help)
        dedup_layout.addWidget(self.btn_phash_help)

        layout.addWidget(dedup_group)

        # 初始启用状态（audio 默认勾选）
        self._on_audio_toggled(self.chk_audio.isChecked())

        # ── 按钮（全中文，01-recorder §2.3）──
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.NoButton)
        self.btn_cancel = btn_box.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_start = btn_box.addButton("开始录制", QDialogButtonBox.ButtonRole.AcceptRole)
        set_kind(self.btn_start, "primary")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setMinimumWidth(120)
        btn_box.rejected.connect(self.reject)
        self.btn_start.clicked.connect(self._on_start_clicked)
        layout.addWidget(btn_box)

        self.setLayout(layout)
        self.setMinimumWidth(560)
        self.adjustSize()

    def _show_phash_help(self) -> None:
        """点击 ⓘ 按钮显示 pHash 说明（WhatsThis 的点击触发）。"""
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, "截图去重原理", self.btn_phash_help.whatsThis())

    # ========== 事件处理 ==========

    def _on_range_changed(self) -> None:
        """范围 radio 切换时启用/禁用对应行。"""
        is_monitor = self.rb_monitor.isChecked()
        is_window = self.rb_window.isChecked()
        self._monitor_row_widget.setEnabled(is_monitor)
        self.cb_monitor.setEnabled(is_monitor)
        self.btn_refresh_monitors.setEnabled(is_monitor)
        self._window_row_widget.setEnabled(is_window)
        self.cb_window.setEnabled(is_window)
        self.btn_refresh_windows.setEnabled(is_window)

    def _refresh_monitors(self) -> None:
        """刷新显示器列表（调用 monitors_provider）。

        动态获取实际连接的显示器，每个显示器标注分辨率和主/副屏身份。
        无显示器时提示"未检测到显示器"。
        """
        try:
            monitors = self._monitors_provider()
        except Exception:
            monitors = []
        self._monitors_cache = monitors or []
        self.cb_monitor.clear()
        if not self._monitors_cache:
            self.cb_monitor.addItem("（未检测到显示器）")
            self.cb_monitor.setEnabled(False)
        else:
            for mon in self._monitors_cache:
                self.cb_monitor.addItem(mon.get("label", f"显示器 {mon.get('index', '?')}"))

    def _refresh_microphones(self) -> None:
        """刷新麦克风列表（调用 microphones_provider）。

        列出所有输入设备（max_input_channels > 0），标注设备索引/名称/主机API/是否默认。
        选中系统默认设备（is_default=True）。
        """
        try:
            mics = self._microphones_provider()
        except Exception:
            mics = []
        self._microphones_cache = mics or []
        self.cb_mic.clear()
        if not self._microphones_cache:
            self.cb_mic.addItem("（未检测到麦克风）")
        else:
            default_idx = 0
            for i, mic in enumerate(self._microphones_cache):
                self.cb_mic.addItem(mic.get("label", f"设备 {mic.get('index', '?')}"))
                if mic.get("is_default"):
                    default_idx = i
            self.cb_mic.setCurrentIndex(default_idx)

    def _on_audio_toggled(self, checked: bool) -> None:
        """录制麦克风复选框切换时启用/禁用麦克风选择行。"""
        self._mic_row_widget.setEnabled(checked)
        self.cb_mic.setEnabled(checked)
        self.btn_refresh_mic.setEnabled(checked)

    def _on_mode_changed(self) -> None:
        """模式 radio 切换时启用/禁用 + 显示/隐藏自定义参数（01-recorder §2.3）。"""
        is_custom = self.rb_custom.isChecked()
        self.custom_box.setEnabled(is_custom)
        self.custom_box.setVisible(is_custom)
        # 折叠/展开后调整对话框大小
        self.adjustSize()

    def _refresh_windows(self) -> None:
        """刷新窗口列表（调用 windows_provider）。

        窗口列表项文本 = "标题（进程名）"，技术细节（hwnd/pid）放 tooltip（patterns §12）。
        """
        try:
            windows = self._windows_provider()
        except Exception:
            windows = []
        self._windows_cache = windows or []
        self.cb_window.clear()
        for w in self._windows_cache:
            title = w.get("title", "")
            hwnd = w.get("hwnd", 0)
            pid = w.get("pid", 0)
            proc = w.get("process_name", "")
            # 主文本：标题（进程名），不暴露 hwnd/pid（patterns §12）
            if title:
                label = f"{title}（{proc}）" if proc else title
            else:
                label = "（未命名窗口）"
            # 技术细节放 tooltip
            tooltip = f"hwnd={hwnd}, pid={pid}, {proc}" if proc else f"hwnd={hwnd}, pid={pid}"
            self.cb_window.addItem(label)
            self.cb_window.setItemData(self.cb_window.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole)

    def _on_start_clicked(self) -> None:
        """开始录制按钮：校验输入后 accept。"""
        # 校验：选 monitor 但无显示器时提示
        if self.rb_monitor.isChecked() and not self._monitors_cache:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无法开始", "未检测到可用显示器，请刷新显示器列表或切换到全屏模式。")
            return
        # 校验：选 window 但无窗口时提示
        if self.rb_window.isChecked() and self.cb_window.count() == 0:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无法开始", "未选择目标窗口，请刷新窗口列表或切换到全屏/某屏模式。")
            return
        # 校验：开启音频但无麦克风时提示
        if self.chk_audio.isChecked() and not self._microphones_cache:
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, "无麦克风",
                "未检测到可用麦克风，将不录制音频。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self.chk_audio.setChecked(False)
        # 校验：自定义模式参数格式
        if self.rb_custom.isChecked():
            try:
                float(self.edit_capture_interval.text())
                [float(x) for x in self.edit_burst_intervals.text().split(",")]
                int(self.edit_max_duration.text())
            except ValueError:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "参数错误", "自定义参数格式错误：截图间隔/Burst 时刻需为数字，最大时长需为整数。")
                return
        self.accept()

    # ========== 结果导出 ==========

    def get_config(self) -> RecordingConfig | None:
        """获取用户配置。

        Returns:
            RecordingConfig 实例；用户取消时返回 None。
        """
        if self.result() != QDialog.DialogCode.Accepted:
            return None

        # 范围
        if self.rb_fullscreen.isChecked():
            range_type = "fullscreen"
        elif self.rb_monitor.isChecked():
            range_type = "monitor"
        else:
            range_type = "window"

        monitor_index = 0
        if range_type == "monitor" and self._monitors_cache:
            # 用实际显示器的 index 字段（1-based，对应 mss.monitors[1], [2], ...）
            idx = self.cb_monitor.currentIndex()
            if 0 <= idx < len(self._monitors_cache):
                monitor_index = int(self._monitors_cache[idx].get("index", idx + 1))

        window_hwnd = None
        if range_type == "window" and 0 <= self.cb_window.currentIndex() < len(self._windows_cache):
            window_hwnd = int(self._windows_cache[self.cb_window.currentIndex()].get("hwnd", 0))

        # 模式
        if self.rb_detailed.isChecked():
            mode = "detailed"
            capture_interval = 0.5
            burst_intervals = (0.05, 0.10, 0.15)
            max_duration = 1800
        elif self.rb_coarse.isChecked():
            mode = "coarse"
            capture_interval = 2.0
            burst_intervals = (0.10, 0.20, 0.30)
            max_duration = 1800
        else:  # custom
            mode = "custom"
            capture_interval = float(self.edit_capture_interval.text())
            burst_intervals = tuple(float(x) for x in self.edit_burst_intervals.text().split(","))
            max_duration = int(self.edit_max_duration.text())

        # 麦克风设备（audio_enabled=False 时为 None）
        audio_device: int | str | None = None
        if self.chk_audio.isChecked() and self._microphones_cache:
            idx = self.cb_mic.currentIndex()
            if 0 <= idx < len(self._microphones_cache):
                audio_device = int(self._microphones_cache[idx].get("index", -1))
                if audio_device < 0:
                    audio_device = None  # -1 表示系统默认，AudioSensor 接受 None

        # pHash 去重阈值（D035 / D036）
        phash_threshold = int(self.slider_phash.value())

        return RecordingConfig(
            range_type=range_type,
            monitor_index=monitor_index,
            window_hwnd=window_hwnd,
            mode=mode,
            capture_interval=capture_interval,
            burst_intervals=burst_intervals,
            audio_enabled=self.chk_audio.isChecked(),
            audio_device=audio_device,
            clipboard_enabled=self.chk_clipboard.isChecked(),
            uia_enabled=self.chk_uia.isChecked(),
            max_duration_seconds=max_duration,
            phash_threshold=phash_threshold,
        )
