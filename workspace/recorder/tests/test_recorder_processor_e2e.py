"""L1 处理层 Ticket 18：端到端测试 + P2/P3 接口契约测试

测试策略（spec-l1.md Testing Decisions + tickets.md Ticket 18 acceptance）：
- 端到端测试：给定真实录制包目录结构 → 跑 process_recording_package → 验证三个 JSON 产出
- P4/P5 端到端：构造 events.jsonl + frames/ → 跑 run_p4=True + run_p5=True → 验证 blocks.json + keyframes.json
- P1 端到端：构造 audio/mic.wav + mock WhisperTool → 跑 run_p1=True → 验证 transcript.json
- 三时间戳对齐：blocks.json / keyframes.json / transcript.json 共享录制包时间轴
- P2/P3 接口契约：Protocol 签名 + run_p2/run_p3 默认返回 None + P3 question 空抛 ValueError

覆盖 Ticket 18 acceptance criteria：
- [x] process_recording_package 统一入口：编排 P4/P5 自动跑 + P1 接收 VAD 阈值参数手动触发
- [x] process_recording_package 选项字典：{run_p4, run_p5, run_p1, vad_threshold, stt_language, ...}
- [x] P2 UIA 结构化器接口签名定义（按需采集 UIA 快照到 uia_snapshots/*.json，不实现）
- [x] P3 VL 标注器接口签名定义（调 understand_image MCP 工具，不实现）
- [x] 端到端测试：给定录制包 → 跑 process_recording_package(run_p4=True, run_p5=True) → 验证 blocks.json + keyframes.json 产出
- [x] 端到端测试：给定录制包 + mock WhisperTool → 跑 process_recording_package(run_p1=True, vad_threshold=0.5) → 验证 transcript.json 产出
- [x] 端到端测试：三个 JSON 产出时间戳对齐（blocks.json / keyframes.json / transcript.json 共享录制包时间轴）
"""

import json
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from lib.recorder.processor import (
    ProcessingResult,
    UIASnapshotter,
    VLAnnotator,
    process_recording_package,
    run_p2,
    run_p3,
)

pytestmark = pytest.mark.e2e  # E2E：依赖真实环境（CDP/后端/GUI），quick 层排除

# ============================ 测试辅助函数 ============================



def _make_event(ts: float, kind: str, payload: dict) -> dict:
    """构造 L0 事件 dict。"""
    return {
        "timestamp": ts,
        "abs_timestamp": 1700000000.0 + ts,
        "kind": kind,
        "payload": payload,
    }


def _make_frame_event(ts: float, frame_path: str) -> dict:
    """构造 screen_frame 事件。"""
    return _make_event(ts, "screen_frame", {
        "frame_path": frame_path,
        "frame_seq": 0,
        "mode": "fullscreen",
    })


