"""STT 重跑后台线程（QThread，避免阻塞 GUI）。

D049：用户在 AudioPanel 调阈值 + 应用配置后，后台线程跑 run_p1 重跑 STT。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


def _resolve_stt_model_dir() -> str:
    """从 config.toml [recording.stt].model_dir 读取 STT 模型目录。

    2026-07-31 T05b：移除硬编码个人路径（E:\\<data_drive>:\<working_root>\\...），改为从 config 读取，
    使组件可公开发布（Apache-2.0）。config.toml 中 [recording.stt].model_dir 配置
    实际路径（如 F:\\ai model\\stt-main\\models），空字符串则用 huggingface 默认缓存。
    """
    from workspace.recorder.tools.config_loader import get_recording_config

    stt_cfg = get_recording_config().get("stt", {})
    return stt_cfg.get("model_dir", "") or ""


class STTRunner(QThread):
    """STT 重跑后台线程。"""

    progress = Signal(str)  # 进度消息
    finished_signal = Signal(bool, str)  # (成功, 消息)

    def __init__(
        self,
        package_path: Path,
        model_name: str = "large-v3",
        device: str = "cpu",
        vad_threshold: float = 0.3,
    ) -> None:
        super().__init__()
        self.package_path = package_path
        self.model_name = model_name
        self.device = device
        self.vad_threshold = vad_threshold

    def run(self) -> None:
        """后台执行 STT 重跑。"""
        try:
            self.progress.emit("加载 STT 模型...")
            from lib.recorder.processor.p1_stt import run_p1
            from lib.recorder.processor.stt_engine import WhisperTool

            stt_model_dir = _resolve_stt_model_dir()
            transcriber = WhisperTool(model_dir=stt_model_dir)
            transcriber.load(self.model_name, device=self.device)

            self.progress.emit(f"转写中（{self.model_name}/{self.device}）...")
            result = run_p1(
                self.package_path,
                vad_threshold=self.vad_threshold,
                language="zh",
                model_name=self.model_name,
                model_dir=stt_model_dir,
                device=self.device,
                transcriber=transcriber,
            )

            self.progress.emit(f"STT 完成：{len(result)} 段")
            self.finished_signal.emit(True, f"STT 完成：{len(result)} 段")
        except Exception as e:
            logger.exception("STT 重跑失败")
            self.finished_signal.emit(False, f"STT 重跑失败：{e}")
