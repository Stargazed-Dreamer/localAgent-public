"""L2 时间轴层 Ticket 22：T3 标注器 + CLI 预览测试

测试策略（spec-l2.md Testing Decisions）：
- T3 测试：init_annotations 初始化空文件 + 已存在不覆盖 + 幂等
- preview 测试：6 项预览输出内容验证（概览/章节/块分布/时间跨度/对齐/帧压缩比）
- CLI 测试：默认模式构建+预览 + --preview-only 模式 + 错误处理 + --idle-threshold 选项

覆盖 Ticket 22 acceptance criteria：
- [x] init_annotations(package_path) -> Path
- [x] 初始化空 annotations.json（{"annotations": []}）
- [x] 已存在 annotations.json 时不覆盖
- [x] CLI 入口 python -m workspace.recorder.tools.timeline_cli <package_path>
- [x] 6 项预览内容验证
- [x] 单测：T3 初始化空文件 + 不覆盖 + CLI 预览输出内容验证
"""

import json
from pathlib import Path

from lib.recorder.timeline import build_timeline, init_annotations
from lib.recorder.timeline.preview import format_preview
from workspace.recorder.tools.timeline_cli import main as cli_main

# ============================ 测试辅助函数 ============================


def _make_operation_block(
    seq: int,
    block_type: str = "mouse_click",
    timestamp: float = 0.0,
    duration: float = 0.0,
) -> dict:
    """构造一个操作块 dict（符合 L1 P4 产出结构）。"""
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": block_type,
        "timestamp": timestamp,
        "duration": duration,
        "primary": {"x": 100, "y": 200, "button": "left", "clicks": 1},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _make_focus_change_block(seq: int, timestamp: float, title: str) -> dict:
    """构造一个 focus_change 操作块（含 title 字段）。"""
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": "focus_change",
        "timestamp": timestamp,
        "duration": 0.0,
        "primary": {"title": title, "process": "app.exe", "pid": 1234},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _write_meta(
    package_root: Path,
    package_name: str = "rec_test",
    duration_seconds: float = 60.0,
    frame_count: int = 5,
    effective_duration: float | None = None,
    audio_seconds: float | None = None,
) -> Path:
    """写 meta.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    meta_path = package_root / "meta.json"
    meta = {
        "package_name": package_name,
        "start_time": 1700000000.0,
        "end_time": 1700000000.0 + duration_seconds,
        "duration_seconds": duration_seconds,
        "event_count": 10,
        "frame_count": frame_count,
        "sensors": ["KeyboardSensor", "MouseSensor"],
        "status": "saved",
        "version": "0.1.0",
        "format": "L0-sensor-layer",
    }
    if effective_duration is not None:
        meta["effective_duration"] = effective_duration
    if audio_seconds is not None:
        meta["audio_seconds"] = audio_seconds
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta_path


def _write_blocks(package_root: Path, blocks: list[dict]) -> Path:
    """写 blocks.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    blocks_path = package_root / "blocks.json"
    blocks_path.write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return blocks_path


def _write_keyframes(package_root: Path, keyframes: list[dict]) -> Path:
    """写 keyframes.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    path = package_root / "keyframes.json"
    path.write_text(
        json.dumps(keyframes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _write_transcript(package_root: Path, segments: list[dict]) -> Path:
    """写 transcript.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    path = package_root / "transcript.json"
    path.write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _build_minimal_package(
    tmp_path: Path,
    blocks: list[dict] | None = None,
    package_name: str = "rec_test",
    duration_seconds: float = 60.0,
    frame_count: int = 5,
    effective_duration: float | None = None,
    audio_seconds: float | None = None,
) -> Path:
    """构造一个最小录制包目录（meta.json + blocks.json）。"""
    package_root = tmp_path / package_name
    _write_meta(
        package_root,
        package_name,
        duration_seconds,
        frame_count=frame_count,
        effective_duration=effective_duration,
        audio_seconds=audio_seconds,
    )
    if blocks is not None:
        _write_blocks(package_root, blocks)
    return package_root


# ============================ T3 标注器测试 ============================