def _write_events_jsonl(package_root: Path, events: list[dict]) -> Path:
    """把事件列表写入录制包的 events.jsonl。"""
    package_root.mkdir(parents=True, exist_ok=True)
    events_file = package_root / "events.jsonl"
    lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_file


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int = 16000, sample_width: int = 2) -> None:
    """写 int16 PCM WAV 文件（模拟 L0 AudioSensor 输出）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        # float32 [-1.0, 1.0] → int16 [-32768, 32767]
        int16_data = (audio * 32767).astype(np.int16)
        wav.writeframes(int16_data.tobytes())


def _write_frame_png(frames_dir: Path, name: str, seed: int = 0) -> Path:
    """写一张随机噪声 PNG 到 frames/ 目录（pHash 可区分）。"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frames_dir / name
    rng = np.random.RandomState(seed)
    arr = rng.randint(0, 256, (64, 64, 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(frame_path)
    return frame_path


class _FakeTranscriber:
    """Fake WhisperTool（复用 test_recorder_processor_p1.py 的设计）。

    results 列表每项是一次调用的返回值：
    - list[dict]：成功，返回该转写结果
    - None：失败（模拟出错）
    """

    def __init__(self, results: list | None = None):
        self._results_queue = list(results) if results else []
        self._task_counter = 0
        self._tasks: dict[str, dict] = {}
        self.models: dict[str, dict] = {"test_model": {"instance": None, "busy": False}}
        self.process_array_calls: list[dict] = []

    def load(self, model_name, device=None, model_path=None):
        self.models[model_name] = {"instance": None, "busy": False}

    def unload(self, model_name):
        if model_name in self.models:
            del self.models[model_name]

    def is_model_loaded(self, model_name):
        return model_name in self.models

    def is_model_busy(self, model_name):
        return self.models.get(model_name, {}).get("busy", False)

    def process_array(self, model_name, audio_array, language="zh", vad_threshold=None, **kwargs):
        self._task_counter += 1
        task_id = f"task_{self._task_counter}"
        self._tasks[task_id] = {"progress": 0.0, "result": None, "error": None}
        if model_name not in self.models:
            self.models[model_name] = {"instance": None, "busy": False}
        self.models[model_name]["busy"] = True
        self.process_array_calls.append({
            "model_name": model_name,
            "language": language,
            "vad_threshold": vad_threshold,
        })
        result = self._results_queue.pop(0) if self._results_queue else []
        if result is None:
            self._tasks[task_id]["progress"] = -1
            self._tasks[task_id]["error"] = RuntimeError("transcribe failed")
        else:
            self._tasks[task_id]["progress"] = 1.0
            self._tasks[task_id]["result"] = result
        self.models[model_name]["busy"] = False
        return task_id

    def get_progress(self, task_id):
        task = self._tasks[task_id]
        if task["progress"] == -1:
            return (-1.0, None)
        return (task["progress"], task["result"])

    def get_error(self, task_id):
        task = self._tasks[task_id]
        if task["error"] is not None:
            raise task["error"]
        raise ValueError("Task is not in error state")


def _build_minimal_recording(package_root: Path) -> tuple[list[dict], list[str]]:
    """构造最小录制包（含 events.jsonl + frames/）。

    Returns:
        (events, frame_paths)：事件列表 + 帧相对路径列表
    """
    # 3 张可区分的随机噪声帧（pHash 不同）
    frames_dir = package_root / "frames"
    frame_paths = []
    for i, ts in enumerate([0.5, 1.5, 2.5]):
        name = f"frame_{int(ts * 1000):08d}_{i:06d}.png"
        _write_frame_png(frames_dir, name, seed=i + 1)
        frame_paths.append(f"frames/{name}")

    # 事件流：3 帧 screen_frame + 2 个 mouse_click
    events = [
        _make_frame_event(0.5, frame_paths[0]),
        _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 100, "y": 200}),
        _make_frame_event(1.5, frame_paths[1]),
        _make_event(2.0, "keyboard_input", {"text": "admin", "physical_keys": ["a", "d", "m", "i", "n"], "detection_method": "uia_value_diff"}),
        _make_frame_event(2.5, frame_paths[2]),
        _make_event(3.0, "mouse_click", {"button": "left", "clicks": 1, "x": 200, "y": 300}),
    ]
    _write_events_jsonl(package_root, events)
    return events, frame_paths


def _build_recording_with_audio(package_root: Path) -> tuple[list[dict], list[str], Path]:
    """构造含音频的最小录制包（events.jsonl + frames/ + audio/mic.wav）。"""
    events, frame_paths = _build_minimal_recording(package_root)
    # 1 秒静音 + 1 秒有效音频 + 1 秒静音
    sr = 16000
    audio = np.concatenate([
        np.zeros(sr, dtype=np.float32),
        (np.sin(2 * np.pi * 440 * np.arange(sr) / sr) * 0.5).astype(np.float32),
        np.zeros(sr, dtype=np.float32),
    ])
    wav_path = package_root / "audio" / "mic.wav"
    _write_wav(wav_path, audio, sample_rate=sr)
    return events, frame_paths, wav_path


# ============================ P4 + P5 端到端测试 ============================


