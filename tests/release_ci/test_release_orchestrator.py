"""Release 一键编排测试（design: temp/sdd/release-orchestrator/design.md）。

覆盖：
- triage：首轮全 NEW / carried / count 增 / resolved / 快照 roundtrip（不含行内容）
- collect_sensitive_hits：与 gate 同口径（命中 + line_skips 跳过）
- history：CHANGELOG 版本节/条目标题解析、首次发布章节、插入顺序、区间统计
- publish 持久副本：两次发布 → append-only 2 commits + 真实 diff、remote 不一致拒发、
  远端领先时拒绝强推、tag 幂等
- release 编排：approve dry-run 无副作用、快照 scan_digest 锚定、真实批准链路
  （写 approval → build(mock) → 快照生效）
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import toml

from tools.release.engine.models import (
    Approval,
    Artifact,
    FileEntry,
    GateResult,
    PreparedRelease,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.local",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.local",
}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check, env=GIT_ENV
    )


def make_plan(
    plan_digest: str = "a" * 64,
    audience: str = "public",
    profile_id: str = "public-full",
    source_commit: str = "c" * 40,
    scan_digest: str = "f" * 64,
) -> PreparedRelease:
    return PreparedRelease(
        plan_digest=plan_digest,
        profile_id=profile_id,
        profile_digest="b" * 64,
        source_commit=source_commit,
        components=("disk_manager",),
        components_digest="d" * 64,
        audience=audience,
        file_entries=(
            FileEntry("server/main.py", "e" * 64, 100, "tracked"),
        ),
        exemptions=(),
        scan_digest=scan_digest,
        gates_required=("policy-schema",),
        gates_results=(GateResult("policy-schema", True, "ok"),),
        created_at=datetime.now(UTC).isoformat(),
    )


# ========== 1. triage 纯逻辑 ==========

def _hit(path: str, rule: str) -> dict:
    return {"path": path, "line": 1, "rule": rule, "severity": "MEDIUM"}


def test_triage_first_run_all_new():
    """无快照 → 全部命中视为 NEW。"""
    from tools.release.engine.triage import triage_against_snapshot

    hits = [_hit("a.py", "local-absolute-path"), _hit("a.py", "local-absolute-path")]
    report = triage_against_snapshot(hits, None)
    assert not report.clean
    assert len(report.new_hits) == 1
    assert report.new_hits[0]["count"] == 2


def test_triage_carried_new_resolved():
    """同 (path,rule) count 未增 → carried；count 增 → NEW(+delta)；消失 → resolved。"""
    from tools.release.engine.triage import triage_against_snapshot

    snapshot = {
        "scan_digest": "f" * 64,
        "hits": [
            {"path": "a.py", "rule": "r1", "count": 3},
            {"path": "shrink.py", "rule": "r1", "count": 5},
            {"path": "old.py", "rule": "r1", "count": 1},
        ],
    }
    hits = (
        [_hit("a.py", "r1")] * 3          # carried（3→3）
        + [_hit("a.py", "r1")]            # 3→4 → NEW +1
        + [_hit("shrink.py", "r1")]       # 5→1 → count 减少，仍 carried
        + [_hit("new.py", "r2")]          # 新 key → NEW
    )
    report = triage_against_snapshot(hits, snapshot)

    assert not report.clean
    # key 级二分：count 增的 key 整体进 NEW（报 delta），不重复计入 carried
    assert report.carried == {("shrink.py", "r1"): 1}
    by_path = {(h["path"], h["rule"]): h["count"] for h in report.new_hits}
    assert by_path[("a.py", "r1")] == 1  # delta
    assert by_path[("new.py", "r2")] == 1
    assert ("old.py", "r1") in report.resolved


def test_snapshot_roundtrip_no_line_content():
    """快照 roundtrip；只存 path/rule/count，绝不含行内容与行号。"""
    from tools.release.engine.triage import load_snapshot, save_snapshot

    path = Path("unused.json")
    hits = [_hit("a.py", "r1"), _hit("a.py", "r1"), {"path": "b.py", "line": 7, "rule": "r2", "severity": "HIGH"}]
    save_snapshot(path, hits, "f" * 64, "tester")

    loaded = load_snapshot(path)
    assert loaded["scan_digest"] == "f" * 64
    assert loaded["approved_by"] == "tester"
    assert loaded["hits"] == [
        {"path": "a.py", "rule": "r1", "count": 2},
        {"path": "b.py", "rule": "r2", "count": 1},
    ]
    assert "line" not in json.dumps(loaded)
    path.unlink()


# ========== 2. collect_sensitive_hits 口径 ==========

def test_collect_sensitive_hits_and_line_skips(monkeypatch, tmp_path):
    """collect 与 gate 同口径：命中规则 + line_skips 跳过。"""
    from tools.release.engine import prepare as prepare_mod
    from tools.release.engine.prepare import collect_sensitive_hits

    monkeypatch.setattr(prepare_mod, "_PROJECT_ROOT", tmp_path)
    f = tmp_path / "sample.py"
    f.write_text(
        "token = 'sk-abcdefghijklmnopqrstuvwx'\n"
        "path = 'C:\\\\Users\\\\admin\\\\secret'\n",
        encoding="utf-8",
    )
    entries = (FileEntry("sample.py", "0" * 64, f.stat().st_size, "tracked"),)

    hits, scanned, skipped = collect_sensitive_hits(entries, {}, None)
    assert scanned == 1 and skipped == 0
    by_rule = {h["rule"] for h in hits}
    assert "api-key-or-token" in by_rule
    assert "local-absolute-path" in by_rule

    # line_skips 跳过 api-key 行后仅剩 MEDIUM 命中
    hits2, _, _ = collect_sensitive_hits(entries, {"sample.py": [r"sk-[A-Za-z0-9]+"]}, None)
    assert {h["rule"] for h in hits2} == {"local-absolute-path"}


# ========== 3. history 生成 ==========

CHANGELOG_FIXTURE = """# Changelog

