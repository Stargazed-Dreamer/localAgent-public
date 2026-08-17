"""L0 采集层控制器（单一测试 seam）

职责：
- 启停所有传感器
- 接收传感器上报的事件，统一打时间戳，写入录制包
- 状态机管理（Ticket 09）：IDLE → RECORDING ⇄ PAUSED → SAVED（终止）/ IDLE（discard）
- pause()/resume()/save()/discard() 支持停止后交互（D028 底部三按钮）
- stop() 时写完整 meta.json

设计：
- RecordingController 是唯一对外暴露的协调者，所有传感器通过 controller.emit_event 上报
- 时间戳由 TimestampService 统一管理，所有传感器共享同一基准
- resume() 时间轴延续（D033）：timestamp 基准不变，meta.json 记录 segments 列表，
  effective_duration = sum of segment durations
- 事件流实时写 JSONL（崩溃只丢最后几条）
- 截图/音频由对应传感器自己管理落盘（直接写 frames/ 和 audio/），只把"事件元信息"上报到 events.jsonl

线程安全：
- emit_event 可能被多个传感器线程并发调用，用 _lock 保护 JSONL 写入和计数器
- pause() 等待所有传感器停止后再写 meta.json
"""

import enum
import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from lib.recorder.package import RecordingPackage
from lib.recorder.timestamp import TimestampService
from lib.recorder.types import make_event

logger = logging.getLogger(__name__)


class RecordingState(enum.Enum):
    """录制状态机枚举（Ticket 09）。

    合法转换：
    - IDLE --start()--> RECORDING
    - RECORDING --pause()/stop()--> PAUSED
    - RECORDING --save()--> SAVED（终止）
    - RECORDING --discard()--> IDLE
    - PAUSED --resume()--> RECORDING（时间轴延续，timestamp 基准不变）
    - PAUSED --save()--> SAVED（终止）
    - PAUSED --discard()--> IDLE

    非法转换（抛 IllegalStateError）：
    - IDLE --resume()--> ✗（未启动不能恢复）
    - IDLE --save()--> ✗（无录制可保存）
    - SAVED --pause/resume/save/discard--> ✗（终止态，不可逆）
    """

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    SAVED = "saved"


class IllegalStateError(RuntimeError):
    """非法状态转换（如 SAVED 后再 pause/resume/save）。"""


