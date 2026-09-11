#!/usr/bin/env python3
"""workspace 关键数据备份工具.

读取各组件 manifest.toml 的 [backup] 段声明，按声明路径备份个人数据
（如股票 watchlist、账单、accounting.db 等）到项目内 + 项目外目录双位置。

背景：与密钥备份（tools/backup_secrets.py）对称设计，但独立目录和保留策略，
避免 workspace 大文件挤占密钥备份的保留窗口。

设计要点：
- 备份目标由 manifest.toml [backup] 段声明（lib/component_manifest.py 解析）
- 文件路径 → 原子拷贝（temp + rename）
- 目录路径（以 "/" 结尾或 is_dir）→ 打 zip 后原子拷贝
- 备份文件名：<basename>_<YYYYMMDD>_<HHMMSS>.<ext|zip>
- 保留策略：保留最近 N 个版本 ∪ M 天内所有版本（与 secret_backup 同语义）
- 单文件失败不阻塞其他文件
- 路径通过 lib.config_reader.load_config() 读取，无硬编码（防 release 包泄露本机路径）

配置见 config.toml [workspace_backup] 段（与 [secret_backup] 对称）。
定时调度见 config.toml [loops.workspace_backup] 段（默认 5 天一次，
与密钥备份频次一致，用户反馈数据更新不频繁）。

ADR-0029: workspace 数据备份方案——与 secret_backup 对称设计但独立配置。

Usage:
    # 默认双位置备份
    python tools/backup_workspace.py

    # 仅备份到项目内
    python tools/backup_workspace.py --location internal

    # 仅备份到项目外
    python tools/backup_workspace.py --location external

    # JSON 输出（agent 用）
    python tools/backup_workspace.py --json

    # 干跑（只显示会备份什么 + 删除哪些旧版本，不实际写）
    python tools/backup_workspace.py --dry-run

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
import zipfile
from datetime import datetime
from pathlib import Path

# 确保项目根在 sys.path（脚本可能由 .venv 直接运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.component_manifest import load_manifests  # noqa: E402
from lib.config_reader import load_config  # noqa: E402

# 备份文件名时间戳格式
TIMESTAMP_FMT = "%Y%m%d_%H%M%S"

# 备份文件名匹配正则（用于 retention 扫描）
# 例：watchlist_20260818_143000.json / 账单_20260818_143000.zip / stock_advisor_20260818_143000.db
# basename 允许 unicode（中文目录名如"账单"）
BACKUP_NAME_RE = re.compile(
    r"^(?P<basename>.+)_(?P<ts>\d{8}_\d{6})\.(?P<ext>[a-z0-9]+)$",
    re.UNICODE,
)


def _load_backup_config() -> dict:
    """读取 config.toml [workspace_backup] 段，返回归一化后的配置.

    Returns:
        dict 含 internal_dir (Path|None), external_dir (Path|None),
              keep_last_n (int), keep_within_days (int)
    """
    config = load_config()
    wb = config.get("workspace_backup", {})
    if not isinstance(wb, dict):
        wb = {}

    # 项目内目录（相对项目根；空则禁用）
    internal_str = wb.get("internal_dir", "backups/workspace")
    internal_dir: Path | None
    if internal_str and isinstance(internal_str, str):
        p = Path(internal_str)
        internal_dir = p if p.is_absolute() else _PROJECT_ROOT / p
    else:
        internal_dir = None

    # 项目外目录（绝对路径；空则禁用）
    external_str = wb.get("external_dir", "")
    external_dir: Path | None
    if external_str and isinstance(external_str, str):
        external_dir = Path(external_str)
    else:
        external_dir = None

    # 保留策略（workspace 数据更新快，默认保留窗口比密钥短）
    try:
        keep_last_n = int(wb.get("keep_last_n", 7))
    except (TypeError, ValueError):
        keep_last_n = 7
    try:
        keep_within_days = int(wb.get("keep_within_days", 14))
    except (TypeError, ValueError):
        keep_within_days = 14

    return {
        "internal_dir": internal_dir,
        "external_dir": external_dir,
        "keep_last_n": keep_last_n,
        "keep_within_days": keep_within_days,
    }


def _atomic_copy_file(src: Path, dst: Path) -> None:
    """原子拷贝：写到 dst.with_suffix('.tmp') 后 rename 到 dst.

    避免中途崩溃产生半写的损坏文件。dst 父目录不存在时自动创建。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    # copy2 保留 mtime / 权限元数据
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)  # 原子 rename（同盘）


def _atomic_write_zip(src_dir: Path, dst: Path) -> None:
    """把 src_dir 递归打 zip 到 dst（原子写：先写 .tmp 再 rename）.

    dst 父目录不存在时自动创建。zip 内文件名用相对 src_dir 的路径，
    不暴露本机绝对路径。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(src_dir.rglob("*")):
                if path.is_file():
                    arcname = path.relative_to(src_dir)
                    zf.write(path, arcname)
        os.replace(tmp, dst)
    except Exception:
        # 出错时清理 tmp
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _list_backups_for_basename(backup_dir: Path, basename: str) -> list[Path]:
    """扫描 backup_dir 中匹配 <basename>_<timestamp>.<ext> 的备份文件.

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
    backups = _list_backups_for_basename(backup_dir, basename)
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


def _derive_basename(rel_path: str, src: Path) -> str:
    """从 manifest [backup].paths 项推导备份文件 basename.

    - 目录路径（以 "/" 结尾或 src.is_dir）：取最后一段目录名（如 "账单/" → "账单"）
    - 文件路径：取 Path.stem（如 "data/stock_advisor.db" → "stock_advisor"，
      "watchlist.json" → "watchlist"）
    """
    if rel_path.endswith("/") or src.is_dir():
        # 目录：去掉结尾 "/"，取最后一段
        return rel_path.rstrip("/").split("/")[-1]
    return src.stem


