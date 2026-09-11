#!/usr/bin/env python3
"""密钥备份工具.

手动触发或定时调度调用，备份以下敏感文件到项目内 + 项目外目录：
- data/llm/keys.json (LLM/AIGC 密钥库，gitignored)
- data/secret/secrets.toml (非 LLM token，gitignored)
- config.toml (项目配置，gitignored)

背景：这些文件因 .gitignore 排除而无法 git 追踪，agent 误删/写坏后难以恢复。
本工具通过定时（每 5 天，由后端 [loops.secret_backup] 调度）+ 手动触发，把明文
副本存到项目内 backups/secrets/ + 项目外目录双位置，防单点故障。

设计要点：
- 路径通过 lib.secret.get_*_path() 读取，无硬编码（防 release 包泄露本机路径）
- 备份文件名：<basename>_<YYYYMMDD>_<HHMMSS>.<ext>
- 保留策略：保留最近 N 个版本 + M 天内所有版本（取并集，超出的删除）
- 原子写：写到临时文件后 rename，避免中途崩溃产生损坏文件
- 单文件失败不阻塞其他文件：keys.json 备份失败仍继续 secrets.toml

配置见 config.toml [secret_backup] 段。
风险登记见 SECURITY-RISKS.md 第 15 项（明文备份，暂不加密）。

Usage:
    # 默认双位置备份（项目内 + 项目外）
    python tools/backup_secrets.py

    # 仅备份到项目内
    python tools/backup_secrets.py --location internal

    # 仅备份到项目外
    python tools/backup_secrets.py --location external

    # JSON 输出（agent 用）
    python tools/backup_secrets.py --json

    # 干跑（只显示会备份什么 + 删除哪些旧版本，不实际写）
    python tools/backup_secrets.py --dry-run

Exit codes:
    0 = 全部成功
    2 = 部分失败（至少一个文件/位置失败但其他成功）
    3 = 配置错误
    1 = 其他错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根在 sys.path（脚本可能由 .venv 直接运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.config_reader import load_config  # noqa: E402
from lib.secret.paths import (  # noqa: E402
    get_config_path,
    get_llm_keys_path,
    get_secrets_toml_path,
)

# 备份目标文件清单（顺序即处理顺序）
# 每项: (源路径获取函数, basename)
BACKUP_TARGETS: list[tuple] = [
    (get_llm_keys_path, "keys"),       # data/llm/keys.json
    (get_secrets_toml_path, "secrets"),  # data/secret/secrets.toml
    (get_config_path, "config"),       # config.toml
]

# 备份文件名时间戳格式
TIMESTAMP_FMT = "%Y%m%d_%H%M%S"

# 备份文件名匹配正则（用于 retention 扫描）
# 例：keys_20260818_143000.json
BACKUP_NAME_RE = re.compile(
    r"^(?P<basename>[a-z]+)_(?P<ts>\d{8}_\d{6})\.(?P<ext>json|toml)$"
)


def _load_backup_config() -> dict:
    """读取 config.toml [secret_backup] 段，返回归一化后的配置.

    Returns:
        dict 含 internal_dir (Path|None), external_dir (Path|None),
              keep_last_n (int), keep_within_days (int)
    """
    config = load_config()
    sb = config.get("secret_backup", {})
    if not isinstance(sb, dict):
        sb = {}

    # 项目内目录（相对项目根；空则禁用）
    internal_str = sb.get("internal_dir", "backups/secrets")
    internal_dir: Path | None
    if internal_str and isinstance(internal_str, str):
        p = Path(internal_str)
        internal_dir = p if p.is_absolute() else _PROJECT_ROOT / p
    else:
        internal_dir = None

    # 项目外目录（绝对路径；空则禁用）
    external_str = sb.get("external_dir", "")
    external_dir: Path | None
    if external_str and isinstance(external_str, str):
        external_dir = Path(external_str)
    else:
        external_dir = None

    # 保留策略
    try:
        keep_last_n = int(sb.get("keep_last_n", 10))
    except (TypeError, ValueError):
        keep_last_n = 10
    try:
        keep_within_days = int(sb.get("keep_within_days", 20))
    except (TypeError, ValueError):
        keep_within_days = 20

    return {
        "internal_dir": internal_dir,
        "external_dir": external_dir,
        "keep_last_n": keep_last_n,
        "keep_within_days": keep_within_days,
    }


def _atomic_copy(src: Path, dst: Path) -> None:
    """原子拷贝：写到 dst.with_suffix('.tmp') 后 rename 到 dst.

    避免中途崩溃产生半写的损坏文件。dst 父目录不存在时自动创建。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    # copy2 保留 mtime / 权限元数据
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)  # 原子 rename（同盘）


