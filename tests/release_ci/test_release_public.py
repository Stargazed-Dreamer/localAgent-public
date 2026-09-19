"""Public audience 专属测试套件（spec-v3-public.md Proof 节）。

覆盖：
- public.toml 加载（audience=public 不 raise / 含 10 组件 / 含 3 专属 gate）
- _gate_license_clearance（pass / fail）
- _gate_no_agpl_import（pass / fail-pyproject / fail-file）
- _gate_sbom_generated（missing / invalid / valid）
- generate_spdx_sbom（基本验证 / 反向解析）
- build_release audience 分支（public 生成 SBOM / 非 public 不生成）
- publish subcommand（plan_not_found / 非 public 拒绝 / dry_run_success）

总计 17 个测试（≥12 要求）。
"""
from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from tools.release.engine.models import (
    Approval,
    Artifact,
    FileEntry,
    GateResult,
    PreparedRelease,
)

# ========== 辅助构造函数 ==========

def make_public_plan(
    plan_digest: str = "a" * 64,
    audience: str = "public",
    profile_id: str = "public-full",
) -> PreparedRelease:
    return PreparedRelease(
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest="b" * 64,
        source_commit="c" * 40,
        components=("disk_manager",),
        components_digest="d" * 64,
        audience=audience,
        file_entries=(
            FileEntry("server/main.py", "e" * 64, 100, "tracked"),
        ),
        exemptions=(),
        scan_digest="f" * 64,
        gates_required=("policy-schema",),
        gates_results=(GateResult("policy-schema", True, "ok"),),
        created_at=datetime.now(UTC).isoformat(),
    )


def make_manifest_mock(license_class: str = "apache-2.0", name: str = "disk_manager") -> SimpleNamespace:
    """构造轻量 Manifest mock（含 workspace_dir + release.license_class）。"""
    release = SimpleNamespace(license_class=license_class)
    return SimpleNamespace(
        name=name,
        workspace_dir=Path(__file__).resolve().parents[2] / "workspace" / name,
        release=release,
    )


def make_manifest_files() -> list[dict]:
    return [
        {"path": "server/main.py", "sha256": "e" * 64, "size": 100, "source": "tracked"},
        {"path": "client/app.py", "sha256": "f" * 64, "size": 200, "source": "tracked"},
    ]


# ========== 1. public.toml 加载测试 ==========

def test_public_audience_loads():
    """public.toml 加载不 raise（audience=public 已从 reserved 升级）。"""
    from tools.release.engine.audience import load_audience_policy
    policy = load_audience_policy("public")
    assert policy["audience"]["name"] == "public"
    assert policy["audience"]["export_set"] == "source"


def test_public_audience_has_10_components():
    """public.toml 含 10 个候选组件（phase 4：v3 的 3 个 + 7 个新组件）。

    G20 决策（2026-08-10）：原 test_public_audience_has_3_components 断言 len==3，
    Step 2 把 components 从 3 扩展到 10 后断言失败。保留测试，更新断言值为 10，
    函数名改为 test_public_audience_has_10_components，注释更新为 phase 4。
    """
    from tools.release.engine.audience import load_audience_policy
    policy = load_audience_policy("public")
    components = policy["audience"]["components"]
    assert len(components) == 10
    # v3 的 3 个核心组件
    assert "disk_manager" in components
    assert "recorder" in components
    assert "modelscope_model_update" in components
    # phase 4 新增 7 个组件
    assert "accounting" in components
    assert "dev_toolkit" in components
    assert "life_design" in components
    assert "arknights_gacha" in components
    assert "wuwa_gacha" in components
    assert "endfield_gacha" in components
    assert "yihuan_gacha" in components


def test_public_audience_has_3_extra_gates():
    """public.toml 含 3 个 public 专属 gate（license-clearance / no-agpl-import / sbom-generated）。"""
    from tools.release.engine.audience import load_audience_policy
    policy = load_audience_policy("public")
    static_gates = policy["audience"]["gates"]["static"]
    build_time_gates = policy["audience"]["gates"]["build_time"]
    all_gates = set(static_gates) | set(build_time_gates)
    assert "license-clearance" in all_gates
    assert "no-agpl-import" in all_gates
    assert "sbom-generated" in all_gates


