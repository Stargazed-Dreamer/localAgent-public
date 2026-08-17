"""Release engine v2 compiler tests (T16).

Tests for the phase 2 release compiler per temp/sdd/release-engine/spec-v2-compiler.md:
- PreparedRelease immutability + digest determinism
- prepare_release() interface (static gates, plan file write)
- build_release() interface (digest verification, build-time gates, archive verification)
- Manifest new shape parsing (fail closed on old shape, 20 manifests load)
- Audience policy loading (friend loads, public reserved)
- Jinja2 template rendering (no zip_size placeholder)
- path_mapping reorganization (replaces personal paths, covers all 74 literals)

Test design principles:
- Mock git/source_commit where needed to avoid depending on real repo state
- Use real engine modules (not mocked) for end-to-end coverage
- Verify external behavior (plan structure, digest mismatch raises, template content)
- Do not depend on real release/dist/ directory
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools.release.engine.models import (
    Approval,
    FileEntry,
    GateResult,
    PreparedRelease,
)

# ========== Test fixtures ==========

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLANS_DIR = PROJECT_ROOT / "release" / "plans"


def _make_file_entry(rel_path: str = "server/main.py", content: bytes = b"test\n") -> FileEntry:
    """Construct a deterministic FileEntry for tests."""
    return FileEntry(
        rel_path=rel_path,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        source="tracked",
    )


def _make_plan(
    plan_digest: str = "a" * 64,
    profile_id: str = "friend-full",
    profile_digest: str = "b" * 64,
    source_commit: str = "0" * 40,
    file_entries: tuple[FileEntry, ...] | None = None,
) -> PreparedRelease:
    """Construct a minimal valid PreparedRelease for tests."""
    if file_entries is None:
        file_entries = (_make_file_entry(),)
    return PreparedRelease(
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest=profile_digest,
        source_commit=source_commit,
        components=("web_archive",),
        components_digest="c" * 64,
        audience="friend",
        file_entries=file_entries,
        exemptions=(),
        scan_digest="d" * 64,
        gates_required=("policy-schema", "tracked-file-inventory",
                        "sensitive-content-scan", "manifest-audit",
                        "no-key-startup", "archive-verification"),
        gates_results=(
            GateResult(name="policy-schema", passed=True, details="ok"),
            GateResult(name="tracked-file-inventory", passed=True, details="ok"),
            GateResult(name="sensitive-content-scan", passed=True, details="ok"),
            GateResult(name="manifest-audit", passed=True, details="ok"),
        ),
        created_at="2026-07-31T00:00:00+00:00",
    )


def _make_approval(plan_digest: str = "a" * 64) -> Approval:
    """Construct an Approval matching a plan's plan_digest."""
    return Approval(
        plan_digest=plan_digest,
        approved_at="2026-07-31T12:00:00+08:00",
        approved_by="test-operator",
    )


# ========== 1. PreparedRelease 模型测试 (2 个) ==========

def test_prepared_release_immutable() -> None:
    """PreparedRelease 必须是 frozen dataclass（spec Anti-Cheat 硬约束）。

    尝试修改任何字段必须抛 FrozenInstanceError。
    """
    plan = _make_plan()
    assert dataclasses.is_dataclass(plan)
    assert plan.__dataclass_params__.frozen, "PreparedRelease must be frozen"

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.plan_digest = "different"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.profile_id = "other-profile"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.file_entries = ()  # type: ignore[misc]


def test_plan_digest_deterministic() -> None:
    """compute_plan_digest 对同输入必须产生同 digest（确定性）。

    不同输入（file_entries 不同）必须产生不同 digest。
    """
    from tools.release.engine.digest import compute_plan_digest

    base_fields = {
        "profile_digest": "a" * 64,
        "components_digest": "b" * 64,
        "scan_digest": "c" * 64,
        "exemptions_digest": "d" * 64,
        "source_commit": "0" * 40,
        "file_entries": [_make_file_entry("a.py").to_dict()],
    }

    digest1 = compute_plan_digest(dict(base_fields))
    digest2 = compute_plan_digest(dict(base_fields))
    assert digest1 == digest2, "same inputs must produce same digest"

    # 不同输入 → 不同 digest
    different_fields = dict(base_fields)
    different_fields["source_commit"] = "1" * 40
    digest3 = compute_plan_digest(different_fields)
    assert digest3 != digest1, "different source_commit must produce different digest"

    # file_entries 顺序无关（按 rel_path 排序后入哈希）
    fields_rev = dict(base_fields)
    fields_rev["file_entries"] = [_make_file_entry("z.py").to_dict(), _make_file_entry("a.py").to_dict()]
    fields_sorted = dict(base_fields)
    fields_sorted["file_entries"] = [_make_file_entry("a.py").to_dict(), _make_file_entry("z.py").to_dict()]
    assert compute_plan_digest(fields_rev) == compute_plan_digest(fields_sorted)


