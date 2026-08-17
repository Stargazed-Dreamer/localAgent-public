"""prepare_release 接口（spec-v2-compiler.md Solution 节"prepare_release"）。

输入：profile_id + source commit + audience name
输出：PreparedRelease（不可变计划，含 plan_digest）
副作用：写 release/plans/<plan_digest>.json

职责：
1. 固定源码快照（git rev-parse + git ls-files，tracked-only）
2. 加载 audience policy + components manifest
3. 计算 file_entries（按 audience.export_set 展开 glob ∩ tracked）
4. 跑 4 静态 gates（policy-schema / tracked-file-inventory / sensitive-content-scan / manifest-audit）
5. 计算 exemptions（从 profile [sensitive_line_skips] + [post_process_remove_lines] 派生）
6. 计算 profile_digest / components_digest / scan_digest / exemptions_digest / plan_digest
7. 写 release/plans/<plan_digest>.json
8. 返回 PreparedRelease

不产任何产物文件；只跑静态 gates；毫秒~秒级。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import toml

from server.component_manifest import load_manifests
from .audience import load_audience_policy
from .digest import (
    compute_components_digest,
    compute_exemptions_digest,
    compute_plan_digest,
    compute_profile_digest,
    compute_scan_digest,
)
from .manifest import compute_file_entries, load_components_for_audience, validate_manifest_shape
from .models import Exemption, FileEntry, GateResult, PreparedRelease

# 项目根目录（prepare.py 在 tools/release/engine/，需 4 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PROFILES_DIR = _PROJECT_ROOT / "release" / "profiles"
_PLANS_DIR = _PROJECT_ROOT / "release" / "plans"


# ========== 简化的敏感扫描模式（spec Solution 节"static gates"） ==========
# 复用 audit_profile.py 的核心 SENSITIVE_PATTERNS，但只保留确定性高的子集
# 完整扫描逻辑在 build 阶段或 audit CLI 中执行；prepare 阶段只做"是否含明显敏感内容"判断

_SENSITIVE_PATTERNS: dict[str, dict] = {
    "api-key-or-token": {
        "severity": "HIGH",
        "patterns": [
            re.compile(r"""\bsk-[A-Za-z0-9]{20,}\b"""),
            re.compile(r"""\bAKIA[0-9A-Z]{16}\b"""),
            re.compile(r"""\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"""),
        ],
    },
    "private-repository-reference": {
        "severity": "HIGH",
        "patterns": [
            re.compile(r"""GitHub\s*镜像"""),
            re.compile(r"""镜像备份"""),
            re.compile(r"""DMCA\s*backup"""),
        ],
    },
    "local-absolute-path": {
        "severity": "MEDIUM",
        "patterns": [
            re.compile(r"""\b[A-Z]:\\[^\s"'<>|*?]+"""),
            re.compile(r"""\b[A-Z]:/[^\s"'<>|*?]+"""),
            re.compile(r"""/home/[a-zA-Z0-9_\-]+/[^\s"'<>|*?]+"""),
            re.compile(r"""/Users/[a-zA-Z0-9_\-]+/[^\s"'<>|*?]+"""),
        ],
    },
}

# 跳过的二进制文件扩展名（不扫描）
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip",
                ".gz", ".tar", ".7z", ".wav", ".mp3", ".mp4", ".avi", ".mov",
                ".onnx", ".pt", ".pth", ".bin", ".pkl", ".npy", ".npz"}


# ========== Source commit 解析 ==========

def _resolve_source_commit(source: str) -> str:
    """解析 source 参数为 git commit sha

    Args:
        source: "HEAD" 或具体 commit sha

    Returns:
        40-char git commit sha
    """
    if source == "HEAD" or len(source) < 40:
        result = subprocess.run(
            ["git", "rev-parse", source],
            cwd=_PROJECT_ROOT,
            check=True,
            stdout=subprocess.PIPE,
        )
        return result.stdout.decode("utf-8").strip()
    return source


