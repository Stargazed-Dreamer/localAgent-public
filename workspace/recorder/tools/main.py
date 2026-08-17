"""L0 采集层 Ticket 08：录制器启动入口（main.py）

设计：
- CLI 参数解析（argparse）：--mode / --range / --monitor-index / --window-hwnd /
  --detail-level / --max-duration / --no-audio / --config / --base-dir
- 加载 config.toml [recording] 段作为默认值
- CLI 参数覆盖配置值
- 缺失必要参数时弹出 ConfigDialog 让用户选择
- 启动 QApplication + FloatingBar（小模式）或 MonitorWindow（大模式）
- wire GUI 信号到 RecorderApp（开始/停止/麦克风开关）
- 大/小模式切换：通过热键 Ctrl+Alt+M 切换（运行时切换，共享 controller）

启动方式：
    # 小模式 + 默认配置
    python -m workspace.recorder.tools.main

    # 大模式（双屏监控）
    python -m workspace.recorder.tools.main --mode large

    # 指定范围 + 粗略模式
    python -m workspace.recorder.tools.main --range monitor --monitor-index 1 --detail-level coarse

    # 指定窗口
    python -m workspace.recorder.tools.main --range window --window-hwnd 12345

    # 跳过 ConfigDialog 直接开始录制（CLI 参数齐全时）
    python -m workspace.recorder.tools.main --range fullscreen --detail-level detailed --autostart

设计偏离说明：
- 05-gui-design.md 待确认设计点 1"录制器与编辑器的启动方式"——本 ticket 用独立 main.py 入口
  （python -m workspace.recorder.tools.main），不走 client 面板集成（独立进程 D018）
- 待确认设计点 2"双屏自动切换"——本 ticket 不自动检测双屏，由用户通过 --mode 指定
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from lib.ui import apply_theme
from workspace.recorder.tools.config import (
    RecordingConfig,
    list_available_windows,
    make_coarse_config,
    make_detailed_config,
)
from workspace.recorder.tools.config_loader import get_recording_config, resolve_base_dir
from workspace.recorder.tools.floating_bar import FloatingBar
from workspace.recorder.tools.monitor_window import MonitorWindow
from workspace.recorder.tools.recorder_app import RecorderApp

# ========== CLI 参数解析 ==========


def build_arg_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="workspace.recorder.tools.main",
        description="L0 采集层录制器（操作录制 GUI）",
    )
    parser.add_argument(
        "--mode",
        choices=["small", "large"],
        default=None,
        help="GUI 模式：small=悬浮条 / large=监控面板（默认从 config.toml 读取，缺失时 small）",
    )
    parser.add_argument(
        "--range",
        choices=["fullscreen", "monitor", "window"],
        default=None,
        help="录制范围：fullscreen=全屏 / monitor=某屏 / window=某窗口",
    )
    parser.add_argument(
        "--monitor-index",
        type=int,
        default=None,
        help="显示器索引（--range=monitor 时用，0=主屏）",
    )
    parser.add_argument(
        "--window-hwnd",
        type=int,
        default=None,
        help="目标窗口句柄（--range=window 时用，10 进制）",
    )
    parser.add_argument(
        "--detail-level",
        choices=["detailed", "coarse"],
        default=None,
        help="录制模式：detailed=详细（500ms 截图）/ coarse=粗略（2s 截图）",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=None,
        help="最大录制时长（秒），默认 1800（30 分钟）",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="禁用麦克风录制（默认开启音频）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（默认项目根 config.toml）",
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="录制包根目录（默认 workspace/recorder/recordings/）",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="跳过 ConfigDialog，直接用 CLI 参数开始录制（参数齐全时生效）",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="无 GUI 模式（仅 CLI + 传感器，用于冒烟测试；需配合 --autostart 和 --max-duration）",
    )
    return parser


# ========== 配置合并 ==========


def merge_cli_and_config(args: argparse.Namespace, config_dict: dict) -> dict:
    """合并 CLI 参数和 config.toml 配置（CLI 优先）。

    Returns:
        合并后的配置 dict（含 mode / range / detail_level / monitor_index /
        window_hwnd / max_duration / audio_enabled / base_dir / capture / audio / stt）
    """
    merged = dict(config_dict)
    if args.mode is not None:
        merged["default_mode"] = args.mode
    if args.range is not None:
        merged["default_range"] = args.range
    if args.monitor_index is not None:
        merged["default_monitor_index"] = args.monitor_index
    if args.window_hwnd is not None:
        merged["default_window_hwnd"] = args.window_hwnd
    if args.detail_level is not None:
        merged["default_detail_level"] = args.detail_level
    if args.max_duration is not None:
        merged["max_duration_seconds"] = args.max_duration
    if args.no_audio:
        merged["audio_enabled"] = False
    if args.base_dir is not None:
        merged["base_dir"] = args.base_dir
    return merged


def build_recording_config(merged: dict) -> RecordingConfig:
    """从合并后的配置 dict 构造 RecordingConfig dataclass。"""
    detail_level = merged["default_detail_level"]
    range_type = merged["default_range"]

    if detail_level == "detailed":
        cfg = make_detailed_config(range_type=range_type)
    elif detail_level == "coarse":
        cfg = make_coarse_config(range_type=range_type)
    else:
        # custom 或未知，用 detailed 作 fallback
        cfg = make_detailed_config(range_type=range_type)

    # 覆盖范围相关字段
    if range_type == "monitor":
        cfg.monitor_index = int(merged.get("default_monitor_index", 0))
    elif range_type == "window":
        hwnd = int(merged.get("default_window_hwnd", 0))
        cfg.window_hwnd = hwnd if hwnd > 0 else None

    # 覆盖全局字段
    cfg.max_duration_seconds = int(merged.get("max_duration_seconds", 1800))
    cfg.audio_enabled = bool(merged.get("audio_enabled", True))
    cfg.clipboard_enabled = bool(merged.get("clipboard_enabled", False))

    # 麦克风设备（config.toml 用 -1 表示系统默认；RecordingConfig 用 None 表示系统默认）
    audio_device_raw = merged.get("audio_device", -1)
    try:
        audio_device_idx = int(audio_device_raw)
        cfg.audio_device = None if audio_device_idx < 0 else audio_device_idx
    except (TypeError, ValueError):
        cfg.audio_device = None

    # 静音阈值（从 [recording.audio] silence_threshold_db 读取）
    audio_cfg = merged.get("audio", {})
    if "silence_threshold_db" in audio_cfg:
        try:
            cfg.silence_threshold_db = float(audio_cfg["silence_threshold_db"])
        except (TypeError, ValueError):
            pass

    # 覆盖 capture 参数（如果 config.toml 自定义了）
    capture = merged.get("capture", {})
    if detail_level == "detailed":
        if "interval_detailed" in capture:
            cfg.capture_interval = float(capture["interval_detailed"])
        if "burst_intervals_detailed" in capture:
            cfg.burst_intervals = tuple(capture["burst_intervals_detailed"])
    elif detail_level == "coarse":
        if "interval_coarse" in capture:
            cfg.capture_interval = float(capture["interval_coarse"])
        if "burst_intervals_coarse" in capture:
            cfg.burst_intervals = tuple(capture["burst_intervals_coarse"])

    # pHash 去重阈值（D035 / D036）
    if "phash_threshold" in capture:
        try:
            cfg.phash_threshold = int(capture["phash_threshold"])
        except (TypeError, ValueError):
            pass

    return cfg


# ========== GUI 协调器 ==========


class _OverlayController(QObject):
    """截图隐藏 FloatingBar 的跨线程信号桥。

    ScreenCaptureSensor 在子线程调用 hide_overlay()/show_overlay()，
    通过 emit 信号把透明度切换排到 Qt 主线程执行（Qt 信号天然跨线程安全）。

    用 setWindowOpacity 替代 hide/show：避免窗口重建闪烁，终止按钮位置稳定可点。
    """

    set_overlay_visible_requested = Signal(bool)

    def __init__(self, floating_bar: FloatingBar) -> None:
        super().__init__()
        # QueuedConnection 自动用于跨线程 emit
        self.set_overlay_visible_requested.connect(floating_bar.set_overlay_visible)

    def hide_overlay(self) -> None:
        """子线程调用：emit 信号到主线程把 FloatingBar 设为完全透明。"""
        self.set_overlay_visible_requested.emit(False)

    def show_overlay(self) -> None:
        """子线程调用：emit 信号到主线程恢复 FloatingBar 半透明可见。"""
        self.set_overlay_visible_requested.emit(True)


class _GUIController(QObject):
    """GUI 协调器：管理 FloatingBar / MonitorWindow / RecorderApp 之间的 wire。

    职责：
    - 持有 RecorderApp 实例
    - 持有 FloatingBar 和 MonitorWindow 实例（同时存在，按 mode 显示一个）
    - wire GUI 信号到 RecorderApp（开始/停止/麦克风开关）
    - 用 QTimer 500ms 更新 GUI（时长/事件数/帧数）
    - 支持运行时大/小模式切换（hide 一个 + show 另一个，共享 controller）
    - 注入 overlay_callbacks 到 RecorderApp，截图瞬间隐藏 FloatingBar
    """

    # 切换模式信号（用于热键触发）
    mode_switched = Signal(str)

    def __init__(self, recorder_app: RecorderApp, mode: str = "small") -> None:
        super().__init__()
        self.recorder = recorder_app
        self.mode = mode
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        # 创建两个 GUI（同时存在，按 mode 显示一个）
        self.floating_bar = FloatingBar()
        self.monitor_window = MonitorWindow()

        # 截图隐藏 FloatingBar 的跨线程信号桥
        self._overlay_ctrl = _OverlayController(self.floating_bar)
        # 注入回调到 RecorderApp（如果还没启动，下次 start 时生效）
        self.recorder._capture_overlay_callbacks = (
            self._overlay_ctrl.hide_overlay,
            self._overlay_ctrl.show_overlay,
        )

        # wire 信号（两个 GUI 的信号都连到同一组 handler）
        self.floating_bar.start_requested.connect(self._on_start)
        self.floating_bar.stop_requested.connect(self._on_stop)
        self.floating_bar.mic_toggled.connect(self._on_mic_toggled)
        self.floating_bar.resume_requested.connect(self._on_resume)
        self.floating_bar.save_requested.connect(self._on_save)
        self.floating_bar.discard_requested.connect(self._on_discard)

        self.monitor_window.start_requested.connect(self._on_start)
        self.monitor_window.stop_requested.connect(self._on_stop)
        self.monitor_window.mic_toggled.connect(self._on_mic_toggled)
        self.monitor_window.resume_requested.connect(self._on_resume)
        self.monitor_window.save_requested.connect(self._on_save)
        self.monitor_window.discard_requested.connect(self._on_discard)

        # 显示对应模式的 GUI
        self._show_mode(mode)

    def _show_mode(self, mode: str) -> None:
        """显示对应模式的 GUI，隐藏另一个。"""
        self.mode = mode
        if mode == "large":
            self.floating_bar.hide()
            self.monitor_window.show()
        else:
            self.monitor_window.hide()
            self.floating_bar.show()
            # 启动时定位到屏幕顶部中央，避免默认屏幕中央挡住操作区域
            self.floating_bar.move_to_top_center()

    def switch_mode(self, mode: str) -> None:
        """运行时切换模式（共享 controller，不中断录制）。"""
        if mode == self.mode:
            return
        # 如果正在录制，先 detach 旧 GUI
        if self.recorder.is_running and self.mode == "large":
            self.monitor_window.detach_recorder()
        self._show_mode(mode)
        # 如果正在录制，attach 新 GUI
        if self.recorder.is_running and mode == "large":
            self.monitor_window.attach_recorder(self.recorder)
        self.mode_switched.emit(mode)

    def _on_start(self) -> None:
        """开始录制。"""
        try:
            self.recorder.start()
        except Exception as e:
            print(f"[recorder] 启动失败：{e}", file=sys.stderr)
            return
        # 更新 GUI 状态
        self.floating_bar.set_recording(True)
        self.monitor_window.set_recording(True)
        # 大模式 attach recorder
        if self.mode == "large":
            self.monitor_window.attach_recorder(self.recorder)
        # 启动 UI 刷新定时器
        self._timer.start(500)

    def _on_stop(self) -> None:
        """停止按钮（RECORDING → PAUSED）：调 controller.pause()，显示三按钮。

        D028：停止后交互 = 底部三按钮（继续/保存/重录），不弹窗。
        注意：调 controller.pause() 而非 recorder.stop()，保留 _is_running 和
        自动停止计时器（resume 时无需重建）。
        """
        # 调 controller.pause()（停传感器 + 写 meta + 状态 PAUSED）
        if self.recorder._controller is not None:
            self.recorder._controller.pause()
        # 大模式 detach recorder（停 QTimer 轮询）
        if self.mode == "large":
            self.monitor_window.detach_recorder()
        # UI 切到 PAUSED（显示三按钮，隐藏停止按钮）
        self.floating_bar.set_paused(True)
        self.monitor_window.set_paused(True)
        # 停 UI 刷新定时器（PAUSED 不需要刷新时长/事件计数）
        self._timer.stop()

    def _on_resume(self) -> None:
        """继续录制按钮（PAUSED → RECORDING）：调 controller.resume()，切回停止按钮。

        D033：resume 时间轴延续（timestamp 基准不变）。
        """
        # 调 controller.resume()（重启传感器 + 状态 RECORDING + 追加新 segment）
        if self.recorder._controller is not None:
            self.recorder._controller.resume()
        # UI 切回 RECORDING（显示停止按钮，隐藏三按钮）
        self.floating_bar.set_recording(True)
        self.monitor_window.set_recording(True)
        # 大模式重新 attach recorder（重启 QTimer 轮询）
        if self.mode == "large":
            self.monitor_window.attach_recorder(self.recorder)
        # 重启 UI 刷新定时器
        self._timer.start(500)

    def _on_save(self) -> None:
        """保存结束按钮：调 controller.save() + 关闭录制窗口。

        D029：保存后立即关闭录制窗口。
        时序：先 recorder.stop()（cancel timers + controller.pause()=no-op，
              因 _on_stop 已将 controller 置 PAUSED），再 controller.save()（PAUSED → SAVED）。
        不能在 save 后调 recorder.stop()，因 controller.pause() 在 SAVED 态会抛 IllegalStateError。
        """
        # 先停 RecorderApp（cancel 自动停止计时器 + controller.pause() 是 no-op，因已 PAUSED）
        self.recorder.stop()
        # 再 save controller（PAUSED → SAVED，写 meta.json status=saved）
        if self.recorder._controller is not None:
            self.recorder._controller.save()
        # 停 UI 刷新定时器
        self._timer.stop()
        # 大模式 detach recorder
        if self.mode == "large":
            self.monitor_window.detach_recorder()
        # 关闭录制窗口（D029）
        self.floating_bar.close()
        self.monitor_window.close()

    def _on_discard(self) -> None:
        """丢弃重录按钮：调 controller.discard() + 回 IDLE。

        D030：丢弃重录 = 弹窗确认后直接删除录制包目录。
        时序：先 recorder.stop()（cancel timers + controller.pause()=no-op，
              因 _on_stop 已将 controller 置 PAUSED），再 controller.discard()（PAUSED → IDLE，
              删除录制包目录）。
        """
        # 先停 RecorderApp（cancel 自动停止计时器 + controller.pause() 是 no-op，因已 PAUSED）
        self.recorder.stop()
        # 再 discard controller（PAUSED → IDLE，删除录制包目录）
        if self.recorder._controller is not None:
            self.recorder._controller.discard()
        # 停 UI 刷新定时器
        self._timer.stop()
        # 大模式 detach recorder
        if self.mode == "large":
            self.monitor_window.detach_recorder()
        # UI 切回 IDLE（显示开始按钮）
        self.floating_bar.set_recording(False)
        self.monitor_window.set_recording(False)

    def _on_mic_toggled(self, enabled: bool) -> None:
        """麦克风开关（同步两个 GUI 的状态）。"""
        # TODO: 实际控制 AudioSensor 的启停（L0 阶段 AudioSensor 不支持运行时切换，
        # 此处仅同步 GUI 状态，下次录制时按 config.audio_enabled 生效）
        self.floating_bar.set_mic_enabled(enabled)
        self.monitor_window.set_mic_enabled(enabled)

    def _on_tick(self) -> None:
        """500ms 刷新 GUI（时长/事件数/帧数）。"""
        if not self.recorder.is_running:
            return
        elapsed = int(self.recorder.elapsed_seconds)
        events = self.recorder.current_event_count
        frames = self.recorder.current_frame_count
        package_name = self.recorder.recording_package_name or ""

        self.floating_bar.update_duration(elapsed)
        self.floating_bar.update_event_count(events)
        self.floating_bar.update_frame_count(frames)

        self.monitor_window.update_duration(elapsed)
        self.monitor_window.update_package_name(package_name)

    def shutdown(self) -> None:
        """关闭时清理（停录制 + 停 timer + detach）。"""
        self._timer.stop()
        if self.mode == "large":
            self.monitor_window.detach_recorder()
        if self.recorder.is_running:
            self.recorder.stop()


# ========== 主入口 ==========


def _maybe_show_config_dialog(args: argparse.Namespace, merged: dict, qapp: QApplication) -> dict | None:
    """如果 CLI 参数齐全 + --autostart，跳过对话框；否则弹 ConfigDialog。

    Returns:
        合并后的配置 dict（用户点开始时），或 None（用户取消）
    """
    # --autostart 且必要参数齐全时跳过对话框
    has_required = (
        args.range is not None
        and args.detail_level is not None
    )
    if args.autostart and has_required:
        return merged

    # 弹 ConfigDialog
    from workspace.recorder.tools.config import list_available_microphones
    from workspace.recorder.tools.config_dialog import ConfigDialog

    dialog = ConfigDialog(
        windows_provider=list_available_windows,
        microphones_provider=list_available_microphones,
    )
    if dialog.exec() != ConfigDialog.DialogCode.Accepted:
        return None
    cfg = dialog.get_config()
    if cfg is None:
        return None

    # 用 ConfigDialog 的结果覆盖 merged（但保留 base_dir / max_duration 等全局配置）
    merged = dict(merged)
    merged["default_range"] = cfg.range_type
    merged["default_detail_level"] = cfg.mode if cfg.mode in ("detailed", "coarse") else "detailed"
    merged["default_monitor_index"] = cfg.monitor_index
    merged["default_window_hwnd"] = cfg.window_hwnd or 0
    merged["max_duration_seconds"] = cfg.max_duration_seconds
    merged["audio_enabled"] = cfg.audio_enabled
    merged["clipboard_enabled"] = cfg.clipboard_enabled
    # 麦克风设备（ConfigDialog 选的设备索引，None→-1 表示系统默认）
    merged["audio_device"] = -1 if cfg.audio_device is None else int(cfg.audio_device)
    # 静音阈值写入 [recording.audio] 段（D034：L0 不再自动裁剪，此值保留供 L1 复用）
    audio_section = dict(merged.get("audio", {}))
    audio_section["silence_threshold_db"] = cfg.silence_threshold_db
    merged["audio"] = audio_section
    # pHash 去重阈值写入 [recording.capture] 段（D035 / D036）
    capture_section = dict(merged.get("capture", {}))
    capture_section["phash_threshold"] = cfg.phash_threshold
    merged["capture"] = capture_section
    # 保存 RecordingConfig 的 capture 参数（用户可能选了 custom 模式）
    merged["_recording_config"] = cfg
    return merged


def _run_no_gui(args: argparse.Namespace, merged: dict) -> int:
    """无 GUI 模式（仅 CLI + 传感器，用于冒烟测试）。

    Returns:
        退出码（0=成功，1=失败）
    """
    cfg = merged["_recording_config"] if "_recording_config" in merged else build_recording_config(merged)

    base_dir = resolve_base_dir(merged.get("base_dir"))
    base_dir.mkdir(parents=True, exist_ok=True)

    recorder = RecorderApp(config=cfg, base_dir=base_dir)
    print(f"[recorder] 无 GUI 模式启动，录制 {cfg.max_duration_seconds}s")
    print(f"[recorder] 录制包目录：{base_dir}")
    recorder.start()

    # 阻塞等待自动停止（max_duration_seconds 后自动 stop）
    try:
        while recorder.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[recorder] 用户中断，停止录制...")
        recorder.stop()

    package_name = recorder.recording_package_name or "unknown"
    print(f"[recorder] 录制完成，包名：{package_name}")
    print(f"[recorder] 事件数：{recorder.current_event_count}，帧数：{recorder.current_frame_count}")
    return 0


def _run_gui(args: argparse.Namespace, merged: dict, qapp: QApplication) -> int:
    """GUI 模式启动。"""
    # 弹 ConfigDialog（除非 --autostart）
    result = _maybe_show_config_dialog(args, merged, qapp)
    if result is None:
        print("[recorder] 用户取消，退出")
        return 0
    merged = result

    # 构造 RecordingConfig
    cfg = merged["_recording_config"] if "_recording_config" in merged else build_recording_config(merged)

    # 解析 base_dir
    base_dir = resolve_base_dir(merged.get("base_dir"))
    base_dir.mkdir(parents=True, exist_ok=True)

    # 创建 RecorderApp
    recorder = RecorderApp(config=cfg, base_dir=base_dir)

    # 创建 GUI 协调器
    mode = merged.get("default_mode", "small")
    controller = _GUIController(recorder_app=recorder, mode=mode)

    # 注册全局热键（Ctrl+Alt+R 开始/停止）— 根据当前状态切换 start/stop
    from workspace.recorder.tools.hotkey_manager import HotkeyManager

    def _on_hotkey_start_stop() -> None:
        if recorder.is_running:
            controller._on_stop()
        else:
            controller._on_start()

    # 挂到 controller 上保持引用，避免 listener 被 GC（HotkeyManager 后台运行）
    controller.hotkey_mgr = HotkeyManager(on_start_stop=_on_hotkey_start_stop)

    # --autostart 时延迟 100ms 自动开始录制（等 Qt 事件循环就绪）
    if args.autostart:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(100, controller._on_start)
        print(f"[recorder] GUI 模式启动（{mode}），自动开始录制，热键 Ctrl+Alt+R 开始/停止")
    else:
        print(f"[recorder] GUI 模式启动（{mode}），热键 Ctrl+Alt+R 开始/停止录制")

    # 运行 Qt 事件循环
    try:
        exit_code = qapp.exec()
    finally:
        controller.shutdown()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """主入口。

    Args:
        argv: 命令行参数，None 时用 sys.argv
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # 加载 config.toml [recording] 段
    config_path = Path(args.config) if args.config else None
    config_dict = get_recording_config(config_path)

    # 合并 CLI 参数（CLI 优先）
    merged = merge_cli_and_config(args, config_dict)

    # 无 GUI 模式
    if args.no_gui:
        if not args.autostart:
            print("[recorder] --no-gui 模式需要 --autostart 参数", file=sys.stderr)
            return 1
        return _run_no_gui(args, merged)

    # GUI 模式
    qapp = QApplication.instance() or QApplication(sys.argv)
    apply_theme(qapp)
    return _run_gui(args, merged, qapp)


if __name__ == "__main__":
    sys.exit(main())
