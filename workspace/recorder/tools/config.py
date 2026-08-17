"""L0 采集层 Ticket 06：录制器配置（RecordingConfig dataclass + 工厂函数）

设计：
- RecordingConfig 是纯数据载体，承载 ConfigDialog 用户选择 → RecorderApp 实例化传感器的参数
- 工厂函数 make_detailed_config / make_coarse_config 提供两种预设
- 自定义模式由 ConfigDialog 直接构造 RecordingConfig
- 与 lib/recorder/sensors/* 的默认参数保持一致（D025）

不依赖 Qt / pynput / mss，纯 Python dataclass，便于单测。
"""

from dataclasses import dataclass


@dataclass
class RecordingConfig:
    """录制配置（由 ConfigDialog 收集，传给 RecorderApp 实例化传感器）。

    Attributes:
        range_type: 录制范围，fullscreen / monitor / window
        monitor_index: mss 显示器索引（range_type=monitor 时用，0=主屏）
        window_hwnd: 目标窗口句柄（range_type=window 时用）
        mode: 录制模式，detailed / coarse / custom
        capture_interval: 定时截图间隔（秒），detailed=0.5 / coarse=2.0
        burst_intervals: click 多帧采样时刻（秒，相对 click 偏移），默认 (0.05, 0.10, 0.15)
        audio_enabled: 是否录麦克风
        audio_device: 麦克风设备索引（int）或名称（str）。None=系统默认。
            用户有多个麦克风时必须显式指定，否则 sounddevice 可能选错设备。
            通过 list_available_microphones() 获取可用设备列表。
        silence_threshold_db: 音频裁剪静音判定阈值（dBFS），默认 -45.0。
            D034：L0 不再自动裁剪，此值保留供 L1 处理层复用。
            麦克风偏弱时建议 -50 ~ -45；噪音大时建议 -40 ~ -35。
        clipboard_enabled: 是否采集剪贴板变化事件（默认 False）。
            开启后监听文本/图片剪贴，密码框复制自动隐藏内容。
        uia_enabled: 是否采集 UIA 树（L0 阶段 KeyboardSensor 内部已用 UIA 检测输入，
            此字段控制"是否额外写 UIA 树到文件"，L0 暂未实现，保留字段供 L1 使用）
        max_duration_seconds: 最大录制时长（秒），默认 1800（30 分钟）
        phash_threshold: 定时截图 pHash 相邻帧去重阈值（D035，汉明距离）。
            0 = 禁用去重；默认 5。仅对定时截图生效，click burst 不受影响。
    """

    range_type: str = "fullscreen"
    monitor_index: int = 0
    window_hwnd: int | None = None
    mode: str = "detailed"
    capture_interval: float = 0.5
    burst_intervals: tuple = (0.05, 0.10, 0.15)
    audio_enabled: bool = True
    audio_device: int | str | None = None
    silence_threshold_db: float = -45.0
    clipboard_enabled: bool = False
    uia_enabled: bool = True
    max_duration_seconds: int = 1800
    phash_threshold: int = 5


def make_detailed_config(range_type: str = "fullscreen") -> RecordingConfig:
    """详细模式预设：截图密（500ms）+ 焦点轮询密（300ms）+ 音频 + UIA。

    Args:
        range_type: 录制范围，默认 fullscreen
    """
    return RecordingConfig(
        range_type=range_type,
        mode="detailed",
        capture_interval=0.5,
        burst_intervals=(0.05, 0.10, 0.15),
        audio_enabled=True,
        uia_enabled=True,
    )


def make_coarse_config(range_type: str = "fullscreen") -> RecordingConfig:
    """粗略模式预设：截图疏（2s）+ 焦点轮询疏（1s）+ 音频 + UIA。

    Args:
        range_type: 录制范围，默认 fullscreen
    """
    return RecordingConfig(
        range_type=range_type,
        mode="coarse",
        capture_interval=2.0,
        burst_intervals=(0.10, 0.20, 0.30),  # 粗略模式 burst 间隔也放大
        audio_enabled=True,
        uia_enabled=True,
    )


