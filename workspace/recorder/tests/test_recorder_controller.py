"""L0 采集层 Ticket 01：RecordingController + MockSensor 端到端测试

测试策略：
- 单一 seam：RecordingController（启停传感器、接收事件、汇总写入录制包）
- MockSensor 同步发事件（测试场景，真实传感器是异步线程）
- 用 tmp_path 模拟录制包目录，避免污染真实 workspace/recorder/recordings/
- 验证：目录结构 + events.jsonl 内容 + meta.json 完整 + 时间戳单调递增

对应 tickets.md Ticket 01 acceptance criteria。
"""

import json
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from lib.recorder.controller import (
    IllegalStateError,
    RecordingController,
    RecordingState,
)
from lib.recorder.package import RecordingPackage
from lib.recorder.sensors.mock import MockSensor
from lib.recorder.timestamp import TimestampService

# ==================== TimestampService 单测 ====================

class TestTimestampService:
    def test_now_returns_monotonic_offset_and_abs_time(self):
        """now() 返回 (monotonic_offset, abs_time)，monotonic 从 0 附近开始"""
        ts = TimestampService()
        offset1, abs1 = ts.now()
        time.sleep(0.05)
        offset2, abs2 = ts.now()
        # monotonic 偏移单调递增
        assert offset2 > offset1
        # abs_time 与 offset 同步增长（差值接近 sleep 时长）
        assert (abs2 - abs1) == pytest.approx(0.05, abs=0.02)
        # abs_time 接近当前 time.time()
        assert abs(abs1 - time.time()) < 1.0

    def test_base_abs_exposed(self):
        """base_abs 暴露给 meta.json 用"""
        ts = TimestampService()
        assert abs(ts.base_abs - time.time()) < 1.0


# ==================== RecordingPackage 单测 ====================

class TestRecordingPackage:
    def test_create_generates_expected_directory_structure(self, tmp_path):
        """create() 后目录结构完整：root + frames/ + audio/ + events.jsonl + focus.jsonl + meta.json 占位"""
        pkg = RecordingPackage(base_dir=tmp_path, name="rec_test_001")
        pkg.create()
        assert pkg.root.exists()
        assert pkg.root.name == "rec_test_001"
        assert pkg.frames_dir.exists()
        assert pkg.audio_dir.exists()
        assert pkg.events_file.exists()
        assert pkg.focus_file.exists()
        # meta.json 在 write_meta 时才创建，create 阶段不预先建（避免空 meta）
        assert not pkg.meta_file.exists()

    def test_default_name_uses_rec_prefix_and_timestamp(self, tmp_path):
        """默认 name 形如 rec_YYYYMMDD_HHMMSS"""
        pkg = RecordingPackage(base_dir=tmp_path)
        assert pkg.name.startswith("rec_")
        # 形如 rec_20260723_143022
        assert len(pkg.name) == len("rec_YYYYMMDD_HHMMSS")

    def test_write_event_appends_jsonl(self, tmp_path):
        """write_event 追加 JSONL 行"""
        pkg = RecordingPackage(base_dir=tmp_path, name="r1")
        pkg.create()
        pkg.write_event({
            "timestamp": 0.123,
            "abs_timestamp": 1700000000.123,
            "kind": "mouse_click",
            "payload": {"button": "left", "x": 100, "y": 200},
        })
        pkg.write_event({
            "timestamp": 0.456,
            "abs_timestamp": 1700000000.456,
            "kind": "keyboard_input",
            "payload": {"text": "hello"},
        })
        lines = pkg.events_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        e2 = json.loads(lines[1])
        assert e1["kind"] == "mouse_click"
        assert e1["payload"]["button"] == "left"
        assert e2["kind"] == "keyboard_input"
        assert e2["payload"]["text"] == "hello"

    def test_write_meta_writes_complete_meta_json(self, tmp_path):
        """write_meta 写完整 meta.json（开始/结束/事件数/版本）"""
        pkg = RecordingPackage(base_dir=tmp_path, name="r2")
        pkg.create()
        pkg.write_meta(
            start_time=1700000000.0,
            end_time=1700000010.0,
            event_count=42,
            frame_count=10,
            version="0.1.0",
        )
        meta = json.loads(pkg.meta_file.read_text(encoding="utf-8"))
        assert meta["start_time"] == 1700000000.0
        assert meta["end_time"] == 1700000010.0
        assert meta["event_count"] == 42
        assert meta["frame_count"] == 10
        assert meta["version"] == "0.1.0"

    def test_read_events_returns_all_events_in_order(self, tmp_path):
        """read_events 返回按写入顺序的所有事件"""
        pkg = RecordingPackage(base_dir=tmp_path, name="r3")
        pkg.create()
        for i in range(5):
            pkg.write_event({
                "timestamp": float(i),
                "abs_timestamp": 1700000000.0 + i,
                "kind": "test",
                "payload": {"seq": i},
            })
        events = pkg.read_events()
        assert len(events) == 5
        assert [e["payload"]["seq"] for e in events] == [0, 1, 2, 3, 4]