def _backup_one(
    src: Path,
    basename: str,
    backup_dir: Path,
    timestamp: str,
    dry_run: bool = False,
) -> tuple[Path | None, str | None]:
    """把 src 备份到 backup_dir/<basename>_<timestamp>.<ext|zip>.

    Returns:
        (dst_path | None, error | None)。dry_run 时返回 dst 路径但不实际写。
    """
    if not src.exists():
        return None, f"source not exist: {src}"

    # 决定 dst 名：目录 → .zip，文件 → 原扩展名
    is_dir = src.is_dir()
    if is_dir:
        ext = "zip"
    else:
        ext = src.suffix.lstrip(".")  # 如 json / db / duckdb
    dst_name = f"{basename}_{timestamp}.{ext}"
    dst = backup_dir / dst_name

    if dry_run:
        return dst, None

    try:
        if is_dir:
            _atomic_write_zip(src, dst)
        else:
            _atomic_copy_file(src, dst)
    except OSError as e:
        return None, f"backup failed: {src} → {dst}: {e}"
    except Exception as e:
        return None, f"backup failed (zip): {src} → {dst}: {e}"

    return dst, None


def run_backup(
    location: str = "both",
    dry_run: bool = False,
) -> dict:
    """运行 workspace 数据备份流程，返回结构化结果（不打印，供 action / agent 调用）.

    Args:
        location: "both" / "internal" / "external"
        dry_run: True 只预览不写不删

    Returns:
        dict 含字段:
        - success (bool): 全部文件全部位置都成功
        - error (str|None): 失败时的主错误（首个），便于 action 上报
        - timestamp (str): 备份时间戳 YYYYMMDD_HHMMSS
        - locations (list[dict]): 实际使用的位置 [{name, path}]
        - components (list[dict]): 每个组件的备份结果
          [{name, paths_count, files: [{source, basename, backups: [{location, path, written}]}]}]
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
            "components": [],
            "retention_deleted": [],
            "errors": ["no_backup_location: 检查 config.toml [workspace_backup] 段"],
            "dry_run": dry_run,
        }

    # 加载所有组件 manifest
    manifests = load_manifests()

    result = {
        "timestamp": timestamp,
        "locations": [{"name": n, "path": str(p)} for n, p in locations],
        "components": [],
        "retention_deleted": [],
        "errors": [],
        "dry_run": dry_run,
    }

    all_ok = True
    for comp_name, m in sorted(manifests.items()):
        if m.backup is None or not m.backup.paths:
            continue

        comp_entry = {
            "name": comp_name,
            "paths_count": len(m.backup.paths),
            "files": [],
        }

        for rel_path in m.backup.paths:
            src = (m.workspace_dir / rel_path).resolve()
            basename = _derive_basename(rel_path, src)
            file_entry = {
                "source": str(src),
                "basename": basename,
                "backups": [],
            }

            # src 不存在时跳过整个文件（不算失败，但记录到 errors 供参考）
            if not src.exists():
                result["errors"].append(f"{comp_name}: source not exist: {src}")
                comp_entry["files"].append(file_entry)
                continue

            for loc_name, base_dir in locations:
                # 每个组件在备份目录下有自己的子目录（按组件隔离）
                backup_dir = base_dir / comp_name
                dst, err = _backup_one(
                    src, basename, backup_dir, timestamp, dry_run=dry_run
                )
                if dst:
                    file_entry["backups"].append({
                        "location": loc_name,
                        "path": str(dst),
                        "written": not dry_run,
                    })
                if err:
                    all_ok = False
                    result["errors"].append(f"{comp_name}: {err}")

            # 应用保留策略（每个位置独立，dry_run 也展示会被删的旧备份）
            for loc_name, base_dir in locations:
                backup_dir = base_dir / comp_name
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

            comp_entry["files"].append(file_entry)

        result["components"].append(comp_entry)

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
            "[ERROR] 没有可用的备份位置：检查 config.toml [workspace_backup] 段 "
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
        for comp in result["components"]:
            print(f"[Backup] 组件 {comp['name']} ({comp['paths_count']} paths):")
            for f in comp["files"]:
                for b in f["backups"]:
                    tag = "[DRY-RUN]" if args.dry_run else "[OK]   "
                    print(f"  {tag} {f['basename']} → {b['path']}")
        for d in result["retention_deleted"]:
            tag = "[DRY-RUN PRUNE] 将删除" if args.dry_run else "[PRUNE] 删除旧备份"
            print(f"  {tag}: {d['path']}")
        print()
        total_files = sum(len(c["files"]) for c in result["components"])
        print(f"[Backup] 完成。{len(result['components'])} 个组件，{total_files} 个文件已处理。")
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
        b["written"]
        for c in result["components"]
        for f in c["files"]
        for b in f["backups"]
    )
    return 2 if any_success else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="workspace 关键数据备份工具（按 manifest [backup] 段声明备份）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
配置：config.toml [workspace_backup] 段
  internal_dir  = "backups/workspace"  # 项目内（相对项目根）
  external_dir  = ""                  # 项目外（绝对路径，建议放不同物理盘）
  keep_last_n   = 7                    # 保留最近 N 个版本（密钥是 10，workspace 数据更新更快）
  keep_within_days = 14               # + 近 N 天所有版本（密钥是 20）

声明备份目标：workspace/<component>/manifest.toml [backup] 段
  [backup]
  paths = ["watchlist.json", "data/stock_advisor.db", "账单/"]
  # 文件路径 → 原子拷贝；目录路径（以 "/" 结尾）→ 打 zip

定时调度：config.toml [loops.workspace_backup] 段，IntervalTrigger 默认 5 天一次
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