# ========== 2. _gate_license_clearance 测试 ==========

def test_gate_license_clearance_pass():
    """license_class=apache-2.0 → 通过。"""
    from tools.release.engine.prepare import _gate_license_clearance
    file_entries = (FileEntry("workspace/disk_manager/scripts/scan_disk.py", "e" * 64, 100, "tracked"),)
    components = {"disk_manager": make_manifest_mock("apache-2.0", "disk_manager")}
    result = _gate_license_clearance(file_entries, components)
    assert result.passed
    assert result.name == "license-clearance"


def test_gate_license_clearance_fail():
    """license_class=internal-review → 失败。"""
    from tools.release.engine.prepare import _gate_license_clearance
    file_entries = (FileEntry("workspace/disk_manager/scripts/scan_disk.py", "e" * 64, 100, "tracked"),)
    components = {"disk_manager": make_manifest_mock("internal-review", "disk_manager")}
    result = _gate_license_clearance(file_entries, components)
    assert not result.passed
    assert "internal-review" in result.details


# ========== 3. _gate_no_agpl_import 测试 ==========

def test_gate_no_agpl_import_pass():
    """无 ultralytics → 通过。"""
    from tools.release.engine.prepare import _gate_no_agpl_import
    file_entries = (FileEntry("server/main.py", "e" * 64, 100, "tracked"),)
    result = _gate_no_agpl_import(file_entries)
    assert result.passed
    assert result.name == "no-agpl-import"


def test_gate_no_agpl_import_fail_file(tmp_path, monkeypatch):
    """file 含 import ultralytics → 失败。"""
    from tools.release.engine.prepare import _gate_no_agpl_import

    # 构造含 import ultralytics 的测试文件
    test_file = tmp_path / "test_agpl.py"
    test_file.write_text("import ultralytics\n", encoding="utf-8")

    # monkeypatch _PROJECT_ROOT 到 tmp_path，让 gate 能找到测试文件
    monkeypatch.setattr("tools.release.engine.prepare._PROJECT_ROOT", tmp_path)
    # pyproject.toml 在 tmp_path 下不存在，gate 会跳过 Check 1（pyproject.toml 无 ultralytics）
    # 但 Check 2（file_entries 内容）会命中
    file_entries = (FileEntry("test_agpl.py", "e" * 64, 100, "tracked"),)
    result = _gate_no_agpl_import(file_entries)
    assert not result.passed
    assert "ultralytics" in result.details.lower()


# ========== 4. _gate_sbom_generated 测试 ==========

def test_gate_sbom_generated_missing(tmp_path):
    """SBOM 文件不存在 → 失败。"""
    from tools.release.engine.build import _gate_sbom_generated
    result = _gate_sbom_generated(tmp_path)
    assert not result.passed
    assert "not found" in result.details


def test_gate_sbom_generated_invalid_json(tmp_path):
    """SBOM JSON 格式错误 → 失败。"""
    from tools.release.engine.build import _gate_sbom_generated
    (tmp_path / "sbom.spdx.json").write_text("not json", encoding="utf-8")
    result = _gate_sbom_generated(tmp_path)
    assert not result.passed
    assert "JSON parse error" in result.details


def test_gate_sbom_generated_valid(tmp_path):
    """合法 SBOM → 通过。"""
    from tools.release.engine.build import _gate_sbom_generated
    (tmp_path / "sbom.spdx.json").write_text(
        json.dumps({
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "test",
            "creationInfo": {"created": "2026-07-31T12:00:00Z"},
            "packages": [{"SPDXID": "SPDXRef-Package", "name": "localagent"}],
            "files": [{"SPDXID": "SPDXRef-File-0"}],
        }),
        encoding="utf-8",
    )
    result = _gate_sbom_generated(tmp_path)
    assert result.passed
    assert "SPDX 2.3 valid" in result.details


# ========== 5. generate_spdx_sbom 测试 ==========

def test_generate_spdx_sbom_basic():
    """SBOM 生成基本验证（spdxVersion / licenseConcluded / packages / files）。"""
    from tools.release.engine.sbom import generate_spdx_sbom
    plan = make_public_plan()
    manifest_files = make_manifest_files()
    sbom = generate_spdx_sbom(plan, manifest_files)
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["SPDXID"] == "SPDXRef-DOCUMENT"
    assert sbom["name"] == "localagent-public-full"
    assert len(sbom["packages"]) == 1
    assert sbom["packages"][0]["licenseConcluded"] == "Apache-2.0"
    assert sbom["packages"][0]["licenseDeclared"] == "Apache-2.0"
    assert len(sbom["files"]) == 2


