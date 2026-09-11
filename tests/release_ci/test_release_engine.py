"""Release engine P0 integration tests (rewritten for v2 compiler).

Tests for product correctness guarantees (P0) per temp/sdd/release-engine/spec.md,
rewritten in T17 to use the v2 release compiler API (prepare_release + build_release
+ engine internals) instead of the old CLI (audit_profile + export_release).

P0 guarantees covered:
- P0-1: tracked-only file inventory (untracked files never enter release)
- P0-2: manifest fail-closed (corrupt manifest raises, not silent empty rules)
- P0-3: digest-bound approval (approval bound to plan digest)
- P0-4: gates before sealing (all gates run before ZIP reaches dist)
- P0-5: generated files in manifest (DEPLOYMENT.md / RELEASE_NOTES.md hashed)
- P0-6: archive verification (ZIP re-opened and verified after generation)
- P0-7: failed artifact stays in staging (dist/ never contains broken releases)

Test design principles (per spec Testing Decisions):
- Only test external behavior (build fail/succeed, artifact contains/excludes file,
  digest match/mismatch), not implementation details
- Use tmp_path + git init for tracked/untracked file scenarios
- Use monkeypatch for gate results and build internals, not for the public API
- Do not depend on real release/dist/ directory
- No imports from tools.release.audit_profile or tools.release.export_release
"""
from __future__ import annotations

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

# ========== Test helpers ==========

def _init_git_repo(root: Path) -> None:
    """Initialize a git repo in root and configure user for commits."""
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root, check=True, capture_output=True,
    )


def _commit_file(root: Path, rel_path: str, content: str = "test\n") -> None:
    """Create a file, git add, and commit it."""
    f = root / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {rel_path}"],
        cwd=root, check=True, capture_output=True,
    )


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
    """Construct a minimal valid PreparedRelease for build_release tests."""
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


def _setup_build_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plan: PreparedRelease,
    *,
    staging_files: dict[str, bytes] | None = None,
    heavy_gate_results: dict[str, GateResult] | None = None,
) -> tuple[Path, Path]:
    """Mock build_release internals to run against tmp_path.

    Args:
        monkeypatch: pytest monkeypatch fixture
        tmp_path: temp directory for staging + dist
        plan: PreparedRelease to build
        staging_files: {rel_path: content} to write into staging (default: from plan.file_entries)
        heavy_gate_results: {gate_name: GateResult} for no-key-startup / activity-task-safe-stop / focused-tests
                           (default: all pass)

    Returns:
        (staging_dir, dist_dir) paths
    """
    from tools.release.engine import build as build_module

    # Bypass drift checks (profile_digest 验证已移除，只需 mock source_commit)
    monkeypatch.setattr(build_module, "_verify_source_commit", lambda plan: None)

    # No path mapping
    monkeypatch.setattr(build_module, "_load_path_mapping", lambda profile_id: {})

    # Controlled staging files
    if staging_files is None:
        def fake_copy(plan, staging_dir):
            staging_dir.mkdir(parents=True, exist_ok=True)
            for entry in plan.file_entries:
                dst = staging_dir / entry.rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"print('hello')\n")
        monkeypatch.setattr(build_module, "_copy_files_to_staging", fake_copy)
    else:
        def fake_copy_custom(plan, staging_dir):
            staging_dir.mkdir(parents=True, exist_ok=True)
            for rel_path, content in staging_files.items():
                dst = staging_dir / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(content)
        monkeypatch.setattr(build_module, "_copy_files_to_staging", fake_copy_custom)

    # Heavy gates
    default_pass = GateResult(name="default", passed=True, details="mock ok")
    if heavy_gate_results is None:
        heavy_gate_results = {}
    monkeypatch.setattr(
        build_module, "_gate_no_key_startup",
        lambda sd: heavy_gate_results.get("no-key-startup", default_pass),
    )
    monkeypatch.setattr(
        build_module, "_gate_activity_task_safe_stop",
        lambda sd: heavy_gate_results.get("activity-task-safe-stop", default_pass),
    )
    monkeypatch.setattr(
        build_module, "_gate_focused_tests",
        lambda sd: heavy_gate_results.get("focused-tests", default_pass),
    )

    # Staging + dist in tmp_path
    staging_dir = tmp_path / "staging"
    dist_dir = tmp_path / "dist"
    monkeypatch.setattr(build_module, "_STAGING_DIR", staging_dir)
    monkeypatch.setattr(build_module, "_DIST_DIR", dist_dir)

    return staging_dir, dist_dir


