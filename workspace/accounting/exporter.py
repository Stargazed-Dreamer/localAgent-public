"""导出层 — SQLite 已确认交易 → Obsidian md

复用 bill_converter.py 的通用格式化函数和 update_md_file，保证与 Obsidian 格式兼容。

模式：
  - append（默认）：追加到已有 md，复用 update_md_file 逻辑
  - rewrite：从 SQLite 重写整月（v1.1 实现，历史回灌用）
"""

import subprocess
from pathlib import Path

from workspace.accounting.bill_converter import (
    update_md_file,
)


def _tx_to_entry(tx: dict) -> dict:
    """SQLite transaction 行 → bill_converter 兼容的 entry dict

    format_entry_part 只用 name/amount/time/direction 四个字段。
    """
    return {
        "name": tx["description"],
        "amount": tx["amount"],
        "time": tx["trade_time"],
        "direction": tx["direction"],
        "sector": tx.get("sector", ""),
    }


def build_classified_from_db(store, month_key: str) -> dict:
    """从 SQLite 读该月已确认交易 → 构造 classified dict

    返回格式：{month_key: {category: [entries]}}
    与 bill_converter.process_records 返回的 classified 结构一致。

    跳过项（is_skip=1）不计入导出。
    """
    confirmed = store.get_month_confirmed(month_key)
    classified: dict = {month_key: {}}
    for tx in confirmed:
        cat = tx["category"] or "未知"
        if cat not in classified[month_key]:
            classified[month_key][cat] = []
        classified[month_key][cat].append(_tx_to_entry(tx))
    return classified


def export_month(month_key: str, store, md_dir: Path, mode: str = "append") -> Path:
    """导出单月到 Obsidian md

    Args:
        month_key: 月份键，如 "2026.5"
        store: AccountingStore 实例
        md_dir: Obsidian 记账目录
        mode: 'append'（追加到已有 md）/ 'rewrite'（重写整月，v1.1）

    Returns:
        写入的 md 文件路径

    Raises:
        FileNotFoundError: md 文件不存在（v1 只支持追加已有 md）
        NotImplementedError: rewrite 模式（v1.1 实现）
    """
    md_path = md_dir / f"{month_key.replace('.', '-')}.md"
    if not md_path.exists():
        raise FileNotFoundError(
            f"Obsidian md 不存在: {md_path}（历史回灌 v1.1 支持）"
        )

    classified = build_classified_from_db(store, month_key)

    if mode == "append":
        # 复用 bill_converter.update_md_file 的追加逻辑
        update_md_file(
            month_key,
            classified,
            md_path,
            category_sections=store.get_config().get("sectors", {}),
        )
    elif mode == "rewrite":
        raise NotImplementedError("rewrite 模式 v1.1 实现")
    else:
        raise ValueError(f"未知模式: {mode}")

    return md_path


def export_month_range(
    start_month: str, end_month: str, store, md_dir: Path
) -> list[Path]:
    """导出月份范围

    Args:
        start_month: 起始月份 "2026.4"
        end_month: 结束月份 "2026.7"
        store: AccountingStore 实例
        md_dir: Obsidian 记账目录

    Returns:
        成功导出的 md 路径列表（md 不存在的月份跳过）
    """
    exported = []
    start_year, start_m = map(int, start_month.split("."))
    end_year, end_m = map(int, end_month.split("."))
    y, m = start_year, start_m
    while (y, m) <= (end_year, end_m):
        month_key = f"{y}.{m}"
        try:
            path = export_month(month_key, store, md_dir)
            exported.append(path)
        except FileNotFoundError as e:
            print(f"跳过 {month_key}: {e}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return exported


def git_commit(month_keys: list[str], md_dir: Path) -> None:
    """git commit 导出的 md 文件

    Args:
        month_keys: 月份键列表，如 ["2026.5", "2026.6"]
        md_dir: Obsidian 记账目录（git 仓库根）
    """
    for mk in month_keys:
        fname = f"{mk.replace('.', '-')}.md"
        subprocess.run(["git", "-C", str(md_dir), "add", fname], check=True)
    msg = f"更新{'、'.join(month_keys)}账单"
    subprocess.run(["git", "-C", str(md_dir), "commit", "-m", msg], check=True)
