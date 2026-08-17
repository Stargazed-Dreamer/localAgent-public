"""L0 采集层 Ticket 08：config.toml [recording] 段加载器

设计：
- 独立加载器，不依赖 server/config.py（D018：L0 采集器是独立进程，避免拉起 server 包）
- 用 tomllib（Python 3.11+ 标准库）或 toml（项目已有依赖）解析 config.toml
- 提供 get_recording_config() 返回 dict，含全部默认值
- CLI 参数可覆盖配置值（main.py 负责合并）

配置 schema（config.toml [recording] 段）：

    [recording]
    default_mode = "small"               # small | large（GUI 启动模式）
    default_range = "fullscreen"         # fullscreen | monitor | window
    default_detail_level = "detailed"    # detailed | coarse
    default_monitor_index = 0            # range=monitor 时用
    default_window_hwnd = 0              # range=window 时用（0 = 不指定，由 ConfigDialog 选）
    max_duration_seconds = 1800          # 最大录制时长（秒）
    audio_enabled = true                 # 是否录麦克风
    audio_device = -1                    # 麦克风设备索引（-1=系统默认；通过 list_available_microphones 查可用设备）
    clipboard_enabled = false            # 是否采集剪贴板变化（默认关）
    base_dir = "workspace/recorder/recordings"    # 录制包根目录（相对项目根）

    [recording.capture]
    interval_detailed = 0.5              # 详细模式截图间隔（秒）
    interval_coarse = 2.0                # 粗略模式截图间隔（秒）
    burst_intervals_detailed = [0.05, 0.10, 0.15]   # 详细模式 click burst 时刻
    burst_intervals_coarse = [0.10, 0.20, 0.30]     # 粗略模式 click burst 时刻
    phash_threshold = 5                  # 定时截图 pHash 去重阈值（汉明距离，0=禁用）

    [recording.audio]
    sample_rate = 16000                  # 采样率（Hz）
    channels = 1                         # 声道数（1=mono）
    silence_threshold_db = -45.0         # 静音判定阈值（dBFS，麦克风偏弱 -50~-45，噪音大 -40~-35）

    [recording.stt]                      # 预留：L1 处理层使用，L0 不消费
    enabled = false                      # L1 是否启用 STT
    model = ""                           # STT 模型名（如 faster-whisper 的模型路径）
"""

from pathlib import Path
from typing import Any

# 项目根目录（workspace/recorder/tools/config_loader.py → 上 4 级是项目根）
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.toml"

# 默认配置（config.toml 缺失或 [recording] 段缺失时用）
_DEFAULTS: dict[str, Any] = {
    "default_mode": "small",
    "default_range": "fullscreen",
    "default_detail_level": "detailed",
    "default_monitor_index": 0,
    "default_window_hwnd": 0,
    "max_duration_seconds": 1800,
    "audio_enabled": True,
    "audio_device": -1,  # -1 = 系统默认；ConfigDialog 选择时改为具体 index
    "clipboard_enabled": False,
    "base_dir": "workspace/recorder/recordings",
    "capture": {
        "interval_detailed": 0.5,
        "interval_coarse": 2.0,
        "burst_intervals_detailed": [0.05, 0.10, 0.15],
        "burst_intervals_coarse": [0.10, 0.20, 0.30],
        "phash_threshold": 5,
    },
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "silence_threshold_db": -45.0,
    },
    "stt": {
        "enabled": False,
        "model": "large-v3",
        "model_dir": "",
        "language": "zh",
        "vad_threshold": 0.5,
        "junk_threshold": 0.3,
        "target_lufs": -15.0,
        "lufs_attempt": 4,
        "lufs_step": 3.0,
        "max_chars_per_line": 40,
        "min_duration_per_line": 1.5,
    },
}


def _load_toml(path: Path) -> dict:
    """加载 TOML 文件，失败返回空 dict。

    优先用 tomllib（Python 3.11+ 标准库），fallback 到 toml 包。
    """
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+

        with path.open("rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import toml

            return toml.load(str(path))
        except Exception:
            return {}
    except Exception:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（override 优先，dict 递归合并，其他类型直接覆盖）。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_recording_config(config_path: Path | None = None) -> dict:
    """加载 config.toml 的 [recording] 段，合并默认值。

    Args:
        config_path: 配置文件路径，None 时用项目根的 config.toml

    Returns:
        完整配置 dict（含全部默认值 + config.toml 覆盖值）。
        结构：
            {
                "default_mode": str,
                "default_range": str,
                "default_detail_level": str,
                "default_monitor_index": int,
                "default_window_hwnd": int,
                "max_duration_seconds": int,
                "audio_enabled": bool,
                "base_dir": str,
                "capture": {...},
                "audio": {...},
                "stt": {...},
            }
    """
    path = config_path or _CONFIG_PATH
    full_config = _load_toml(path)
    recording_section = full_config.get("recording", {})
    if not isinstance(recording_section, dict):
        return dict(_DEFAULTS)
    return _deep_merge(_DEFAULTS, recording_section)


def resolve_base_dir(base_dir: str | None = None, project_root: Path | None = None) -> Path:
    """解析录制包根目录为绝对路径。

    Args:
        base_dir: 配置或 CLI 传入的目录字符串（可能是相对路径）
        project_root: 项目根目录，None 时用本文件推导的项目根

    Returns:
        绝对路径 Path 对象
    """
    root = project_root or _PROJECT_ROOT
    if base_dir is None:
        base_dir = _DEFAULTS["base_dir"]
    p = Path(base_dir)
    if p.is_absolute():
        return p
    return (root / p).resolve()
