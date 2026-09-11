"""Ticket 33：L4 消费层端到端测试。

用 3 个真实录制包（rec_20260723_141608 / rec_20260723_161415 / rec_20260723_162924）
跑完整 L4 消费层流程，验证 consumer 公用库 + 多轮 VL 协议辅助函数。

> **D055 变更（2026-07-23）**：后端 HTTP 端点已移除，原 `TestRecordingsHTTPEndpoints`
> 类已删除。测试只覆盖 consumer 公用库（agent 直接 import 读本地文件）。

测试覆盖：
- Consumer 公用库端到端流程：list → summary → view → chapters → block_detail →
  vl_candidates → vl_question → log_consumption → read_consumption_log
- consumption.log 读写验证
- 录制包被 .gitignore 排除，CI 环境自动跳过
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from workspace.recorder.consumer import (
    RecordingSummary,
    VLCandidate,
    build_vl_question,
    get_block_detail,
    get_chapters,
    get_merged_view,
    get_recording_summary,
    list_recordings,
    log_consumption,
    read_consumption_log,
    regenerate_manifest,
)

pytestmark = pytest.mark.e2e  # E2E：依赖真实环境（CDP/后端/GUI），quick 层排除

# ============================ 测试数据 ============================

RECORDINGS_DIR = Path("workspace/recorder/recordings")
REAL_PACKAGES = [
    RECORDINGS_DIR / "rec_20260723_141608",
    RECORDINGS_DIR / "rec_20260723_161415",
    RECORDINGS_DIR / "rec_20260723_162924",
]
_HAS_REAL_PACKAGES = all(p.exists() for p in REAL_PACKAGES)
_skip_if_no_real = pytest.mark.skipif(
    not _HAS_REAL_PACKAGES,
    reason="真实录制包被 .gitignore 排除，CI 环境自动跳过",
)


# ============================ Consumer 公用库端到端流程 ============================



@_skip_if_no_real
class TestConsumerE2EFlow:
    """Consumer 公用库完整流程测试（3 个真实录制包参数化）。

    验证 agent 调用 consumer API 的完整流程：
    list_recordings → get_recording_summary → get_merged_view →
    get_chapters → get_block_detail → select_vl_candidates →
    build_vl_question → log_consumption → read_consumption_log
    """

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_list_recordings_includes_package(self, pkg_name: str) -> None:
        """list_recordings 应包含目标录制包。"""
        summaries = list_recordings()
        ids = [s.recording_id for s in summaries]
        assert pkg_name in ids, f"list_recordings 未包含 {pkg_name}"

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_get_recording_summary_fields(self, pkg_name: str) -> None:
        """get_recording_summary 返回完整字段。"""
        pkg = RECORDINGS_DIR / pkg_name
        summary = get_recording_summary(pkg)
        assert isinstance(summary, RecordingSummary)
        assert summary.recording_id == pkg_name
        assert summary.duration_seconds > 0
        assert summary.block_count > 0
        assert summary.chapter_count > 0
        assert summary.package_path == str(pkg)
        # has_transcript 是 bool
        assert isinstance(summary.has_transcript, bool)
        assert isinstance(summary.finalized, bool)

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_get_merged_view_structure(self, pkg_name: str) -> None:
        """get_merged_view 返回 merge 后的视图（含 blocks 数组）。"""
        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        assert "blocks" in view
        assert isinstance(view["blocks"], list)
        assert len(view["blocks"]) > 0
        assert view["block_count"] > 0
        # merged view 应过滤 trimmed 块（无 is_trimmed=True 的块）
        trimmed = [b for b in view["blocks"] if (b.get("status") or {}).get("is_trimmed")]
        assert trimmed == [], "merged view 不应包含 is_trimmed=True 的块"

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_get_chapters_returns_chapter_blocks(self, pkg_name: str) -> None:
        """get_chapters 应返回 chapter 块列表（按时间戳排序）。"""
        pkg = RECORDINGS_DIR / pkg_name
        chapters = get_chapters(pkg)
        assert len(chapters) > 0
        for ch in chapters:
            assert ch.get("type") == "chapter"
        # 按时间戳升序
        timestamps = [ch.get("timestamp", 0) for ch in chapters]
        assert timestamps == sorted(timestamps)

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_get_block_detail_resolves_supplement_paths(self, pkg_name: str) -> None:
        """get_block_detail 应将 supplements 中的相对路径解析为绝对路径。"""
        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        # 找一个有 supplements.frame_path 的块
        target_block = None
        for b in view["blocks"]:
            supp = b.get("supplements") or {}
            for v in supp.values():
                if isinstance(v, str) and v.startswith("frames/"):
                    target_block = b
                    break
            if target_block:
                break

        if target_block is None:
            pytest.skip("录制包中无带 frame_path 的块，跳过路径解析验证")

        block_id = target_block["id"]
        detail = get_block_detail(pkg, block_id)
        assert detail is not None
        assert detail["id"] == block_id
        # 验证 supplements 路径已解析为绝对路径
        for key, value in (detail.get("supplements") or {}).items():
            if isinstance(value, str) and (
                target_block.get("supplements", {}).get(key, "").startswith("frames/")
                or target_block.get("supplements", {}).get(key, "").startswith("uia_snapshots/")
            ):
                assert Path(value).is_absolute(), f"supplements.{key} 应为绝对路径，实际: {value}"

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_get_block_detail_returns_none_for_unknown_block(self, pkg_name: str) -> None:
        """get_block_detail 对不存在的 block_id 返回 None。"""
        pkg = RECORDINGS_DIR / pkg_name
        assert get_block_detail(pkg, "nonexistent_block") is None

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_select_vl_candidates_with_focus_all(self, pkg_name: str) -> None:
        """select_vl_candidates focus=all 返回候选帧（含 suggested_question）。"""
        from workspace.recorder.consumer import select_vl_candidates

        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        candidates = select_vl_candidates(view, focus="all", package_path=pkg)
        # 至少应有一些候选帧（image/screenshot 块或带 frame_path 的块）
        # 注：若录制包无 image 块且无 marked_key/anomaly，可能返回空 list
        for cand in candidates:
            assert isinstance(cand, VLCandidate)
            assert cand.block_id
            assert cand.frame_path
            assert cand.reason
            # suggested_question 可为空（stt_transcript 块），但通常非空
            assert isinstance(cand.suggested_question, str)
            # package_path 提供时 frame_path 应为绝对路径
            assert Path(cand.frame_path).is_absolute()

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_build_vl_question_for_block_types(self, pkg_name: str) -> None:
        """build_vl_question 根据块类型生成对应模板。"""
        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        # 找一个 mouse_click 块验证
        for block in view["blocks"]:
            if block.get("type") == "mouse_click":
                q = build_vl_question(block)
                assert q  # 非空
                assert "点击" in q or "界面" in q
                break
        # 找一个 chapter 块验证
        for block in view["blocks"]:
            if block.get("type") == "chapter":
                q = build_vl_question(block)
                assert q
                assert "章节" in q
                break
        # 找一个 stt_transcript 块验证（返回空字符串）
        for block in view["blocks"]:
            if block.get("type") == "stt_transcript":
                q = build_vl_question(block)
                assert q == ""
                break

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_build_vl_question_with_context(self, pkg_name: str) -> None:
        """build_vl_question context 参数追加自定义问题。"""
        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        # 找一个非 stt_transcript 块
        target = next(
            (b for b in view["blocks"] if b.get("type") != "stt_transcript"),
            None,
        )
        if target is None:
            pytest.skip("录制包无非 stt_transcript 块")
        q = build_vl_question(target, context="这个控件是否有禁用状态？")
        assert "这个控件是否有禁用状态？" in q

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_log_and_read_consumption_roundtrip(self, pkg_name: str) -> None:
        """log_consumption 写入 + read_consumption_log 读回应一致。"""
        pkg = RECORDINGS_DIR / pkg_name
        log_path = pkg / "consumption.log"
        # 记录原始内容（如已存在），测试后恢复
        original_content = log_path.read_bytes() if log_path.exists() else None
        try:
            # 先清空日志
            log_path.write_text("", encoding="utf-8") if log_path.exists() else None

            # 写入测试日志
            log_consumption(pkg, "vl_call", {
                "block_id": "b001",
                "question": "这个界面有什么内容？",
                "frame": "frames/00001250.png",
            })
            log_consumption(pkg, "read_view", {"view": "merged"})

            # 读回
            entries = read_consumption_log(pkg)
            assert len(entries) == 2
            assert entries[0]["action"] == "vl_call"
            assert entries[0]["details"]["block_id"] == "b001"
            assert entries[1]["action"] == "read_view"
            # 每条日志都应有 timestamp
            for entry in entries:
                assert "timestamp" in entry
                assert "action" in entry
                assert "details" in entry
        finally:
            # 恢复原始内容
            if original_content is not None:
                log_path.write_bytes(original_content)
            elif log_path.exists():
                log_path.unlink()

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_regenerate_manifest_writes_format_version(self, pkg_name: str) -> None:
        """regenerate_manifest 应写入 format_version="1.0" 字段到 manifest.json。"""
        pkg = RECORDINGS_DIR / pkg_name
        manifest_path = pkg / "manifest.json"
        original_content = manifest_path.read_bytes() if manifest_path.exists() else None
        try:
            manifest = regenerate_manifest(pkg)
            assert manifest["recording_id"] == pkg_name
            assert manifest["format_version"] == "1.0"
            assert "files" in manifest
            assert "stats" in manifest
            # manifest.json 文件应已写入
            assert manifest_path.exists()
            written = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert written["format_version"] == "1.0"
        finally:
            # 恢复原始 manifest.json
            if original_content is not None:
                manifest_path.write_bytes(original_content)
            elif manifest_path.exists():
                manifest_path.unlink()

    def test_list_recordings_sorted_by_created_at_desc(self) -> None:
        """list_recordings 按 created_at 倒序（新的在前）。"""
        summaries = list_recordings()
        if len(summaries) < 2:
            pytest.skip("录制包不足 2 个，跳过排序验证")
        created_at_list = [s.created_at for s in summaries]
        # 倒序：前一个 >= 后一个（字符串 ISO 格式可直接比较）
        for i in range(len(created_at_list) - 1):
            assert created_at_list[i] >= created_at_list[i + 1], (
                f"list_recordings 未按 created_at 倒序：位置 {i} ({created_at_list[i]}) "
                f"< 位置 {i+1} ({created_at_list[i+1]})"
            )


# ============================ 多轮 VL 协议辅助函数集成测试 ============================


@_skip_if_no_real
class TestMultiRoundVLProtocol:
    """多轮 VL 协议辅助函数端到端集成测试。

    模拟 agent 执行三阶段协议：
    Round 1: get_merged_view 建立全局认知
    Round 2: select_vl_candidates + build_vl_question + log_consumption
    Round 3: 收敛（再次选候选 + log）
    """

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_round1_build_global_awareness(self, pkg_name: str) -> None:
        """Round 1：get_merged_view 建立全局认知。"""
        from workspace.recorder.consumer import select_vl_candidates

        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        assert view["block_count"] > 0

        # 统计各类块的数量，建立全局认知
        chapter_count = sum(1 for b in view["blocks"] if b.get("type") == "chapter")
        operation_count = sum(
            1 for b in view["blocks"]
            if b.get("type") in ("mouse_click", "keyboard_input", "mouse_scroll", "mouse_drag")
        )
        image_count = sum(1 for b in view["blocks"] if b.get("category") == "image")

        assert chapter_count > 0, "至少应有一个章节"
        # 操作块和图像块至少一个 > 0
        assert operation_count + image_count > 0, "至少应有操作块或图像块"

        # Round 1 不调 VL，但可统计潜在 VL 候选数量
        candidates = select_vl_candidates(view, focus="all", package_path=pkg)
        # 候选数量应 <= image_count + marked_key/marked_anomaly 块的关联帧数
        assert isinstance(candidates, list)

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_round2_select_and_question(self, pkg_name: str) -> None:
        """Round 2：select_vl_candidates + build_vl_question + log_consumption。"""
        from workspace.recorder.consumer import select_vl_candidates

        pkg = RECORDINGS_DIR / pkg_name
        log_path = pkg / "consumption.log"
        original_content = log_path.read_bytes() if log_path.exists() else None
        try:
            if log_path.exists():
                log_path.write_text("", encoding="utf-8")

            view = get_merged_view(pkg)
            candidates = select_vl_candidates(view, focus="all", package_path=pkg)

            # 模拟 agent 调 VL 看图（不真实调 VL，只记录日志）
            for cand in candidates[:3]:  # 只取前 3 个候选模拟
                # 候选有 suggested_question
                question = cand.suggested_question
                # 模拟 VL 调用 + 记录消费日志
                log_consumption(pkg, "vl_call", {
                    "block_id": cand.block_id,
                    "frame": cand.frame_path,
                    "question": question,
                })

            # 验证日志记录
            entries = read_consumption_log(pkg)
            assert len(entries) <= 3  # 最多 3 条
            for entry in entries:
                assert entry["action"] == "vl_call"
                assert "block_id" in entry["details"]
                assert "frame" in entry["details"]
                assert "question" in entry["details"]
                # D005：不记录 VL 返回内容
                assert "answer" not in entry["details"]
                assert "response" not in entry["details"]
        finally:
            if original_content is not None:
                log_path.write_bytes(original_content)
            elif log_path.exists():
                log_path.unlink()

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_vl_call_count_within_recommended_limit(self, pkg_name: str) -> None:
        """VL 调用次数应在推荐上限内（D054：15-30 次/次消费，不 enforce）。"""
        from workspace.recorder.consumer import select_vl_candidates

        pkg = RECORDINGS_DIR / pkg_name
        view = get_merged_view(pkg)
        candidates = select_vl_candidates(view, focus="all", package_path=pkg)
        # 推荐上限 15-30 次，候选数应在此范围内或更少
        # 注：不 enforce，只验证统计功能可用
        assert isinstance(candidates, list)
        # 真实录制包的候选数应在合理范围（通常 < 50）
        # 若超过 30，agent 应在日志中预警
        if len(candidates) > 30:
            # 不阻止，只标记（D054：不 enforce）
            pass


# ============================ consumption.log 验证 ============================


@_skip_if_no_real
class TestConsumptionLogFormat:
    """consumption.log 格式和读写验证。"""

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_consumption_log_jsonl_format(self, pkg_name: str):
        """consumption.log 应为 JSONL 格式（每行一个 JSON）。"""
        pkg = RECORDINGS_DIR / pkg_name
        log_path = pkg / "consumption.log"
        original_content = log_path.read_bytes() if log_path.exists() else None
        try:
            if log_path.exists():
                log_path.write_text("", encoding="utf-8")

            # 写入多条不同 action 的日志
            log_consumption(pkg, "list_recordings", {"count": 3})
            log_consumption(pkg, "read_view", {"block_count": 50})
            log_consumption(pkg, "select_vl_candidates", {"focus": "anomaly", "count": 5})
            log_consumption(pkg, "vl_call", {
                "block_id": "b001",
                "frame": "frames/00001250.png",
                "question": "这个界面有什么内容？",
            })

            # 读回验证
            entries = read_consumption_log(pkg)
            assert len(entries) == 4
            actions = [e["action"] for e in entries]
            assert actions == [
                "list_recordings", "read_view", "select_vl_candidates", "vl_call",
            ]

            # 验证 JSONL 格式（每行独立 JSON）
            text = log_path.read_text(encoding="utf-8")
            lines = [line for line in text.splitlines() if line.strip()]
            assert len(lines) == 4
            for line in lines:
                parsed = json.loads(line)  # 每行都能独立解析
                assert "timestamp" in parsed
                assert "action" in parsed
                assert "details" in parsed
        finally:
            if original_content is not None:
                log_path.write_bytes(original_content)
            elif log_path.exists():
                log_path.unlink()

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_consumption_log_does_not_store_vl_response(self, pkg_name: str):
        """consumption.log 不记录 VL 返回内容（D005：不预存 VL 结果）。"""
        pkg = RECORDINGS_DIR / pkg_name
        log_path = pkg / "consumption.log"
        original_content = log_path.read_bytes() if log_path.exists() else None
        try:
            if log_path.exists():
                log_path.write_text("", encoding="utf-8")

            # 写入 VL 调用日志（agent 应只记录元信息，不记录 VL 返回内容）
            log_consumption(pkg, "vl_call", {
                "block_id": "b001",
                "frame": "frames/00001250.png",
                "question": "这个界面有什么内容？",
                # 注意：不应有 "answer" / "response" / "vl_result" 等字段
            })

            entries = read_consumption_log(pkg)
            assert len(entries) == 1
            details = entries[0]["details"]
            # 验证只记录元信息
            assert "block_id" in details
            assert "frame" in details
            assert "question" in details
            # 不应有 VL 返回内容字段
            assert "answer" not in details
            assert "response" not in details
            assert "vl_result" not in details
        finally:
            if original_content is not None:
                log_path.write_bytes(original_content)
            elif log_path.exists():
                log_path.unlink()

    def test_read_consumption_log_empty_for_missing_file(self, tmp_path: Path):
        """read_consumption_log 文件不存在时返回空 list。"""
        pkg = tmp_path / "nonexistent_package"
        pkg.mkdir()
        assert read_consumption_log(pkg) == []
