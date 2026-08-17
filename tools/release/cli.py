"""Release engine CLI — single entry point with subcommands (T15).

Spec: temp/sdd/release-engine/spec-v2-compiler.md Solution 节"新 CLI"

Usage:
    python -m tools.release.cli <subcommand> [options]

Subcommands:
    prepare          编译输入为 PreparedRelease（写 release/plans/<plan_digest>.json）
    compute-digest   计算 plan_digest 但不写 plan 文件（操作员工作流）
    build            从 plan 文件 + profile [approval] 构建产物到 release/dist/
    list-components  列出 audience policy 允许的组件
    scan             独立运行 sensitive-content-scan gate，打印扫描结果
    publish          推送 sanitized source + plan 到 public 仓库触发 CI（public audience 专属）

Exit codes:
    0  成功
    2  gate 失败 / digest 不匹配 / 构建失败
    3  配置错误（profile/audience 文件不存在、schema 错误等）
    1  其他错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import toml

# 项目根目录（cli.py 在 tools/release/，需 3 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROFILES_DIR = _PROJECT_ROOT / "release" / "profiles"
_PLANS_DIR = _PROJECT_ROOT / "release" / "plans"

# 退出码常量
EXIT_OK = 0
EXIT_GATE_FAILURE = 2
EXIT_CONFIG_ERROR = 3
EXIT_OTHER_ERROR = 1


def _print_err(msg: str) -> None:
    """打印红色错误信息到 stderr"""
    print(f"\033[91m[ERROR]\033[0m {msg}", file=sys.stderr)


def _print_ok(msg: str) -> None:
    """打印绿色成功信息到 stdout"""
    print(f"\033[92m[OK]\033[0m {msg}")


def _print_info(msg: str) -> None:
    """打印蓝色信息到 stdout"""
    print(f"\033[94m[INFO]\033[0m {msg}")


# ========== Subcommand: prepare ==========

def cmd_prepare(args: argparse.Namespace) -> int:
    """prepare --profile <id> --source <commit|HEAD> --audience <name>

    调 prepare_release()，输出 plan_digest + plan 文件路径。
    静态 gate 失败 → 退出码 2；配置错误 → 退出码 3。
    """
    from tools.release.engine.prepare import prepare_release
    from tools.release.engine.models import PreparedRelease

    try:
        plan = prepare_release(
            profile=args.profile,
            source=args.source,
            audience=args.audience,
        )
    except FileNotFoundError as e:
        _print_err(f"配置错误：{e}")
        return EXIT_CONFIG_ERROR
    except ValueError as e:
        # gate 失败 / manifest 解析失败 / 组件缺失
        _print_err(f"prepare 失败：{e}")
        return EXIT_GATE_FAILURE
    except Exception as e:
        _print_err(f"未预期错误：{type(e).__name__}: {e}")
        return EXIT_OTHER_ERROR

    plan_path = _PLANS_DIR / f"{plan.plan_digest}.json"
    _print_ok(f"prepare 成功")
    _print_info(f"profile:       {plan.profile_id}")
    _print_info(f"audience:      {plan.audience}")
    _print_info(f"source_commit: {plan.source_commit}")
    _print_info(f"plan_digest:   {plan.plan_digest}")
    _print_info(f"plan file:     {plan_path}")
    _print_info(f"components:    {list(plan.components)}")
    _print_info(f"file_entries:  {len(plan.file_entries)} files")
    _print_info(f"static gates:  {len(plan.gates_results)} run, all passed")

    # 操作员下一步提示
    print()
    print("下一步：")
    print(f"  1. 把 plan_digest 复制到 release/profiles/{args.profile}.toml [approval] 段")
    print(f"  2. 填写 approved_at / approved_by")
    print(f"  3. 运行：python -m tools.release.cli build --plan {plan.plan_digest}")

    return EXIT_OK


# ========== Subcommand: compute-digest ==========

def cmd_compute_digest(args: argparse.Namespace) -> int:
    """compute-digest --profile <id>

    计算各 digest 但不写 plan 文件（操作员工作流：先拿 digest → 填 [approval] → 跑 build）。
    实际上仍调 prepare_release() 写 plan 文件（plan 文件就是审批凭证），但此命令
    重点输出 digest 供操作员复制。
    """
    from tools.release.engine.prepare import prepare_release

    # 推断 audience：从 profile.audience 字段或默认 friend
    profile_path = _PROFILES_DIR / f"{args.profile}.toml"
    if not profile_path.exists():
        _print_err(f"profile 不存在：{profile_path}")
        return EXIT_CONFIG_ERROR

    try:
        profile_data = toml.load(profile_path)
    except Exception as e:
        _print_err(f"profile 解析失败：{e}")
        return EXIT_CONFIG_ERROR

    # 从 profile.toml 读 audience 字段（值为 "public" / "friend"，直接是 audience policy name）
    audience = profile_data.get("audience", "friend")

    try:
        plan = prepare_release(
            profile=args.profile,
            source="HEAD",
            audience=audience,
        )
    except FileNotFoundError as e:
        _print_err(f"配置错误：{e}")
        return EXIT_CONFIG_ERROR
    except ValueError as e:
        _print_err(f"compute-digest 失败：{e}")
        return EXIT_GATE_FAILURE
    except Exception as e:
        _print_err(f"未预期错误：{type(e).__name__}: {e}")
        return EXIT_OTHER_ERROR

    _print_ok("digest 计算完成")
    print()
    print(f"把以下值复制到 release/profiles/{args.profile}.toml [approval] 段：")
    print()
    print(f"[approval]")
    print(f'plan_digest = "{plan.plan_digest}"')
    print(f'profile_digest = "{plan.profile_digest}"')
    print(f'components_digest = "{plan.components_digest}"')
    print(f'scan_digest = "{plan.scan_digest}"')
    # exemptions_digest 是派生值，PreparedRelease 不存为字段（plan_digest 已含），
    # 这里用 compute_exemptions_digest(plan.exemptions) 重算供文档化输出。
    from tools.release.engine.digest import compute_exemptions_digest
    print(f'exemptions_digest = "{compute_exemptions_digest(plan.exemptions)}"')
    print(f'approved_at = "<ISO 8601 时间，如 2026-07-31T12:00:00+08:00>"')
    print(f'approved_by = "<审批人姓名>"')
    print()
    print(f"plan file: { _PLANS_DIR / f'{plan.plan_digest}.json' }")

    return EXIT_OK


# ========== Subcommand: build ==========

def cmd_build(args: argparse.Namespace) -> int:
    """build --plan <plan_digest>

    从 release/plans/<plan_digest>.json 读 plan + 从 profile [approval] 读 approval →
    调 build_release()，输出 artifact 路径到 release/dist/。
    """
    from tools.release.engine.build import build_release
    from tools.release.engine.models import Approval, PreparedRelease

    # 1. 读 plan 文件
    plan_path = _PLANS_DIR / f"{args.plan}.json"
    if not plan_path.exists():
        _print_err(f"plan 文件不存在：{plan_path}")
        _print_err(f"先运行：python -m tools.release.cli prepare --profile <id> --audience <name>")
        return EXIT_CONFIG_ERROR

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _print_err(f"plan 文件 JSON 解析失败：{e}")
        return EXIT_CONFIG_ERROR

    try:
        plan = PreparedRelease.from_dict(plan_data)
    except (KeyError, TypeError) as e:
        _print_err(f"plan 文件结构无效：{e}")
        return EXIT_CONFIG_ERROR

    # 2. 读 profile [approval] 段
    profile_path = _PROFILES_DIR / f"{plan.profile_id}.toml"
    if not profile_path.exists():
        _print_err(f"profile 不存在：{profile_path}")
        return EXIT_CONFIG_ERROR

    try:
        profile_data = toml.load(profile_path)
    except Exception as e:
        _print_err(f"profile 解析失败：{e}")
        return EXIT_CONFIG_ERROR

    approval_data = profile_data.get("approval", {})
    if not approval_data.get("plan_digest"):
        _print_err(f"profile [approval].plan_digest 为空 — 未审批")
        _print_err(f"先运行：python -m tools.release.cli compute-digest --profile {plan.profile_id}")
        return EXIT_CONFIG_ERROR

    try:
        approval = Approval(
            plan_digest=approval_data["plan_digest"],
            approved_at=approval_data.get("approved_at", ""),
            approved_by=approval_data.get("approved_by", ""),
            signature=approval_data.get("signature"),
        )
    except KeyError as e:
        _print_err(f"profile [approval] 缺字段：{e}")
        return EXIT_CONFIG_ERROR

    # 3. 调 build_release
    _print_info(f"开始构建：profile={plan.profile_id}, audience={plan.audience}")
    _print_info(f"plan_digest: {plan.plan_digest}")

    try:
        artifact = build_release(
            plan=plan,
            approval=approval,
            skip_heavy_gates=args.skip_heavy_gates,
        )
    except ValueError as e:
        _print_err(f"build 失败：{e}")
        return EXIT_GATE_FAILURE
    except Exception as e:
        _print_err(f"未预期错误：{type(e).__name__}: {e}")
        return EXIT_OTHER_ERROR

    _print_ok("build 成功")
    _print_info(f"artifact:      {artifact.zip_path}")
    _print_info(f"zip_sha256:    {artifact.zip_sha256}")
    _print_info(f"plan_digest:   {artifact.plan_digest}")
    _print_info(f"built_at:      {artifact.built_at}")
    _print_info(f"archive_verified: {artifact.archive_verification_passed}")

    # .zip.sha256 sidecar 路径
    sha_path = artifact.zip_path.with_suffix(".zip.sha256")
    if sha_path.exists():
        _print_info(f"sha256 sidecar: {sha_path}")

    return EXIT_OK


# ========== Subcommand: list-components ==========

def cmd_list_components(args: argparse.Namespace) -> int:
    """list-components --audience <name>

    列出 audience policy 允许的组件清单。
    """
    from tools.release.engine.audience import load_audience_policy
    from tools.release.engine.manifest import load_components_for_audience

    try:
        policy = load_audience_policy(args.audience)
    except FileNotFoundError as e:
        _print_err(f"配置错误：{e}")
        return EXIT_CONFIG_ERROR
    except ValueError as e:
        _print_err(f"audience policy 无效：{e}")
        return EXIT_CONFIG_ERROR

    audience = policy["audience"]
    _print_ok(f"audience: {audience['name']}")
    _print_info(f"profile:     {audience['profile']}")
    _print_info(f"export_set:  {audience['export_set']}")
    print()
    print(f"components ({len(audience['components'])}):")
    for name in audience["components"]:
        print(f"  - {name}")

    print()
    print(f"static gates: {audience['gates']['static']}")
    print(f"build_time gates: {audience['gates']['build_time']}")

    # 尝试加载组件 manifest 验证
    try:
        components = load_components_for_audience(policy)
        _print_ok(f"全部 {len(components)} 个组件 manifest 加载成功（新 shape）")
    except ValueError as e:
        _print_err(f"组件 manifest 加载失败：{e}")
        return EXIT_GATE_FAILURE

    return EXIT_OK


# ========== Subcommand: scan ==========

def cmd_scan(args: argparse.Namespace) -> int:
    """scan --profile <id>

    独立运行 sensitive-content-scan gate，打印扫描结果。
    实际上调 prepare_release() 并打印 scan gate 的 details。
    """
    from tools.release.engine.prepare import prepare_release

    # 推断 audience（同 compute-digest）
    profile_path = _PROFILES_DIR / f"{args.profile}.toml"
    if not profile_path.exists():
        _print_err(f"profile 不存在：{profile_path}")
        return EXIT_CONFIG_ERROR

    try:
        profile_data = toml.load(profile_path)
    except Exception as e:
        _print_err(f"profile 解析失败：{e}")
        return EXIT_CONFIG_ERROR

    # 从 profile.toml 读 audience 字段（值为 "public" / "friend"，直接是 audience policy name）
    audience = profile_data.get("audience", "friend")

    try:
        plan = prepare_release(
            profile=args.profile,
            source="HEAD",
            audience=audience,
        )
    except FileNotFoundError as e:
        _print_err(f"配置错误：{e}")
        return EXIT_CONFIG_ERROR
    except ValueError as e:
        # gate 失败信息含 scan 结果
        _print_err(f"prepare 失败（含扫描）：{e}")
        return EXIT_GATE_FAILURE
    except Exception as e:
        _print_err(f"未预期错误：{type(e).__name__}: {e}")
        return EXIT_OTHER_ERROR

    # 找到 sensitive-content-scan gate 结果
    scan_gate = next(
        (g for g in plan.gates_results if g.name == "sensitive-content-scan"),
        None,
    )

    _print_ok("scan 完成")
    if scan_gate:
        _print_info(f"gate:      {scan_gate.name}")
        _print_info(f"passed:    {scan_gate.passed}")
        _print_info(f"details:   {scan_gate.details}")
    else:
        _print_err("未找到 sensitive-content-scan gate 结果")
        return EXIT_OTHER_ERROR

    _print_info(f"scan_digest: {plan.scan_digest}")
    _print_info(f"file_entries: {len(plan.file_entries)} files scanned")

    return EXIT_OK if scan_gate and scan_gate.passed else EXIT_GATE_FAILURE


# ========== Subcommand: publish ==========

def cmd_publish(args: argparse.Namespace) -> int:
    """publish --plan <digest> [--dry-run]

    推送 sanitized source + plan 到 public 仓库触发 CI（spec-v3-public.md Solution 节）。

    步骤：
    1. 读 plan 文件（release/plans/<digest>.json）
    2. 校验 plan.audience == "public"（friend plan 不允许 publish）
    3. 检查 staging 目录存在（release/staging/<digest>/，由 build_release 产生）
    4. 读 profile [publication] 段获取 public_repo_url + public_repo_branch
    5. 创建临时目录，复制 staging 里的 sanitized source（排除 ZIP 等大文件）
    6. 复制 plan 文件到临时目录（CI 需要读 plan 重建产物）
    7. git init + add + commit
    8. 添加 public 仓库 remote + push（--dry-run 时跳过 push）
    9. 打印 public 仓库 URL + CI workflow 触发状态
    """
    import shutil
    import subprocess
    import tempfile

    from tools.release.engine.models import PreparedRelease

    # 1. 读 plan 文件
    plan_path = _PLANS_DIR / f"{args.plan}.json"
    if not plan_path.exists():
        _print_err(f"plan 文件不存在：{plan_path}")
        _print_err(f"先运行：python -m tools.release.cli prepare --profile <id> --audience public")
        return EXIT_CONFIG_ERROR

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = PreparedRelease.from_dict(plan_data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        _print_err(f"plan 文件无效：{e}")
        return EXIT_CONFIG_ERROR

    # 2. 校验 audience == "public"
    if plan.audience != "public":
        _print_err(f"plan.audience != 'public'（实际：{plan.audience}），publish 仅用于 public audience")
        return EXIT_GATE_FAILURE

    # 3. 检查 staging 目录存在
    staging_dir = _PROJECT_ROOT / "release" / "staging" / plan.plan_digest
    if not staging_dir.exists():
        _print_err(f"staging 目录不存在：{staging_dir}")
        _print_err(f"先运行：python -m tools.release.cli build --plan {plan.plan_digest}")
        return EXIT_GATE_FAILURE

    # 4. 读 profile [publication] 段
    profile_path = _PROFILES_DIR / f"{plan.profile_id}.toml"
    if not profile_path.exists():
        _print_err(f"profile 不存在：{profile_path}")
        return EXIT_CONFIG_ERROR

    try:
        profile_data = toml.load(profile_path)
    except Exception as e:
        _print_err(f"profile 解析失败：{e}")
        return EXIT_CONFIG_ERROR

    publication = profile_data.get("publication", {})
    repo_url = publication.get("public_repo_url", "")
    repo_branch = publication.get("public_repo_branch", "main")

    if not repo_url or "<user>" in repo_url:
        _print_err(f"profile [publication].public_repo_url 未配置（当前值：{repo_url}）")
        _print_err(f"请在 {profile_path} 中填入真实的 public 仓库 URL")
        return EXIT_CONFIG_ERROR

    _print_info(f"publish 配置：")
    _print_info(f"  plan_digest:  {plan.plan_digest}")
    _print_info(f"  audience:     {plan.audience}")
    _print_info(f"  staging:      {staging_dir}")
    _print_info(f"  repo_url:     {repo_url}")
    _print_info(f"  repo_branch:  {repo_branch}")
    _print_info(f"  dry_run:      {args.dry_run}")

    # 5. 创建临时目录 + 复制 sanitized source
    with tempfile.TemporaryDirectory(prefix="localagent-publish-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        # 复制 staging 内容到 source_dir（排除 ZIP 和 sidecar，CI 会重建）
        excluded_suffixes = {".zip", ".zip.sha256"}
        copied_count = 0
        for item in staging_dir.rglob("*"):
            if item.is_file() and item.suffix not in excluded_suffixes:
                rel = item.relative_to(staging_dir)
                dest = source_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                copied_count += 1

        # 6. 复制 plan 文件到 source 根目录（CI 需要读 plan）
        plan_dest = source_dir / "release-plan.json"
        shutil.copy2(plan_path, plan_dest)

        _print_info(f"  copied files: {copied_count} + release-plan.json")

        # 7. git init + add + commit
        try:
            subprocess.run(
                ["git", "init", "--initial-branch", repo_branch],
                cwd=source_dir, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=source_dir, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Release public-full plan {plan.plan_digest[:12]}"],
                cwd=source_dir, check=True, capture_output=True,
                env={**__import__("os").environ, "GIT_AUTHOR_NAME": "localagent-release",
                     "GIT_AUTHOR_EMAIL": "release@localagent.local",
                     "GIT_COMMITTER_NAME": "localagent-release",
                     "GIT_COMMITTER_EMAIL": "release@localagent.local"},
            )
        except subprocess.CalledProcessError as e:
            _print_err(f"git 操作失败：{e.stderr.decode('utf-8', errors='ignore') if e.stderr else e}")
            return EXIT_CONFIG_ERROR

        # 8. push（--dry-run 时跳过）
        if args.dry_run:
            _print_ok("dry-run 模式：未实际 push")
            _print_info(f"  临时目录：{source_dir}")
            _print_info(f"  下一步：去掉 --dry-run 实际 push 到 {repo_url}")
        else:
            try:
                subprocess.run(
                    ["git", "remote", "add", "origin", repo_url],
                    cwd=source_dir, check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "push", "-u", "origin", repo_branch, "--force"],
                    cwd=source_dir, check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                _print_err(f"git push 失败：{e.stderr.decode('utf-8', errors='ignore') if e.stderr else e}")
                return EXIT_CONFIG_ERROR

            _print_ok("push 成功，CI 已触发")
            _print_info(f"  public 仓库：{repo_url}")
            _print_info(f"  分支：{repo_branch}")
            _print_info(f"  CI workflow：.github/workflows/release-public.yml")

    return EXIT_OK


# ========== Argument parser ==========

def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器（git/kubectl 风格 subcommand）"""
    parser = argparse.ArgumentParser(
        prog="tools.release.cli",
        description="LocalAgent release engine CLI（spec-v2-compiler.md）",
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="subcommands",
        metavar="<subcommand>",
        required=True,
    )

    # prepare
    p_prepare = subparsers.add_parser(
        "prepare",
        help="编译输入为 PreparedRelease（写 release/plans/<plan_digest>.json）",
    )
    p_prepare.add_argument("--profile", required=True, help="profile_id，如 friend-full")
    p_prepare.add_argument("--source", default="HEAD", help="git commit sha 或 HEAD（默认 HEAD）")
    p_prepare.add_argument("--audience", default="friend", help="audience name（默认 friend）")
    p_prepare.set_defaults(func=cmd_prepare)

    # compute-digest
    p_digest = subparsers.add_parser(
        "compute-digest",
        help="计算 plan_digest 供操作员填写 [approval] 段",
    )
    p_digest.add_argument("--profile", required=True, help="profile_id，如 friend-full")
    p_digest.set_defaults(func=cmd_compute_digest)

    # build
    p_build = subparsers.add_parser(
        "build",
        help="从 plan 文件 + profile [approval] 构建产物到 release/dist/",
    )
    p_build.add_argument("--plan", required=True, help="plan_digest（plan 文件名前缀）")
    p_build.add_argument(
        "--skip-heavy-gates",
        action="store_true",
        help="跳过 no-key-startup / activity-task-safe-stop / focused-tests（仅跑 archive-verification，用于快速测试）",
    )
    p_build.set_defaults(func=cmd_build)

    # list-components
    p_list = subparsers.add_parser(
        "list-components",
        help="列出 audience policy 允许的组件",
    )
    p_list.add_argument("--audience", default="friend", help="audience name（默认 friend）")
    p_list.set_defaults(func=cmd_list_components)

    # scan
    p_scan = subparsers.add_parser(
        "scan",
        help="独立运行 sensitive-content-scan，打印扫描结果",
    )
    p_scan.add_argument("--profile", required=True, help="profile_id，如 friend-full")
    p_scan.set_defaults(func=cmd_scan)

    # publish（public audience 专属）
    p_publish = subparsers.add_parser(
        "publish",
        help="推送 sanitized source + plan 到 public 仓库触发 CI（public audience 专属）",
    )
    p_publish.add_argument("--plan", required=True, help="plan_digest（plan 文件名前缀）")
    p_publish.add_argument(
        "--dry-run",
        action="store_true",
        help="只复制 + git commit，不实际 push（用于测试）",
    )
    p_publish.set_defaults(func=cmd_publish)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