# ========== 2. prepare_release 测试 (3 个) ==========

def test_prepare_release_produces_plan() -> None:
    """prepare_release() 返回 PreparedRelease，13 字段全非空。"""
    from tools.release.engine.prepare import prepare_release

    try:
        plan = prepare_release(
            profile="friend-full",
            source="HEAD",
            audience="friend",
        )
    except (FileNotFoundError, ValueError) as e:
        pytest.skip(f"prepare_release requires real repo state: {e}")

    # 13 字段全非空
    assert plan.plan_digest and len(plan.plan_digest) == 64
    assert plan.profile_id == "friend-full"
    assert plan.profile_digest and len(plan.profile_digest) == 64
    assert plan.source_commit and len(plan.source_commit) == 40
    assert plan.components  # non-empty tuple
    assert plan.components_digest and len(plan.components_digest) == 64
    assert plan.audience == "friend"
    assert plan.file_entries  # non-empty
    assert isinstance(plan.exemptions, tuple)
    assert plan.scan_digest and len(plan.scan_digest) == 64
    assert plan.gates_required  # non-empty
    assert plan.gates_results  # non-empty
    assert plan.created_at


def test_prepare_release_runs_static_gates() -> None:
    """prepare_release() 运行 4 静态 gates，gates_results 含 4 项全 passed。"""
    from tools.release.engine.prepare import prepare_release

    try:
        plan = prepare_release(
            profile="friend-full",
            source="HEAD",
            audience="friend",
        )
    except (FileNotFoundError, ValueError) as e:
        pytest.skip(f"prepare_release requires real repo state: {e}")

    assert len(plan.gates_results) == 4, (
        f"expected 4 static gate results, got {len(plan.gates_results)}"
    )

    expected_gates = {"policy-schema", "tracked-file-inventory",
                      "sensitive-content-scan", "manifest-audit"}
    actual_gates = {g.name for g in plan.gates_results}
    assert actual_gates == expected_gates, (
        f"gate names mismatch: expected {expected_gates}, got {actual_gates}"
    )

    for g in plan.gates_results:
        assert g.passed, f"static gate {g.name} should pass: {g.details}"


def test_prepare_release_writes_plan_file() -> None:
    """prepare_release() 写 release/plans/<plan_digest>.json，可反序列化。"""
    from tools.release.engine.prepare import prepare_release

    try:
        plan = prepare_release(
            profile="friend-full",
            source="HEAD",
            audience="friend",
        )
    except (FileNotFoundError, ValueError) as e:
        pytest.skip(f"prepare_release requires real repo state: {e}")

    plan_path = PLANS_DIR / f"{plan.plan_digest}.json"
    assert plan_path.exists(), f"plan file not written: {plan_path}"

    # 可反序列化
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_data["plan_digest"] == plan.plan_digest
    assert plan_data["profile_id"] == plan.profile_id
    assert plan_data["source_commit"] == plan.source_commit

    # 通过 from_dict 还原
    restored = PreparedRelease.from_dict(plan_data)
    assert restored == plan, "restored plan must equal original"


# ========== 3. build_release 测试 (5 个) ==========

def test_build_release_verifies_plan_digest() -> None:
    """build_release() 验证 approval.plan_digest == plan.plan_digest；不匹配 raise。"""
    from tools.release.engine.build import build_release

    plan = _make_plan(plan_digest="a" * 64)
    # 改一字符
    wrong_approval = _make_approval(plan_digest="b" * 64)

    with pytest.raises(ValueError, match="plan_digest mismatch"):
        build_release(plan, wrong_approval, skip_heavy_gates=True)