def _list_backups_for_file(backup_dir: Path, basename: str) -> list[Path]:
    """扫描 backup_dir 中匹配 basename_<timestamp>.<ext> 的备份文件.

    返回按时间戳升序排列的 Path 列表（旧→新）。
    不匹配命名规则的文件（用户手动放/旧格式）被忽略，不参与 retention。
    """
    if not backup_dir.exists():
        return []
    out: list[tuple[str, Path]] = []
    for p in backup_dir.iterdir():
        if not p.is_file():
            continue
        m = BACKUP_NAME_RE.match(p.name)
        if not m or m.group("basename") != basename:
            continue
        out.append((m.group("ts"), p))
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def _apply_retention(
    backup_dir: Path,
    basename: str,
    keep_last_n: int,
    keep_within_days: int,
    dry_run: bool = False,
) -> list[Path]:
    """对 backup_dir 中 basename 的备份应用保留策略.

    保留：最近 N 个版本 ∪ 近 keep_within_days 天的所有版本。
    删除：同时满足「排序后位次 >= N」且「年龄 > keep_within_days 天」的版本。

    Returns:
        被删除的 Path 列表
    """
    backups = _list_backups_for_file(backup_dir, basename)
    if not backups:
        return []

    now = datetime.now()
    to_delete: list[Path] = []

    # backups 已按时间戳升序，最新在末尾
    # 倒序遍历，记录位次（从 0 开始）
    for idx_from_newest, path in enumerate(reversed(backups)):
        m = BACKUP_NAME_RE.match(path.name)
        ts_str = m.group("ts")
        try:
            ts = datetime.strptime(ts_str, TIMESTAMP_FMT)
        except ValueError:
            continue  # 不该发生，正则已限制格式

        age_days = (now - ts).days
        is_in_last_n = idx_from_newest < keep_last_n
        is_within_days = age_days <= keep_within_days

        if not is_in_last_n and not is_within_days:
            to_delete.append(path)

    if not dry_run:
        for p in to_delete:
            try:
                p.unlink()
            except OSError:
                pass  # 删除失败不阻塞，下次再清
    return to_delete


def backup_to_dir(
    src: Path,
    basename: str,
    backup_dir: Path,
    timestamp: str,
    dry_run: bool = False,
) -> Path | None:
    """把 src 备份到 backup_dir/<basename>_<timestamp>.<ext>.

    Returns:
        备份文件 Path；src 不存在或失败返回 None
    """
    if not src.exists():
        print(f"  [SKIP] 源文件不存在: {src}", file=sys.stderr)
        return None

    ext = src.suffix.lstrip(".")  # json / toml
    dst_name = f"{basename}_{timestamp}.{ext}"
    dst = backup_dir / dst_name

    if dry_run:
        print(f"  [DRY-RUN] 将备份: {src} → {dst}")
        return dst  # 返回路径但不实际写

    try:
        _atomic_copy(src, dst)
    except OSError as e:
        print(f"  [FAIL] 备份失败 {src} → {dst}: {e}", file=sys.stderr)
        return None
    print(f"  [OK]   {src.name} → {dst}")
    return dst