# ========== 静态 gates 实现 ==========

def _gate_policy_schema(profile_id: str, profile_path: Path) -> GateResult:
    """静态 gate: policy-schema（profile 必须含 schema_version / profile_id / audience / [approval] / [behavior]）"""
    try:
        data = toml.load(profile_path)
    except Exception as e:
        return GateResult(name="policy-schema", passed=False, details=f"profile parse error: {e}")

    required_top = ["schema_version", "profile_id", "audience", "approval", "behavior"]
    missing = [k for k in required_top if k not in data]
    if missing:
        return GateResult(
            name="policy-schema",
            passed=False,
            details=f"profile missing required fields: {missing}",
        )

    if data["profile_id"] != profile_id:
        return GateResult(
            name="policy-schema",
            passed=False,
            details=f"profile_id mismatch: file={data['profile_id']}, expected={profile_id}",
        )

    if data["schema_version"] != 1:
        return GateResult(
            name="policy-schema",
            passed=False,
            details=f"unsupported schema_version: {data['schema_version']}",
        )

    return GateResult(
        name="policy-schema",
        passed=True,
        details=f"profile_id={profile_id}, schema_version=1, has [approval]+[behavior]",
    )


def _gate_tracked_file_inventory(file_entries: tuple[FileEntry, ...]) -> GateResult:
    """静态 gate: tracked-file-inventory（file_entries 全部 source=tracked）"""
    non_tracked = [e for e in file_entries if e.source != "tracked"]
    if non_tracked:
        return GateResult(
            name="tracked-file-inventory",
            passed=False,
            details=f"{len(non_tracked)} non-tracked entries: {[e.rel_path for e in non_tracked[:5]]}",
        )
    return GateResult(
        name="tracked-file-inventory",
        passed=True,
        details=f"{len(file_entries)} entries all tracked",
    )


def _gate_sensitive_content_scan(
    file_entries: tuple[FileEntry, ...],
    line_skips: dict[str, list[str]],
    post_process_remove_lines: dict[str, list[str]] | None = None,
) -> tuple[GateResult, dict]:
    """静态 gate: sensitive-content-scan（扫源文件敏感内容，命中且无 exemption → fail）

    G30 决策（2026-08-10）：post_process_remove_lines 统一语义——
    扫描时跳过（prepare 期）+ build 期物理删除。避免 prepare 期因将被
    物理删除的 HIGH 命中行而 gate 失败（如 changelog-archive.md 中私有仓库引用行）。

    Returns:
        (GateResult, scan_results_dict)
    """
    hits: list[dict] = []
    scanned = 0
    skipped = 0

    for entry in file_entries:
        abs_path = _PROJECT_ROOT / entry.rel_path
        if abs_path.suffix.lower() in _BINARY_EXTS:
            skipped += 1
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            skipped += 1
            continue

        # 应用 line_skips + post_process_remove_lines：匹配行跳过所有敏感规则
        # G30 决策：post_process_remove_lines 在扫描时也跳过（统一语义：
        # 扫描跳过 + build 期物理删除），避免将被删除的 HIGH 行导致 prepare 失败
        skip_patterns = list(line_skips.get(entry.rel_path, []))
        if post_process_remove_lines:
            skip_patterns.extend(post_process_remove_lines.get(entry.rel_path, []))
        skip_res = [re.compile(p) for p in skip_patterns]

        for line_no, line in enumerate(content.splitlines(), start=1):
            # 检查该行是否被 skip
            if any(r.search(line) for r in skip_res):
                continue
            for rule_name, cfg in _SENSITIVE_PATTERNS.items():
                for pat in cfg["patterns"]:
                    if pat.search(line):
                        hits.append({
                            "path": entry.rel_path,
                            "line": line_no,
                            "rule": rule_name,
                            "severity": cfg["severity"],
                        })

        scanned += 1

    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_path: dict[str, int] = {}
    for h in hits:
        by_severity[h["severity"]] = by_severity.get(h["severity"], 0) + 1
        by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
        by_path[h["path"]] = by_path.get(h["path"], 0) + 1

    scan_results = {
        "scanned_files": scanned,
        "skipped_files": skipped,
        "total_hits": len(hits),
        "hits_by_severity": by_severity,
        "hits_by_rule": by_rule,
        "hits_by_path": by_path,
    }

    # gate 失败标准：HIGH 命中 > 0（MEDIUM 命中可由 path_mapping 在 build 阶段清掉）
    high_hits = by_severity.get("HIGH", 0)
    if high_hits > 0:
        return (
            GateResult(
                name="sensitive-content-scan",
                passed=False,
                details=f"{high_hits} HIGH hits: {by_rule}",
            ),
            scan_results,
        )
    return (
        GateResult(
            name="sensitive-content-scan",
            passed=True,
            details=f"{len(hits)} hits (HIGH=0, MEDIUM={by_severity.get('MEDIUM', 0)}); {scanned} scanned",
        ),
        scan_results,
    )


