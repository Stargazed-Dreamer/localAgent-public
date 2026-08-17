"""C2 discovery：list_recordings / get_recording_summary。

agent 发现录制包的入口。扫描 workspace/recorder/recordings/ 下所有录制包，
返回 RecordingSummary 列表（按创建时间倒序）。
"""

from __future__ import annotations

from pathlib import Path

from workspace.recorder.consumer.manifest import read_manifest, regenerate_manifest
from workspace.recorder.consumer.types import RecordingSummary

# 默认录制包根目录（相对项目根）
_DEFAULT_RECORDINGS_DIR = Path("workspace/recorder/recordings")


def list_recordings(base_dir: Path | str | None = None) -> list[RecordingSummary]:
    """列出所有录制包摘要（按 created_at 倒序，新的在前）。

    Args:
        base_dir: 录制包根目录，None 时用默认 workspace/recorder/recordings/

    Returns:
        RecordingSummary 列表，跳过非录制包目录（无 meta.json）
    """
    base = Path(base_dir) if base_dir else _DEFAULT_RECORDINGS_DIR
    if not base.is_dir():
        return []

    summaries: list[RecordingSummary] = []
    for package_path in base.iterdir():
        if not package_path.is_dir():
            continue
        # 录制包必须有 meta.json（L0 产出标志）
        if not (package_path / "meta.json").exists():
            continue
        try:
            summaries.append(get_recording_summary(package_path))
        except Exception:
            # 单个录制包损坏不影响其它录制包列出
            continue

    # 按 created_at 倒序（新的在前）；空字符串排在最后
    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries


def get_recording_summary(package_path: Path | str) -> RecordingSummary:
    """单个录制包摘要（读 manifest + meta + annotations 组装）。

    Args:
        package_path: 录制包根目录

    Returns:
        RecordingSummary

    Raises:
        FileNotFoundError: 录制包目录不存在
    """
    package_path = Path(package_path)
    if not package_path.is_dir():
        raise FileNotFoundError(f"录制包目录不存在: {package_path}")

    # 优先读 manifest，不存在则现场生成
    manifest = read_manifest(package_path)
    if manifest is None:
        manifest = regenerate_manifest(package_path)

    stats = manifest.get("stats", {})
    return RecordingSummary(
        recording_id=manifest.get("recording_id", package_path.name),
        created_at=manifest.get("created_at", ""),
        duration_seconds=float(manifest.get("duration_seconds", 0.0)),
        finalized=bool(manifest.get("finalized", False)),
        block_count=int(stats.get("block_count", 0)),
        chapter_count=int(stats.get("chapter_count", 0)),
        marked_key_count=int(stats.get("marked_key_count", 0)),
        marked_anomaly_count=int(stats.get("marked_anomaly_count", 0)),
        has_transcript=manifest.get("stt_status") == "completed",
        stt_model=manifest.get("stt_model"),
        package_path=str(package_path),
    )
