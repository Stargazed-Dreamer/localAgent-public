"""C2 log：log_consumption / read_consumption_log（Ticket 30）。

记录 agent 消费行为到 consumption.log（JSONL 格式），便于事后分析
VL 调用次数等。

设计要点：
- JSONL 格式（每行一个 JSON），追加写入，崩溃只丢最后一条
- 不记录 VL 返回内容（D005：不预存 VL 结果），只记录调用元信息
- 文件不存在时自动创建；录制包目录不存在时静默 no-op
- 读取时跳过损坏行（json.JSONDecodeError），不抛异常
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# consumption.log 文件名（放在录制包根目录下）
CONSUMPTION_LOG_FILENAME = "consumption.log"


def log_consumption(
    package_path: Path | str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """记录 agent 消费行为到 consumption.log（JSONL 追加）。

    Args:
        package_path: 录制包根目录
        action: 行为类型，常用值：
            - "vl_call": 调 VL 看图（details 含 block_id/question/frame）
            - "list_recordings": 列出录制包
            - "read_view": 读 merged view
            - "read_block": 读块详情
            - "select_vl_candidates": 选 VL 候选
        details: 行为详情 dict，结构随 action 变化；None 时为空 dict

    Note:
        - 不记录 VL 返回内容（D005），只记录调用元信息
        - 录制包目录不存在时静默 no-op（防御性，避免异常阻断 agent 流程）
    """
    package_path = Path(package_path)
    if not package_path.is_dir():
        return

    log_path = package_path / CONSUMPTION_LOG_FILENAME

    entry: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "action": action,
        "details": details or {},
    }

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_consumption_log(package_path: Path | str) -> list[dict[str, Any]]:
    """读消费日志。

    Args:
        package_path: 录制包根目录

    Returns:
        消费日志 list[dict]，每项 {timestamp, action, details}，按写入顺序。
        文件不存在时返回空 list；损坏行跳过（不抛异常）。
    """
    log_path = Path(package_path) / CONSUMPTION_LOG_FILENAME
    if not log_path.exists():
        return []

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 跳过损坏行

    return entries