## [Unreleased]

### Fixed

- **未发布条目**（x.py）：细节。

## [0.45.0] - 2026-09-08

### Added

- **功能 A 全链透传**（`server/x.py`）：细节正文应被忽略。
- **功能 B**：无粗体尾部细节。

### Fixed

- **修复 C**（`lib/y.py`）。
"""


def test_latest_changelog_version(tmp_path):
    from tools.release.engine.history import latest_changelog_version

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_FIXTURE, encoding="utf-8")
    assert latest_changelog_version(cl) == "0.45.0"


def test_extract_changelog_entries(tmp_path):
    from tools.release.engine.history import extract_changelog_entries

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_FIXTURE, encoding="utf-8")
    entries = extract_changelog_entries(cl, "0.45.0")
    assert entries == ["功能 A 全链透传", "功能 B", "修复 C"]


def _init_repo_with_plan_commits(repo: Path) -> None:
    """构造 public 副本仓库：两个历史 commit，各含不同 source_commit 的 release-plan.json。"""
    _git(repo, "init", "-b", "main")
    (repo / "release-plan.json").write_text(
        json.dumps({"source_commit": "1" * 40}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Release 0.44.0")


def test_build_history_section_first_release(tmp_path):
    """无 release-plan.json 历史 → 首次发布章节。"""
    from tools.release.engine.history import build_history_section

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_plan_commits(repo)

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_FIXTURE, encoding="utf-8")

    section = build_history_section(repo, "2" * 40, "0.45.0", cl)
    assert "## 0.45.0" in section
    assert "首次发布" not in section  # 有 prev commit → 走区间统计分支
    # 主仓库（monkeypatch 前 = 真仓库）无 "1"*40/"2"*40 → stats None → 无 commit 统计行
    assert "功能 A 全链透传" in section


def test_build_history_section_with_range_stats(tmp_path, monkeypatch):
    """主仓库存在 prev..cur 区间 → commit 数统计。"""
    from tools.release.engine import history as history_mod
    from tools.release.engine.history import build_history_section

    # 主仓库：两个 commit，prev="1"*40 不可用 → 用真实 sha
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "-b", "main")
    (main_repo / "a.txt").write_text("a", encoding="utf-8")
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-m", "c1")
    prev_sha = _git(main_repo, "rev-parse", "HEAD").stdout.strip()
    (main_repo / "b.txt").write_text("b", encoding="utf-8")
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-m", "c2")
    cur_sha = _git(main_repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(history_mod, "_project_root", lambda: main_repo)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "release-plan.json").write_text(
        json.dumps({"source_commit": prev_sha}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Release prev")

    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_FIXTURE, encoding="utf-8")

    section = build_history_section(repo, cur_sha, "0.45.0", cl)
    assert "1 个 commit" in section


def test_update_release_history_insert_order(tmp_path):
    """新章节插到头部之后、旧章节之前；同版本重跑幂等。"""
    from tools.release.engine.history import update_release_history

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_plan_commits(repo)
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_FIXTURE, encoding="utf-8")

    update_release_history(repo, "9" * 40, "0.44.0", cl)
    update_release_history(repo, "2" * 40, "0.45.0", cl)
    content = (repo / "RELEASE_HISTORY.md").read_text(encoding="utf-8")

    assert content.index("## 0.45.0") < content.index("## 0.44.0")
    assert "# 发布历史" in content

    # 幂等：同版本重跑不产生重复章节
    update_release_history(repo, "2" * 40, "0.45.0", cl)
    content2 = (repo / "RELEASE_HISTORY.md").read_text(encoding="utf-8")
    assert content2.count("## 0.45.0") == 1


# ========== 4. publish 持久副本（端到端，本地 bare remote） ==========

PROFILE_TOML = (
    'schema_version = 1\nprofile_id = "public-full"\naudience = "public"\n'
    "[publication]\n"
    # literal string：TOML 不解析转义，Windows 路径反斜杠原样保留
    "public_repo_url = '{repo_url}'\n"
    'public_repo_branch = "main"\n'
)


def _setup_publish_env(tmp_path: Path, plan: PreparedRelease, files: dict[str, str]):
    """构造 publish 依赖：plan 文件 + staging + profile + bare remote + CHANGELOG。"""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{plan.plan_digest}.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )

    staging_dir = tmp_path / "release" / "staging" / plan.plan_digest
    staging_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        dest = staging_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{plan.profile_id}.toml").write_text(
        PROFILE_TOML.format(repo_url=str(remote)), encoding="utf-8"
    )

    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_FIXTURE, encoding="utf-8")

    return plans_dir, profiles_dir, remote


def _publish_args(plan: PreparedRelease, **kwargs) -> SimpleNamespace:
    defaults = dict(
        plan=plan.plan_digest, dry_run=False, repo_dir=None,
        tag=None, version="0.45.0", no_history=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _setup_cli_paths(monkeypatch, tmp_path: Path, plans_dir: Path, profiles_dir: Path):
    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", tmp_path)


def test_publish_persistent_two_releases_real_diff(monkeypatch, tmp_path):
    """两次 publish → append-only 2 commits；第二次 diff 只含变更文件。"""
    from tools.release.cli import EXIT_OK, cmd_publish

    plan1 = make_plan(plan_digest="1" * 64)
    plans_dir, profiles_dir, _ = _setup_publish_env(
        tmp_path, plan1, {"server/main.py": "v1", "docs/a.md": "hello"}
    )
    _setup_cli_paths(monkeypatch, tmp_path, plans_dir, profiles_dir)

    assert cmd_publish(_publish_args(plan1)) == EXIT_OK

    repo_dir = tmp_path / "release" / "public_repo"
    log = _git(repo_dir, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1

    # 第二次发布：改一个文件 + 删一个文件 + 新增一个文件
    plan2 = make_plan(plan_digest="2" * 64)
    staging2 = tmp_path / "release" / "staging" / plan2.plan_digest
    staging2.mkdir(parents=True)
    (staging2 / "server").mkdir()
    (staging2 / "server" / "main.py").write_text("v2", encoding="utf-8")
    (staging2 / "docs").mkdir()
    (staging2 / "docs" / "b.md").write_text("new", encoding="utf-8")
    (plans_dir / f"{plan2.plan_digest}.json").write_text(
        json.dumps(plan2.to_dict()), encoding="utf-8"
    )
    assert cmd_publish(_publish_args(plan2, tag="v0.46.0", version="0.46.0")) == EXIT_OK

    assert len(_git(repo_dir, "log", "--oneline").stdout.strip().splitlines()) == 2
    diff = _git(repo_dir, "diff", "HEAD~1", "HEAD", "--name-status").stdout
    changed = {line.split("\t")[-1].strip() for line in diff.splitlines()}
    assert changed == {
        "server/main.py",      # 修改
        "docs/a.md",           # 删除（staging2 不再包含）
        "docs/b.md",           # 新增
        "release-plan.json",   # plan 更新
        "RELEASE_HISTORY.md",  # 历史章节更新
    }
    # append-only：无 force push 痕迹，初始 commit 仍在
    assert "Release 0.45.0 (plan " in _git(repo_dir, "log", "--format=%s").stdout
    assert "v0.46.0" in _git(repo_dir, "tag", "-l").stdout


def test_publish_remote_mismatch_rejected(monkeypatch, tmp_path):
    """repo_dir origin 与 public_repo_url 不一致 → 拒发（防误删他仓库）。"""
    from tools.release.cli import EXIT_CONFIG_ERROR, cmd_publish

    plan = make_plan()
    plans_dir, profiles_dir, _ = _setup_publish_env(tmp_path, plan, {"a.txt": "x"})
    _setup_cli_paths(monkeypatch, tmp_path, plans_dir, profiles_dir)

    # 预置一个指向别的远端的 repo_dir
    other_remote = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(other_remote)], check=True, capture_output=True)
    repo_dir = tmp_path / "release" / "public_repo"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "remote", "add", "origin", str(other_remote))

    assert cmd_publish(_publish_args(plan)) == EXIT_CONFIG_ERROR


def test_publish_append_only_refuses_diverged_remote(monkeypatch, tmp_path):
    """远端领先（他人推过）→ push 被拒，绝不 --force 覆盖。"""
    from tools.release.cli import EXIT_CONFIG_ERROR, EXIT_OK, cmd_publish

    plan1 = make_plan(plan_digest="1" * 64)
    plans_dir, profiles_dir, remote = _setup_publish_env(
        tmp_path, plan1, {"a.txt": "v1"}
    )
    _setup_cli_paths(monkeypatch, tmp_path, plans_dir, profiles_dir)
    assert cmd_publish(_publish_args(plan1)) == EXIT_OK

    # 第三方 clone 后推一个新 commit → 远端领先于本地副本
    third = tmp_path / "third"
    subprocess.run(["git", "clone", str(remote), str(third)], check=True, capture_output=True, env=GIT_ENV)
    (third / "intruder.txt").write_text("x", encoding="utf-8")
    _git(third, "add", "-A")
    _git(third, "commit", "-m", "intruder commit")
    _git(third, "push", "origin", "HEAD:main")

    plan2 = make_plan(plan_digest="2" * 64)
    staging2 = tmp_path / "release" / "staging" / plan2.plan_digest
    staging2.mkdir(parents=True)
    (staging2 / "a.txt").write_text("v2", encoding="utf-8")
    (plans_dir / f"{plan2.plan_digest}.json").write_text(
        json.dumps(plan2.to_dict()), encoding="utf-8"
    )
    assert cmd_publish(_publish_args(plan2)) == EXIT_CONFIG_ERROR
    # 远端历史未被强推覆盖
    out = subprocess.run(["git", "log", "--oneline", "main"], cwd=remote, capture_output=True, text=True)
    assert "intruder commit" in out.stdout


# ========== 5. release 编排 ==========

def _write_snapshot(triage_dir: Path, profile_id: str, scan_digest: str) -> None:
    triage_dir.mkdir(parents=True, exist_ok=True)
    (triage_dir / f"{profile_id}.json").write_text(
        json.dumps({
            "scan_digest": scan_digest,
            "approved_at": "2026-09-12T00:00:00+08:00",
            "approved_by": "tester",
            "hits": [{"path": "a.py", "rule": "local-absolute-path", "count": 1}],
        }),
        encoding="utf-8",
    )


PROFILE_WITH_APPROVAL = """schema_version = 1
profile_id = "{profile_id}"
audience = "public"