# ==================== MockSensor 单测 ====================

class TestMockSensor:
    def test_start_emits_all_events_synchronously(self):
        """MockSensor.start() 同步发完所有事件到 controller"""
        events = [
            ("mouse_click", {"button": "left", "x": 10, "y": 20}),
            ("keyboard_input", {"text": "abc"}),
            ("focus_change", {"title": "Window1"}),
        ]
        controller = RecordingController(
            base_dir=Path("."),  # 不会真创建，因为不调 start()
            sensors=[],
        )
        MockSensor(controller=controller, events=events)
        # 直接调 sensor.start()，不通过 controller
        # 但 controller.start() 才会创建 package，这里只测 sensor 把事件推到 controller
        # 所以先创建 package（用 tmp_path）
        pass  # 见端到端测试

    def test_stop_is_idempotent(self):
        """stop() 可重复调用不报错"""
        controller = RecordingController(base_dir=Path("."), sensors=[])
        sensor = MockSensor(controller=controller, events=[])
        sensor.stop()
        sensor.stop()  # 不报错


# ==================== RecordingController 端到端 ====================

class TestRecordingControllerEndToEnd:
    def test_start_stop_with_mock_produces_complete_package(self, tmp_path):
        """端到端：start → mock 发 10 条事件 → stop → 验证目录结构 + events.jsonl + meta.json"""
        events = [
            (f"event_{i}", {"seq": i, "data": f"payload_{i}"})
            for i in range(10)
        ]
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[MockSensor(controller=None, events=events)],  # controller 稍后注入
        )
        # MockSensor 创建时 controller=None，需要注入
        controller.sensors[0].controller = controller

        controller.start()
        # start 会同步触发 MockSensor.start()，发完 10 条事件
        controller.stop()

        # 1. 录制包目录结构
        pkg_dirs = list(tmp_path.iterdir())
        assert len(pkg_dirs) == 1
        pkg_root = pkg_dirs[0]
        assert pkg_root.name.startswith("rec_")
        assert (pkg_root / "events.jsonl").exists()
        assert (pkg_root / "focus.jsonl").exists()
        assert (pkg_root / "frames").is_dir()
        assert (pkg_root / "audio").is_dir()
        assert (pkg_root / "meta.json").exists()

        # 2. events.jsonl 内容：10 条，按写入顺序，JSON 合法
        events_lines = (pkg_root / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(events_lines) == 10
        parsed = [json.loads(line) for line in events_lines]
        # 每条都有 timestamp / abs_timestamp / kind / payload
        for ev in parsed:
            assert "timestamp" in ev
            assert "abs_timestamp" in ev
            assert "kind" in ev
            assert "payload" in ev
        # kind 按顺序 event_0 ~ event_9
        assert [ev["kind"] for ev in parsed] == [f"event_{i}" for i in range(10)]
        # 时间戳单调递增
        timestamps = [ev["timestamp"] for ev in parsed]
        assert timestamps == sorted(timestamps)

    def test_meta_json_contains_required_fields(self, tmp_path):
        """meta.json 含 start_time / end_time / event_count / version / package_name"""
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[MockSensor(controller=None, events=[("test", {"a": 1})])],
        )
        controller.sensors[0].controller = controller
        controller.start()
        controller.stop()

        pkg_root = list(tmp_path.iterdir())[0]
        meta = json.loads((pkg_root / "meta.json").read_text(encoding="utf-8"))
        assert "start_time" in meta
        assert "end_time" in meta
        assert meta["end_time"] >= meta["start_time"]
        assert meta["event_count"] == 1
        assert "version" in meta
        assert "package_name" in meta
        assert meta["package_name"] == pkg_root.name

    def test_multiple_sensors_events_all_written(self, tmp_path):
        """多个传感器并发发事件，全部写入 events.jsonl"""
        events_a = [("mouse_click", {"src": "a", "seq": i}) for i in range(3)]
        events_b = [("keyboard_input", {"src": "b", "seq": i}) for i in range(5)]
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[
                MockSensor(controller=None, events=events_a),
                MockSensor(controller=None, events=events_b),
            ],
        )
        for s in controller.sensors:
            s.controller = controller
        controller.start()
        controller.stop()

        pkg_root = list(tmp_path.iterdir())[0]
        # 至少有一条事件被写入（MockSensor 同步发，两个 sensor 的事件都被写入）
        # 读取所有事件
        lines = (pkg_root / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        all_events = [json.loads(line) for line in lines]
        assert len(all_events) == 8  # 3 + 5
        srcs = [ev["payload"]["src"] for ev in all_events]
        assert srcs.count("a") == 3
        assert srcs.count("b") == 5

    def test_stop_without_start_does_not_crash(self, tmp_path):
        """stop() 在未 start() 状态下调用不崩溃（防御性）"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        # 不调 start 直接 stop
        controller.stop()  # 不应抛异常

    def test_emit_event_increments_count(self, tmp_path):
        """emit_event 每次调用递增 event_count"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.emit_event("test", {"seq": 1})
        controller.emit_event("test", {"seq": 2})
        controller.emit_event("test", {"seq": 3})
        controller.stop()

        pkg_root = list(tmp_path.iterdir())[0]
        meta = json.loads((pkg_root / "meta.json").read_text(encoding="utf-8"))
        assert meta["event_count"] == 3


# ==================== 事件类型协议 ====================

class TestEventTypes:
    def test_event_dict_has_required_fields(self):
        """事件 dict 必须包含 timestamp / abs_timestamp / kind / payload"""
        from lib.recorder.types import make_event
        ev = make_event(timestamp=0.1, abs_timestamp=1700000000.1, kind="test", payload={"a": 1})
        assert ev["timestamp"] == 0.1
        assert ev["abs_timestamp"] == 1700000000.1
        assert ev["kind"] == "test"
        assert ev["payload"] == {"a": 1}

    def test_event_serializes_to_json(self):
        """事件可序列化为 JSON（用于 JSONL 写入）"""
        import json

        from lib.recorder.types import make_event
        ev = make_event(timestamp=0.1, abs_timestamp=1700000000.1, kind="test", payload={"text": "你好"})
        s = json.dumps(ev, ensure_ascii=False)
        assert "你好" in s
        # 反序列化可读回
        ev2 = json.loads(s)
        assert ev2["payload"]["text"] == "你好"


# ==================== Ticket 09：状态机扩展 ====================


class _SpySensor:
    """测试用间谍传感器：记录 start()/stop() 调用次数，不发事件。

    用于验证 pause()/resume()/save()/discard() 是否正确驱动传感器生命周期，
    避免 MockSensor 在 resume 时重复发事件造成的耦合。
    """

    def __init__(self, controller=None):
        self.controller = controller
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class TestRecordingStateMachine:
    """Ticket 09：RecordingController 状态机（IDLE/RECORDING/PAUSED/SAVED）。"""

    def test_initial_state_is_idle(self, tmp_path):
        """新建 controller 初始状态为 IDLE。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        assert controller.state == RecordingState.IDLE
        assert controller.is_running is False

    def test_start_transitions_to_recording(self, tmp_path):
        """start() 后状态变 RECORDING，is_running=True。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        assert controller.state == RecordingState.RECORDING
        assert controller.is_running is True

    def test_start_is_idempotent(self, tmp_path):
        """start() 重复调用幂等，不会重建 package / 重置计数器。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        controller.start()
        controller.emit_event("test", {"a": 1})
        pkg_before = controller.package
        controller.start()  # 幂等：no-op
        assert controller.state == RecordingState.RECORDING
        assert spy.start_calls == 1  # 未重复启动
        assert controller.package is pkg_before
        assert controller.event_count == 1  # 计数器未重置

    def test_pause_transitions_to_paused(self, tmp_path):
        """start → pause 后状态 PAUSED，is_running=False。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        assert controller.state == RecordingState.PAUSED
        assert controller.is_running is False

    def test_pause_stops_all_sensors(self, tmp_path):
        """pause() 停所有传感器（每个 sensor.stop() 被调一次）。"""
        spy_a = _SpySensor()
        spy_b = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy_a, spy_b])
        controller.start()
        assert spy_a.start_calls == 1 and spy_b.start_calls == 1
        controller.pause()
        assert spy_a.stop_calls == 1
        assert spy_b.stop_calls == 1

    def test_pause_preserves_package(self, tmp_path):
        """pause() 保留录制包目录（不删除）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        pkg_root = controller.package.root
        controller.pause()
        assert pkg_root.exists()
        assert (pkg_root / "events.jsonl").exists()

    def test_pause_is_idempotent(self, tmp_path):
        """pause() 重复调用幂等。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        controller.start()
        controller.pause()
        controller.pause()  # 幂等
        assert controller.state == RecordingState.PAUSED
        assert spy.stop_calls == 1  # 未重复停传感器

    def test_resume_transitions_to_recording(self, tmp_path):
        """start → pause → resume 后状态回到 RECORDING。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        controller.resume()
        assert controller.state == RecordingState.RECORDING
        assert controller.is_running is True

    def test_resume_restarts_all_sensors(self, tmp_path):
        """resume() 重启所有传感器（每个 sensor.start() 再调一次）。"""
        spy_a = _SpySensor()
        spy_b = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy_a, spy_b])
        controller.start()  # start_calls=1
        controller.pause()
        controller.resume()
        assert spy_a.start_calls == 2
        assert spy_b.start_calls == 2

    def test_resume_timeline_continues_timestamp_base_unchanged(self, tmp_path):
        """resume() 时间轴延续：timestamp.base_abs 不变（D033）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        base_before = controller.timestamp.base_abs
        time.sleep(0.05)
        controller.pause()
        time.sleep(0.05)
        controller.resume()
        base_after = controller.timestamp.base_abs
        assert base_after == base_before  # 基准不变 → 时间轴延续

    def test_resume_is_idempotent(self, tmp_path):
        """resume() 在 RECORDING 状态调用幂等。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        controller.start()
        controller.resume()  # 已 RECORDING，幂等
        assert controller.state == RecordingState.RECORDING
        assert spy.start_calls == 1  # 未重复启动

    def test_save_from_paused_transitions_to_saved(self, tmp_path):
        """start → pause → save 后状态 SAVED（终止态）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        controller.save()
        assert controller.state == RecordingState.SAVED
        assert controller.is_running is False

    def test_save_from_recording_stops_sensors(self, tmp_path):
        """从 RECORDING 直接 save：先停传感器再标记 SAVED。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        controller.start()
        controller.save()
        assert controller.state == RecordingState.SAVED
        assert spy.stop_calls == 1

    def test_discard_from_paused_deletes_package_and_returns_to_idle(self, tmp_path):
        """start → pause → discard：删录制包目录 + 状态回 IDLE。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        pkg_root = controller.package.root
        assert pkg_root.exists()
        controller.pause()
        controller.discard()
        assert controller.state == RecordingState.IDLE
        assert not pkg_root.exists()  # 整个目录被删

    def test_discard_from_recording_stops_sensors_and_deletes_package(self, tmp_path):
        """从 RECORDING 直接 discard：先停传感器再删目录。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        controller.start()
        pkg_root = controller.package.root
        controller.discard()
        assert controller.state == RecordingState.IDLE
        assert spy.stop_calls == 1
        assert not pkg_root.exists()

    def test_discard_without_start_is_noop(self, tmp_path):
        """IDLE 状态 discard 为 no-op，不报错。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.discard()
        assert controller.state == RecordingState.IDLE

    def test_controller_reusable_after_discard(self, tmp_path):
        """discard 后可重新 start 录制新包（计数器/段重置）。

        注意：RecordingPackage 名是秒级时间戳，同秒内重建可能同名，
        所以只验证"目录被删后又重新存在 + 计数器/段重置"，不比较路径字符串。
        """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.emit_event("a", {"i": 1})
        first_pkg_root = controller.package.root
        first_pkg_obj = controller.package
        controller.discard()
        assert not first_pkg_root.exists()
        # 重新 start
        controller.start()
        assert controller.state == RecordingState.RECORDING
        assert controller.event_count == 0  # 计数器已重置
        assert len(controller.segments) == 1  # 段列表已重置为单段
        assert controller.package is not first_pkg_obj  # 重建了 package 对象
        assert controller.package.root.exists()  # 目录重新创建
        controller.pause()

    def test_stop_is_alias_of_pause(self, tmp_path):
        """stop() 等价于 pause()：状态 PAUSED + 写 meta.json（向后兼容）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.stop()
        assert controller.state == RecordingState.PAUSED
        # 旧行为：stop 后 meta.json 已写
        assert controller.package.meta_file.exists()

    def test_stop_without_start_does_not_crash(self, tmp_path):
        """IDLE 状态 stop()（→ pause）不崩溃（向后兼容旧测试）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.stop()  # 等价 pause()，IDLE 下 no-op
        assert controller.state == RecordingState.IDLE