def _gate_manifest_audit(components: dict) -> GateResult:
    """静态 gate: manifest-audit（所有 audience components 的 manifest 满足新 shape）"""
    for name, manifest in components.items():
        manifest_path = manifest.workspace_dir / "manifest.toml"
        try:
            validate_manifest_shape(manifest_path)
        except ValueError as e:
            return GateResult(
                name="manifest-audit",
                passed=False,
                details=f"component '{name}' manifest invalid: {e}",
            )
    return GateResult(
        name="manifest-audit",
        passed=True,
        details=f"{len(components)} manifests all new-shape",
    )


# ========== public 专属静态 gates（spec-v3-public.md Solution 节） ==========

# license-clearance 允许的 license_class（public 受众）
# 兼容 SPDX 标识符（apache-2.0）和简写（apache-2）
_LICENSE_ALLOWED = {"mit", "apache-2", "apache-2.0"}


def _gate_license_clearance(
    file_entries: tuple[FileEntry, ...],
    components: dict,
) -> GateResult:
    """静态 gate: license-clearance（public 专属）

    按 file_entries 的 rel_path 前缀反查所属组件，校验组件 license_class ∈ {mit, apache-2}。
    禁止只检查组件 manifest 不检查 file_entries 实际归属（spec Anti-Cheat 硬约束）。
    """
    # 构建组件前缀映射：workspace/disk_manager/ → disk_manager
    prefix_map: dict[str, str] = {}
    for name, manifest in components.items():
        try:
            rel = manifest.workspace_dir.relative_to(_PROJECT_ROOT)
            prefix = str(rel).replace("\\", "/") + "/"
            prefix_map[prefix] = name
        except ValueError:
            continue

    # 按 file_entries 反查所属组件（只记录实际出现在 file_entries 中的组件）
    component_license: dict[str, str] = {}  # component_name -> license_class
    for entry in file_entries:
        rel_path = entry.rel_path.replace("\\", "/")
        for prefix, name in prefix_map.items():
            if rel_path.startswith(prefix):
                if name not in component_license:
                    manifest = components[name]
                    lc = manifest.release.license_class if manifest.release else "unknown"
                    component_license[name] = lc
                break

    # 检查 license_class
    violations = {name: lc for name, lc in component_license.items() if lc not in _LICENSE_ALLOWED}
    if violations:
        return GateResult(
            name="license-clearance",
            passed=False,
            details=f"components with non-allowed license_class: {violations} (allowed: {_LICENSE_ALLOWED})",
        )
    return GateResult(
        name="license-clearance",
        passed=True,
        details=f"{len(component_license)} components all license_class in {_LICENSE_ALLOWED}: {sorted(component_license.keys())}",
    )