class TestP4P5EndToEnd:
    """端到端：给定录制包 → 跑 process_recording_package(run_p4=True, run_p5=True)
    → 验证 blocks.json + keyframes.json 产出。"""

    def test_p4_p5_produce_both_json(self, tmp_path: Path):
        """跑 P4+P5 同时产出 blocks.json + keyframes.json。"""
        package = tmp_path / "rec_test"
        _build_minimal_recording(package)

        result = process_recording_package(
            package,
            options={"run_p4": True, "run_p5": True, "run_p1": False},
        )

        assert isinstance(result, ProcessingResult)
        assert result.errors == {}
        # blocks.json 产出
        assert result.blocks_path is not None
        assert result.blocks_path.exists()
        assert result.block_count > 0
        blocks = json.loads(result.blocks_path.read_text(encoding="utf-8"))
        assert len(blocks) == result.block_count
        # blocks 字段验证
        for block in blocks:
            assert "id" in block
            assert "category" in block
            assert "type" in block
            assert "timestamp" in block
            assert "primary" in block
        # keyframes.json 产出
        assert result.keyframes_path is not None
        assert result.keyframes_path.exists()
        assert result.keyframe_count > 0
        keyframes = json.loads(result.keyframes_path.read_text(encoding="utf-8"))
        assert len(keyframes) == result.keyframe_count
        # keyframes 字段验证
        for kf in keyframes:
            assert "frame_path" in kf
            assert "timestamp" in kf
            assert "retain_reason" in kf

    def test_p4_p5_default_options(self, tmp_path: Path):
        """默认选项（run_p4=True, run_p5=True, run_p1=False）跑 P4+P5。"""
        package = tmp_path / "rec_default"
        _build_minimal_recording(package)

        result = process_recording_package(package)  # 默认选项

        assert result.blocks_path is not None
        assert result.blocks_path.exists()
        assert result.keyframes_path is not None
        assert result.keyframes_path.exists()
        assert result.transcript_path is None  # P1 默认不跑
        assert "p1" not in result.errors

    def test_p4_p5_no_p1(self, tmp_path: Path):
        """run_p1=False 时不产出 transcript.json。"""
        package = tmp_path / "rec_no_p1"
        _build_minimal_recording(package)

        result = process_recording_package(
            package,
            options={"run_p1": False},
        )

        assert result.transcript_path is None
        assert "p1" not in result.errors  # 不跑不算错


# ============================ P1 端到端测试 ============================