class TestInitAnnotations:
    """T3 重点标注器：init_annotations 函数测试。"""

    def test_init_creates_empty_file(self, tmp_path):
        """初始化空 annotations.json。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        annotations_path = pkg / "annotations.json"

        # 调用前不存在
        assert not annotations_path.exists()

        result = init_annotations(pkg)

        assert result == annotations_path
        assert annotations_path.exists()
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == {"annotations": []}

    def test_init_returns_correct_path(self, tmp_path):
        """返回 annotations.json 的正确路径。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        result = init_annotations(pkg)
        assert result == pkg / "annotations.json"
        assert result.name == "annotations.json"

    def test_init_not_overwrite_existing(self, tmp_path):
        """已存在 annotations.json 时不覆盖（保留 L3 编辑器的标注）。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        annotations_path = pkg / "annotations.json"

        # 预先写入有标注的内容（模拟 L3 编辑器已标注）
        existing_data = {
            "annotations": [
                {
                    "block_id": "b001",
                    "type": "marked_key",
                    "note": "重要操作",
                }
            ]
        }
        annotations_path.write_text(
            json.dumps(existing_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 调用 init_annotations
        result = init_annotations(pkg)

        # 内容应保持不变
        assert result == annotations_path
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == existing_data
        assert len(data["annotations"]) == 1
        assert data["annotations"][0]["block_id"] == "b001"

    def test_init_idempotent(self, tmp_path):
        """多次调用幂等（第一次创建，后续不覆盖）。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])

        result1 = init_annotations(pkg)
        result2 = init_annotations(pkg)
        result3 = init_annotations(pkg)

        assert result1 == result2 == result3
        data = json.loads(result1.read_text(encoding="utf-8"))
        assert data == {"annotations": []}

    def test_init_accepts_str_path(self, tmp_path):
        """接受 str 类型路径（不仅限于 Path）。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        result = init_annotations(str(pkg))
        assert result.exists()
        assert result.is_file()


# ============================ Preview 预览测试 ============================


class TestFormatPreview:
    """format_preview：6 项预览输出内容验证。"""

    def test_preview_overview_section(self, tmp_path):
        """预览项 1：录制包概览。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(
            tmp_path,
            blocks,
            duration_seconds=120.5,
            frame_count=86,
            effective_duration=50.17,
            audio_seconds=50.076,
        )
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 1. 录制包概览 ---" in text
        # duration 120.5s 被格式化为 "2:00.50"（2分0.5秒）
        assert "2:00.50" in text  # duration
        assert "50.170s" in text  # effective_duration
        assert "帧数: 86" in text  # frame_count
        assert "音频时长: 50.076s" in text  # audio_seconds
        assert "状态: saved" in text  # status

    def test_preview_chapters_section(self, tmp_path):
        """预览项 2：章节列表（章节名 + 时间范围 + 块数）。"""
        blocks = [
            _make_focus_change_block(1, 0.0, "TRAE Work CN"),
            _make_operation_block(2, "mouse_click", 1.0),
            _make_focus_change_block(3, 30.0, "Chrome"),
            _make_operation_block(4, "mouse_click", 31.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 2. 章节列表 ---" in text
        assert "TRAE Work CN" in text
        assert "Chrome" in text
        # 章节块 ID 前缀 c
        assert "c001" in text
        assert "c002" in text

    def test_preview_block_distribution_section(self, tmp_path):
        """预览项 3：块类型分布。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "keyboard_input", 2.0),
            _make_focus_change_block(3, 3.0, "App"),
        ]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 3. 块类型分布 ---" in text
        assert "operation/mouse_click" in text
        assert "operation/keyboard_input" in text
        assert "operation/focus_change" in text
        assert "text/chapter" in text  # 章节块
        assert "总计" in text

    def test_preview_time_span_section(self, tmp_path):
        """预览项 4：时间跨度（首尾时间戳 + 总时长）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 5.0),
            _make_operation_block(2, "mouse_click", 30.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 4. 时间跨度 ---" in text
        assert "首块时间戳" in text
        assert "尾块时间戳" in text
        assert "时间跨度" in text
        assert "录制时长" in text

    def test_preview_timestamp_alignment_section(self, tmp_path):
        """预览项 5：三时间戳对齐验证（blocks/keyframes/transcript 时间戳范围）。"""
        blocks = [_make_operation_block(1, "mouse_click", 5.0)]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)
        _write_keyframes(
            pkg,
            [
                {"frame_path": "frames/00001.png", "timestamp": 4.5, "retain_reason": "click_burst"},
                {"frame_path": "frames/00002.png", "timestamp": 10.0, "retain_reason": "timer"},
            ],
        )
        _write_transcript(
            pkg,
            [
                {"line": 1, "start": 2.0, "end": 5.5, "text": "你好"},
                {"line": 2, "start": 6.0, "end": 8.5, "text": "世界"},
            ],
        )
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 5. 三时间戳对齐验证 ---" in text
        assert "操作块" in text
        assert "关键帧" in text
        assert "转写" in text

    def test_preview_frame_compression_section(self, tmp_path):
        """预览项 6：帧压缩比（原始帧 → 关键帧 → image 块）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(
            tmp_path, blocks, duration_seconds=60.0, frame_count=86
        )
        _write_keyframes(
            pkg,
            [
                {"frame_path": "frames/00001.png", "timestamp": 0.5, "retain_reason": "click_burst"},
                {"frame_path": "frames/00002.png", "timestamp": 1.5, "retain_reason": "timer"},
                {"frame_path": "frames/00003.png", "timestamp": 2.5, "retain_reason": "timer"},
            ],
        )
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "--- 6. 帧压缩比 ---" in text
        assert "原始帧" in text
        assert "关键帧" in text
        assert "image 块" in text
        assert "86" in text  # 原始帧数
        assert "3" in text  # 关键帧数

    def test_preview_no_timeline_warns(self, tmp_path):
        """timeline.json 不存在时给出警告。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        # 不调用 build_timeline，timeline.json 不存在

        text = format_preview(pkg)

        assert "timeline.json 不存在" in text
        assert "请先运行 build_timeline" in text

    def test_preview_no_meta_handled(self, tmp_path):
        """meta.json 不存在时降级处理（不崩溃）。"""
        pkg = tmp_path / "rec_no_meta"
        pkg.mkdir(parents=True)
        _write_blocks(pkg, [_make_operation_block(1)])
        build_timeline(pkg)

        text = format_preview(pkg)

        # 不崩溃，给出降级提示
        assert "meta.json 不存在" in text or "时间轴预览" in text

    def test_preview_header_and_footer(self, tmp_path):
        """预览输出有标题头和结尾标志。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        build_timeline(pkg)

        text = format_preview(pkg)

        assert "=== 时间轴预览" in text
        assert "=== 预览结束 ===" in text


# ============================ CLI 入口测试 ============================


class TestTimelineCli:
    """workspace/recorder/tools/timeline_cli.py CLI 入口测试。"""

    def test_cli_default_builds_and_previews(self, tmp_path, capsys):
        """默认模式：构建 timeline.json + 初始化 annotations.json + 预览。"""
        blocks = [
            _make_focus_change_block(1, 0.0, "App1"),
            _make_operation_block(2, "mouse_click", 1.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)

        # 调用前 timeline.json / annotations.json 都不存在
        assert not (pkg / "timeline.json").exists()
        assert not (pkg / "annotations.json").exists()

        exit_code = cli_main([str(pkg)])

        captured = capsys.readouterr()
        assert exit_code == 0
        # timeline.json 已生成
        assert (pkg / "timeline.json").exists()
        # annotations.json 已初始化
        assert (pkg / "annotations.json").exists()
        ann_data = json.loads((pkg / "annotations.json").read_text(encoding="utf-8"))
        assert ann_data == {"annotations": []}
        # stderr 有构建信息
        assert "[ok] timeline.json 已生成" in captured.err
        # stdout 有预览内容
        assert "=== 时间轴预览" in captured.out
        assert "--- 1. 录制包概览 ---" in captured.out

    def test_cli_preview_only_with_existing_timeline(self, tmp_path, capsys):
        """--preview-only 模式：timeline.json 已存在时只预览不重建。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)
        # 先构建一次
        build_timeline(pkg)
        timeline_path = pkg / "timeline.json"
        original_mtime = timeline_path.stat().st_mtime_ns

        # 用 --preview-only 模式调用
        exit_code = cli_main([str(pkg), "--preview-only"])

        captured = capsys.readouterr()
        assert exit_code == 0
        # timeline.json 未被重建（mtime 不变）
        assert timeline_path.stat().st_mtime_ns == original_mtime
        # stdout 有预览内容
        assert "=== 时间轴预览" in captured.out
        # stderr 没有 [ok] 构建信息
        assert "[ok] timeline.json 已生成" not in captured.err

    def test_cli_preview_only_without_timeline_fails(self, tmp_path, capsys):
        """--preview-only 模式但 timeline.json 不存在时报错退出。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        # 不构建 timeline.json

        exit_code = cli_main([str(pkg), "--preview-only"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "timeline.json 不存在" in captured.err
        assert "[hint]" in captured.err

    def test_cli_nonexistent_package_fails(self, tmp_path, capsys):
        """包目录不存在时退出码 1。"""
        nonexistent = tmp_path / "does_not_exist"
        exit_code = cli_main([str(nonexistent)])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "录制包目录不存在" in captured.err

    def test_cli_path_is_file_fails(self, tmp_path, capsys):
        """路径是文件而非目录时退出码 1。"""
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello", encoding="utf-8")

        exit_code = cli_main([str(file_path)])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "路径不是目录" in captured.err

    def test_cli_idle_threshold_option(self, tmp_path, capsys):
        """--idle-threshold 选项：自定义章节切分阈值。"""
        # 构造一个有 idle 块的录制包，idle=1.5s（默认阈值 2.0 不切，3.0 也不切，0.5 切）
        blocks = [
            _make_focus_change_block(1, 0.0, "App1"),
            _make_operation_block(2, "mouse_click", 1.0),
            {
                "id": "b003",
                "category": "operation",
                "type": "idle",
                "timestamp": 1.0,
                "duration": 1.5,  # idle 1.5s
                "primary": {"duration": 1.5},
                "supplements": {
                    "before_frame": None,
                    "after_frame": None,
                    "uia_snapshot": None,
                    "linked_text": [],
                },
                "status": {
                    "marked_key": False,
                    "marked_anomaly": False,
                    "marked_automatable": False,
                    "is_trimmed": False,
                },
            },
            _make_operation_block(4, "mouse_click", 2.5),
        ]
        pkg = _build_minimal_package(tmp_path, blocks, duration_seconds=60.0)

        # 默认阈值 2.0：idle 1.5 不切章节（除"开始"外无新章节）
        exit_code = cli_main([str(pkg), "--idle-threshold", "2.0"])
        capsys.readouterr()
        assert exit_code == 0
        timeline_data = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
        chapter_blocks_default = [b for b in timeline_data["blocks"] if b.get("type") == "chapter"]
        # 1 个章节块（"开始"@0.0），idle 1.5s < 2.0 不切
        assert len(chapter_blocks_default) == 1

        # 阈值 0.5：idle 1.5 > 0.5 切新章节
        exit_code = cli_main([str(pkg), "--idle-threshold", "0.5"])
        capsys.readouterr()
        assert exit_code == 0
        timeline_data = json.loads((pkg / "timeline.json").read_text(encoding="utf-8"))
        chapter_blocks_low = [b for b in timeline_data["blocks"] if b.get("type") == "chapter"]
        # 2 个章节块（"开始"@0.0 + idle 切的章节）
        assert len(chapter_blocks_low) == 2

    def test_cli_init_annotations_called(self, tmp_path, capsys):
        """默认模式调用 init_annotations 初始化 annotations.json。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])

        exit_code = cli_main([str(pkg)])

        capsys.readouterr()
        assert exit_code == 0
        assert (pkg / "annotations.json").exists()
        ann_data = json.loads((pkg / "annotations.json").read_text(encoding="utf-8"))
        assert ann_data == {"annotations": []}

    def test_cli_preserves_existing_annotations(self, tmp_path, capsys):
        """默认模式下 annotations.json 已存在时不覆盖。"""
        pkg = _build_minimal_package(tmp_path, [_make_operation_block(1)])
        annotations_path = pkg / "annotations.json"
        existing_data = {
            "annotations": [
                {"block_id": "b001", "type": "marked_key", "note": "重要"}
            ]
        }
        annotations_path.write_text(
            json.dumps(existing_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        exit_code = cli_main([str(pkg)])

        capsys.readouterr()
        assert exit_code == 0
        # annotations.json 内容未被覆盖
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == existing_data
        assert len(data["annotations"]) == 1

    def test_cli_reports_build_errors_to_stderr(self, tmp_path, capsys):
        """构建过程中的错误输出到 stderr（不污染 stdout 预览）。"""
        # 构造一个缺 blocks.json 的包（只有 meta.json）
        pkg = tmp_path / "rec_no_blocks"
        _write_meta(pkg, "rec_no_blocks", 60.0)
        # 不写 blocks.json

        exit_code = cli_main([str(pkg)])

        captured = capsys.readouterr()
        assert exit_code == 0  # 降级处理，不是错误退出
        assert "[warn]" in captured.err
        assert "blocks" in captured.err
        # stdout 仍有预览内容
        assert "=== 时间轴预览" in captured.out
