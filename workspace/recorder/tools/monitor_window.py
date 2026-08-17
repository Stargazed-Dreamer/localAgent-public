"""L0 采集层 Ticket 07：录制器 GUI 大模式（MonitorWindow，监控面板）

设计（05-gui-design.md 第二节"大模式"）：
- 双屏监控场景：一个屏操作另一个屏监控
- 实时屏幕预览（缩略图）
- 实时事件流（滚动显示最近 100 条）
- 录制参数面板（截图频率/音频状态/目标窗口/已采集块数）
- 章节列表（按窗口切换自动生成的章节）
- 音频波形（实时显示麦克风输入电平）
- 底部按钮栏（停止 / 麦克风开关）

设计偏离说明：
- 05-gui-design.md 设计稿含"暂停""标记重点"按钮，本 ticket 不实现：
  - 暂停：与 Ticket 06 保持一致（spec user stories 未强制要求，传感器暂停状态复杂）
  - 标记重点：需要在 events.jsonl 写 marker 事件，侵入 RecordingController，留待未来
- 章节列表：L0 不实现自动章节生成（需分析 focus.jsonl 时序，属 L1 聚合范畴），
  本 ticket 仅显示焦点切换事件流（用户可视为原始章节信号）

实时数据来源策略（轮询）：
- 不侵入 RecordingController（保持 L0 核心稳定）
- QTimer 500ms 轮询：
  - events.jsonl 新增行（offset 跟踪）→ 事件流
  - focus.jsonl 最后一行 → 当前窗口信息
  - controller.event_count / frame_count → 统计
  - frames/ 最新 PNG（按文件名 seq 排序）→ 截图预览
- QTimer 1s 轮询：
  - audio/mic.wav 文件大小 → 音频大小统计
  - WAV 末尾 N 采样 → 波形绘制

attach/detach 模式：
- attach_recorder(recorder_app) 关联 RecorderApp + 启动 QTimer
- detach_recorder() 断开 + 停 QTimer
- 与 FloatingBar 类似的纯 UI 组件模式（不持有 RecorderApp 业务逻辑）
- 大/小模式切换：main.py 隐藏 FloatingBar + attach MonitorWindow（共享 controller）

主题（01-recorder.md §3）：
- 接入 lib/ui 主题（main.py 调 apply_theme(app)）
- 删除全部本地 COLOR_* 常量与内联色值
- emoji 全部替换为 lib/ui/icon()
- 事件类型色用 tokens.BLOCK_COLORS
- 统计卡片用 QFrame kind="card"
"""

import json
import wave
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lib.ui import icon, icon_pixmap, tokens
from lib.ui.theme import set_kind, set_text_role

# 轮询间隔
TICK_INTERVAL_MS = 500  # 事件流/窗口信息/统计/截图预览
AUDIO_TICK_INTERVAL_MS = 1000  # 音频波形/大小

# 事件流最大保留条数
MAX_EVENTS_DISPLAY = 100

# 截图缩略图最大保留数
MAX_THUMBNAILS = 20

# 事件 kind → BLOCK_COLORS key 映射（统一时间轴/事件流配色，01-recorder §3.4）
_EVENT_COLOR_MAP = {
    "mouse_click": "mouse_click",
    "mouse_scroll": "mouse_scroll",
    "mouse_drag": "mouse_drag",
    "keyboard_input": "keyboard",
    "hotkey": "keyboard",
    "ime_switch": "keyboard",
    "password_masked": "keyboard",
    "focus_change": "focus",
    "screen_frame": "screenshot",
}


def _event_color(kind: str) -> str:
    """根据事件 kind 返回对应的 BLOCK_COLORS 色值。

    未知 kind 用 TEXT_TERTIARY（灰）兜底。
    """
    key = _EVENT_COLOR_MAP.get(kind)
    if key is None:
        return tokens.TEXT_TERTIARY
    return tokens.BLOCK_COLORS.get(key, tokens.TEXT_TERTIARY)