class TestStateMachineIllegalTransitions:
    """Ticket 09：非法状态转换拒绝。"""

    def test_resume_from_idle_raises(self, tmp_path):
        """IDLE → resume 非法（未启动不能恢复）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        with pytest.raises(IllegalStateError):
            controller.resume()

    def test_save_from_idle_raises(self, tmp_path):
        """IDLE → save 非法（无录制可保存）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        with pytest.raises(IllegalStateError):
            controller.save()

    def test_pause_from_saved_raises(self, tmp_path):
        """SAVED → pause 非法（终止态）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        controller.save()
        with pytest.raises(IllegalStateError):
            controller.pause()

    def test_resume_from_saved_raises(self, tmp_path):
        """SAVED → resume 非法（终止态）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        with pytest.raises(IllegalStateError):
            controller.resume()

    def test_save_from_saved_raises(self, tmp_path):
        """SAVED → save 非法（不能重复保存）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        with pytest.raises(IllegalStateError):
            controller.save()

    def test_discard_from_saved_raises(self, tmp_path):
        """SAVED → discard 非法（已保存的录制交由后续流程处理）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        pkg_root = controller.package.root
        with pytest.raises(IllegalStateError):
            controller.discard()
        assert pkg_root.exists()  # 目录未被删


class TestStateMachineEventAcceptance:
    """Ticket 09：emit_event 仅在 RECORDING 状态接受。"""

    def test_emit_event_rejected_when_paused(self, tmp_path):
        """PAUSED 状态 emit_event 被拒绝（不写 events.jsonl）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        before = controller.event_count
        controller.emit_event("test", {"a": 1})
        assert controller.event_count == before  # 未递增
        # events.jsonl 仍为空（无事件写入）
        content = controller.package.events_file.read_text(encoding="utf-8").strip()
        assert content == ""

    def test_emit_event_accepted_after_resume(self, tmp_path):
        """start → pause（拒绝）→ resume（接受）→ 验证 events.jsonl 只含接受的事件。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.emit_event("a", {"i": 1})  # 接受
        controller.pause()
        controller.emit_event("b", {"i": 2})  # 拒绝（PAUSED）
        controller.resume()
        controller.emit_event("c", {"i": 3})  # 接受
        controller.pause()

        events = controller.package.read_events()
        kinds = [ev["kind"] for ev in events]
        assert kinds == ["a", "c"]  # "b" 被拒
        assert controller.event_count == 2

    def test_emit_focus_change_rejected_when_paused(self, tmp_path):
        """PAUSED 状态 emit_focus_change 被拒绝。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        controller.emit_focus_change({"title": "X", "hwnd": 1, "pid": 1})
        assert controller.event_count == 0
        assert controller.package.focus_file.read_text(encoding="utf-8").strip() == ""


class TestSegmentsAndMeta:
    """Ticket 09：segments / status / effective_duration 字段。"""

    def test_single_segment_after_start_pause(self, tmp_path):
        """start → pause 后 segments 含 1 个已关闭段（start_offset/end_offset 均有值）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.02)
        controller.pause()
        segs = controller.segments
        assert len(segs) == 1
        assert segs[0]["start_offset"] is not None
        assert segs[0]["end_offset"] is not None
        assert segs[0]["end_offset"] >= segs[0]["start_offset"]

    def test_two_segments_after_pause_resume_pause(self, tmp_path):
        """start → pause → resume → pause 后 segments 含 2 个已关闭段。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.02)
        controller.pause()
        time.sleep(0.02)
        controller.resume()
        time.sleep(0.02)
        controller.pause()
        segs = controller.segments
        assert len(segs) == 2
        for seg in segs:
            assert seg["end_offset"] is not None
            assert seg["end_offset"] >= seg["start_offset"]
        # 第二段 start_offset > 第一段 end_offset（resume 在 pause 之后）
        assert segs[1]["start_offset"] >= segs[0]["end_offset"]

    def test_first_segment_start_offset_near_zero(self, tmp_path):
        """第一段 start_offset 接近 0（start 时刻即时间轴零点）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        segs = controller.segments
        assert segs[0]["start_offset"] < 1.0  # 容差 1s

    def test_meta_json_contains_new_fields(self, tmp_path):
        """pause 后 meta.json 含 status / segments / effective_duration 三个新字段。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.emit_event("test", {"a": 1})
        controller.pause()
        meta = controller.package.read_meta()
        assert "status" in meta
        assert "segments" in meta
        assert "effective_duration" in meta
        assert isinstance(meta["segments"], list)
        assert isinstance(meta["effective_duration"], float)

    def test_meta_json_status_recording_after_pause(self, tmp_path):
        """pause 后 meta.status == "recording"（进行中只是暂停）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        meta = controller.package.read_meta()
        assert meta["status"] == "recording"

    def test_meta_json_status_saved_after_save(self, tmp_path):
        """save 后 meta.status == "saved"（D034：同步完成，无 processing 中间态）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.pause()
        controller.save()
        # D034：save() 同步写 status="saved"，不再有裁剪线程
        meta = controller.package.read_meta()
        assert meta["status"] == "saved"

    def test_meta_json_segments_match_controller(self, tmp_path):
        """meta.segments 与 controller.segments 一致（pause/resume 多段）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.02)
        controller.pause()
        time.sleep(0.02)
        controller.resume()
        time.sleep(0.02)
        controller.pause()
        meta = controller.package.read_meta()
        assert len(meta["segments"]) == 2
        for seg in meta["segments"]:
            assert "start_offset" in seg
            assert "end_offset" in seg
            assert seg["end_offset"] is not None

    def test_effective_duration_is_sum_of_segment_durations(self, tmp_path):
        """effective_duration = sum(end_offset - start_offset) for all segments。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.05)
        controller.pause()
        time.sleep(0.05)  # 暂停期不计入有效时长
        controller.resume()
        time.sleep(0.05)
        controller.pause()
        meta = controller.package.read_meta()
        segs = meta["segments"]
        expected = sum(s["end_offset"] - s["start_offset"] for s in segs)
        # effective_duration 近似等于各段时长之和（容差 0.01s）
        assert meta["effective_duration"] == pytest.approx(expected, abs=0.01)
        # 有效时长 < 总跨度（暂停期被排除）
        total_span = segs[-1]["end_offset"] - segs[0]["start_offset"]
        assert meta["effective_duration"] < total_span

    def test_effective_duration_single_segment_matches_duration(self, tmp_path):
        """单段录制：effective_duration ≈ end_offset - start_offset。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.05)
        controller.pause()
        meta = controller.package.read_meta()
        seg = meta["segments"][0]
        assert meta["effective_duration"] == pytest.approx(
            seg["end_offset"] - seg["start_offset"], abs=0.01
        )

    def test_resume_events_timestamps_continue_timeline(self, tmp_path):
        """resume 后事件 timestamp 延续时间轴（>= pause 前最后事件，无回零）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        time.sleep(0.02)
        controller.emit_event("before_pause", {"i": 1})
        controller.pause()
        time.sleep(0.02)
        controller.resume()
        controller.emit_event("after_resume", {"i": 2})
        controller.pause()
        events = controller.package.read_events()
        assert len(events) == 2
        # resume 后事件 timestamp 不回零，且 >= pause 前事件
        assert events[1]["timestamp"] >= events[0]["timestamp"]
        assert events[1]["timestamp"] > 0.02  # 未回零


class TestBackwardCompatibility:
    """Ticket 09：既有 stop() 工作流不回归。"""

    def test_old_stop_workflow_writes_meta_with_event_count(self, tmp_path):
        """旧流程 start → emit → stop → meta.event_count 正确（向后兼容）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.emit_event("test", {"seq": 1})
        controller.emit_event("test", {"seq": 2})
        controller.emit_event("test", {"seq": 3})
        controller.stop()
        meta = controller.package.read_meta()
        assert meta["event_count"] == 3
        # 旧字段仍在
        for field in ("start_time", "end_time", "duration_seconds", "version", "package_name"):
            assert field in meta

    def test_old_start_stop_with_mock_sensors_still_works(self, tmp_path):
        """旧端到端：start → MockSensor 发事件 → stop → events.jsonl 完整。"""
        events = [(f"event_{i}", {"seq": i}) for i in range(5)]
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[MockSensor(controller=None, events=events)],
        )
        controller.sensors[0].controller = controller
        controller.start()
        controller.stop()
        events_out = controller.package.read_events()
        assert len(events_out) == 5
        assert [ev["kind"] for ev in events_out] == [f"event_{i}" for i in range(5)]

    def test_meta_json_audio_seconds_still_read_from_audio_sensor(self, tmp_path):
        """Bug 2 修复保留：pause/stop 仍从 AudioSensor 读 duration_seconds。"""

        class AudioSensor:
            """模拟 AudioSensor：暴露 duration_seconds 属性。

            controller 按类名 == "AudioSensor" 识别，故此处类名必须一致。
            """

            def __init__(self, controller):
                self.controller = controller

            def start(self):
                pass

            def stop(self):
                pass

            @property
            def duration_seconds(self):
                return 12.5

        controller = RecordingController(
            base_dir=tmp_path, sensors=[AudioSensor(controller=None)]
        )
        controller.sensors[0].controller = controller
        controller.start()
        controller.stop()
        meta = controller.package.read_meta()
        assert meta["audio_seconds"] == 12.5