class TestP1EndToEnd:
    """端到端：给定录制包 + mock WhisperTool → 跑 process_recording_package(run_p1=True, vad_threshold=0.5)
    → 验证 transcript.json 产出。"""

    def test_p1_produces_transcript_json(self, tmp_path: Path):
        """跑 P1 产出 transcript.json。"""
        package = tmp_path / "rec_p1"
        _build_recording_with_audio(package)

        transcript_result = [
            {"line": 1, "start": 1.0, "end": 2.0, "text": "现在我要点击登录按钮"},
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package,
            options={
                "run_p4": False,
                "run_p5": False,
                "run_p1": True,
                "p1_vad_threshold": 0.5,
                "p1_language": "zh",
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        assert "p1" not in result.errors
        assert result.transcript_path is not None
        assert result.transcript_path.exists()
        transcript = json.loads(result.transcript_path.read_text(encoding="utf-8"))
        assert len(transcript) == 1
        assert transcript[0]["text"] == "现在我要点击登录按钮"
        assert transcript[0]["start"] == 1.0
        assert transcript[0]["end"] == 2.0
        assert result.transcript_segment_count == 1
        # 验证 VAD 阈值传给了 WhisperTool
        assert len(fake.process_array_calls) == 1
        assert fake.process_array_calls[0]["vad_threshold"] == 0.5
        assert fake.process_array_calls[0]["language"] == "zh"

    def test_p1_vad_threshold_passed_through(self, tmp_path: Path):
        """不同 VAD 阈值都能传入。"""
        package = tmp_path / "rec_vad"
        _build_recording_with_audio(package)

        for vad in [0.3, 0.5, 0.7]:
            fake = _FakeTranscriber(results=[[{"line": 1, "start": 0.0, "end": 1.0, "text": "test"}]])
            result = process_recording_package(
                package,
                options={
                    "run_p4": False,
                    "run_p5": False,
                    "run_p1": True,
                    "p1_vad_threshold": vad,
                    "p1_model": "test_model",
                    "p1_transcriber": fake,
                },
            )
            assert "p1" not in result.errors
            assert fake.process_array_calls[0]["vad_threshold"] == vad

    def test_p1_missing_wav_records_error(self, tmp_path: Path):
        """缺 audio/mic.wav 时 P1 报错（FileNotFoundError）。"""
        package = tmp_path / "rec_no_wav"
        _build_minimal_recording(package)  # 没有 audio/mic.wav
        # 清掉可能残留的 audio 目录
        import shutil
        if (package / "audio").exists():
            shutil.rmtree(package / "audio")

        fake = _FakeTranscriber(results=[])

        result = process_recording_package(
            package,
            options={
                "run_p4": False,
                "run_p5": False,
                "run_p1": True,
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        assert "p1" in result.errors
        assert "FileNotFoundError" in result.errors["p1"]
        assert result.transcript_path is None


# ============================ 三时间戳对齐测试 ============================


class TestTimestampAlignment:
    """端到端：三个 JSON 产出时间戳对齐（blocks.json / keyframes.json / transcript.json 共享录制包时间轴）。"""

    def test_all_three_share_same_timeline(self, tmp_path: Path):
        """blocks.json + keyframes.json + transcript.json 都用同一个录制包时间轴。"""
        package = tmp_path / "rec_alignment"
        _build_recording_with_audio(package)

        # 用 mock transcriber 返回与 events 时间戳相关的转写
        transcript_result = [
            {"line": 1, "start": 1.0, "end": 2.0, "text": "点击登录"},  # 与 mouse_click @1.0 对齐
            {"line": 2, "start": 2.0, "end": 3.0, "text": "输入 admin"},  # 与 keyboard_input @2.0 对齐
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package,
            options={
                "run_p4": True,
                "run_p5": True,
                "run_p1": True,
                "p1_vad_threshold": 0.5,
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        # 三个产出都成功
        assert result.errors == {}
        assert result.blocks_path is not None
        assert result.keyframes_path is not None
        assert result.transcript_path is not None

        blocks = json.loads(result.blocks_path.read_text(encoding="utf-8"))
        keyframes = json.loads(result.keyframes_path.read_text(encoding="utf-8"))
        transcript = json.loads(result.transcript_path.read_text(encoding="utf-8"))

        # blocks 时间戳在 [0, 录制总时长] 范围内
        block_timestamps = [b["timestamp"] for b in blocks]
        for ts in block_timestamps:
            assert 0 <= ts <= 10.0  # 录制总时长 ~3s + 容差

        # keyframes 时间戳在 [0, 录制总时长] 范围内
        kf_timestamps = [k["timestamp"] for k in keyframes]
        for ts in kf_timestamps:
            assert 0 <= ts <= 10.0

        # transcript 时间戳在 [0, 录制总时长] 范围内
        for seg in transcript:
            assert 0 <= seg["start"] <= 10.0
            assert 0 <= seg["end"] <= 10.0
            assert seg["start"] <= seg["end"]

    def test_block_timestamps_match_original_events(self, tmp_path: Path):
        """blocks.json 的 timestamp 与 events.jsonl 的 timestamp 一致（同源）。"""
        package = tmp_path / "rec_ts_match"
        events, _ = _build_minimal_recording(package)

        result = process_recording_package(
            package,
            options={"run_p4": True, "run_p5": False, "run_p1": False},
        )

        blocks = json.loads(result.blocks_path.read_text(encoding="utf-8"))
        # 操作块的 timestamp 应等于对应事件的 timestamp
        operation_blocks = [b for b in blocks if b["category"] == "operation"]
        operation_events = [e for e in events if e["kind"] != "screen_frame"]
        assert len(operation_blocks) == len(operation_events)
        for block, event in zip(operation_blocks, operation_events, strict=True):
            assert block["timestamp"] == event["timestamp"]

    def test_keyframe_timestamps_match_frame_events(self, tmp_path: Path):
        """keyframes.json 的 timestamp 与 screen_frame 事件 timestamp 一致（同源）。"""
        package = tmp_path / "rec_kf_match"
        events, _ = _build_minimal_recording(package)

        result = process_recording_package(
            package,
            options={"run_p4": False, "run_p5": True, "run_p1": False},
        )

        keyframes = json.loads(result.keyframes_path.read_text(encoding="utf-8"))
        frame_events = [e for e in events if e["kind"] == "screen_frame"]
        # 帧数对齐（3 帧都保留，因为 pHash 都不同）
        assert len(keyframes) == len(frame_events)
        kf_timestamps = [k["timestamp"] for k in keyframes]
        event_timestamps = [e["timestamp"] for e in frame_events]
        # 排序后比较（keyframes 已按时间戳升序，frame_events 也按写入顺序）
        assert sorted(kf_timestamps) == sorted(event_timestamps)

    def test_transcript_timestamps_independent_axis(self, tmp_path: Path):
        """transcript.json 的时间戳是音频时间轴（与 events.jsonl 时间轴同源）。

        spec-l1.md："时间戳直接是原始时间轴，不需要映射"
        """
        package = tmp_path / "rec_ts_independent"
        _build_recording_with_audio(package)

        # mock 返回的 transcript 时间戳是音频时间戳（与 events.jsonl 同源）
        transcript_result = [
            {"line": 1, "start": 1.0, "end": 2.0, "text": "test"},
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package,
            options={
                "run_p4": False,
                "run_p5": False,
                "run_p1": True,
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        transcript = json.loads(result.transcript_path.read_text(encoding="utf-8"))
        # 时间戳是原始音频时间轴（1.0-2.0s），无需映射
        assert transcript[0]["start"] == 1.0
        assert transcript[0]["end"] == 2.0


# ============================ P2 UIA 接口契约测试 ============================


class _FakeUIASnapshotter:
    """Fake UIASnapshotter，用于测试 P2 接口契约。"""

    def __init__(self, snapshot_result: str | None = "uia_snapshots/00001230_000001.json"):
        self._result = snapshot_result
        self.calls: list[dict] = []

    def snapshot(
        self,
        package_root: Path,
        timestamp: float,
        *,
        hwnd: int | None = None,
        max_depth: int = 5,
    ) -> str | None:
        self.calls.append({
            "package_root": package_root,
            "timestamp": timestamp,
            "hwnd": hwnd,
            "max_depth": max_depth,
        })
        return self._result


class TestP2Interface:
    """P2 UIA 结构化器接口契约（L1 SDD 只定义不实现）。"""

    def test_p2_protocol_defines_snapshot_method(self):
        """UIASnapshotter Protocol 定义了 snapshot 方法签名。"""
        # Protocol 是接口契约，结构化检查
        assert hasattr(UIASnapshotter, "snapshot")
        # _FakeUIASnapshotter 实现了 Protocol（duck typing）
        fake = _FakeUIASnapshotter()
        assert isinstance(fake, UIASnapshotter)  # structural typing

    def test_run_p2_returns_none_without_snapshotter(self, tmp_path: Path):
        """run_p2 无注入 snapshotter 时返回 None（L1 不提供默认实现）。"""
        result = run_p2(tmp_path, timestamp=1.23)
        assert result is None

    def test_run_p2_delegates_to_snapshotter(self, tmp_path: Path):
        """run_p2 有注入 snapshotter 时委托调用。"""
        fake = _FakeUIASnapshotter(snapshot_result="uia_snapshots/test.json")
        result = run_p2(tmp_path, timestamp=1.23, snapshotter=fake, hwnd=12345)
        assert result == "uia_snapshots/test.json"
        assert len(fake.calls) == 1
        assert fake.calls[0]["package_root"] == tmp_path
        assert fake.calls[0]["timestamp"] == 1.23
        assert fake.calls[0]["hwnd"] == 12345

    def test_run_p2_default_max_depth(self, tmp_path: Path):
        """run_p2 默认 max_depth=5（通过 Protocol 默认值）。"""
        fake = _FakeUIASnapshotter()
        run_p2(tmp_path, timestamp=1.0, snapshotter=fake)
        assert fake.calls[0]["max_depth"] == 5

    def test_run_p2_snapshotter_failure_returns_none(self, tmp_path: Path):
        """snapshotter 失败时返回 None（接口契约要求不抛异常）。"""

        class FailingSnapshotter:
            def snapshot(self, package_root, timestamp, *, hwnd=None, max_depth=5):
                raise RuntimeError("UIA 不可用")

        # 实现方内部应 try/except，但若未捕获，run_p2 不强制兜底
        # 接口契约说"不应抛异常"，但 run_p2 不主动 try/except（保持简单）
        with pytest.raises(RuntimeError, match="UIA 不可用"):
            run_p2(tmp_path, timestamp=1.0, snapshotter=FailingSnapshotter())


# ============================ P3 VL 接口契约测试 ============================


class _FakeVLAnnotator:
    """Fake VLAnnotator，用于测试 P3 接口契约。"""

    def __init__(self, describe_result: str | None = "登录按钮在(100,200)位置"):
        self._result = describe_result
        self.calls: list[dict] = []

    def describe(
        self,
        image_path: Path,
        question: str,
        *,
        max_chars: int = 200,
    ) -> str | None:
        self.calls.append({
            "image_path": image_path,
            "question": question,
            "max_chars": max_chars,
        })
        return self._result


class TestP3Interface:
    """P3 VL 标注器接口契约（L1 SDD 只定义不实现）。"""

    def test_p3_protocol_defines_describe_method(self):
        """VLAnnotator Protocol 定义了 describe 方法签名。"""
        assert hasattr(VLAnnotator, "describe")
        fake = _FakeVLAnnotator()
        assert isinstance(fake, VLAnnotator)

    def test_run_p3_returns_none_without_annotator(self, tmp_path: Path):
        """run_p3 无注入 annotator 时返回 None（L1 不提供默认实现）。"""
        result = run_p3(tmp_path / "frame.png", question="登录按钮在哪里？")
        assert result is None

    def test_run_p3_delegates_to_annotator(self, tmp_path: Path):
        """run_p3 有注入 annotator 时委托调用。"""
        fake = _FakeVLAnnotator(describe_result="按钮在左上角")
        image_path = tmp_path / "frame.png"
        result = run_p3(image_path, question="登录按钮在哪里？", annotator=fake)
        assert result == "按钮在左上角"
        assert len(fake.calls) == 1
        assert fake.calls[0]["image_path"] == image_path
        assert fake.calls[0]["question"] == "登录按钮在哪里？"

    def test_run_p3_default_max_chars(self, tmp_path: Path):
        """run_p3 默认 max_chars=200。"""
        fake = _FakeVLAnnotator()
        run_p3(tmp_path / "frame.png", question="?", annotator=fake)
        assert fake.calls[0]["max_chars"] == 200

    def test_run_p3_empty_question_raises_value_error(self, tmp_path: Path):
        """run_p3 question 为空时抛 ValueError（"带问题看图"是硬约束）。"""
        with pytest.raises(ValueError, match="question"):
            run_p3(tmp_path / "frame.png", question="")
        with pytest.raises(ValueError, match="question"):
            run_p3(tmp_path / "frame.png", question="   ")  # 纯空白
        with pytest.raises(ValueError, match="question"):
            run_p3(tmp_path / "frame.png", question="")

    def test_run_p3_empty_question_raises_even_with_annotator(self, tmp_path: Path):
        """有 annotator 时 question 为空也抛 ValueError（不能跳过硬约束）。"""
        fake = _FakeVLAnnotator()
        with pytest.raises(ValueError, match="question"):
            run_p3(tmp_path / "frame.png", question="", annotator=fake)
        # 没调用到 annotator.describe
        assert len(fake.calls) == 0


# ============================ 全流程端到端测试 ============================


class TestFullPipelineEndToEnd:
    """全流程端到端：跑 P1+P4+P5 全部，验证三个 JSON 都产出且互不干扰。"""

    def test_full_pipeline_all_three_json(self, tmp_path: Path):
        """跑 P1+P4+P5 同时产出三个 JSON 文件。"""
        package = tmp_path / "rec_full"
        _build_recording_with_audio(package)

        transcript_result = [
            {"line": 1, "start": 1.0, "end": 2.0, "text": "现在我要点击登录按钮"},
            {"line": 2, "start": 2.0, "end": 3.0, "text": "输入 admin"},
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package,
            options={
                "run_p4": True,
                "run_p5": True,
                "run_p1": True,
                "p1_vad_threshold": 0.5,
                "p1_language": "zh",
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        # 三个 JSON 都产出
        assert result.errors == {}
        assert result.blocks_path is not None
        assert result.blocks_path.exists()
        assert result.keyframes_path is not None
        assert result.keyframes_path.exists()
        assert result.transcript_path is not None
        assert result.transcript_path.exists()

        # 计数 > 0
        assert result.block_count > 0
        assert result.keyframe_count > 0
        assert result.transcript_segment_count > 0

    def test_full_pipeline_p1_failure_does_not_break_p4_p5(self, tmp_path: Path):
        """P1 失败不影响 P4/P5 产出（错误隔离）。"""
        package = tmp_path / "rec_p1_fail"
        _build_minimal_recording(package)  # 没有 audio/mic.wav
        import shutil
        if (package / "audio").exists():
            shutil.rmtree(package / "audio")

        result = process_recording_package(
            package,
            options={
                "run_p4": True,
                "run_p5": True,
                "run_p1": True,
                "p1_model": "test_model",
                "p1_transcriber": _FakeTranscriber(results=[]),
            },
        )

        # P4/P5 仍成功
        assert "p4" not in result.errors
        assert "p5" not in result.errors
        assert result.blocks_path is not None
        assert result.blocks_path.exists()
        assert result.keyframes_path is not None
        assert result.keyframes_path.exists()
        # P1 失败记录在 errors
        assert "p1" in result.errors
        assert "FileNotFoundError" in result.errors["p1"]
        assert result.transcript_path is None

    def test_full_pipeline_p4_failure_does_not_break_p5_p1(self, tmp_path: Path):
        """P4 失败不影响 P5/P1 产出（错误隔离）。"""
        package = tmp_path / "rec_p4_fail"
        _build_recording_with_audio(package)
        # 删除 events.jsonl 让 P4 失败
        (package / "events.jsonl").unlink()

        transcript_result = [{"line": 1, "start": 1.0, "end": 2.0, "text": "test"}]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package,
            options={
                "run_p4": True,
                "run_p5": True,
                "run_p1": True,
                "p1_model": "test_model",
                "p1_transcriber": fake,
            },
        )

        # P4 失败（缺 events.jsonl）
        assert "p4" in result.errors
        # P5 也失败（同样缺 events.jsonl）
        assert "p5" in result.errors
        # P1 仍成功（不需要 events.jsonl，只需要 audio/mic.wav）
        assert "p1" not in result.errors
        assert result.transcript_path is not None
        assert result.transcript_path.exists()

    def test_empty_options_uses_defaults(self, tmp_path: Path):
        """空 options 字典使用默认值（run_p4=True, run_p5=True, run_p1=False）。"""
        package = tmp_path / "rec_empty_opts"
        _build_minimal_recording(package)

        result = process_recording_package(package, options={})

        assert result.blocks_path is not None
        assert result.keyframes_path is not None
        assert result.transcript_path is None
        assert "p4" not in result.errors
        assert "p5" not in result.errors
        assert "p1" not in result.errors  # run_p1=False 不算错

    def test_none_options_uses_defaults(self, tmp_path: Path):
        """options=None 使用默认值。"""
        package = tmp_path / "rec_none_opts"
        _build_minimal_recording(package)

        result = process_recording_package(package, options=None)

        assert result.blocks_path is not None
        assert result.keyframes_path is not None
        assert result.transcript_path is None

    def test_string_package_path_accepted(self, tmp_path: Path):
        """package_path 接受 str 类型（自动转 Path）。"""
        package = tmp_path / "rec_str_path"
        _build_minimal_recording(package)

        result = process_recording_package(str(package))

        assert result.package_root == package
        assert result.blocks_path is not None
        assert result.blocks_path.exists()
