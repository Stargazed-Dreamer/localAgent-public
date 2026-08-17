"""账单解析层 — 读取微信/支付宝账单文件 + 分类 + 导入到 SQLite

复用 bill_converter.py 的纯函数（read_wechat_xlsx/read_alipay_csv/build_map_key/
classify_record/determine_sector/sanitize_name），不修改原文件。

新增能力：
  - read_alipay_zip: 解压支付宝 zip（ZipCrypto）+ 解析 csv
  - read_bill_file: 按后缀分发（xlsx/csv/zip）
  - classify_and_prepare: 分类 + 补充 sector/month_key/is_skip/is_refund 等字段
  - import_bill_file: 完整导入流程 read → classify → upsert → 记录批次
"""

import csv
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from workspace.accounting.bill_converter import (
    build_map_key,
    classify_record,
    determine_sector,
    read_alipay_csv,
    read_wechat_xlsx,
    sanitize_name,
)


def _parse_alipay_csv_text(content: str) -> list[dict]:
    """从 csv 文本解析支付宝账单

    内联 read_alipay_csv 的解析逻辑（避免修改 bill_converter.py 原文件）。
    字段结构与 read_alipay_csv 返回值一致。
    """
    lines = content.splitlines(keepends=True)

    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("交易时间,"):
            header_idx = i
            break
    if header_idx is None:
        return []

    csv_content = "".join(lines[header_idx:])
    reader = csv.DictReader(csv_content.splitlines())
    records = []
    for row in reader:
        time_str = row.get("交易时间", "").strip()
        if not time_str:
            continue
        try:
            dt_obj = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        try:
            amount = float(row.get("金额", "0").strip())
        except ValueError:
            amount = 0

        records.append({
            "source": "支付宝",
            "time": time_str,
            "dt_obj": dt_obj,
            "trade_type": "商户消费",
            "counterparty": row.get("交易对方", "").strip(),
            "product": row.get("商品说明", "").strip(),
            "direction": row.get("收/支", "").strip(),
            "amount": amount,
            "status": "交易成功",
        })

    return records


def read_alipay_zip(zip_path: Path, password: str) -> list[dict]:
    """解压支付宝 zip（ZipCrypto）→ 解析 csv

    Python zipfile 原生支持 ZipCrypto（非 AES），无需 pyzipper。
    """
    pwd_bytes = password.encode("utf-8")
    with zipfile.ZipFile(zip_path, "r") as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return []
        # 支付宝 zip 内通常只有一个 csv
        with zf.open(csv_names[0], pwd=pwd_bytes) as f:
            raw = f.read()
        # 尝试多种编码（支付宝默认 gbk）
        for encoding in ("gbk", "utf-8-sig", "utf-8"):
            try:
                content = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = raw.decode("gbk", errors="replace")
        return _parse_alipay_csv_text(content)


def read_bill_file(path: Path, zip_password: str = None) -> list[dict]:
    """按后缀分发：xlsx→read_wechat_xlsx, csv→read_alipay_csv, zip→read_alipay_zip

    Args:
        path: 账单文件路径
        zip_password: 支付宝 zip 密码（zip 文件必填）

    Returns:
        records 列表，每条含 source/time/dt_obj/trade_type/counterparty/
        product/direction/amount/status 字段
    """
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return read_wechat_xlsx(path)
    elif suffix == ".csv":
        return read_alipay_csv(path)
    elif suffix == ".zip":
        if not zip_password:
            raise ValueError("支付宝 zip 需要密码")
        return read_alipay_zip(path, zip_password)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")