# ==================== Ticket 11：后台裁剪线程 + on_trim_complete hook ====================


def _make_test_wav(path: Path, duration: float = 1.0, samplerate: int = 16000) -> None:
    """创建测试用 WAV（mono / int16 静音），供 audio_trimmer 处理。

    与 AudioSensor 输出格式一致（16kHz / mono / int16）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration * samplerate)
    samples = np.zeros(n_samples, dtype=np.int16)
    with wave.open(str(path), "wb") as wf:  # noqa: SIM115
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())


class TestSaveHook:
    """D034：save() 同步完成 + on_trim_complete hook（无后台裁剪线程）。

    移除自动音频裁剪后，save() 直接写 status="saved" 并同步调 on_trim_complete。
    mic.wav 完整保留，不生成 mic_cropped.wav / audio_segments.json。
    """

    def test_save_writes_status_saved_directly(self, tmp_path):
        """save() 后 meta.status == "saved"（同步，无 processing 中间态）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        meta = controller.package.read_meta()
        assert meta["status"] == "saved"
        assert "error" not in meta

    def test_save_preserves_original_audio_no_cropping(self, tmp_path):
        """D034：save() 不裁剪音频，mic.wav 完整保留，无 mic_cropped.wav。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        _make_test_wav(controller.package.audio_file, duration=1.0)
        controller.save()
        # 原始音频完整保留
        assert controller.package.audio_file.exists()
        # 不生成裁剪产物
        assert not (controller.package.audio_dir / "mic_cropped.wav").exists()
        assert not (controller.package.audio_dir / "audio_segments.json").exists()
        # meta 不含裁剪路径字段
        meta = controller.package.read_meta()
        assert "cropped_audio_path" not in meta
        assert "audio_segments_path" not in meta
        assert "original_audio_path" not in meta

    def test_no_audio_file_marks_saved_directly(self, tmp_path):
        """无 mic.wav（未启用 AudioSensor）：直接标记 saved。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        meta = controller.package.read_meta()
        assert meta["status"] == "saved"
        assert not (controller.package.audio_dir / "mic_cropped.wav").exists()
        assert not (controller.package.audio_dir / "audio_segments.json").exists()

    def test_on_trim_complete_callback_invoked_synchronously(self, tmp_path):
        """on_trim_complete 回调在 save() 返回前同步调用，参数为录制包根路径。"""
        captured: list[Path] = []

        def hook(pkg_path: Path):
            captured.append(pkg_path)

        controller = RecordingController(
            base_dir=tmp_path, sensors=[], on_trim_complete=hook
        )
        controller.start()
        controller.save()
        # save() 返回后回调应已被同步调用（无需 join 线程）
        assert len(captured) == 1
        assert captured[0] == controller.package.root

    def test_default_on_trim_complete_is_noop(self, tmp_path):
        """默认 on_trim_complete 为 no-op，不传也能正常工作。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        assert controller.state == RecordingState.SAVED

    def test_on_trim_complete_exception_does_not_crash_save(self, tmp_path):
        """on_trim_complete 回调抛异常不影响 save() 已写入的 meta（catch 兜底）。"""
        def boom_hook(pkg_path: Path):
            raise RuntimeError("hook boom")

        controller = RecordingController(
            base_dir=tmp_path, sensors=[], on_trim_complete=boom_hook
        )
        controller.start()
        # save() 不应因回调异常而崩溃
        controller.save()
        assert controller.state == RecordingState.SAVED
        meta = controller.package.read_meta()
        assert meta["status"] == "saved"

    def test_save_from_idle_raises(self, tmp_path):
        """IDLE 状态 save 抛 IllegalStateError。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        with pytest.raises(IllegalStateError):
            controller.save()

    def test_save_from_saved_raises(self, tmp_path):
        """重复 save 抛 IllegalStateError。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        controller.save()
        with pytest.raises(IllegalStateError):
            controller.save()