def _gate_no_agpl_import(file_entries: tuple[FileEntry, ...]) -> GateResult:
    """静态 gate: no-agpl-import（public 专属）

    双保险检查（spec Anti-Cheat 硬约束：缺一不可）：
    1. pyproject.toml 无 ultralytics 依赖（跳过注释行）
    2. file_entries 内容无 import ultralytics / from ultralytics 语句（跳过注释行）
    """
    violations: list[str] = []

    # Check 1: pyproject.toml 无 ultralytics 依赖
    pyproject_path = _PROJECT_ROOT / "pyproject.toml"
    if pyproject_path.exists():
        try:
            content = pyproject_path.read_text(encoding="utf-8")
            for line_no, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "ultralytics" in line.lower():
                    violations.append(f"pyproject.toml:{line_no}: {line.strip()}")
        except Exception:
            pass

    # Check 2: file_entries 内容无 import ultralytics / from ultralytics
    for entry in file_entries:
        if not entry.rel_path.endswith(".py"):
            continue
        abs_path = _PROJECT_ROOT / entry.rel_path
        if not abs_path.exists():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\b(import\s+ultralytics|from\s+ultralytics)\b", line):
                violations.append(f"{entry.rel_path}:{line_no}: {line.strip()}")

    if violations:
        return GateResult(
            name="no-agpl-import",
            passed=False,
            details=f"{len(violations)} violations: {violations[:5]}",
        )
    return GateResult(
        name="no-agpl-import",
        passed=True,
        details="pyproject.toml + file_entries all clean (no ultralytics)",
    )


# ========== Exemptions 派生 ==========

def _derive_exemptions(profile_data: dict) -> tuple[Exemption, ...]:
    """从 profile [sensitive_line_skips] + [post_process_remove_lines] 派生 Exemption tuple

    每条 exemption 含 path/reason/fingerprint/expires_at：
    - path: 文件相对路径
    - reason: "sensitive_line_skips" 或 "post_process_remove_lines"
    - fingerprint: sha256(patterns concat)
    - expires_at: 远期 ISO 日期（phase 2 不强制过期，统一设为 2099-12-31）
    """
    expires = "2099-12-31"
    result: list[Exemption] = []

    line_skips = profile_data.get("sensitive_line_skips", {})
    for path, patterns in line_skips.items():
        if not isinstance(patterns, list):
            continue
        fp = hashlib.sha256("|".join(patterns).encode("utf-8")).hexdigest()
        result.append(Exemption(
            path=path,
            reason="sensitive_line_skips",
            fingerprint=fp,
            expires_at=expires,
        ))

    post_remove = profile_data.get("post_process_remove_lines", {})
    for path, patterns in post_remove.items():
        if not isinstance(patterns, list):
            continue
        fp = hashlib.sha256("|".join(patterns).encode("utf-8")).hexdigest()
        result.append(Exemption(
            path=path,
            reason="post_process_remove_lines",
            fingerprint=fp,
            expires_at=expires,
        ))

    # 按 path 排序保证确定性
    result.sort(key=lambda e: (e.path, e.reason))
    return tuple(result)


# ========== PreparedRelease 写出 ==========

def _write_plan_file(plan: PreparedRelease) -> Path:
    """写 release/plans/<plan_digest>.json"""
    _PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = _PLANS_DIR / f"{plan.plan_digest}.json"
    plan_path.write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return plan_path


# ========== prepare_release 主接口 ==========