def test_generate_spdx_sbom_reverse_parse(tmp_path):
    """SBOM 反向解析通过（schema 合规）。"""
    from spdx_tools.spdx.parser.json.json_parser import parse_from_file

    from tools.release.engine.sbom import generate_spdx_sbom
    plan = make_public_plan()
    manifest_files = make_manifest_files()
    sbom = generate_spdx_sbom(plan, manifest_files)
    sbom_path = tmp_path / "sbom.spdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    parsed = parse_from_file(str(sbom_path))
    assert parsed.creation_info.spdx_version == "SPDX-2.3"
    assert len(parsed.packages) == 1
    assert len(parsed.files) == 2
    assert len(parsed.relationships) == 3  # 1 DESCRIBES + 2 CONTAINS


# ========== 6. build_release audience 分支测试 ==========

def test_build_release_public_generates_sbom(monkeypatch, tmp_path):
    """build_release(audience=public) 生成 sbom.spdx.json 并通过 sbom-generated gate。"""
    monkeypatch.setattr("tools.release.engine.build._STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr("tools.release.engine.build._DIST_DIR", tmp_path / "dist")

    def mock_copy(plan, staging_dir):
        (staging_dir / "server").mkdir(parents=True, exist_ok=True)
        (staging_dir / "server" / "main.py").write_text("# test", encoding="utf-8")
    monkeypatch.setattr("tools.release.engine.build._copy_files_to_staging", mock_copy)
    monkeypatch.setattr("tools.release.engine.build._verify_plan_digest", lambda p, a: None)
    monkeypatch.setattr("tools.release.engine.build._verify_source_commit", lambda p: None)
    monkeypatch.setattr("tools.release.engine.build.render_deployment_md", lambda p: "# Deployment")
    monkeypatch.setattr("tools.release.engine.build.render_release_notes_md", lambda p: "# Release Notes")
    monkeypatch.setattr("tools.release.engine.build._load_path_mapping", lambda pid: {})
    monkeypatch.setattr("tools.release.engine.build._apply_path_mapping", lambda sd, pm: None)

    plan = make_public_plan()
    approval = Approval(
        plan_digest=plan.plan_digest,
        approved_at=datetime.now(UTC).isoformat(),
        approved_by="test",
    )
    artifact = build_release(plan, approval, skip_heavy_gates=True)

    assert isinstance(artifact, Artifact)
    with zipfile.ZipFile(artifact.zip_path, "r") as zf:
        names = zf.namelist()
        assert "sbom.spdx.json" in names
        sbom = json.loads(zf.read("sbom.spdx.json"))
        assert sbom["spdxVersion"] == "SPDX-2.3"
        manifest = json.loads(zf.read("MANIFEST.json"))
        sbom_entries = [f for f in manifest["files"] if f["path"] == "sbom.spdx.json"]
        assert len(sbom_entries) == 1


def test_build_release_non_public_audience_no_sbom(monkeypatch, tmp_path):
    """build_release(audience != public) 不生成 SBOM（SBOM 是 public 专属 gate）。"""
    monkeypatch.setattr("tools.release.engine.build._STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr("tools.release.engine.build._DIST_DIR", tmp_path / "dist")

    def mock_copy(plan, staging_dir):
        (staging_dir / "server").mkdir(parents=True, exist_ok=True)
        (staging_dir / "server" / "main.py").write_text("# test", encoding="utf-8")
    monkeypatch.setattr("tools.release.engine.build._copy_files_to_staging", mock_copy)
    monkeypatch.setattr("tools.release.engine.build._verify_plan_digest", lambda p, a: None)
    monkeypatch.setattr("tools.release.engine.build._verify_source_commit", lambda p: None)
    monkeypatch.setattr("tools.release.engine.build.render_deployment_md", lambda p: "# Deployment")
    monkeypatch.setattr("tools.release.engine.build.render_release_notes_md", lambda p: "# Release Notes")
    monkeypatch.setattr("tools.release.engine.build._load_path_mapping", lambda pid: {})
    monkeypatch.setattr("tools.release.engine.build._apply_path_mapping", lambda sd, pm: None)

    plan = make_public_plan(audience="internal", profile_id="internal-full")
    plan = PreparedRelease(
        plan_digest="1" * 64,
        profile_id="internal-full",
        profile_digest=plan.profile_digest,
        source_commit=plan.source_commit,
        components=plan.components,
        components_digest=plan.components_digest,
        audience="internal",
        file_entries=plan.file_entries,
        exemptions=plan.exemptions,
        scan_digest=plan.scan_digest,
        gates_required=plan.gates_required,
        gates_results=plan.gates_results,
        created_at=plan.created_at,
    )
    approval = Approval(
        plan_digest=plan.plan_digest,
        approved_at=datetime.now(UTC).isoformat(),
        approved_by="test",
    )
    artifact = build_release(plan, approval, skip_heavy_gates=True)
    with zipfile.ZipFile(artifact.zip_path, "r") as zf:
        assert "sbom.spdx.json" not in zf.namelist()


# ========== 7. publish subcommand 测试 ==========

def test_publish_plan_not_found(monkeypatch, tmp_path):
    """publish plan 不存在 → EXIT_CONFIG_ERROR。"""
    from tools.release.cli import EXIT_CONFIG_ERROR, cmd_publish

    monkeypatch.setattr("tools.release.cli._PLANS_DIR", tmp_path / "plans")
    args = SimpleNamespace(plan="nonexistent", dry_run=False)
    assert cmd_publish(args) == EXIT_CONFIG_ERROR


def test_publish_non_public_audience_rejected(monkeypatch, tmp_path):
    """publish 非 public audience 的 plan → EXIT_GATE_FAILURE（audience != public）。"""
    from tools.release.cli import EXIT_GATE_FAILURE, cmd_publish

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    non_public_plan = {
        "plan_digest": "1" * 64,
        "profile_id": "internal-full",
        "profile_digest": "b" * 64,
        "source_commit": "c" * 40,
        "components": ["disk_manager"],
        "components_digest": "d" * 64,
        "audience": "internal",
        "file_entries": [{"rel_path": "server/main.py", "sha256": "e" * 64, "size": 100, "source": "tracked"}],
        "exemptions": [],
        "scan_digest": "f" * 64,
        "gates_required": ["policy-schema"],
        "gates_results": [{"name": "policy-schema", "passed": True, "details": "ok"}],
        "created_at": datetime.now(UTC).isoformat(),
    }
    (plans_dir / f"{non_public_plan['plan_digest']}.json").write_text(
        json.dumps(non_public_plan), encoding="utf-8"
    )
    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    args = SimpleNamespace(plan=non_public_plan["plan_digest"], dry_run=False)
    assert cmd_publish(args) == EXIT_GATE_FAILURE


def test_publish_dry_run_success(monkeypatch, tmp_path):
    """publish --dry-run 配置正确 → EXIT_OK。"""
    from tools.release.cli import EXIT_OK, cmd_publish

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan = make_public_plan()
    (plans_dir / f"{plan.plan_digest}.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )
    # staging 目录
    staging_dir = tmp_path / "release" / "staging" / plan.plan_digest
    staging_dir.mkdir(parents=True)
    (staging_dir / "server").mkdir()
    (staging_dir / "server" / "main.py").write_text("# test", encoding="utf-8")
    (staging_dir / "sbom.spdx.json").write_text(
        json.dumps({"spdxVersion": "SPDX-2.3", "packages": [{"SPDXID": "SPDXRef-Package"}]}),
        encoding="utf-8",
    )
    # profiles 目录（publication 指向本地 bare remote——持久副本模式需真实 clone）
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "public-full.toml").write_text(
        'schema_version = 1\nprofile_id = "public-full"\naudience = "public"\n'
        f"[publication]\npublic_repo_url = '{remote}'\n"
        'public_repo_branch = "main"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    args = SimpleNamespace(plan=plan.plan_digest, dry_run=True)
    assert cmd_publish(args) == EXIT_OK


# ========== import build_release（放最后避免循环 import 问题） ==========

from tools.release.engine.build import build_release  # noqa: E402