# ========== P0-1: tracked-only file inventory ==========

def test_untracked_files_excluded(tmp_path: Path) -> None:
    """P0-1: _git_ls_files returns only tracked files.

    Untracked files in the working directory must never enter the release
    file inventory. The new engine's _git_ls_files() (used by
    compute_file_entries) must return only git-tracked files.

    This is the foundational P0 guarantee: accidental sensitive files in
    the working dir don't leak into release artifacts.
    """
    from tools.release.engine.manifest import _git_ls_files

    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "tracked_file.py", "print('tracked')\n")

    # Create an untracked sensitive file in the working directory
    (tmp_path / "secret_untracked.txt").write_text("SECRET_API_KEY=abc123", encoding="utf-8")

    # _git_ls_files must return only tracked files
    files = _git_ls_files(tmp_path)
    assert "tracked_file.py" in files
    assert "secret_untracked.txt" not in files, (
        "P0-1 violated: untracked file leaked into file inventory. "
        "_git_ls_files() must return only git-tracked files."
    )


def test_compute_file_entries_uses_tracked_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: compute_file_entries only includes tracked files in file_entries.

    Even if a component's exports glob would match an untracked file,
    compute_file_entries must exclude it because it intersects with
    _git_ls_files output (tracked-only).
    """
    from server.component_manifest import Manifest, ReleaseEntry
    from tools.release.engine import manifest as manifest_module
    from tools.release.engine.manifest import compute_file_entries

    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "workspace/test_module/main.py", "print('tracked')\n")

    # Create an untracked file that matches the glob but is NOT tracked
    (tmp_path / "workspace" / "test_module" / "secret_untracked.py").write_text(
        "SECRET = 'leaked'\n", encoding="utf-8",
    )

    # Mock _PROJECT_ROOT to tmp_path
    monkeypatch.setattr(manifest_module, "_PROJECT_ROOT", tmp_path)

    # Construct a minimal Manifest with new shape
    release_entry = ReleaseEntry(
        exports_runtime=["workspace/test_module/**"],
        exports_source=["workspace/test_module/**"],
        contains_personal_data=False,
        requires_external_credentials=False,
        license_class="internal-review",
    )
    manifest = Manifest(
        name="test_module",
        version="0.1.0",
        description="test",
        enabled=True,
        workspace_dir=tmp_path / "workspace" / "test_module",
        release=release_entry,
    )
    components = {"test_module": manifest}

    entries = compute_file_entries(components, "source", "abc123")

    rel_paths = {e.rel_path for e in entries}
    assert "workspace/test_module/main.py" in rel_paths
    assert "workspace/test_module/secret_untracked.py" not in rel_paths, (
        "P0-1 violated: untracked file leaked into compute_file_entries output. "
        "compute_file_entries must intersect globs with _git_ls_files (tracked-only)."
    )
    # All entries must have source="tracked"
    for e in entries:
        assert e.source == "tracked", f"entry {e.rel_path} source must be 'tracked'"


# ========== P0-2: manifest fail-closed ==========

def test_manifest_corruption_fails_closed(tmp_path: Path) -> None:
    """P0-2: manifest corruption must fail closed, not silently produce empty rules.

    Two scenarios:
    1. Manifest missing [component] section (valid TOML but wrong schema) → returns None
    2. Manifest with TOML syntax error (unparseable) → returns None

    _parse_manifest returns None on corruption (fail-closed: doesn't produce a
    Manifest with empty/default rules). The engine's load_components_for_audience
    then raises when a referenced component is missing, and validate_manifest_shape
    raises on old [release] shape. This test verifies the foundation: corrupt
    manifests never produce a Manifest object with empty rules.
    """
    from server.component_manifest import _parse_manifest

    # Scenario 1: manifest missing [component] section (valid TOML, wrong schema)
    workspace_dir = tmp_path / "workspace" / "bad_module"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "manifest.toml").write_text('foo = "bar"\n', encoding="utf-8")

    result = _parse_manifest(workspace_dir / "manifest.toml")
    assert result is None, (
        "P0-2 violated: corrupt manifest (missing [component]) must return None, "
        "not a Manifest with empty rules"
    )

    # Scenario 2: manifest with TOML syntax error (unparseable)
    import shutil
    shutil.rmtree(workspace_dir)
    workspace_dir.mkdir()
    (workspace_dir / "manifest.toml").write_text(
        'this is = not = valid toml\n', encoding="utf-8",
    )

    result = _parse_manifest(workspace_dir / "manifest.toml")
    assert result is None, (
        "P0-2 violated: unparseable manifest must return None, "
        "not a Manifest with empty rules"
    )


def test_validate_manifest_shape_rejects_old_shape(tmp_path: Path) -> None:
    """P0-2: validate_manifest_shape must reject old [release] shape (fail closed).

    The new engine uses big bang manifest migration (spec Q5). Any manifest
    using the deprecated [release] section (with distribution/include_scripts/
    exclude_personal_data) must raise, not silently degrade to empty rules.
    """
    from tools.release.engine.manifest import validate_manifest_shape

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
        validate_manifest_shape(manifest_path)


# ========== P0-3: digest-bound approval ==========

def test_approval_digest_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-3: approval digest mismatch must reject build with 'plan_digest mismatch'.

    The approval's plan_digest must match the plan's plan_digest.
    Any mismatch (wrong digest, missing approval, empty digest) must reject
    the build. Correct digest must pass.

    Scenarios tested:
    1. Wrong plan_digest → raises ValueError("plan_digest mismatch")
    2. Correct plan_digest → no raise (approval valid)
    """
    from tools.release.engine.build import build_release

    plan = _make_plan(plan_digest="a" * 64)

    # Scenario 1: Wrong plan_digest → raise
    wrong_approval = _make_approval(plan_digest="b" * 64)
    with pytest.raises(ValueError, match="plan_digest mismatch"):
        build_release(plan, wrong_approval, skip_heavy_gates=True)

    # Scenario 2: Correct plan_digest → proceeds past digest check
    # (will fail later on source_commit check, but that's past digest binding)
    correct_approval = _make_approval(plan_digest="a" * 64)
    _setup_build_mocks(monkeypatch, tmp_path, plan)
    # Should not raise "plan_digest mismatch" — it should succeed fully
    artifact = build_release(plan, correct_approval, skip_heavy_gates=True)
    assert artifact is not None
    assert artifact.archive_verification_passed