def classify_and_prepare(records: list[dict], mapping: dict) -> list[dict]:
    """分类 + 补充字段，返回 store.upsert_transactions 需要的格式

    复用 classify_record，但补充：
    - sector: determine_sector(direction, is_skip)
    - month_key: f"{dt.year}.{dt.month}"
    - is_skip: category == 'skip'
    - is_refund: '退款' in category or '退款' in trade_type
    - description: sanitize_name(name) 或 trade_type（跳过项）
    - map_key: build_map_key(rec)
    - 收入/支出方向保持原样：同一大类允许出现正负流水
    """
    prepared = []
    for rec in records:
        result = classify_record(rec, mapping)
        dt_obj = rec.get("dt_obj")
        if dt_obj is None:
            dt_obj = datetime.strptime(rec["time"], "%Y-%m-%d %H:%M:%S")
        month_key = f"{dt_obj.year}.{dt_obj.month}"
        direction = rec["direction"]

        is_skip = result["category"] == "skip"
        category = result["category"]

        is_refund = (
            "退款" in rec.get("trade_type", "")
            or "退款" in rec.get("status", "")
            or "已退款" in rec.get("status", "")
        )

        # 跳过项的 description 用 trade_type（如"零钱通转出-到零钱"）
        if is_skip:
            description = rec["trade_type"]
            mark = "⊘"
        else:
            description = sanitize_name(result["name"])
            mark = result["mark"]

        prepared.append({
            "source": rec["source"],
            "time": rec["time"],
            "trade_type": rec["trade_type"],
            "counterparty": rec["counterparty"],
            "product": rec["product"],
            "direction": direction,
            "amount": rec["amount"],
            "status": rec["status"],
            "map_key": build_map_key(rec),
            "sector": determine_sector(direction, is_skip),
            "category": category,
            "description": description,
            "mark": mark,
            "confirm_reason": result.get("confirm_reason", ""),
            "is_skip": is_skip,
            "is_refund": is_refund,
            "month_key": month_key,
        })
    return prepared


def import_bill_file(
    path: Path,
    store,
    zip_password: str = None,
    batch_id: str = None,
) -> dict:
    """完整导入流程：read → classify → upsert → 记录批次

    Args:
        path: 账单文件路径
        store: AccountingStore 实例
        zip_password: 支付宝 zip 密码
        batch_id: 可选批次 ID（不传则自动生成）

    Returns:
        {inserted, skipped, total, unmapped_count, batch_id}
    """
    records = read_bill_file(path, zip_password)
    mapping = store.get_mapping()
    prepared = classify_and_prepare(records, mapping)

    batch_id = batch_id or (
        f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    )
    for p in prepared:
        p["import_batch"] = batch_id

    inserted, skipped = store.upsert_transactions(prepared)

    # 每条交易都保留映射命中轨迹，便于以后诊断同一原始名称为何未命中。
    events = []
    for rec, item in zip(records, prepared, strict=False):
        map_key = item.get("map_key", "")
        matched_key = ""
        mapping_type = "unmapped"
        if map_key in mapping.get("fixed", {}):
            matched_key, mapping_type = map_key, "fixed"
        elif map_key in mapping.get("variable", {}):
            matched_key, mapping_type = map_key, "variable"
        else:
            for pattern in mapping.get("skip_patterns", []):
                if pattern in map_key or pattern in rec.get("trade_type", ""):
                    matched_key, mapping_type = pattern, "skip"
                    break
        if not matched_key and item.get("mark") == "?":
            mapping_type = "unmapped"
        elif not matched_key:
            mapping_type = "heuristic"
        events.append({
            "transaction_id": store._make_tx_id(
                rec["source"], rec["time"], rec["amount"], rec.get("counterparty", "")
            ),
            "map_key": map_key,
            "matched_key": matched_key,
            "mapping_type": mapping_type,
            "outcome": item.get("mark", "?"),
            "source": rec.get("source", ""),
            "trade_time": rec.get("time", ""),
            "details": item.get("confirm_reason", ""),
        })
    store.record_mapping_events(events)

    # 记录批次
    if prepared:
        times = [p["time"] for p in prepared]
        store.record_import_batch(
            batch_id=batch_id,
            source_file=str(path),
            item_count=len(prepared),
            date_range_start=min(times),
            date_range_end=max(times),
        )

    unmapped_count = sum(1 for p in prepared if p["mark"] == "?")

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total": len(prepared),
        "unmapped_count": unmapped_count,
        "batch_id": batch_id,
    }