def prepare_release(
    profile: str,
    source: str = "HEAD",
    audience: str = "friend",
) -> PreparedRelease:
    """编译输入为不可变 PreparedRelease

    Args:
        profile: profile_id，如 "friend-full"
        source: git commit sha 或 "HEAD"
        audience: audience name，如 "friend"（从 profile.audience 推断或显式传）

    Returns:
        PreparedRelease（13 字段全非空）

    Raises:
        FileNotFoundError: profile 或 audience 文件不存在
        ValueError: 任意静态 gate 失败 / manifest 解析失败 / 组件缺失
    """
    # 1. 加载 audience policy
    audience_policy = load_audience_policy(audience)

    # 2. 解析 source commit
    source_commit = _resolve_source_commit(source)

    # 3. 加载 profile 文件
    profile_path = _PROFILES_DIR / f"{profile}.toml"
    if not profile_path.exists():
        raise FileNotFoundError(f"profile not found: {profile_path}")
    profile_data = toml.load(profile_path)

    # 4. 加载 components
    components = load_components_for_audience(audience_policy)

    # 5. 计算 file_entries
    export_set = audience_policy["audience"]["export_set"]
    # spec-v4 决策 G1：从 audience policy 取 core_files / core_files_exclude
    # 若 audience 无此字段（如 v3 audience），get 返回空 list，compute_file_entries 走原逻辑
    core_files = audience_policy["audience"].get("core_files", [])
    core_files_exclude = audience_policy["audience"].get("core_files_exclude", [])
    file_entries = compute_file_entries(
        components, export_set, source_commit,
        core_files=core_files or None,
        core_files_exclude=core_files_exclude or None,
    )

    # 6. 准备 line_skips + post_process_remove_lines（用于 sensitive-content-scan）
    # G30 决策：post_process_remove_lines 也用于扫描跳过（统一语义：
    # 扫描时跳过 + build 期物理删除）
    line_skips: dict[str, list[str]] = {}
    for k, v in profile_data.get("sensitive_line_skips", {}).items():
        if isinstance(v, list):
            line_skips[k] = v
    post_process_remove_lines: dict[str, list[str]] = {}
    for k, v in profile_data.get("post_process_remove_lines", {}).items():
        if isinstance(v, list):
            post_process_remove_lines[k] = v

    # 7. 跑 4 静态 gates
    gate_results: list[GateResult] = []

    g1 = _gate_policy_schema(profile, profile_path)
    gate_results.append(g1)
    if not g1.passed:
        raise ValueError(f"static gate failed: {g1.name}: {g1.details}")

    g2 = _gate_tracked_file_inventory(file_entries)
    gate_results.append(g2)
    if not g2.passed:
        raise ValueError(f"static gate failed: {g2.name}: {g2.details}")

    g3, scan_results = _gate_sensitive_content_scan(file_entries, line_skips, post_process_remove_lines)
    gate_results.append(g3)
    if not g3.passed:
        raise ValueError(f"static gate failed: {g3.name}: {g3.details}")

    g4 = _gate_manifest_audit(components)
    gate_results.append(g4)
    if not g4.passed:
        raise ValueError(f"static gate failed: {g4.name}: {g4.details}")

    # 8. 计算 digests
    profile_digest = compute_profile_digest(profile_path)
    components_digest = compute_components_digest(tuple(components.keys()))
    scan_digest = compute_scan_digest(scan_results)
    exemptions = _derive_exemptions(profile_data)
    exemptions_digest = compute_exemptions_digest(exemptions)

    # 9. 计算 plan_digest
    plan_digest = compute_plan_digest({
        "profile_digest": profile_digest,
        "components_digest": components_digest,
        "scan_digest": scan_digest,
        "exemptions_digest": exemptions_digest,
        "source_commit": source_commit,
        "file_entries": file_entries,
    })

    # 10. 构造 PreparedRelease
    gates_required = tuple(
        list(audience_policy["audience"]["gates"]["static"])
        + list(audience_policy["audience"]["gates"]["build_time"])
    )

    now = datetime.now(timezone.utc).isoformat()

    plan = PreparedRelease(
        plan_digest=plan_digest,
        profile_id=profile,
        profile_digest=profile_digest,
        source_commit=source_commit,
        components=tuple(components.keys()),
        components_digest=components_digest,
        audience=audience,
        file_entries=file_entries,
        exemptions=exemptions,
        scan_digest=scan_digest,
        gates_required=gates_required,
        gates_results=tuple(gate_results),
        created_at=now,
    )

    # 11. 写计划文件
    _write_plan_file(plan)

    return plan