class MonitorWindow(QWidget):
    """录制器监控面板主窗口（大模式）。

    与 FloatingBar 并存（可同时显示），用于双屏监控场景。
    通过 attach_recorder 关联 RecorderApp 后启动 QTimer 轮询数据源。

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
        self._recorder_app = None
        self._controller = None
        self._is_recording = False
        self._mic_enabled = True
        # events.jsonl 读取 offset（用于增量读取）
        self._events_offset = 0
        # 已显示的帧 seq 集合（避免重复添加缩略图）
        self._shown_frame_seqs: set[int] = set()
        # QTimer
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._audio_timer = QTimer(self)
        self._audio_timer.timeout.connect(self._on_audio_tick)
        # 构建窗口
        self._build_window_flags()
        self._build_ui()

    def _build_window_flags(self) -> None:
        """窗口标志：独立顶层 + 不置顶（监控屏不需要置顶）。"""
        # 不用 WindowStaysOnTopHint：大模式在另一个屏幕显示，不需要置顶遮挡操作屏
        # 不用 FramelessWindowHint：大模式需要标题栏可移动/最小化/关闭
        self.setWindowTitle("录制监控")
        # 01-recorder §3.2：960×640，minimumSize 840×520
        self.setMinimumSize(840, 520)
        self.resize(960, 640)

    def _build_ui(self) -> None:
        """构建主 UI：顶部状态栏 + 中间 5 面板 Splitter + 底部按钮栏。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ===== 顶部状态栏（标题 + 计时器 + 状态指示灯） =====
        root.addLayout(self._build_top_bar())

        # ===== 中间 5 面板 Splitter =====
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 上半区：截图预览（左） + 事件流（右）
        top_split = QSplitter(Qt.Orientation.Horizontal)
        self.frame_preview = _FramePreviewPanel()
        self.event_stream = _EventStreamPanel()
        top_split.addWidget(self.frame_preview)
        top_split.addWidget(self.event_stream)
        top_split.setStretchFactor(0, 1)
        top_split.setStretchFactor(1, 1)
        splitter.addWidget(top_split)

        # 下半区：左=录制参数 + 当前窗口 / 中=音频波形 / 右=统计
        bottom_split = QSplitter(Qt.Orientation.Horizontal)
        self.window_info = _WindowInfoPanel()
        self.audio_waveform = _AudioWaveformPanel()
        self.stats_panel = _StatsPanel()
        bottom_split.addWidget(self.window_info)
        bottom_split.addWidget(self.audio_waveform)
        bottom_split.addWidget(self.stats_panel)
        bottom_split.setStretchFactor(0, 1)
        bottom_split.setStretchFactor(1, 1)
        bottom_split.setStretchFactor(2, 1)
        splitter.addWidget(bottom_split)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        # ===== 底部按钮栏 =====
        root.addLayout(self._build_bottom_bar())

    def _build_top_bar(self) -> QHBoxLayout:
        """顶部状态栏：状态指示灯 + 计时器 + 包名。"""
        layout = QHBoxLayout()
        layout.setSpacing(10)

        # 状态指示灯（icon + 文本，01-recorder §3.3）
        self.lbl_status_icon = QLabel()
        self.lbl_status_icon.setPixmap(icon_pixmap("record", tokens.TEXT_TERTIARY, tokens.ICON_SIZE_MD))
        layout.addWidget(self.lbl_status_icon)

        self.lbl_status = QLabel("IDLE")
        set_text_role(self.lbl_status, "tertiary")
        layout.addWidget(self.lbl_status)

        # 计时器（mono 等宽字体，01-recorder §3.3）
        self.lbl_timer = QLabel("00:00:00")
        set_text_role(self.lbl_timer, "mono")
        layout.addWidget(self.lbl_timer)

        layout.addStretch(1)

        # 包名（caption 小字）
        self.lbl_package = QLabel("(未启动)")
        set_text_role(self.lbl_package, "caption")
        layout.addWidget(self.lbl_package)

        return layout

    def _build_bottom_bar(self) -> QHBoxLayout:
        """底部按钮栏：开始/停止切换 + 三按钮（PAUSED）+ 麦克风开关。"""
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)

        # 麦克风开关（icon + 文本，checkable）
        self.btn_mic = QPushButton("麦克风:开")
        self.btn_mic.setCheckable(True)
        self.btn_mic.setChecked(True)
        self.btn_mic.setIcon(icon("mic", tokens.ICON_DEFAULT))
        self.btn_mic.setToolTip("麦克风：开，点击关闭 (Ctrl+M)")
        self.btn_mic.toggled.connect(self._on_mic_toggled)
        layout.addWidget(self.btn_mic)

        # 开始/停止切换按钮（Bug 4 修复：停止后可再次开始）
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

        return layout

    def _set_status_indicator(self, text: str, role: str, icon_color: str) -> None:
        """统一更新顶部状态指示灯：文本角色 + 图标色（避免重复 setStyleSheet）。"""
        self.lbl_status.setText(text)
        set_text_role(self.lbl_status, role)
        self.lbl_status_icon.setPixmap(icon_pixmap("record", icon_color, tokens.ICON_SIZE_MD))

    # ========== 公开接口 ==========

    def attach_recorder(self, recorder_app) -> None:
        """关联 RecorderApp，启动 QTimer 轮询。

        Args:
            recorder_app: RecorderApp 实例（已 start 或未 start 均可）
        """
        self._recorder_app = recorder_app
        self._controller = recorder_app._controller
        # 重置 offset（如果 controller 已有 events.jsonl）
        self._events_offset = 0
        self._shown_frame_seqs = set()
        # 清空 UI
        self.event_stream.clear()
        self.frame_preview.clear()
        # 启动 QTimer
        self._tick_timer.start(TICK_INTERVAL_MS)
        self._audio_timer.start(AUDIO_TICK_INTERVAL_MS)
        # 立即触发一次刷新
        self._on_tick()
        self._on_audio_tick()

    def detach_recorder(self) -> None:
        """断开与 RecorderApp 的关联，停止 QTimer。"""
        self._tick_timer.stop()
        self._audio_timer.stop()
        self._recorder_app = None
        self._controller = None

    def set_recording(self, is_recording: bool) -> None:
        """切换录制状态：更新指示灯 + 按钮文字。

        RECORDING/IDLE 状态显示 btn_start_stop，隐藏三按钮。
        """
        self._is_recording = is_recording
        # 确保三按钮隐藏（从 PAUSED 切回时干净）
        self.btn_resume.hide()
        self.btn_save.hide()
        self.btn_discard.hide()
        self.btn_start_stop.show()
        if is_recording:
            self._set_status_indicator("REC", "accent", tokens.ICON_REC)
            self.btn_start_stop.setText("停止录制")
            self.btn_start_stop.setIcon(icon("square", tokens.ICON_REC))
            self.btn_start_stop.setToolTip("停止录制 (Ctrl+R)")
        else:
            self._set_status_indicator("IDLE", "tertiary", tokens.TEXT_TERTIARY)
            self.btn_start_stop.setText("开始录制")
            self.btn_start_stop.setIcon(icon("record", tokens.ICON_REC))
            self.btn_start_stop.setToolTip("开始录制 (Ctrl+R)")

    def set_paused(self, is_paused: bool) -> None:
        """切换暂停状态：隐藏停止按钮，显示三按钮（Ticket 12 D028）。

        Args:
            is_paused: True=进入 PAUSED（显示三按钮）；False=退出 PAUSED（隐藏三按钮）
        """
        if is_paused:
            self._is_recording = True  # 仍在录制会话中（仅暂停）
            self._set_status_indicator("PAUSE", "warning", tokens.WARNING)
            self.btn_start_stop.hide()
            self.btn_resume.show()
            self.btn_save.show()
            self.btn_discard.show()
        else:
            # 退出 PAUSED：隐藏三按钮，恢复 btn_start_stop 显示（具体文字由 set_recording 控制）
            self.btn_resume.hide()
            self.btn_save.hide()
            self.btn_discard.hide()
            self.btn_start_stop.show()

    def update_duration(self, seconds: int) -> None:
        """更新计时器显示。"""
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.lbl_timer.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def update_package_name(self, name: str | None) -> None:
        """更新录制包名显示。"""
        if name:
            self.lbl_package.setText(name)
            self.lbl_package.setToolTip(f"录制包名：{name}")
        else:
            self.lbl_package.setText("(未启动)")
            self.lbl_package.setToolTip("")

    def set_mic_enabled(self, enabled: bool) -> None:
        """设置麦克风开关状态（不触发 toggled 信号）。"""
        self._mic_enabled = enabled
        self.btn_mic.blockSignals(True)
        self.btn_mic.setChecked(enabled)
        self.btn_mic.setText("麦克风:" + ("开" if enabled else "关"))
        self.btn_mic.setIcon(icon("mic" if enabled else "mic-off", tokens.ICON_DEFAULT))
        self.btn_mic.setToolTip(
            f"麦克风：{'开' if enabled else '关'}，点击{'关闭' if enabled else '开启'} (Ctrl+M)"
        )
        self.btn_mic.blockSignals(False)

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
        按规范 01-recorder §3.3：danger 按钮「丢弃」，默认按钮「取消」。
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

    # ========== QTimer 轮询 ==========

    def _on_tick(self) -> None:
        """500ms 轮询：事件流 + 窗口信息 + 统计 + 截图预览。"""
        if self._controller is None:
            return
        # 读取新增事件
        new_events = self._read_new_events()
        if new_events:
            self.event_stream.append_events(new_events)
        # 当前窗口信息（读 focus.jsonl 最后一行）
        window_info = self._read_latest_window()
        if window_info:
            self.window_info.update_info(window_info)
        # 统计（从 controller 读计数器）
        event_count = self._controller.event_count
        frame_count = self._controller.frame_count
        elapsed = (
            int(self._recorder_app.elapsed_seconds)
            if self._recorder_app is not None
            else 0
        )
        self.stats_panel.update_stats(
            event_count=event_count,
            frame_count=frame_count,
            elapsed=elapsed,
            audio_seconds=self.audio_waveform.audio_seconds,
        )
        self.update_duration(elapsed)
        # 截图预览（读 frames/ 最新 PNG）
        latest_frame = self._read_latest_frame()
        if latest_frame:
            self.frame_preview.set_latest_frame(latest_frame)
            seq = latest_frame.get("seq")
            if seq is not None and seq not in self._shown_frame_seqs:
                self._shown_frame_seqs.add(seq)
                self.frame_preview.add_thumbnail(latest_frame)
                # 限制缩略图数量
                if len(self._shown_frame_seqs) > MAX_THUMBNAILS:
                    # 不主动清理 set，只让 QListWidget 内部限制行数
                    pass
        # 录制包名
        if self._controller.package is not None:
            self.update_package_name(self._controller.package.name)

    def _on_audio_tick(self) -> None:
        """1s 轮询：音频波形 + 音频大小。"""
        if self._controller is None:
            return
        audio_file = self._controller.package.audio_file
        if audio_file is None or not audio_file.exists():
            return
        try:
            with wave.open(str(audio_file), "rb") as wf:
                n_frames = wf.getnframes()
                framerate = wf.getframerate()
                # 读最后 N 个采样用于波形（最多 1s 的数据）
                sample_count = min(n_frames, framerate)
                start = max(0, n_frames - sample_count)
                wf.setpos(start)
                raw = wf.readframes(sample_count)
            # 转换 int16 → 数值列表
            samples = _bytes_to_int16_samples(raw)
            self.audio_waveform.set_samples(samples, framerate, n_frames / framerate)
        except Exception:
            # 音频读取失败不阻塞监控面板
            pass

    # ========== 数据读取辅助方法 ==========

    def _read_new_events(self) -> list[dict]:
        """增量读取 events.jsonl 新增的行（offset 跟踪）。

        Returns:
            新事件列表（已解析为 dict），读取失败时返回空列表
        """
        if self._controller is None:
            return []
        events_file = self._controller.package.events_file
        if not events_file.exists():
            return []
        try:
            file_size = events_file.stat().st_size
            # 如果文件被重置（新录制包），重置 offset
            if file_size < self._events_offset:
                self._events_offset = 0
            if file_size == self._events_offset:
                return []
            with events_file.open("rb") as f:
                f.seek(self._events_offset)
                chunk = f.read(file_size - self._events_offset)
            self._events_offset = file_size
            # 解析每行（跳过不完整的行）
            new_events = []
            for line in chunk.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    new_events.append(json.loads(line.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # 不完整的行（控制器正在写），跳过
                    continue
            return new_events
        except OSError:
            return []

    def _read_latest_window(self) -> dict | None:
        """读 focus.jsonl 最后一行（最新焦点切换事件）。"""
        if self._controller is None:
            return None
        focus_file = self._controller.package.focus_file
        if not focus_file.exists():
            return None
        try:
            # 读最后 4KB 足够（每行约 200 字节）
            file_size = focus_file.stat().st_size
            with focus_file.open("rb") as f:
                if file_size > 4096:
                    f.seek(file_size - 4096)
                chunk = f.read()
            lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
            if not lines:
                return None
            try:
                event = json.loads(lines[-1].decode("utf-8"))
                return event.get("payload", {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
        except OSError:
            return None

    def _read_latest_frame(self) -> dict | None:
        """读 frames/ 目录下最新的 PNG 文件（按文件名 seq 排序）。

        Returns:
            {"path": Path, "seq": int} 或 None
        """
        if self._controller is None:
            return None
        frames_dir = self._controller.package.frames_dir
        if not frames_dir.exists():
            return None
        try:
            png_files = list(frames_dir.glob("frame_*.png"))
            if not png_files:
                return None
            # 按文件名 seq 排序（frame_{ms:08d}_{seq:06d}.png）
            def _seq_key(p: Path) -> tuple:
                parts = p.stem.split("_")
                if len(parts) >= 3:
                    try:
                        return (int(parts[1]), int(parts[2]))
                    except ValueError:
                        return (0, 0)
                return (0, 0)

            png_files.sort(key=_seq_key)
            latest = png_files[-1]
            seq = _seq_key(latest)[1]
            return {"path": latest, "seq": seq}
        except OSError:
            return None

    # ========== 关闭事件 ==========

    def closeEvent(self, event) -> None:
        """关闭窗口时停 QTimer。"""
        self._tick_timer.stop()
        self._audio_timer.stop()
        super().closeEvent(event)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def mic_enabled(self) -> bool:
        return self._mic_enabled


# ==================== 子面板：实时事件流 ====================


class _EventStreamPanel(QWidget):
    """实时事件流面板：QListWidget 显示最近 100 条事件。

    每行格式：[mm:ss.SSS] [类型] 描述
    类型用 BLOCK_COLORS 配色区分（鼠标蓝 / 键盘绿 / 焦点紫 / 截图青 / 其他灰）
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("实时事件流")
        set_text_role(title, "title")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        self._wrap_in_panel_frame()

    def _wrap_in_panel_frame(self) -> None:
        """包一层 Panel 框架（边框 + 圆角）。"""
        wrap = QFrame()
        wrap.setObjectName("Panel")
        # 把现有 layout 转移到 wrap
        # 实际上直接给 self 设 frame 样式即可，这里用 setObjectName 简化
        self.setObjectName("PanelContainer")

    def append_events(self, events: list[dict]) -> None:
        """追加事件到列表（自动滚动到底部）。"""
        for ev in events:
            text, color = self._format_event(ev)
            item = QListWidgetItem(text)
            item.setForeground(QColor(color))
            self.list_widget.addItem(item)
        # 限制最大行数（QListWidget.setMaximumBlockCount 不生效，手动清理）
        while self.list_widget.count() > MAX_EVENTS_DISPLAY:
            self.list_widget.takeItem(0)
        # 滚动到底部
        self.list_widget.scrollToBottom()

    def clear(self) -> None:
        self.list_widget.clear()

    def _format_event(self, ev: dict) -> tuple[str, str]:
        """格式化事件为单行文本 + 颜色。"""
        kind = ev.get("kind", "unknown")
        timestamp = ev.get("timestamp", 0.0)
        # 时间戳格式 mm:ss.SSS（相对录制开始的秒数）
        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            ts = 0.0
        mm = int(ts // 60)
        ss = int(ts % 60)
        ms = int((ts - int(ts)) * 1000)
        time_str = f"{mm:02d}:{ss:02d}.{ms:03d}"

        payload = ev.get("payload", {})
        if kind == "mouse_click":
            x = payload.get("x", 0)
            y = payload.get("y", 0)
            button = payload.get("button", "left")
            return (f"[{time_str}] 点击 {button}({x},{y})", _event_color(kind))
        if kind == "mouse_scroll":
            dx = payload.get("dx", 0)
            dy = payload.get("dy", 0)
            return (f"[{time_str}] 滚轮 dx={dx} dy={dy}", _event_color(kind))
        if kind == "mouse_drag":
            return (f"[{time_str}] 拖拽", _event_color(kind))
        if kind == "keyboard_input":
            text = payload.get("text", "")
            detection = payload.get("detection_method", "")
            preview = text[:30] + ("..." if len(text) > 30 else "")
            return (f"[{time_str}] 输入 '{preview}' [{detection}]", _event_color(kind))
        if kind == "hotkey":
            combo = payload.get("combo", "")
            return (f"[{time_str}] 热键 {combo}", _event_color(kind))
        if kind == "ime_switch":
            return (f"[{time_str}] 输入法切换", _event_color(kind))
        if kind == "password_masked":
            return (f"[{time_str}] [密码已隐藏]", _event_color(kind))
        if kind == "focus_change":
            title = payload.get("title", "")[:40]
            return (f"[{time_str}] 焦点切换 → {title}", _event_color(kind))
        if kind == "screen_frame":
            seq = payload.get("frame_seq", 0)
            return (f"[{time_str}] 截图 #{seq}", _event_color(kind))
        # 未知类型
        return (f"[{time_str}] {kind}", _event_color(kind))


# ==================== 子面板：截图预览 ====================


class _FramePreviewPanel(QWidget):
    """截图预览面板：最新帧大图 + 历史帧缩略图列表。"""

    THUMBNAIL_SIZE = 80  # 缩略图边长 px

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("实时屏幕预览")
        set_text_role(title, "title")
        layout.addWidget(title)

        # 最新帧大图（QFrame kind="card" 提供卡片背景）
        self.lbl_latest = QLabel("(无截图)")
        self.lbl_latest.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_latest.setMinimumHeight(200)
        set_text_role(self.lbl_latest, "tertiary")
        self.lbl_latest.setObjectName("EmptyState")
        layout.addWidget(self.lbl_latest, 1)

        # 历史缩略图列表
        thumbs_title = QLabel("历史帧:")
        set_text_role(thumbs_title, "caption")
        layout.addWidget(thumbs_title)

        # 用 QScrollArea 包 QListWidget 实现横向滚动
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(self.THUMBNAIL_SIZE + 28)
        self.thumbs_list = QListWidget()
        self.thumbs_list.setFlow(QListWidget.Flow.LeftToRight)
        self.thumbs_list.setWrapping(False)
        self.thumbs_list.setIconSize(
            self._icon_size()
        )
        scroll.setWidget(self.thumbs_list)
        layout.addWidget(scroll)

    def _icon_size(self):
        return QSize(self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE)

    def set_latest_frame(self, frame_info: dict) -> None:
        """设置最新帧大图。

        Args:
            frame_info: {"path": Path, "seq": int}
        """
        path = frame_info.get("path")
        if path is None or not path.exists():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        # 缩放到面板大小（保持比例）
        scaled = pixmap.scaledToHeight(
            max(200, self.lbl_latest.height() - 4),
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_latest.setPixmap(scaled)
        # 清除 EmptyState 标识（让文本角色还原）
        self.lbl_latest.setObjectName("")
        set_text_role(self.lbl_latest, "tertiary")

    def add_thumbnail(self, frame_info: dict) -> None:
        """添加缩略图到历史列表。

        Args:
            frame_info: {"path": Path, "seq": int}
        """
        path = frame_info.get("path")
        if path is None or not path.exists():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        # 缩略图（正方形裁剪）
        thumb = pixmap.scaled(
            self.THUMBNAIL_SIZE,
            self.THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        item = QListWidgetItem()
        item.setIcon(thumb)
        item.setToolTip(f"seq={frame_info.get('seq')} | {path.name}")
        self.thumbs_list.addItem(item)
        # 限制缩略图数量
        while self.thumbs_list.count() > MAX_THUMBNAILS:
            self.thumbs_list.takeItem(0)
        # 滚动到末尾
        self.thumbs_list.scrollToBottom()

    def clear(self) -> None:
        """清空所有显示。"""
        self.lbl_latest.setText("(无截图)")
        self.lbl_latest.setPixmap(QPixmap())
        # 恢复 EmptyState 标识
        self.lbl_latest.setObjectName("EmptyState")
        self.thumbs_list.clear()


# ==================== 子面板：音频波形 ====================


class _AudioWaveformPanel(QWidget):
    """音频波形面板：自定义 paintEvent 绘制 WAV 末尾采样波形。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(120)
        # 音频数据
        self._samples: list[int] = []
        self._framerate: int = 16000
        self._audio_seconds: float = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("音频波形")
        set_text_role(title, "title")
        layout.addWidget(title)

        # 波形绘制区域
        self.wave_canvas = _WaveformCanvas(self)
        layout.addWidget(self.wave_canvas, 1)

        # 时长显示（caption 小字）
        self.lbl_duration = QLabel("时长: 0.0s")
        set_text_role(self.lbl_duration, "caption")
        layout.addWidget(self.lbl_duration)

    @property
    def audio_seconds(self) -> float:
        return self._audio_seconds

    def set_samples(self, samples: list[int], framerate: int, audio_seconds: float) -> None:
        """设置波形数据并触发重绘。

        Args:
            samples: int16 采样值列表
            framerate: 采样率（Hz）
            audio_seconds: 音频总时长（秒）
        """
        self._samples = samples
        self._framerate = framerate
        self._audio_seconds = audio_seconds
        self.wave_canvas.set_samples(samples)
        self.lbl_duration.setText(f"时长: {audio_seconds:.1f}s | 采样率: {framerate}Hz")


class _WaveformCanvas(QWidget):
    """波形画布：自定义 paintEvent 绘制波形。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: list[int] = []
        self.setMinimumHeight(80)

    def set_samples(self, samples: list[int]) -> None:
        self._samples = samples
        self.update()  # 触发重绘

    def paintEvent(self, event) -> None:
        """绘制波形。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()

        # 背景（用 tokens.BG_PANEL）
        painter.fillRect(self.rect(), QColor(tokens.BG_PANEL))

        # 中线（用 tokens.BORDER）
        painter.setPen(QColor(tokens.BORDER))
        painter.drawLine(0, height // 2, width, height // 2)

        if not self._samples:
            painter.setPen(QColor(tokens.TEXT_TERTIARY))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "(无音频数据)")
            return

        # 绘制波形（柱状）
        # 每个像素一列，取该列对应采样的最大绝对值
        painter.setPen(QColor(tokens.ACCENT))
        max_amplitude = 32768.0  # int16 最大值
        n_samples = len(self._samples)
        samples_per_pixel = max(1, n_samples // width)
        mid_y = height // 2
        for x in range(width):
            start = x * samples_per_pixel
            end = min(start + samples_per_pixel, n_samples)
            if start >= n_samples:
                break
            # 取该列最大绝对值
            chunk = self._samples[start:end]
            if not chunk:
                continue
            peak = max(abs(s) for s in chunk) / max_amplitude
            bar_height = int(peak * (height // 2 - 2))
            painter.drawLine(x, mid_y - bar_height, x, mid_y + bar_height)


# ==================== 子面板：当前窗口信息 ====================


class _WindowInfoPanel(QWidget):
    """当前窗口信息面板：显示最新 focus_change 事件的窗口标题/进程/句柄。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("当前窗口")
        set_text_role(title, "title")
        layout.addWidget(title)

        form_container = QWidget()
        form = QFormLayout(form_container)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(6)

        self.lbl_title = QLabel("(未切换)")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_hwnd = QLabel("-")
        set_text_role(self.lbl_hwnd, "mono")
        self.lbl_pid = QLabel("-")
        set_text_role(self.lbl_pid, "mono")

        form.addRow("标题:", self.lbl_title)
        form.addRow("句柄:", self.lbl_hwnd)
        form.addRow("PID:", self.lbl_pid)
        layout.addWidget(form_container)
        layout.addStretch(1)

    def update_info(self, payload: dict) -> None:
        """更新当前窗口信息。

        Args:
            payload: focus_change 事件的 payload（含 hwnd/title/pid）
        """
        self.lbl_title.setText(payload.get("title", "(空标题)") or "(空标题)")
        self.lbl_hwnd.setText(str(payload.get("hwnd", "-")))
        self.lbl_pid.setText(str(payload.get("pid", "-")))


# ==================== 子面板：统计 ====================


class _StatsPanel(QWidget):
    """统计面板：时长/事件数/帧数/音频大小实时显示。

    01-recorder §3.3：统计卡片用 QFrame kind="card"，标题 tertiary，数值 mono。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("录制统计")
        set_text_role(title, "title")
        layout.addWidget(title)

        form_container = QWidget()
        form = QFormLayout(form_container)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(6)

        self.lbl_elapsed = QLabel("00:00:00")
        set_text_role(self.lbl_elapsed, "mono")
        self.lbl_event_count = QLabel("0")
        set_text_role(self.lbl_event_count, "mono")
        self.lbl_frame_count = QLabel("0")
        set_text_role(self.lbl_frame_count, "mono")
        self.lbl_audio_seconds = QLabel("0.0s")
        set_text_role(self.lbl_audio_seconds, "mono")

        form.addRow("时长:", self.lbl_elapsed)
        form.addRow("事件数:", self.lbl_event_count)
        form.addRow("帧数:", self.lbl_frame_count)
        form.addRow("音频时长:", self.lbl_audio_seconds)
        layout.addWidget(form_container)
        layout.addStretch(1)

    def update_stats(
        self,
        event_count: int,
        frame_count: int,
        elapsed: int,
        audio_seconds: float,
    ) -> None:
        """更新统计显示。"""
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        self.lbl_elapsed.setText(f"{h:02d}:{m:02d}:{s:02d}")
        self.lbl_event_count.setText(str(event_count))
        self.lbl_frame_count.setText(str(frame_count))
        self.lbl_audio_seconds.setText(f"{audio_seconds:.1f}s")


# ==================== 工具函数 ====================


def _bytes_to_int16_samples(raw: bytes, max_samples: int = 2000) -> list[int]:
    """将 int16 PCM 字节流转换为 int 数值列表（用于波形绘制）。

    Args:
        raw: WAV readframes 返回的字节流
        max_samples: 最大采样数（避免 UI 绘制过多点）

    Returns:
        int16 采样值列表
    """
    import struct

    if not raw:
        return []
    # struct.unpack 解析 int16 数组
    n = len(raw) // 2
    samples = list(struct.unpack(f"<{n}h", raw[: n * 2]))
    # 降采样（如果太多）：step 用 +1 保证结果不超过 max_samples
    # 例：5000 samples / max 2000 → step = 2 → 2500 仍超；step = 3 → 1667 安全
    if len(samples) > max_samples:
        step = len(samples) // max_samples + 1
        samples = samples[::step]
    return samples