def list_available_windows() -> list[dict]:
    """枚举所有可见窗口供 ConfigDialog 选择。

    复用 server/screen/windows.py 的 _enum_windows 实现，但本录制器独立进程运行
    （D018：L0 采集器是独立 PySide6 进程不走后端 API），所以直接 import 调用。

    Returns:
        窗口字典列表，每个含 hwnd/title/class_name/bbox/is_minimized/pid/process_name。
        无可用窗口或 pywin32/psutil 缺失时返回空列表。
    """
    try:
        # server 是项目包，录制器作为独立进程也能 import（在同项目根目录运行）
        from server.screen.windows import _enum_windows
        return _enum_windows()
    except Exception:
        return []


def list_available_monitors() -> list[dict]:
    """枚举所有物理显示器供 ConfigDialog 选择。

    用 mss 获取显示器列表。mss.monitors[0] 是虚拟屏（所有显示器合集），
    mss.monitors[1:] 是各个物理显示器。

    Returns:
        显示器字典列表，每个含：
        - index: 物理显示器索引（1-based，对应 mss.monitors[1], [2], ...）
        - width / height: 分辨率
        - left / top: 相对虚拟屏左上角的坐标（可判断主副屏布局）
        - is_primary: 是否主屏（left=0 且 top=0）
        - label: 显示用标签，如"显示器 1 (1920x1080, 主屏)"
        mss 不可用时返回空列表。
    """
    try:
        import mss
        results = []
        with mss.mss() as sct:
            # monitors[0] 是虚拟屏，跳过；从 [1:] 开始是物理显示器
            for i, mon in enumerate(sct.monitors[1:], start=1):
                left = int(mon.get("left", 0))
                top = int(mon.get("top", 0))
                width = int(mon.get("width", 0))
                height = int(mon.get("height", 0))
                is_primary = (left == 0 and top == 0)
                tag = "主屏" if is_primary else "副屏"
                label = f"显示器 {i} ({width}x{height}, {tag})"
                results.append({
                    "index": i,
                    "width": width,
                    "height": height,
                    "left": left,
                    "top": top,
                    "is_primary": is_primary,
                    "label": label,
                })
        return results
    except Exception:
        return []


def list_available_microphones() -> list[dict]:
    """枚举所有输入设备（麦克风）供 ConfigDialog 选择。

    用 sounddevice.query_devices() 获取设备列表，只保留输入设备（max_input_channels > 0）。
    这是 mic-1 修复的核心：用户有多个麦克风时，让用户显式选择，避免 sounddevice
    用系统默认设备时选错麦克风导致录音音量过低（被音频裁剪误删）。

    Returns:
        麦克风字典列表，每个含：
        - index: sounddevice 设备索引（int，传给 AudioSensor 的 device 参数）
        - name: 设备名称（如 "麦克风 (Realtek Audio)"）
        - hostapi: 主机 API 名称（如 "MME" / "Windows WASAPI"）
        - channels: 输入声道数
        - default_samplerate: 默认采样率
        - is_default: 是否系统默认输入设备
        - label: 显示用标签，如 "[0] 麦克风 (Realtek Audio) [MME, 默认]"
        sounddevice 不可用时返回空列表。
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        # query_devices() 返回 list[dict] 或单个 dict（只有一个设备时）
        if isinstance(devices, dict):
            devices = [devices]

        # 系统默认输入设备索引（用于标注）
        try:
            default_input = sd.default.device[0]  # (input, output)
        except Exception:
            default_input = None

        results = []
        for i, dev in enumerate(devices):
            max_input = int(dev.get("max_input_channels", 0))
            if max_input <= 0:
                continue  # 跳过纯输出设备
            name = str(dev.get("name", f"设备 {i}"))
            hostapi_index = int(dev.get("hostapi", 0))
            hostapi_name = "?"
            try:
                hostapi_name = sd.query_hostapis(hostapi_index).get("name", "?")
            except Exception:
                pass
            channels = max_input
            default_sr = float(dev.get("default_samplerate", 16000))
            is_default = (default_input == i)
            tag = "默认" if is_default else ""
            label = f"[{i}] {name} [{hostapi_name}]" + (f", {tag}" if is_default else "")
            results.append({
                "index": i,
                "name": name,
                "hostapi": hostapi_name,
                "channels": channels,
                "default_samplerate": default_sr,
                "is_default": is_default,
                "label": label,
            })
        return results
    except Exception:
        return []