def test_build_release_verifies_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_release() 验证当前 git commit == plan.source_commit；漂移 raise。"""
    from tools.release.engine import build as build_module
    from tools.release.engine.build import build_release

    plan = _make_plan(source_commit="0" * 40)
    approval = _make_approval(plan_digest=plan.plan_digest)

    # Mock git rev-parse to return a different commit
    def fake_run(cmd, **kwargs):
        if cmd == ["git", "rev-parse", "HEAD"]:
            class FakeResult:
                stdout = b"1" * 40
            return FakeResult()
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)
    # profile_digest 验证已移除（过度严格），不再需要 mock _verify_profile_digest

    with pytest.raises(ValueError, match="source commit drifted"):
        build_release(plan, approval, skip_heavy_gates=True)


def test_build_release_no_longer_verifies_profile_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_release() 不再验证 profile_digest（2026-07-31 设计变更）。

    原验证过度严格：填 [approval] 段就漂移，导致 build 不可用。
    profile 内容已通过 plan_digest 间接绑定（plan_digest 含 profile_digest 字段）。
    此测试验证：即使 profile_digest 不匹配，build 也能走到完成（不 raise "profile drifted"）。

    修复（2026-08-06 测试修复铁律）：原用 try/except Exception: pass 吞 staging 异常，
    违反铁律"禁止用 try/except 掩盖 crash"。改为 mock 下游依赖让 build_release 走到完成，
    断言 artifact 创建成功——若 build_release 引入新 bug 会被捕获，而非被吞。
    参照同文件 test_build_release_runs_build_time_gates 的 mock 模式。
    """
    from tools.release.engine import build as build_module
    from tools.release.engine.build import build_release

    # file_entries 用真实 content 让 fake_copy 写入的文件 sha256 与 entry 匹配
    file_content = b"print('hello')\n"
    plan = _make_plan(
        profile_digest="a" * 64,  # 故意不匹配真实 profile
        file_entries=(_make_file_entry("main.py", file_content),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)

    # Mock source_commit 验证通过（profile_digest 验证已移除，无需 mock）
    monkeypatch.setattr(build_module, "_verify_source_commit", lambda plan: None)
    # Mock path_mapping loader 返回空（无替换）
    monkeypatch.setattr(build_module, "_load_path_mapping", lambda profile_id: {})
    # Mock _copy_files_to_staging 写入测试文件（与 entry sha256 匹配）
    def fake_copy(plan, staging_dir):
        staging_dir.mkdir(parents=True, exist_ok=True)
        for entry in plan.file_entries:
            dst = staging_dir / entry.rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(file_content)
    monkeypatch.setattr(build_module, "_copy_files_to_staging", fake_copy)
    # Mock staging/dist 目录到 tmp_path，避免污染真实 release/dist/
    monkeypatch.setattr(build_module, "_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(build_module, "_DIST_DIR", tmp_path / "dist")

    # 若 profile_digest 验证仍存在，build_release 会在早期 raise ValueError("profile drifted")
    # 修复后 profile_digest 验证已移除，build 应走到完成并返回 artifact
    artifact = build_release(plan, approval, skip_heavy_gates=True)

    # 断言 artifact 创建成功（证明 profile drift 检查没在早期 raise）
    assert artifact is not None
    assert artifact.zip_path.exists()
    assert artifact.zip_sha256 and len(artifact.zip_sha256) == 64


def test_build_release_runs_build_time_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_release() 运行 4 构建期 gates（含 archive-verification）。"""
    from tools.release.engine import build as build_module
    from tools.release.engine.build import build_release

    # Set up a minimal "real" build by mocking source commit
    plan = _make_plan(
        file_entries=(_make_file_entry("main.py", b"print('hello')\n"),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)

    monkeypatch.setattr(build_module, "_verify_source_commit", lambda plan: None)
    # profile_digest 验证已移除，不再 mock _verify_profile_digest

    # Mock the path_mapping loader to return empty (no replacements)
    monkeypatch.setattr(build_module, "_load_path_mapping", lambda profile_id: {})

    # Mock _copy_files_to_staging to write the test file
    def fake_copy(plan, staging_dir):
        staging_dir.mkdir(parents=True, exist_ok=True)
        for entry in plan.file_entries:
            dst = staging_dir / entry.rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Recreate content matching the entry's sha256
            dst.write_bytes(b"print('hello')\n")

    monkeypatch.setattr(build_module, "_copy_files_to_staging", fake_copy)

    # Mock heavy gates to pass
    monkeypatch.setattr(
        build_module,
        "_gate_no_key_startup",
        lambda sd: GateResult(name="no-key-startup", passed=True, details="mock ok"),
    )
    monkeypatch.setattr(
        build_module,
        "_gate_activity_task_safe_stop",
        lambda sd: GateResult(name="activity-task-safe-stop", passed=True, details="mock ok"),
    )
    monkeypatch.setattr(
        build_module,
        "_gate_focused_tests",
        lambda sd: GateResult(name="focused-tests", passed=True, details="mock ok"),
    )

    # Mock the staging and dist dirs to use tmp_path
    monkeypatch.setattr(build_module, "_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(build_module, "_DIST_DIR", tmp_path / "dist")

    artifact = build_release(plan, approval, skip_heavy_gates=False)

    assert artifact.archive_verification_passed
    assert artifact.zip_path.exists()
    assert artifact.zip_sha256 and len(artifact.zip_sha256) == 64

    # Verify ZIP contains MANIFEST.json + sidecar
    with zipfile.ZipFile(artifact.zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "MANIFEST.json" in names
        assert "MANIFEST.json.sha256" in names
        assert "main.py" in names


def test_build_release_archive_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_release() archive-verification：ZIP 篡改时 raise。"""
    from tools.release.engine.build import _verify_archive

    # Construct a valid ZIP with MANIFEST.json + sidecar + 1 file
    staging = tmp_path / "staging"
    staging.mkdir()

    file_content = b"print('hello')\n"
    file_sha = hashlib.sha256(file_content).hexdigest()

    (staging / "main.py").write_bytes(file_content)

    manifest_data = {
        "files": [
            {"path": "main.py", "sha256": file_sha, "size": len(file_content), "source": "tracked"},
        ],
    }
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (staging / "MANIFEST.json.sha256").write_text(f"{manifest_sha}  MANIFEST.json\n", encoding="utf-8")

    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(staging / "main.py", "main.py")
        zf.write(manifest_path, "MANIFEST.json")
        zf.write(staging / "MANIFEST.json.sha256", "MANIFEST.json.sha256")

    # Verification passes on unmodified ZIP
    _verify_archive(zip_path, manifest_data["files"])

    # Now tamper: create a ZIP with different content for main.py
    tampered_zip = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", b"TAMPERED CONTENT\n")
        zf.write(manifest_path, "MANIFEST.json")
        zf.write(staging / "MANIFEST.json.sha256", "MANIFEST.json.sha256")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        _verify_archive(tampered_zip, manifest_data["files"])


# ========== 4. manifest 新 shape 测试 (3 个) ==========

def test_manifest_new_shape_parsed(tmp_path: Path) -> None:
    """新 shape manifest（[exports.runtime] / [exports.source] / [release_facts]）解析正确。"""
    from server.component_manifest import ReleaseEntry, _parse_manifest

    workspace_dir = tmp_path / "workspace" / "test_module"
    workspace_dir.mkdir(parents=True)
    manifest_path = workspace_dir / "manifest.toml"

    manifest_path.write_text(
        """
[component]
name = "test_module"
version = "0.1.0"
description = "test component"
enabled = true

[exports.runtime]
files = ["server/main.py", "workspace/test_module/**"]

[exports.source]
files = ["server/main.py", "workspace/test_module/**", "tests/test_test_module.py"]

[release_facts]
contains_personal_data = false
requires_external_credentials = true
license_class = "internal-review"
""",
        encoding="utf-8",
    )

    m = _parse_manifest(manifest_path)
    assert m is not None
    assert m.release is not None
    assert isinstance(m.release, ReleaseEntry)
    assert m.release.exports_runtime == ["server/main.py", "workspace/test_module/**"]
    assert m.release.exports_source == ["server/main.py", "workspace/test_module/**", "tests/test_test_module.py"]
    assert m.release.contains_personal_data is False
    assert m.release.requires_external_credentials is True
    assert m.release.license_class == "internal-review"


def test_manifest_old_shape_rejected(tmp_path: Path) -> None:
    """旧 shape manifest（[release] 段含 distribution）必须 raise（big bang，不维持双 shape）。"""
    from server.component_manifest import _parse_manifest

    workspace_dir = tmp_path / "workspace" / "old_module"
    workspace_dir.mkdir(parents=True)
    manifest_path = workspace_dir / "manifest.toml"

    manifest_path.write_text(
        """
[component]
name = "old_module"
version = "0.1.0"
description = "old shape component"
enabled = true

[release]
distribution = "include"
include_scripts = []
exclude_personal_data = []
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="deprecated|old shape|\\[release\\]"):
        _parse_manifest(manifest_path)


def test_all_workspace_manifests_load() -> None:
    """workspace manifest 全部用新 shape 加载通过（T14 big bang 验证）。

    注：原断言 >= 20。v0.24.1 把 deep_research 和 office_docs 迁回 .agents/skills/
    目录（commit 2d6335a），删除了它们的 workspace/manifest.toml，manifest 数量从 20 降为 19。
    这是设计决策（组件迁移）不是 bug，断言改为 >= 19。
    """
    from server.component_manifest import load_manifests

    manifests = load_manifests()

    assert len(manifests) >= 19, (
        f"expected >= 19 manifests, got {len(manifests)}. "
        f"T14 big bang rewrite incomplete?"
    )

    # 全部 release 字段非 None（已迁移到新 shape）
    for name, m in manifests.items():
        if m.release is None:
            # Some components might not have [exports] (e.g., disabled or private)
            # but workspace manifests should all have migrated
            continue
        # Verify new shape fields exist
        assert hasattr(m.release, "exports_runtime"), f"{name} missing exports_runtime"
        assert hasattr(m.release, "exports_source"), f"{name} missing exports_source"
        assert hasattr(m.release, "contains_personal_data"), f"{name} missing contains_personal_data"
        assert hasattr(m.release, "requires_external_credentials"), f"{name} missing requires_external_credentials"
        assert hasattr(m.release, "license_class"), f"{name} missing license_class"


# ========== 5. audience policy 测试 (2 个) ==========

def test_friend_audience_policy_loads() -> None:
    """friend audience policy 加载并校验字段。"""
    from tools.release.engine.audience import load_audience_policy

    policy = load_audience_policy("friend")

    assert policy["schema_version"] == 1
    assert policy["audience"]["name"] == "friend"
    assert policy["audience"]["profile"] == "friend-full"
    assert isinstance(policy["audience"]["components"], list)
    assert len(policy["audience"]["components"]) > 0
    assert policy["audience"]["export_set"] in ("source", "runtime")
    assert "static" in policy["audience"]["gates"]
    assert "build_time" in policy["audience"]["gates"]
    assert "policy-schema" in policy["audience"]["gates"]["static"]
    assert "archive-verification" in policy["audience"]["gates"]["build_time"]


def test_public_audience_not_reserved() -> None:
    """public audience policy 已从 reserved 升级为完整实现（phase 3），加载时不 raise。

    详见 temp/sdd/release-engine/spec-v3-public.md（T02 + T04）。
    public.toml 移除 status="reserved" 字段后，audience.py 的 reserved 检查不再触发。
    完整的 public policy 字段校验在 tests/test_release_public.py::test_public_audience_policy_loads。
    """
    from tools.release.engine.audience import load_audience_policy

    # 不再 raise "reserved"——public.toml 已升级为完整 audience policy（phase 3 T02）
    policy = load_audience_policy("public")
    assert policy["audience"]["name"] == "public"
    assert "status" not in policy["audience"]


# ========== 6. Jinja2 模板测试 (3 个) ==========

def test_deployment_md_from_template() -> None:
    """render_deployment_md 返回含 profile_id / 不含 zip_size 的 markdown。"""
    from tools.release.engine.templates import render_deployment_md

    plan = _make_plan()
    rendered = render_deployment_md(plan)

    assert plan.profile_id in rendered
    assert plan.audience in rendered
    assert plan.source_commit in rendered
    assert plan.plan_digest in rendered
    # spec Anti-Cheat 硬约束：模板不含 zip_size
    assert "zip_size" not in rendered
    assert "{{" not in rendered, "unrendered Jinja2 template variables"


def test_release_notes_from_template() -> None:
    """render_release_notes_md 返回含 plan_digest / 不含 zip_size 的 markdown。"""
    from tools.release.engine.templates import render_release_notes_md

    plan = _make_plan()
    rendered = render_release_notes_md(plan)

    assert plan.plan_digest in rendered
    assert plan.profile_id in rendered
    assert plan.source_commit in rendered
    # spec Anti-Cheat 硬约束：模板不含 zip_size
    assert "zip_size" not in rendered
    assert "{{" not in rendered


def test_template_has_no_zip_size() -> None:
    """模板源文件 grep 'zip_size' 应 0 命中（spec Anti-Cheat 硬约束）。"""
    templates_dir = PROJECT_ROOT / "tools" / "release" / "templates"
    assert templates_dir.exists(), f"templates dir not found: {templates_dir}"

    for template_file in templates_dir.glob("*.j2"):
        content = template_file.read_text(encoding="utf-8")
        assert "zip_size" not in content, (
            f"template {template_file.name} contains 'zip_size' — "
            "spec Anti-Cheat requires no zip_size placeholder (user 方案 B)"
        )


# ========== 7. path_mapping 测试 (2 个) ==========

def test_path_mapping_replaces_personal_paths() -> None:
    """path_mapping 规则正确替换个人路径为占位符。

    构造含 `users\\admin` 的文本 → 应用 path_mapping → 替换为 `<user_home>`。
    大小写不敏感（spec Q6 决策）。
    """
    import re

    import toml

    profile_path = PROJECT_ROOT / "release" / "profiles" / "friend-full.toml"
    profile_data = toml.load(profile_path)
    path_mapping = profile_data.get("content_replacements", {}).get("path_mapping", {})

    assert path_mapping, "path_mapping section empty or missing"
    assert "users\\admin" in path_mapping, "users\\admin rule missing"

    # 大小写不敏感字面替换
    test_content = r"C:\<user_home>\.trae-cn\mcps"
    path_mapping["users\\admin"]  # <user_home>

    # Apply: re.escape + re.IGNORECASE (per build.py _apply_path_mapping)
    # Note: repl wrapped in lambda to prevent re.subn from parsing backslash
    # escapes in replacement string (e.g., '<external_project_root>\MindForge'
    # has \M which would be misinterpreted as bad escape). Mirrors build.py.
    new_content = test_content
    for find, repl in path_mapping.items():
        new_content, _ = re.subn(
            re.escape(find), lambda m, r=repl: r, new_content, flags=re.IGNORECASE,
        )

    assert "<user_home>" in new_content, (
        f"users\\admin should be replaced with <user_home>, got: {new_content}"
    )
    assert "Users\\admin" not in new_content, (
        f"original 'Users\\admin' should not remain: {new_content}"
    )


def test_path_mapping_covers_personal_path_patterns() -> None:
    r"""path_mapping 覆盖原 74 条 literal 的核心场景。

    验证关键路径模式被 path_mapping 覆盖（不要求 1:1 映射，但所有原 literal
    的 find 字符串在 path_mapping 应用后应为空或被替换为占位符）。

    重点验证以下场景：
    - <project_root> → <project_root>
    - <external_project_root>\MindForge → <external_project_root>\MindForge
    - <user_home> → <user_home>
    - <data_drive>:\<working_root> → <data_drive>:\<working_root>
    - <data_drive>:\Documents → <data_drive>:\<data_drive>:\Documents
    - <data_drive>:\<bilibili_videos> → <data_drive>:\<bilibili_videos>
    """
    import re

    import toml

    profile_path = PROJECT_ROOT / "release" / "profiles" / "friend-full.toml"
    profile_data = toml.load(profile_path)
    path_mapping = profile_data.get("content_replacements", {}).get("path_mapping", {})

    # 编译所有 path_mapping 规则
    # Note: repl wrapped in lambda to prevent re.subn from parsing backslash
    # escapes in replacement string (e.g., '<external_project_root>\MindForge'
    # has \M which would be misinterpreted as bad escape). Mirrors build.py.
    compiled = [(re.escape(find), repl) for find, repl in path_mapping.items()]

    def apply_mapping(text: str) -> str:
        for pat, repl in compiled:
            text, _ = re.subn(pat, lambda m, r=repl: r, text, flags=re.IGNORECASE)
        return text

    # 验证核心场景
    test_cases = [
        # (input, expected_substring_after_mapping)
        (r"F:\<project_root>\server", "<project_root>"),
        (r"F:\<external_project_root>\MindForge\src", "<external_project_root>"),
        (r"C:\<user_home>\.trae-cn", "<user_home>"),
        (r"E:\<data_drive>:\<academic_root>\doc", "<academic_root>"),
        (r"F:\<data_drive>:\Documents\notes", "<data_drive>:\\<data_drive>:\Documents"),
        (r"F:\<data_drive>:\Pictures\photos", "<data_drive>:\\<data_drive>:\Pictures"),
        (r"D:\<data_drive>:\<bilibili_organized_output>\output", "<bilibili_organized_output>"),
        (r"E:\<data_drive>:\<bilibili_videos>\video", "<bilibili_videos>"),
    ]

    for input_text, expected_substring in test_cases:
        result = apply_mapping(input_text)
        assert expected_substring in result, (
            f"path_mapping failed for {input_text!r}: "
            f"expected {expected_substring!r} in result, got {result!r}"
        )
