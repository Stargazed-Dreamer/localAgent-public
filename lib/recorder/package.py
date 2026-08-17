"""L0 采集层录制包目录 schema

录制包目录结构（D009 / D025）：

    workspace/recorder/recordings/rec_{YYYYMMDD}_{HHMMSS}/
    ├── meta.json         # 录制元信息（开始/结束时间/事件数/版本/传感器列表）
    ├── events.jsonl      # 所有事件流（键鼠/焦点），每行一个 JSON
    ├── focus.jsonl       # 窗口焦点切换事件（M4 单独文件，方便 L4 快速定位章节）
    ├── frames/           # 屏幕截图 PNG 文件，命名 frame_{ms_offset}_{seq}.png
    └── audio/
        └── mic.wav       # 麦克风音频（16kHz/mono/float32 WAV）

设计：
- 纯目录结构，不压缩不打包（D009）
- 事件流实时写 JSONL 追加（崩溃只丢最后几条）
- 截图缓冲 10 帧批量写（减少 I/O）
- 音频流式写 WAV
- meta.json 在 stop() 时一次性写完整
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# 录制包格式版本号（每次 schema 变更 +1，L4 消费时按版本路由）
PACKAGE_FORMAT_VERSION = "0.1.0"


class RecordingPackage:
    """录制包目录的抽象：创建目录结构 + 读写各文件。

    Args:
        base_dir: 录制包根目录的父目录（如 workspace/recorder/recordings/）
        name: 录制包目录名，None 时自动生成 rec_{YYYYMMDD}_{HHMMSS}
    """

    def __init__(self, base_dir: Path, name: str | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.name = name or f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.root = self.base_dir / self.name
        self.events_file = self.root / "events.jsonl"
        self.focus_file = self.root / "focus.jsonl"
        self.frames_dir = self.root / "frames"
        self.audio_dir = self.root / "audio"
        self.audio_file = self.audio_dir / "mic.wav"
        self.meta_file = self.root / "meta.json"

    def create(self) -> None:
        """创建录制包目录结构（root + frames/ + audio/ + 空 events.jsonl + 空 focus.jsonl）。

        meta.json 不在此创建，stop() 时由 write_meta 写入。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(exist_ok=True)
        self.audio_dir.mkdir(exist_ok=True)
        # 创建空 JSONL 文件，便于传感器直接 append
        self.events_file.touch()
        self.focus_file.touch()

    def write_event(self, event: dict[str, Any]) -> None:
        """向 events.jsonl 追加一行事件。

        Args:
            event: 事件 dict，应通过 make_event() 构造
        """
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_focus(self, event: dict[str, Any]) -> None:
        """向 focus.jsonl 追加一行焦点切换事件。

        焦点事件同时也会写到 events.jsonl（由 controller 决定），
        单独文件便于 L4 快速定位章节切换点。
        """
        with self.focus_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_meta(
        self,
        start_time: float,
        end_time: float,
        event_count: int,
        frame_count: int = 0,
        audio_seconds: float = 0.0,
        sensors: list[str] | None = None,
        version: str = PACKAGE_FORMAT_VERSION,
        status: str = "recording",
        segments: list[dict[str, Any]] | None = None,
        effective_duration: float | None = None,
    ) -> None:
        """写完整 meta.json。

        Args:
            start_time: 录制开始绝对时间戳
            end_time: 录制结束绝对时间戳
            event_count: 事件总数（events.jsonl 行数）
            frame_count: 截图帧数（frames/ 下 PNG 数量）
            audio_seconds: 音频时长（秒）
            sensors: 启用的传感器名称列表
            version: 录制包格式版本
            status: 录制状态（recording 进行中/暂停；saved 已保存；
                processing 裁剪中；failed 失败）。Ticket 09 新增。
            segments: 段列表 [{start_offset, end_offset}, ...]，pause/resume 产生多段，
                end_offset=None 表示当前段进行中。Ticket 09 新增（D033）。
            effective_duration: 有效录制时长（各段时长之和），None 时回退到 end-start。
                Ticket 09 新增（D033）。
        """
        segs = [
            {
                "start_offset": float(seg["start_offset"]),
                "end_offset": float(seg["end_offset"]) if seg.get("end_offset") is not None else None,
            }
            for seg in (segments or [])
        ]
        eff_dur = (
            float(effective_duration)
            if effective_duration is not None
            else float(end_time - start_time)
        )
        meta = {
            "package_name": self.name,
            "start_time": float(start_time),
            "end_time": float(end_time),
            "duration_seconds": float(end_time - start_time),
            "effective_duration": eff_dur,
            "event_count": int(event_count),
            "frame_count": int(frame_count),
            "audio_seconds": float(audio_seconds),
            "sensors": sensors or [],
            "status": status,
            "segments": segs,
            "version": version,
            "format": "L0-sensor-layer",
        }
        with self.meta_file.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def read_events(self) -> list[dict[str, Any]]:
        """读取 events.jsonl 全部事件，按写入顺序返回。"""
        if not self.events_file.exists():
            return []
        lines = self.events_file.read_text(encoding="utf-8").strip().split("\n")
        return [json.loads(line) for line in lines if line.strip()]

    def read_meta(self) -> dict[str, Any]:
        """读取 meta.json。"""
        return json.loads(self.meta_file.read_text(encoding="utf-8"))

    def update_status(self, status: str, error: str | None = None, **extra: Any) -> None:
        """局部更新 meta.json 的 status 字段（Ticket 11，D031）。

        读取现有 meta.json → 更新 status / error / 额外字段 → 写回。
        用于后台裁剪线程切换 status: processing → saved / failed，
        避免重写整个 meta.json（保留 _write_meta 写入的其它字段）。

        Args:
            status: 新状态（"processing" / "saved" / "failed"）
            error: 失败原因，None 时移除 error 字段（成功路径）
            **extra: 附加字段（如 cropped_audio_path / original_audio_path）

        防御性：
        - meta.json 不存在（包已删除）→ no-op，避免裁剪线程写到已删除的目录
        - meta.json 解析失败 → no-op，避免覆盖损坏文件
        """
        if not self.meta_file.exists():
            return
        try:
            meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(meta, dict):
            return
        meta["status"] = status
        if error is not None:
            meta["error"] = error
        else:
            meta.pop("error", None)
        meta.update(extra)
        with self.meta_file.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