def run_backup(
    location: str = "both",
    dry_run: bool = False,
) -> dict:
    """运行备份流程，返回结构化结果（不打印，供 action / agent 调用）.

    Args:
        location: "both" / "internal" / "external"
        dry_run: True 只预览不写不删

    Returns:
        dict 含字段:
        - success (bool): 全部文件全部位置都成功
        - error (str|None): 失败时的主错误（首个），便于 action 上报
        - timestamp (str): 备份时间戳 YYYYMMDD_HHMMSS
        - locations (list[dict]): 实际使用的位置 [{name, path}]
        - files (list[dict]): 每个源文件的备份结果 [{source, basename, backups: [{location, path, written}]}]
        - retention_deleted (list[dict]): 被清理的旧版本 [{location, path}]
        - errors (list[str]): 所有错误描述
        - dry_run (bool): 本次是否为 dry_run
    """
    cfg = _load_backup_config()

    # 决定目标位置
    locations: list[tuple[str, Path]] = []
    if location in ("both", "internal") and cfg["internal_dir"]:
        locations.append(("internal", cfg["internal_dir"]))
    if location in ("both", "external") and cfg["external_dir"]:
        locations.append(("external", cfg["external_dir"]))

    timestamp = datetime.now().strftime(TIMESTAMP_FMT)

    if not locations:
        return {
            "success": False,
            "error": "no_backup_location",
            "timestamp": timestamp,
            "locations": [],
            "files": [],
            "retention_deleted": [],
            "errors": ["no_backup_location: 检查 config.toml [secret_backup] 段"],
            "dry_run": dry_run,
        }

    result = {
        "timestamp": timestamp,
        "locations": [{"name": n, "path": str(p)} for n, p in locations],
        "files": [],
        "retention_deleted": [],
        "errors": [],
        "dry_run": dry_run,
    }

    all_ok = True
    for src_fn, basename in BACKUP_TARGETS:
        src = src_fn()
        file_entry = {
            "source": str(src),
            "basename": basename,
            "backups": [],
        }
        for loc_name, backup_dir in locations:
            # dry_run=True 时 backup_to_dir 返回 dst 但不写
            dst = backup_to_dir(src, basename, backup_dir, timestamp, dry_run=dry_run)
            if dst:
                file_entry["backups"].append({
                    "location": loc_name,
                    "path": str(dst),
                    "written": not dry_run,
                })
            else:
                # src 不存在 → 跳过（不算失败，但仍记录在 errors 中供参考）
                if not src.exists():
                    result["errors"].append(f"source not exist: {src}")
                else:
                    all_ok = False
                    result["errors"].append(f"backup failed: {src} → {loc_name}")

        # 应用保留策略（每个位置独立，dry_run 也展示会被删的旧备份）
        for loc_name, backup_dir in locations:
            deleted = _apply_retention(
                backup_dir,
                basename,
                cfg["keep_last_n"],
                cfg["keep_within_days"],
                dry_run=dry_run,
            )
            for p in deleted:
                result["retention_deleted"].append({
                    "location": loc_name,
                    "path": str(p),
                })

        result["files"].append(file_entry)

    result["success"] = all_ok
    result["error"] = None if all_ok else (
        result["errors"][0] if result["errors"] else "unknown backup failure"
    )
    return result


def cmd_backup(args: argparse.Namespace) -> int:
    """argparse 入口：调用 run_backup 并按 --json 控制输出."""
    result = run_backup(location=args.location, dry_run=args.dry_run)

    if not result["locations"]:
        print(
            "[ERROR] 没有可用的备份位置：检查 config.toml [secret_backup] 段 "
            "(internal_dir / external_dir 至少一个非空)",
            file=sys.stderr,
        )
        return 3

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[Backup] 时间戳: {result['timestamp']}")
        print(
            "[Backup] 目标位置: "
            + ", ".join(f"{l['name']}={l['path']}" for l in result["locations"])
        )
        for f in result["files"]:
            for b in f["backups"]:
                tag = "[DRY-RUN]" if args.dry_run else "[OK]   "
                print(f"  {tag} {f['basename']} → {b['path']}")
        for d in result["retention_deleted"]:
            tag = "[DRY-RUN PRUNE] 将删除" if args.dry_run else "[PRUNE] 删除旧备份"
            print(f"  {tag}: {d['path']}")
        print()
        print(f"[Backup] 完成。{len(result['files'])} 个文件已处理。")
        if result["retention_deleted"]:
            print(
                f"[Backup] retention 清理：删除 {len(result['retention_deleted'])} 个旧版本"
            )
        if result["errors"]:
            print(f"[Backup] 错误 {len(result['errors'])} 个：", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    # 全部失败 → 1；部分失败 → 2；全部成功 → 0
    if result["success"]:
        return 0
    any_success = any(
        b["written"] for f in result["files"] for b in f["backups"]
    )
    return 2 if any_success else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="密钥备份工具（项目内 + 项目外双位置，明文副本）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
配置：config.toml [secret_backup] 段
  internal_dir  = "backups/secrets"      # 项目内（相对项目根）
  external_dir  = "<backup_drive>\\secret"  # 项目外（绝对路径）
  keep_last_n   = 10                     # 保留最近 N 个版本
  keep_within_days = 20                  # + 近 N 天所有版本

定时调度：后端 [loops.secret_backup] 段配置 IntervalTrigger，默认 5 天一次
""",
    )
    parser.add_argument(
        "--location",
        choices=["both", "internal", "external"],
        default="both",
        help="备份位置（默认 both）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 到 stdout（agent 用），人类可读信息走 stderr",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示会做什么，不实际写/删",
    )
    args = parser.parse_args()
    return cmd_backup(args)


if __name__ == "__main__":
    sys.exit(main())