# ========== P0-4: gates before sealing ==========

def test_gates_run_before_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-4: failing build-time gates must prevent ZIP from reaching dist/.

    When a build-time gate (e.g., no-key-startup) fails, build_release must
    raise ValueError and NOT move any ZIP to dist/. The staging directory
    may contain a .failed.zip (if ZIP was generated before the gate check),
    but dist/ must be empty.
    """
    from tools.release.engine.build import build_release

    plan = _make_plan(
        file_entries=(_make_file_entry("main.py", b"print('hello')\n"),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)

    # Mock a failing heavy gate
    failing_gate = GateResult(
        name="no-key-startup",
        passed=False,
        details="mocked gate failure for P0-4 test",
    )
    _setup_build_mocks(
        monkeypatch, tmp_path, plan,
        heavy_gate_results={"no-key-startup": failing_gate},
    )

    # build_release must raise due to gate failure
    with pytest.raises(ValueError, match="build-time gate.*failed"):
        build_release(plan, approval, skip_heavy_gates=False)

    # KEY ASSERTION: dist/ must NOT contain any .zip file
    dist_dir = tmp_path / "dist"
    if dist_dir.exists():
        dist_zips = list(dist_dir.glob("*.zip"))
        assert dist_zips == [], (
            f"P0-4 violated: ZIP reached dist/ despite gate failure. "
            f"Found in dist/: {[z.name for z in dist_zips]}. "
            f"Gates must prevent ZIP from reaching dist/."
        )


# 注：原 test_gate_failure_preserves_failed_zip_in_staging（P0-4 版）与
# test_failed_artifact_stays_in_staging（P0-7 版）同场景，P0-7 断言更强，保留后者。

# ========== P0-5: generated files in manifest ==========

def test_generated_files_in_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-5: generated files (DEPLOYMENT.md / RELEASE_NOTES.md) must be in MANIFEST.json.

    After build_release completes, the ZIP's MANIFEST.json must contain
    sha256 + size for each generated file (DEPLOYMENT.md, RELEASE_NOTES.md).

    MANIFEST.json itself is NOT in its own file list (self-reference paradox).
    Instead, its sha256 is written to a sidecar file MANIFEST.json.sha256.
    """
    from tools.release.engine.build import build_release

    plan = _make_plan(
        file_entries=(_make_file_entry("main.py", b"print('hello')\n"),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)
    _setup_build_mocks(monkeypatch, tmp_path, plan)

    artifact = build_release(plan, approval, skip_heavy_gates=True)

    # Read MANIFEST.json from the ZIP
    with zipfile.ZipFile(artifact.zip_path, "r") as zf:
        manifest_data = json.loads(zf.read("MANIFEST.json"))
        file_paths = [f["path"] for f in manifest_data["files"]]

    # 1. MANIFEST.json must contain DEPLOYMENT.md / RELEASE_NOTES.md entries
    assert "DEPLOYMENT.md" in file_paths, "MANIFEST.json must contain DEPLOYMENT.md"
    assert "RELEASE_NOTES.md" in file_paths, "MANIFEST.json must contain RELEASE_NOTES.md"

    # 2. Each generated file entry must have sha256 + size
    for entry in manifest_data["files"]:
        if entry["path"] in ("DEPLOYMENT.md", "RELEASE_NOTES.md"):
            assert "sha256" in entry, f"{entry['path']} entry must have sha256"
            assert "size" in entry, f"{entry['path']} entry must have size"
            assert len(entry["sha256"]) == 64, (
                f"{entry['path']} sha256 must be 64 chars (SHA-256 hex), "
                f"got {len(entry['sha256'])}"
            )
            assert entry["size"] > 0, f"{entry['path']} size must be > 0"
            assert entry["source"] == "generated", (
                f"{entry['path']} source must be 'generated'"
            )

    # 3. MANIFEST.json must NOT contain itself in its file list (self-reference paradox)
    assert "MANIFEST.json" not in file_paths, (
        "MANIFEST.json must NOT contain itself in its file list "
        "(self-reference paradox — its sha256 is in the sidecar instead)"
    )

    # 4. MANIFEST.json.sha256 sidecar must be in the ZIP
    with zipfile.ZipFile(artifact.zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "MANIFEST.json.sha256" in names, "MANIFEST.json.sha256 sidecar must be in ZIP"

        # 5. Sidecar must contain MANIFEST.json's actual sha256
        sidecar_content = zf.read("MANIFEST.json.sha256").decode("utf-8").strip()
        actual_manifest_sha = hashlib.sha256(zf.read("MANIFEST.json")).hexdigest()
        assert actual_manifest_sha in sidecar_content, (
            f"MANIFEST.json.sha256 sidecar must contain MANIFEST.json's actual sha256. "
            f"Sidecar: {sidecar_content}, actual: {actual_manifest_sha}"
        )


# ========== P0-6: archive verification ==========

def test_archive_verification_passes_on_consistent_zip(tmp_path: Path) -> None:
    """P0-6: _verify_archive must pass when ZIP content matches manifest.

    Constructs a ZIP from a staging directory whose MANIFEST.json's file list
    matches the actual files, then calls _verify_archive() — must not raise.
    This is the happy path: every file in the manifest has matching sha256
    in the ZIP, no unexpected files, sidecar matches MANIFEST.json.
    """
    from tools.release.engine.build import _verify_archive

    staging = tmp_path / "staging"
    staging.mkdir()

    # Source file with known content
    main_bytes = b"print('hello')\n"
    (staging / "main.py").write_bytes(main_bytes)
    main_sha = hashlib.sha256(main_bytes).hexdigest()

    # Generated file with known content
    deploy_bytes = b"# Deployment\n\nNo zip_size field.\n"
    (staging / "DEPLOYMENT.md").write_bytes(deploy_bytes)
    deploy_sha = hashlib.sha256(deploy_bytes).hexdigest()

    # Build manifest (files list — does NOT include MANIFEST.json itself)
    manifest = [
        {"path": "main.py", "size": len(main_bytes), "sha256": main_sha, "source": "tracked"},
        {"path": "DEPLOYMENT.md", "size": len(deploy_bytes), "sha256": deploy_sha, "source": "generated"},
    ]

    # Write MANIFEST.json + sidecar
    manifest_data = {
        "profile_id": "test",
        "files": sorted(manifest, key=lambda x: x["path"]),
    }
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_bytes(
        json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (staging / "MANIFEST.json.sha256").write_text(
        f"{manifest_sha}  MANIFEST.json\n", encoding="utf-8",
    )

    # Build ZIP from staging
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in staging.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(staging).as_posix())

    # _verify_archive must NOT raise (happy path)
    _verify_archive(zip_path, manifest)


def test_archive_verification_catches_sha256_mismatch(tmp_path: Path) -> None:
    """P0-6: _verify_archive must catch sha256 mismatch between ZIP and manifest.

    Constructs a ZIP where main.py's content differs from what the manifest
    claims. _verify_archive must raise ValueError with "sha256 mismatch".
    """
    from tools.release.engine.build import _verify_archive

    staging = tmp_path / "staging"
    staging.mkdir()

    actual_main_bytes = b"print('actual content')\n"
    (staging / "main.py").write_bytes(actual_main_bytes)

    fake_sha = "0" * 64
    manifest = [
        {"path": "main.py", "size": len(actual_main_bytes), "sha256": fake_sha},
    ]

    manifest_data = {"profile_id": "test", "files": manifest}
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_bytes(
        json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (staging / "MANIFEST.json.sha256").write_text(
        f"{manifest_sha}  MANIFEST.json\n", encoding="utf-8",
    )

    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in staging.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(staging).as_posix())

    with pytest.raises(ValueError, match="sha256 mismatch"):
        _verify_archive(zip_path, manifest)


def test_archive_verification_catches_missing_file_in_zip(tmp_path: Path) -> None:
    """P0-6: _verify_archive must catch expected file missing from ZIP.

    Manifest claims a file exists, but it's not in the ZIP. Must raise
    ValueError mentioning "missing from ZIP".
    """
    from tools.release.engine.build import _verify_archive

    staging = tmp_path / "staging"
    staging.mkdir()

    main_bytes = b"print('hello')\n"
    (staging / "main.py").write_bytes(main_bytes)
    main_sha = hashlib.sha256(main_bytes).hexdigest()

    other_bytes = b"# other file\n"
    other_sha = hashlib.sha256(other_bytes).hexdigest()

    manifest = [
        {"path": "main.py", "size": len(main_bytes), "sha256": main_sha},
        {"path": "OTHER.md", "size": len(other_bytes), "sha256": other_sha},
    ]

    manifest_data = {"profile_id": "test", "files": manifest}
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_bytes(
        json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (staging / "MANIFEST.json.sha256").write_text(
        f"{manifest_sha}  MANIFEST.json\n", encoding="utf-8",
    )

    # Build ZIP but EXCLUDE OTHER.md
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in staging.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(staging).as_posix())

    with pytest.raises(ValueError, match="missing from ZIP"):
        _verify_archive(zip_path, manifest)


def test_archive_verification_catches_sidecar_mismatch(tmp_path: Path) -> None:
    """P0-6: _verify_archive must catch MANIFEST.json.sha256 sidecar mismatch.

    Sidecar claims a sha256 that doesn't match MANIFEST.json's actual sha256.
    Must raise ValueError mentioning sidecar or MANIFEST.json.
    """
    from tools.release.engine.build import _verify_archive

    staging = tmp_path / "staging"
    staging.mkdir()

    main_bytes = b"print('hello')\n"
    (staging / "main.py").write_bytes(main_bytes)
    main_sha = hashlib.sha256(main_bytes).hexdigest()

    manifest = [{"path": "main.py", "size": len(main_bytes), "sha256": main_sha}]
    manifest_data = {"profile_id": "test", "files": manifest}
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_bytes(
        json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8"),
    )

    # Write a WRONG sidecar
    fake_manifest_sha = "0" * 64
    (staging / "MANIFEST.json.sha256").write_text(
        f"{fake_manifest_sha}  MANIFEST.json\n", encoding="utf-8",
    )

    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in staging.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(staging).as_posix())

    with pytest.raises(ValueError, match="sidecar|MANIFEST.json"):
        _verify_archive(zip_path, manifest)


# ========== P0-7: failed artifact stays in staging ==========

def test_failed_artifact_stays_in_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-7: failed archive verification must keep ZIP in staging as .failed.zip.

    When archive verification fails (after ZIP is generated), the ZIP must be
    renamed to <name>.failed.zip and stay in staging. dist/ must NEVER contain
    the failed artifact.
    """
    from tools.release.engine.build import build_release

    plan = _make_plan(
        file_entries=(_make_file_entry("main.py", b"print('hello')\n"),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)

    staging_dir, dist_dir = _setup_build_mocks(monkeypatch, tmp_path, plan)

    # Mock archive-verification to fail
    from tools.release.engine import build as build_module
    monkeypatch.setattr(
        build_module, "_gate_archive_verification",
        lambda zip_path, manifest: GateResult(
            name="archive-verification",
            passed=False,
            details="mocked failure for P0-7 test",
        ),
    )

    with pytest.raises(ValueError, match="build-time gate.*failed"):
        build_release(plan, approval, skip_heavy_gates=False)

    # KEY ASSERTION 1: dist/ must NOT contain any .zip file
    if dist_dir.exists():
        dist_zips = list(dist_dir.glob("*.zip"))
        assert dist_zips == [], (
            f"P0-7 violated: failed ZIP reached dist/. "
            f"Found in dist/: {[z.name for z in dist_zips]}. "
            f"Failed artifact must stay in staging as .failed.zip."
        )

    # KEY ASSERTION 2: staging must contain a .failed.zip file
    failed_zips = list(staging_dir.rglob("*.failed.zip"))
    assert len(failed_zips) >= 1, (
        f"P0-7 violated: no .failed.zip file in staging. "
        f"Found in staging: {[p.name for p in staging_dir.rglob('*')]}. "
        f"Failed ZIP must be renamed to .failed.zip and preserved for debugging."
    )

    # KEY ASSERTION 3: staging must NOT contain a "valid" .zip (only .failed.zip)
    valid_zips = [
        z for z in staging_dir.rglob("*.zip")
        if not z.name.endswith(".failed.zip")
    ]
    assert valid_zips == [], (
        f"P0-7 violated: staging contains a valid .zip (not renamed to .failed.zip). "
        f"Found: {[z.name for z in valid_zips]}."
    )


def test_successful_release_moves_to_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-7: successful release must atomically move ZIP to dist/.

    When all gates + archive verification pass, the ZIP must be atomically
    moved to dist/ (via os.replace). staging/ should NOT retain the .zip
    (it was moved, not copied).

    Asserts:
    1. No exception (successful build)
    2. dist/ contains the .zip file
    3. dist/ also contains the .zip.sha256 checksum file
    4. staging/ does NOT retain a valid .zip (only .failed.zip if any, but none expected)
    """
    from tools.release.engine.build import build_release

    plan = _make_plan(
        file_entries=(_make_file_entry("main.py", b"print('hello')\n"),),
    )
    approval = _make_approval(plan_digest=plan.plan_digest)
    staging_dir, dist_dir = _setup_build_mocks(monkeypatch, tmp_path, plan)

    artifact = build_release(plan, approval, skip_heavy_gates=True)

    # 1. Artifact returned successfully
    assert artifact is not None, "build_release must return Artifact on success"
    assert artifact.archive_verification_passed

    # 2. dist/ must contain the .zip file
    assert dist_dir.exists(), "dist/ directory must exist after successful build"
    dist_zips = list(dist_dir.glob("*.zip"))
    assert len(dist_zips) >= 1, (
        f"P0-7 violated: successful build did not move ZIP to dist/. "
        f"Found in dist/: {[p.name for p in dist_dir.glob('*')]}."
    )

    # 3. dist/ must also contain the .zip.sha256 checksum file
    dist_sha256 = list(dist_dir.glob("*.zip.sha256"))
    assert len(dist_sha256) >= 1, (
        f"P0-7 violated: SHA-256 checksum not moved to dist/. "
        f"Found in dist/: {[p.name for p in dist_dir.glob('*')]}."
    )

    # 4. staging/ should NOT retain a valid .zip (it was moved, not copied)
    valid_zips = [
        z for z in staging_dir.rglob("*.zip")
        if not z.name.endswith(".failed.zip")
    ]
    assert valid_zips == [], (
        f"P0-7 violated: staging retains valid .zip (should have been moved to dist/). "
        f"Found: {[z.name for z in valid_zips]}."
    )
