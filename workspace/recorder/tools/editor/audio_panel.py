"""L3 音频调参面板（D049）：试音 + 阈值滑块 + 切片预览 + STT 重跑。

界面布局：
- 上半：音频波形显示（QPainter 绘制 RMS 波形）
- 中间：静音阈值滑块（-60 到 -20 dB）+ 最小静音时长滑块（0.5 到 5.0s）
- 下半：切片预览（保留段 + 删除段 + 时长统计）
- 底部：试音按钮 + 应用配置按钮（生成 mic_cropped.wav + STT 重跑）

视觉规范：docs/ui/（颜色全走 tokens）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lib.recorder.audio_trimmer import DEFAULT_SILENCE_THRESHOLD_DB, trim_audio
from lib.recorder.editor.audio_preview import preview_trim
from lib.recorder.timeline import build_timeline
from lib.ui import icon, tokens
from lib.ui.theme import set_kind, set_text_role
from workspace.recorder.tools.editor.stt_runner import STTRunner

logger = logging.getLogger(__name__)


class WaveformWidget(QWidget):
    """音频波形显示（RMS dBFS 柱状图）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(100)
        self._rms_db: list[float] = []
        self._threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB

    def set_data(self, rms_db: list[float], threshold_db: float) -> None:
        self._rms_db = rms_db
        self._threshold_db = threshold_db
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(tokens.BG_INPUT))

        if not self._rms_db:
            painter.setPen(QColor(tokens.TEXT_TERTIARY))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "加载音频后显示波形")
            return

        w = self.width()
        h = self.height()
        n = len(self._rms_db)
        bar_w = max(1, w / n)

        # dB 范围：-60 到 0
        db_min, db_max = -60.0, 0.0

        for i, db in enumerate(self._rms_db):
            # 归一化到 0-1
            normalized = max(0.0, min(1.0, (db - db_min) / (db_max - db_min)))
            bar_h = normalized * h
            x = int(i * bar_w)
            y = h - int(bar_h)
            # 低于阈值用 tertiary，高于用 ACCENT
            color = QColor(tokens.TEXT_TERTIARY) if db < self._threshold_db else QColor(tokens.ACCENT)
            painter.fillRect(x, y, max(1, int(bar_w)), int(bar_h), color)

        # 阈值线
        threshold_normalized = (self._threshold_db - db_min) / (db_max - db_min)
        threshold_y = h - int(threshold_normalized * h)
        painter.setPen(QPen(QColor(tokens.DANGER), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, threshold_y, w, threshold_y)


class AudioPanel(QDialog):
    """音频调参面板（D049）。"""

    def __init__(self, package_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.package_path = package_path
        self._stt_runner: STTRunner | None = None
        self._build_ui()
        self._load_and_preview()

    def _build_ui(self) -> None:
        self.setWindowTitle("音频调参 - STT 重跑")
        self.resize(800, 600)

        layout = QVBoxLayout(self)

        # 上半：波形
        self.waveform = WaveformWidget()
        waveform_caption = QLabel("音频波形（青=保留，灰=删除，红虚线=阈值）")
        set_text_role(waveform_caption, "secondary")
        layout.addWidget(waveform_caption)
        layout.addWidget(self.waveform)

        # 中间：滑块
        slider_layout = QHBoxLayout()

        # 阈值滑块
        threshold_box = QVBoxLayout()
        threshold_box.addWidget(QLabel("静音阈值（dBFS）"))
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(-60, -20)
        self.threshold_slider.setValue(int(DEFAULT_SILENCE_THRESHOLD_DB))
        self.threshold_label = QLabel(f"{DEFAULT_SILENCE_THRESHOLD_DB:.0f} dB")
        set_text_role(self.threshold_label, "mono")
        self.threshold_slider.valueChanged.connect(self._on_threshold_changed)
        threshold_box.addWidget(self.threshold_slider)
        threshold_box.addWidget(self.threshold_label)
        slider_layout.addLayout(threshold_box)

        # 最小静音时长滑块
        duration_box = QVBoxLayout()
        duration_box.addWidget(QLabel("最小静音时长（秒）"))
        self.duration_slider = QSlider(Qt.Orientation.Horizontal)
        self.duration_slider.setRange(5, 50)  # 0.5s 到 5.0s（×0.1）
        self.duration_slider.setValue(15)  # 1.5s
        self.duration_label = QLabel("1.5 s")
        set_text_role(self.duration_label, "mono")
        self.duration_slider.valueChanged.connect(self._on_duration_changed)
        duration_box.addWidget(self.duration_slider)
        duration_box.addWidget(self.duration_label)
        slider_layout.addLayout(duration_box)

        layout.addLayout(slider_layout)

        # 下半：切片预览
        layout.addWidget(QLabel("切片预览"))
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(150)
        layout.addWidget(self.preview_text)

        # 底部：按钮
        button_layout = QHBoxLayout()
        self.play_button = QPushButton("试音")
        self.play_button.setIcon(icon("play", tokens.ICON_DEFAULT))
        self.play_button.clicked.connect(self._on_play)
        self.apply_button = QPushButton("应用配置 + STT 重跑")
        self.apply_button.setIcon(icon("check-circle", tokens.SUCCESS))
        set_kind(self.apply_button, "primary")
        self.apply_button.clicked.connect(self._on_apply)
        button_layout.addWidget(self.play_button)
        button_layout.addWidget(self.apply_button)
        layout.addLayout(button_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        set_text_role(self.progress_label, "tertiary")
        layout.addWidget(self.progress_label)

    def _load_and_preview(self) -> None:
        """加载音频并预览。"""
        wav_path = self.package_path / "audio" / "mic.wav"
        if not wav_path.exists():
            self.preview_text.setPlainText("[mic.wav 不存在]")
            return
        self._update_preview()

    def _on_threshold_changed(self, value: int) -> None:
        self.threshold_label.setText(f"{value} dB")
        self._update_preview()

    def _on_duration_changed(self, value: int) -> None:
        duration = value / 10.0
        self.duration_label.setText(f"{duration:.1f} s")
        self._update_preview()

    def _update_preview(self) -> None:
        """更新切片预览（实时响应滑块变化）。"""
        wav_path = self.package_path / "audio" / "mic.wav"
        if not wav_path.exists():
            return

        threshold_db = float(self.threshold_slider.value())
        min_duration = self.duration_slider.value() / 10.0

        result = preview_trim(wav_path, threshold_db=threshold_db, min_duration=min_duration)

        # 更新波形
        self.waveform.set_data(result["rms_db"], threshold_db)

        # 更新预览文本
        text = (
            f"原始时长: {result['original_duration']:.2f}s\n"
            f"保留时长: {result['kept_duration']:.2f}s ({result['kept_duration']/result['original_duration']*100:.1f}%)\n"
            f"删除时长: {result['removed_duration']:.2f}s ({result['removed_duration']/result['original_duration']*100:.1f}%)\n"
            f"保留段数: {len(result['kept_segments'])}，删除段数: {len(result['removed_segments'])}\n"
        )
        if result["kept_segments"]:
            text += "\n保留段（前 5 个）:\n"
            for seg in result["kept_segments"][:5]:
                text += f"  {seg['original_start']:.2f}s - {seg['original_end']:.2f}s\n"
        self.preview_text.setPlainText(text)

    def _on_play(self) -> None:
        """试音（播放第一个保留段，简化版用系统默认播放器）。"""
        import subprocess

        wav_path = self.package_path / "audio" / "mic.wav"
        if wav_path.exists():
            # Windows 用 start 命令打开默认播放器
            subprocess.Popen(["cmd", "/c", "start", str(wav_path)], shell=False)

    def _on_apply(self) -> None:
        """应用配置：生成 mic_cropped.wav + STT 重跑。"""
        reply = QMessageBox.question(
            self, "应用配置",
            "将生成 mic_cropped.wav 并重跑 STT（large-v3/CPU，预计 1-2 分钟）。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.apply_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不确定进度
        self.progress_label.setText("生成剪辑音频...")

        # 1. 剪辑音频（去除静音段）
        wav_path = self.package_path / "audio" / "mic.wav"
        threshold_db = float(self.threshold_slider.value())
        min_duration = self.duration_slider.value() / 10.0

        try:
            cropped_wav, segments_json = trim_audio(
                wav_path,
                output_dir=wav_path.parent,
                silence_threshold_db=threshold_db,
                silence_min_duration=min_duration,
            )
            self.progress_label.setText(f"剪辑完成：{cropped_wav.name}")

            # 2. STT 重跑
            self.progress_label.setText("STT 重跑中...")
            self._stt_runner = STTRunner(
                self.package_path,
                model_name="large-v3",
                device="cpu",
                vad_threshold=0.3,
            )
            self._stt_runner.progress.connect(self._on_stt_progress)
            self._stt_runner.finished_signal.connect(self._on_stt_finished)
            self._stt_runner.start()
        except Exception as e:
            logger.exception("应用配置失败")
            QMessageBox.critical(self, "错误", f"应用配置失败：{e}")
            self._reset_ui()

    def _on_stt_progress(self, msg: str) -> None:
        self.progress_label.setText(msg)

    def _on_stt_finished(self, success: bool, msg: str) -> None:
        """STT 重跑完成。"""
        if success:
            # 3. 重建 timeline.json（因为 transcript 变了）
            self.progress_label.setText("重建 timeline.json...")
            try:
                build_timeline(self.package_path)
                self.progress_label.setText("全部完成！")
                QMessageBox.information(self, "完成", msg + "\n timeline.json 已重建")
                self.accept()
            except Exception as e:
                QMessageBox.warning(self, "部分完成", f"STT 完成但重建 timeline 失败：{e}")
        else:
            QMessageBox.critical(self, "STT 失败", msg)
        self._reset_ui()

    def _reset_ui(self) -> None:
        self.apply_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")
        self._stt_runner = None
