#!/usr/bin/env python3
"""密钥恢复工具.

列出可用备份 + 从指定备份恢复密钥文件。

支持恢复的目标文件（与 backup_secrets.py 对应）：
- keys.json    (data/llm/keys.json)
- secrets.toml (data/secret/secrets.toml)
- config.toml  (config.toml)

安全机制：
- 恢复前自动备份当前文件到 <file>.pre_restore.<timestamp>.bak（防误恢复）
- 恢复需 --version 显式指定备份版本时间戳，避免误操作
- 默认交互确认（--yes / -y 跳过，--dry-run 只看不动）
- 路径通过 lib.secret.get_*_path() 读取，无硬编码

Usage:
    # 列出所有可用备份（默认）
    python tools/restore_secrets.py
    python tools/restore_secrets.py list

    # 列出指定文件的所有备份
    python tools/restore_secrets.py list --file keys.json

    # 从指定备份恢复
    python tools/restore_secrets.py restore \\
        --file keys.json \\
        --version 20260818_143000 \\
        --source internal

    # 恢复前先 dry-run
    python tools/restore_secrets.py restore \\
        --file keys.json --version 20260818_143000 --source internal --dry-run

    # 跳过交互确认（agent 用）
    python tools/restore_secrets.py restore \\
        --file keys.json --version 20260818_143000 --source internal --yes

    # JSON 输出（agent 用）
    python tools/restore_secrets.py list --json

Exit codes:
    0 = 成功
    1 = 找不到备份 / 参数错误
    2 = 恢复过程中部分失败
    3 = 配置错误
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

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.config_reader import load_config  # noqa: E402
from lib.secret.paths import (  # noqa: E402
    get_config_path,
    get_llm_keys_path,
    get_secrets_toml_path,
)

TIMESTAMP_FMT = "%Y%m%d_%H%M%S"

# 用户传 --file 时用的别名（统一成 basename）
# basename 是 backup_secrets.py 中 BACKUP_TARGETS 用的标识
FILE_ALIAS_MAP: dict[str, str] = {
    "keys.json": "keys",
    "keys": "keys",
    "secrets.toml": "secrets",
    "secrets": "secrets",
    "config.toml": "config",
    "config": "config",
}

# 反向映射：basename → 实际源文件路径获取函数
BASENAME_TO_SOURCE: dict[str, callable] = {
    "keys": get_llm_keys_path,
    "secrets": get_secrets_toml_path,
    "config": get_config_path,
}

# 备份文件名匹配正则（与 backup_secrets.py 同步）
BACKUP_NAME_RE = re.compile(
    r"^(?P<basename>[a-z]+)_(?P<ts>\d{8}_\d{6})\.(?P<ext>json|toml)$"
)


def _load_backup_dirs() -> dict[str, Path | None]:
    """读取 config.toml [secret_backup] 的 internal_dir / external_dir.

    Returns:
        {"internal": Path|None, "external": Path|None}
    """
    config = load_config()
    sb = config.get("secret_backup", {})
    if not isinstance(sb, dict):
        sb = {}

    out: dict[str, Path | None] = {"internal": None, "external": None}

    internal_str = sb.get("internal_dir", "backups/secrets")
    if internal_str and isinstance(internal_str, str):
        p = Path(internal_str)
        out["internal"] = p if p.is_absolute() else _PROJECT_ROOT / p

    external_str = sb.get("external_dir", "")
    if external_str and isinstance(external_str, str):
        out["external"] = Path(external_str)

    return out


def _list_backups_in_dir(
    backup_dir: Path, basename: str | None = None
) -> list[dict]:
    """扫描 backup_dir 中所有备份，返回元信息列表.

    Args:
        backup_dir: 备份目录
        basename: 仅返回该 basename 的备份；None 则返回全部

    Returns:
        list of {
            "basename": str,
            "timestamp": str (YYYYMMDD_HHMMSS),
            "datetime": str (ISO),
            "ext": str,
            "size": int,
            "path": str (绝对路径),
        }
    """
    if not backup_dir.exists():
        return []
    out: list[dict] = []
    for p in backup_dir.iterdir():
        if not p.is_file():
            continue
        m = BACKUP_NAME_RE.match(p.name)
        if not m:
            continue
        if basename and m.group("basename") != basename:
            continue
        ts_str = m.group("ts")
        try:
            dt = datetime.strptime(ts_str, TIMESTAMP_FMT)
            iso_dt = dt.isoformat()
        except ValueError:
            iso_dt = ""
        out.append(
            {
                "basename": m.group("basename"),
                "timestamp": ts_str,
                "datetime": iso_dt,
                "ext": m.group("ext"),
                "size": p.stat().st_size,
                "path": str(p.resolve()),
            }
        )
    # 按时间戳降序（最新在前）
    out.sort(key=lambda x: x["timestamp"], reverse=True)
    return out


def _format_size(n: int) -> str:
    """字节数人类可读化."""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_list(args: argparse.Namespace) -> int:
    """列出可用备份."""
    dirs = _load_backup_dirs()
    basename = FILE_ALIAS_MAP.get(args.file) if args.file else None
    if args.file and basename is None:
        print(
            f"[ERROR] 不识别的 --file 参数: {args.file}。"
            f"可选: keys.json / secrets.toml / config.toml",
            file=sys.stderr,
        )
        return 1

    result: dict = {"filter_basename": basename, "locations": []}
    print(f"[List] 备份目录扫描（filter={basename or 'ALL'}）")
    any_found = False
    for loc_name in ("internal", "external"):
        d = dirs[loc_name]
        if d is None:
            print(f"  [{loc_name}] 未配置（config.toml [secret_backup] 段为空）")
            continue
        backups = _list_backups_in_dir(d, basename)
        result["locations"].append(
            {"name": loc_name, "path": str(d), "backups": backups}
        )
        print(f"  [{loc_name}] {d}")
        if not backups:
            print("    (无备份)")
            continue
        any_found = True
        # 按文件分组打印
        by_file: dict[str, list[dict]] = {}
        for b in backups:
            by_file.setdefault(b["basename"], []).append(b)
        for f_basename, items in sorted(by_file.items()):
            print(f"    {f_basename} ({len(items)} 个版本):")
            for b in items[:20]:  # 最多打印 20 个，避免刷屏
                print(
                    f"      {b['timestamp']}  {_format_size(b['size']):>10s}  "
                    f"{b['path']}"
                )
            if len(items) > 20:
                print(f"      ... 还有 {len(items) - 20} 个")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not any_found:
            print("\n[List] 未找到任何备份。先运行 tools/backup_secrets.py 创建。")

    return 0


def _make_pre_restore_bak(target: Path) -> Path | None:
    """恢复前给当前文件做安全备份：target → target.pre_restore.<ts>.bak.

    Returns:
        bak Path；target 不存在则返回 None
    """
    if not target.exists():
        return None
    ts = datetime.now().strftime(TIMESTAMP_FMT)
    bak = target.with_name(f"{target.name}.pre_restore.{ts}.bak")
    shutil.copy2(target, bak)
    return bak


def cmd_restore(args: argparse.Namespace) -> int:
    """从指定备份恢复."""
    if not args.file or not args.version or not args.source:
        print(
            "[ERROR] restore 子命令需要 --file / --version / --source 全部指定",
            file=sys.stderr,
        )
        return 1

    basename = FILE_ALIAS_MAP.get(args.file)
    if basename is None:
        print(
            f"[ERROR] 不识别的 --file 参数: {args.file}。"
            f"可选: keys.json / secrets.toml / config.toml",
            file=sys.stderr,
        )
        return 1

    dirs = _load_backup_dirs()
    backup_dir = dirs.get(args.source)
    if backup_dir is None:
        print(
            f"[ERROR] --source={args.source} 未在 config.toml [secret_backup] 中配置",
            file=sys.stderr,
        )
        return 3

    if not backup_dir.exists():
        print(
            f"[ERROR] 备份目录不存在: {backup_dir}",
            file=sys.stderr,
        )
        return 1

    # 查找指定版本
    backups = _list_backups_in_dir(backup_dir, basename)
    target_backup: Path | None = None
    for b in backups:
        if b["timestamp"] == args.version:
            target_backup = Path(b["path"])
            break
    if target_backup is None or not target_backup.exists():
        print(
            f"[ERROR] 找不到备份: basename={basename}, "
            f"version={args.version}, source={args.source}",
            file=sys.stderr,
        )
        print(
            f"  运行 `python tools/restore_secrets.py list --file {args.file}` "
            f"查看可用版本",
            file=sys.stderr,
        )
        return 1

    # 目标文件路径
    src_fn = BASENAME_TO_SOURCE[basename]
    target = src_fn()

    print(f"[Restore] 备份源: {target_backup}")
    print(f"[Restore] 恢复目标: {target}")
    print(f"  备份时间: {datetime.strptime(args.version, TIMESTAMP_FMT).isoformat()}")
    print(f"  备份大小: {_format_size(target_backup.stat().st_size)}")
    if target.exists():
        print(f"  当前文件大小: {_format_size(target.stat().st_size)}")
    else:
        print("  当前文件不存在（恢复即新建）")

    if args.dry_run:
        print("\n[DRY-RUN] 不会实际写入。去掉 --dry-run 执行恢复。")
        return 0

    # 交互确认
    if not args.yes:
        print()
        confirm = input(
            f"确认恢复？这将覆盖 {target}（当前内容会先备份到 .pre_restore.bak）[y/N]: "
        )
        if confirm.lower() not in ("y", "yes"):
            print("已取消。")
            return 0

    # 恢复前先备份当前状态
    pre_bak = _make_pre_restore_bak(target)
    if pre_bak:
        print(f"  [SAFE] 当前文件已备份到: {pre_bak}")

    # 原子拷贝
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".restore_tmp")
    try:
        shutil.copy2(target_backup, tmp)
        os.replace(tmp, target)
    except OSError as e:
        print(f"[FAIL] 恢复失败: {e}", file=sys.stderr)
        return 2

    print(f"\n[OK] 已恢复 {target}")
    print(f"  来源: {target_backup}")
    if pre_bak:
        print(f"  撤销恢复: cp {pre_bak} {target}（或手动覆盖）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="密钥恢复工具（从备份恢复 keys.json / secrets.toml / config.toml）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    # 默认子命令 = list
    list_parser = sub.add_parser(
        "list", help="列出可用备份（默认子命令）"
    )
    list_parser.add_argument(
        "--file",
        help="只列出指定文件（keys.json / secrets.toml / config.toml）",
    )
    list_parser.add_argument(
        "--json", action="store_true", help="输出 JSON（agent 用）"
    )

    restore_parser = sub.add_parser("restore", help="从备份恢复")
    restore_parser.add_argument(
        "--file", required=True,
        help="恢复的目标文件（keys.json / secrets.toml / config.toml）",
    )
    restore_parser.add_argument(
        "--version", required=True,
        help="备份版本时间戳（YYYYMMDD_HHMMSS，如 20260818_143000）",
    )
    restore_parser.add_argument(
        "--source", required=True, choices=["internal", "external"],
        help="备份来源位置",
    )
    restore_parser.add_argument(
        "--dry-run", action="store_true",
        help="只显示会做什么，不实际恢复",
    )
    restore_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过交互确认（agent 用）",
    )

    args = parser.parse_args()

    # 默认走 list
    if args.cmd is None or args.cmd == "list":
        if args.cmd is None:
            # 没传子命令时，构造 list 默认参数
            args.cmd = "list"
            args.file = None
            args.json = False
        return cmd_list(args)
    elif args.cmd == "restore":
        return cmd_restore(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