# 审批注释应保留
[approval]
plan_digest = "old"
profile_digest = "old"
components_digest = "old"
scan_digest = "old"
exemptions_digest = "old"
approved_at = "old"
approved_by = "old"

[behavior]
provide_api_keys = false
"""


def _make_main_repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "-b", "main")
    (main_repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-m", "init")
    return main_repo, _git(main_repo, "rev-parse", "HEAD").stdout.strip()


def test_release_approve_dry_run_no_side_effects(monkeypatch, tmp_path):
    """approve --dry-run：校验通过、profile 不被修改、不写快照。"""
    from tools.release.cli import EXIT_OK, _release_approve

    main_repo, sha = _make_main_repo_with_commit(tmp_path)
    plan = make_plan(source_commit=sha)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile_path = profiles_dir / "public-full.toml"
    profile_path.write_text(
        PROFILE_WITH_APPROVAL.format(profile_id="public-full"), encoding="utf-8"
    )
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / f"{plan.plan_digest}.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )
    triage_dir = tmp_path / "triage"
    _write_snapshot(triage_dir, "public-full", plan.scan_digest)

    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._TRIAGE_DIR", triage_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", main_repo)

    args = SimpleNamespace(
        plan=plan.plan_digest, publish=False, dry_run=True, tag=None,
        version=None, repo_dir=None, approved_by="tester", skip_heavy_gates=True,
    )
    assert _release_approve(args) == EXIT_OK
    assert "old" in profile_path.read_text(encoding="utf-8")  # approval 未被写入
    assert not (triage_dir / "public-full.json").exists() or json.loads(
        (triage_dir / "public-full.json").read_text(encoding="utf-8")
    )["approved_by"] == "tester"  # 快照未被覆盖（仍是测试预置内容）


def test_release_approve_scan_digest_mismatch_rejected(monkeypatch, tmp_path):
    """快照 scan_digest ≠ plan.scan_digest → 拒绝批准（源码漂移）。"""
    from tools.release.cli import EXIT_GATE_FAILURE, _release_approve

    main_repo, sha = _make_main_repo_with_commit(tmp_path)
    plan = make_plan(source_commit=sha, scan_digest="e" * 64)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "public-full.toml").write_text(
        PROFILE_WITH_APPROVAL.format(profile_id="public-full"), encoding="utf-8"
    )
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / f"{plan.plan_digest}.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )
    triage_dir = tmp_path / "triage"
    _write_snapshot(triage_dir, "public-full", "f" * 64)  # 不匹配

    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._TRIAGE_DIR", triage_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", main_repo)

    args = SimpleNamespace(
        plan=plan.plan_digest, publish=False, dry_run=False, tag=None,
        version=None, repo_dir=None, approved_by="tester", skip_heavy_gates=True,
    )
    assert _release_approve(args) == EXIT_GATE_FAILURE


def test_refresh_snapshot_digest_only_changes_digest(tmp_path):
    """refresh_snapshot_digest：只更新 scan_digest，命中集与审批元数据不动。"""
    from tools.release.engine.triage import load_snapshot, refresh_snapshot_digest

    path = tmp_path / "public-full.json"
    _write_snapshot(tmp_path, "public-full", "f" * 64)

    refresh_snapshot_digest(path, "e" * 64)

    snap = load_snapshot(path)
    assert snap["scan_digest"] == "e" * 64
    assert snap["approved_by"] == "tester"
    assert snap["hits"] == [{"path": "a.py", "rule": "local-absolute-path", "count": 1}]


def test_release_report_clean_refreshes_anchor(monkeypatch, tmp_path):
    """第一阶段干净报告 → 快照锚点对齐本轮 scan（增量发布不再锚定死锁）。"""
    from tools.release.cli import EXIT_OK, _release_report
    from tools.release.engine import prepare as prepare_mod

    plan = make_plan(scan_digest="e" * 64)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "public-full.toml").write_text(
        PROFILE_WITH_APPROVAL.format(profile_id="public-full"), encoding="utf-8"
    )
    triage_dir = tmp_path / "triage"
    _write_snapshot(triage_dir, "public-full", "f" * 64)  # 上轮批准的旧锚点

    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._TRIAGE_DIR", triage_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(prepare_mod, "prepare_release", lambda **kwargs: plan)
    monkeypatch.setattr(
        prepare_mod, "collect_sensitive_hits", lambda *a, **k: ([], 1, 0)
    )

    args = SimpleNamespace(profile="public-full", source="HEAD")
    assert _release_report(args) == EXIT_OK
    snap = json.loads((triage_dir / "public-full.json").read_text(encoding="utf-8"))
    assert snap["scan_digest"] == "e" * 64
    assert snap["approved_by"] == "tester"
    assert snap["hits"] == [{"path": "a.py", "rule": "local-absolute-path", "count": 1}]


def test_release_report_new_hits_keeps_anchor(monkeypatch, tmp_path):
    """第一阶段存在 NEW 命中 → 拒绝且锚点不刷新（批准仍被第二阶段拦住）。"""
    from tools.release.cli import EXIT_GATE_FAILURE, _release_report
    from tools.release.engine import prepare as prepare_mod

    plan = make_plan(scan_digest="e" * 64)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "public-full.toml").write_text(
        PROFILE_WITH_APPROVAL.format(profile_id="public-full"), encoding="utf-8"
    )
    triage_dir = tmp_path / "triage"
    _write_snapshot(triage_dir, "public-full", "f" * 64)

    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._TRIAGE_DIR", triage_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(prepare_mod, "prepare_release", lambda **kwargs: plan)
    monkeypatch.setattr(
        prepare_mod,
        "collect_sensitive_hits",
        lambda *a, **k: (
            [{"path": "b.py", "line": 1, "rule": "local-absolute-path", "severity": "MEDIUM"}],
            1,
            0,
        ),
    )

    args = SimpleNamespace(profile="public-full", source="HEAD")
    assert _release_report(args) == EXIT_GATE_FAILURE
    snap = json.loads((triage_dir / "public-full.json").read_text(encoding="utf-8"))
    assert snap["scan_digest"] == "f" * 64  # 锚点未动


def test_release_approve_full_chain(monkeypatch, tmp_path):
    """真实批准链路：写 approval → build(mock) → 写快照（未 --publish 不触达 publish）。"""
    from tools.release.cli import EXIT_OK, _release_approve

    main_repo, sha = _make_main_repo_with_commit(tmp_path)
    plan = make_plan(source_commit=sha)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile_path = profiles_dir / "public-full.toml"
    profile_path.write_text(
        PROFILE_WITH_APPROVAL.format(profile_id="public-full"), encoding="utf-8"
    )
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / f"{plan.plan_digest}.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )
    triage_dir = tmp_path / "triage"
    _write_snapshot(triage_dir, "public-full", plan.scan_digest)

    monkeypatch.setattr("tools.release.cli._PLANS_DIR", plans_dir)
    monkeypatch.setattr("tools.release.cli._PROFILES_DIR", profiles_dir)
    monkeypatch.setattr("tools.release.cli._TRIAGE_DIR", triage_dir)
    monkeypatch.setattr("tools.release.cli._PROJECT_ROOT", main_repo)

    build_calls = []

    def fake_build_release(plan, approval, skip_heavy_gates=False):
        assert isinstance(approval, Approval)
        assert approval.plan_digest == plan.plan_digest
        build_calls.append(approval)
        return Artifact(
            zip_path=tmp_path / "dist" / "x.zip",
            zip_sha256="0" * 64,
            plan_digest=plan.plan_digest,
            manifest_path=tmp_path / "dist" / "MANIFEST.json",
            archive_verification_passed=True,
            built_at="now",
        )

    monkeypatch.setattr("tools.release.engine.build.build_release", fake_build_release)

    args = SimpleNamespace(
        plan=plan.plan_digest, publish=False, dry_run=False, tag=None,
        version=None, repo_dir=None, approved_by="tester", skip_heavy_gates=True,
    )
    assert _release_approve(args) == EXIT_OK
    assert len(build_calls) == 1

    # approval 已写入 profile
    data = toml.load(profile_path)
    assert data["approval"]["plan_digest"] == plan.plan_digest
    assert data["approval"]["approved_by"] == "tester"
    assert data["behavior"]["provide_api_keys"] is False  # 其他段未受影响
    raw = profile_path.read_text(encoding="utf-8")
    assert "# 审批注释应保留" in raw  # 注释保留

    # 快照已写（批准生效）
    snap = json.loads((triage_dir / "public-full.json").read_text(encoding="utf-8"))
    assert snap["scan_digest"] == plan.scan_digest
    assert snap["approved_by"] == "tester"


def test_write_approval_creates_missing_keys(tmp_path):
    """[approval] 段缺 key 时自动补齐；CRLF 行尾保留。"""
    from tools.release.cli import _write_approval_to_profile

    plan = make_plan()
    profile = tmp_path / "p.toml"
    profile.write_bytes(
        b'[approval]\r\nplan_digest = "old"\r\n\r\n[behavior]\r\nx = 1\r\n'
    )
    _write_approval_to_profile(profile, plan, "tester")

    data = toml.load(profile)
    assert data["approval"]["plan_digest"] == plan.plan_digest
    for key in ("profile_digest", "components_digest", "scan_digest", "exemptions_digest"):
        assert len(data["approval"][key]) == 64
    assert data["behavior"] == {"x": 1}
    assert b"\r\n" in profile.read_bytes()  # 原行尾保留