class RecordingController:
    """录制控制器：协调传感器 + 汇总事件 + 写录制包 + 状态机管理。

    Args:
        base_dir: 录制包根目录的父目录（如 workspace/recorder/recordings/ 或测试用 tmp_path）
        sensors: 传感器列表，每个传感器需持有本控制器引用（可在创建后注入）
    """

    def __init__(
        self,
        base_dir: Path,
        sensors: list,
        on_trim_complete: Callable[[Path], None] | None = None,
        silence_threshold_db: float | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.sensors = list(sensors)
        self.package = RecordingPackage(base_dir=self.base_dir)
        self.timestamp = TimestampService()
        self._lock = threading.Lock()
        self._event_count = 0
        self._frame_count = 0
        self._state = RecordingState.IDLE
        # segments 列表：每项 {start_offset, end_offset}，end_offset=None 表示当前段进行中
        self._segments: list[dict] = []
        # on_trim_complete 回调 hook（D032 / D034）：L1 就绪后注入实际回调触发 STT。
        # L0 默认 no-op，save() 完成后同步调用，参数为录制包根路径。
        # L1 的回调实现可自行起线程做异步 STT 处理。
        self._on_trim_complete: Callable[[Path], None] = (
            on_trim_complete if on_trim_complete is not None else (lambda path: None)
        )
        # silence_threshold_db 保留为构造参数（D034：L0 不再自动裁剪，但 L1 可复用此值）。
        # 当前 L0 不消费，仅存储。
        self._silence_threshold_db = silence_threshold_db

    # ========== 状态机 ==========

    def start(self) -> None:
        """启动录制：创建录制包目录 + 启动所有传感器。

        幂等：非 IDLE 状态调用直接返回（已启动/已暂停/已保存）。
        支持 discard() 后复用同一 controller 重新录制。
        """
        if self._state != RecordingState.IDLE:
            return
        # 重新构造 TimestampService，确保基准是 start 时刻
        self.timestamp = TimestampService()
        # 重新构造 RecordingPackage，确保 name 用 start 时刻的时间戳
        self.package = RecordingPackage(base_dir=self.base_dir)
        self.package.create()
        # 重置计数器与段列表（支持 discard 后复用）
        self._event_count = 0
        self._frame_count = 0
        self._segments = []
        # 记录第一段起始偏移（约 0.0）
        self._segments.append({"start_offset": self.timestamp.now()[0], "end_offset": None})
        self._state = RecordingState.RECORDING
        # 启动所有传感器（MockSensor 同步发事件，真实传感器起线程）
        for sensor in self.sensors:
            sensor.start()

    def pause(self) -> None:
        """暂停录制：停所有传感器 + 写 meta.json + 状态置 PAUSED。幂等。

        时序设计（修复 Bug 1/2/3）：
        - 先停所有传感器（触发各自的 flush_buffer / 关闭流 / join 线程），
          此时 _state 仍为 RECORDING，flush_buffer 中的 emit_event 能正常写入
          events.jsonl，避免"帧写盘但事件未写入"的丢失（Bug 1/3）
        - 传感器全部停止后再关闭 segment + 设 _state=PAUSED，拒绝后续 emit_event
        - 从 AudioSensor 读 duration_seconds 传给 write_meta（Bug 2）
        - meta.json status="recording"（录制进行中只是暂停，尚未保存）
        """
        if self._state == RecordingState.IDLE:
            # 未启动直接 pause：防御性 no-op（向后兼容旧 stop() 无 start 行为）
            return
        if self._state == RecordingState.PAUSED:
            return  # 幂等
        if self._state == RecordingState.SAVED:
            raise IllegalStateError("录制已保存（SAVED），不能再 pause")
        # _state == RECORDING
        # 1. 先停所有传感器（flush buffer 时 emit_event 仍可写入，因 _state 仍 RECORDING）
        self._stop_all_sensors()
        # 2. 关闭当前 segment（记录 end_offset）
        mono_now = self.timestamp.now()[0]
        if self._segments and self._segments[-1]["end_offset"] is None:
            self._segments[-1]["end_offset"] = mono_now
        # 3. 标记 PAUSED，拒绝后续 emit_event
        self._state = RecordingState.PAUSED
        # 4. 写 meta.json（status="recording"，录制进行中只是暂停）
        self._write_meta(status="recording")

    def resume(self) -> None:
        """恢复录制：重启所有传感器 + 状态置 RECORDING。时间轴延续（D033）。

        - 不重新构造 TimestampService（基准保持 start 时刻，时间轴延续）
        - 不重新构造 RecordingPackage（继续写同一录制包）
        - 追加新 segment（start_offset = 当前 monotonic 偏移）
        """
        if self._state == RecordingState.IDLE:
            raise IllegalStateError("未启动录制（IDLE），不能 resume")
        if self._state == RecordingState.RECORDING:
            return  # 幂等
        if self._state == RecordingState.SAVED:
            raise IllegalStateError("录制已保存（SAVED），不能再 resume")
        # _state == PAUSED
        # 1. 先置 RECORDING，允许 emit_event（传感器 start 可能立即发事件）
        self._state = RecordingState.RECORDING
        # 2. 追加新 segment（start_offset 用当前偏移，timestamp 基准不变 → 时间轴延续）
        self._segments.append({"start_offset": self.timestamp.now()[0], "end_offset": None})
        # 3. 重启所有传感器
        for sensor in self.sensors:
            try:
                sensor.start()
            except Exception:
                # 单个传感器启动失败不应阻塞其它传感器
                pass

    def save(self) -> None:
        """保存录制：状态置 SAVED（不可逆）+ 写 meta.json（status="saved"）+ 调 on_trim_complete hook。

        D034：移除自动音频裁剪，mic.wav 完整保留，audio_trimmer 代码保留但不自动调用。
        L1 处理层通过 on_trim_complete 回调 hook 接入 STT 等后处理。

        - 可从 RECORDING 或 PAUSED 调用
        - 若从 RECORDING 调用：先停传感器 + 关闭当前 segment
        - 若从 PAUSED 调用：segment 已关闭
        - SAVED 为终止态，后续 pause/resume/save/discard 均拒绝
        - save() 同步完成：写 meta.json status="saved" + 调 on_trim_complete(package_path)
        - on_trim_complete 默认 no-op；L1 注入实际回调触发 STT（回调可自行起线程异步处理）
        """
        if self._state == RecordingState.IDLE:
            raise IllegalStateError("未启动录制（IDLE），不能 save")
        if self._state == RecordingState.SAVED:
            raise IllegalStateError("录制已保存（SAVED），不能重复 save")
        # 若正在录制，先停传感器并关闭 segment
        if self._state == RecordingState.RECORDING:
            self._stop_all_sensors()
            mono_now = self.timestamp.now()[0]
            if self._segments and self._segments[-1]["end_offset"] is None:
                self._segments[-1]["end_offset"] = mono_now
        # 标记 SAVED（终止态）
        self._state = RecordingState.SAVED
        # 写 meta.json（D034：直接 status="saved"，不再有 processing 中间态）
        self._write_meta(status="saved")
        # 调 on_trim_complete hook（D032 / D034：L1 STT 接入点，默认 no-op）
        try:
            self._on_trim_complete(self.package.root)
        except Exception:
            logger.exception("on_trim_complete 回调异常: package=%s", self.package.name)

    def discard(self) -> None:
        """丢弃录制：删除录制包目录 + 状态置 IDLE。

        - 可从 RECORDING 或 PAUSED 调用
        - 若从 RECORDING 调用：先停传感器
        - 删除整个录制包目录（shutil.rmtree）
        - IDLE 调用为 no-op
        - SAVED 为终止态，discard 拒绝（已保存的录制交由 Ticket 11 处理）
        """
        if self._state == RecordingState.IDLE:
            return  # 无可丢弃
        if self._state == RecordingState.SAVED:
            raise IllegalStateError("录制已保存（SAVED），不能 discard")
        # 若正在录制，先停传感器
        if self._state == RecordingState.RECORDING:
            self._stop_all_sensors()
        # 删除整个录制包目录
        try:
            if self.package.root.exists():
                shutil.rmtree(self.package.root)
        except Exception:
            # 删除失败不阻塞状态重置
            pass
        # 重置状态（保留 controller 实例可被 start() 复用）
        self._state = RecordingState.IDLE
        self._segments = []

    def stop(self) -> None:
        """停止录制（pause 的别名，向后兼容）。

        旧代码调 stop() 等价于 pause()：停传感器 + 写 meta.json + 状态 PAUSED。
        """
        self.pause()

    # ========== 事件上报 ==========

    def emit_event(self, kind: str, payload: dict) -> None:
        """传感器调用此方法上报事件。

        线程安全：加锁保护 JSONL 写入和计数器。
        仅在 RECORDING 状态接受事件（PAUSED/SAVED/IDLE 拒绝）。
        """
        if self._state != RecordingState.RECORDING:
            return
        mono, abs_t = self.timestamp.now()
        event = make_event(timestamp=mono, abs_timestamp=abs_t, kind=kind, payload=payload)
        with self._lock:
            self.package.write_event(event)
            self._event_count += 1

    def emit_focus_change(self, payload: dict) -> None:
        """焦点切换事件：同时写 events.jsonl 和 focus.jsonl。

        focus.jsonl 是 events.jsonl 的子集（仅 focus_change 事件），
        单独文件便于 L4 快速定位章节切换点，不用扫全量 events.jsonl。
        """
        if self._state != RecordingState.RECORDING:
            return
        mono, abs_t = self.timestamp.now()
        event = make_event(timestamp=mono, abs_timestamp=abs_t, kind="focus_change", payload=payload)
        with self._lock:
            self.package.write_event(event)
            self.package.write_focus(event)
            self._event_count += 1

    def increment_frame_count(self, n: int = 1) -> None:
        """传感器截图落盘后调用，累加帧数计数（用于 meta.json）。"""
        with self._lock:
            self._frame_count += n

    # ========== 内部辅助 ==========

    def _stop_all_sensors(self) -> None:
        """停所有传感器，单个失败不阻塞其它。"""
        for sensor in self.sensors:
            try:
                sensor.stop()
            except Exception:
                # 传感器停止失败不应阻塞 meta.json 写入
                pass

    def _write_meta(self, status: str) -> None:
        """写完整 meta.json（含 segments / status / effective_duration 新字段）。

        Args:
            status: 录制状态字符串（"recording" 进行中/暂停；"saved" 已保存）
        """
        end_abs = self.timestamp.base_abs + self.timestamp.now()[0]
        # 从 AudioSensor 读音频时长（Bug 2 修复）
        audio_seconds = 0.0
        for sensor in self.sensors:
            if type(sensor).__name__ == "AudioSensor":
                try:
                    audio_seconds = float(sensor.duration_seconds)
                except Exception:
                    pass
                break
        # effective_duration = 所有段时长之和（进行中的段用当前偏移近似）
        effective_duration = self._compute_effective_duration()
        # segments 序列化（深拷贝，避免外部修改内部状态）
        segments_snapshot = [
            {
                "start_offset": float(seg["start_offset"]),
                "end_offset": float(seg["end_offset"]) if seg["end_offset"] is not None else None,
            }
            for seg in self._segments
        ]
        self.package.write_meta(
            start_time=self.timestamp.base_abs,
            end_time=end_abs,
            event_count=self._event_count,
            frame_count=self._frame_count,
            audio_seconds=audio_seconds,
            sensors=[type(s).__name__ for s in self.sensors],
            status=status,
            segments=segments_snapshot,
            effective_duration=effective_duration,
        )

    def _compute_effective_duration(self) -> float:
        """有效录制时长 = sum of (end_offset - start_offset) for all segments。

        仍有 end_offset=None 的段（理论上仅在 RECORDING 状态下存在）用当前偏移近似。
        """
        total = 0.0
        mono_now = self.timestamp.now()[0]
        for seg in self._segments:
            start = float(seg["start_offset"])
            end = seg["end_offset"]
            if end is None:
                end = mono_now
            total += max(0.0, float(end) - start)
        return total

    # ========== 便捷访问器（供传感器和测试用） ==========

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_running(self) -> bool:
        """是否正在录制（仅 RECORDING 状态为 True；PAUSED/SAVED/IDLE 为 False）。"""
        return self._state == RecordingState.RECORDING

    @property
    def state(self) -> RecordingState:
        """当前状态机状态（供 GUI / 测试读取，不应外部修改）。"""
        return self._state

    @property
    def segments(self) -> list[dict]:
        """段列表快照（供测试读取，不应外部修改）。"""
        return [dict(seg) for seg in self._segments]
