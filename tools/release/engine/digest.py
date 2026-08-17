"""Digest 计算工具（spec-v2-compiler.md Solution 节"plan_digest 计算"）。

所有 digest = SHA-256 of canonical JSON（sort_keys + 紧凑分隔符）。
plan_digest 不含 plan_digest 自身和 created_at（避免自引用 + 时间戳不影响计划身份）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Exemption, FileEntry


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sort_keys + 紧凑分隔符（无空格）"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_canonical_digest(obj: Any) -> str:
    """SHA-256 of canonical JSON representation"""
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def compute_profile_digest(profile_path: Path) -> str:
    """Profile 文件级 digest（读 bytes → SHA-256，不解析 TOML）

    文件级 digest 比 TOML 解析后 canonical JSON 更稳定（不受 TOML 解析器差异影响）。
    profile 任何字节变化都会让 digest 变化。
    """
    return hashlib.sha256(profile_path.read_bytes()).hexdigest()


def compute_components_digest(components: tuple[str, ...]) -> str:
    """Components 选择 digest（canonical JSON of sorted components list）"""
    sorted_components = sorted(components)
    return compute_canonical_digest({"components": list(sorted_components)})


def compute_scan_digest(scan_results: dict) -> str:
    """Scan 结果 digest（canonical JSON of scan_results dict）"""
    return compute_canonical_digest({"scan_results": scan_results})


def compute_exemptions_digest(exemptions: tuple[Exemption, ...]) -> str:
    """Exemptions digest（canonical JSON of sorted-by-path exemptions）"""
    sorted_exemptions = sorted((e.to_dict() for e in exemptions), key=lambda d: d["path"])
    return compute_canonical_digest({"exemptions": sorted_exemptions})


def compute_plan_digest(plan_fields: dict) -> str:
    """Plan digest = SHA-256 of canonical JSON of plan fields.

    plan_fields 应含：
    - profile_digest: str
    - components_digest: str
    - scan_digest: str
    - exemptions_digest: str
    - source_commit: str
    - file_entries: list[dict]（按 rel_path 排序后入哈希）

    plan_fields 不含 plan_digest 自身（避免自引用）和 created_at（时间戳不影响计划身份）。
    """
    # 防御性复制，避免修改调用方传入的 dict
    fields = dict(plan_fields)
    # 确保 file_entries 按 rel_path 排序（确定性）
    if "file_entries" in fields:
        sorted_entries = sorted(
            (e.to_dict() if isinstance(e, FileEntry) else e for e in fields["file_entries"]),
            key=lambda d: d["rel_path"],
        )
        fields["file_entries"] = sorted_entries
    return compute_canonical_digest(fields)
