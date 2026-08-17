"""
账单转换脚本 v4：读取微信/支付宝账单文件，按映射表转换为Obsidian记账格式

使用方式：
  uv run python workspace/accounting/bill_converter.py              # 分析+生成核对文档
  uv run python workspace/accounting/bill_converter.py --apply       # 应用修改+写入md+git commit

核心规则：
  - 追加模式：不覆盖已有记账数据，只追加新条目
  - 整数金额不加小数点（5.0 → 5）
  - 名称中不允许出现 + 或 - 符号
  - 同一大类允许收入/支出同时存在，导出时使用正负号表达流水

核对文档格式：
  时间 | 映射名-大类 | 金额 标记
  标记: ✓=自动映射  ?=未映射(必填)  !=需确认(映射表要求)
  修改: 行末 >> 后写 名称/类别  (如 >> 宿舍聚餐/娱乐)
  只改名称: >> 新名/   只改类别: >> /新类
"""

import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import openpyxl

# === 路径配置 ===
PROJECT_DIR = Path(r"F:\<project_root>")
INPUT_DIR = PROJECT_DIR / "workspace" / "accounting" / "账单"
# bill_review*.txt/json 是含个人消费记录的私有产出，挪到 private_vault/accounting/（obsidian vault，不进 release）
OUTPUT_DIR = PROJECT_DIR / "private_vault" / "accounting"
MAPPING_FILE = PROJECT_DIR / "workspace" / "accounting" / "name_mapping.json"
ACCOUNTING_DIR = Path(r"E:\<data_drive>:\<system_data_root>\Obsidian\Task\Task\main\记账")

REVIEW_FILE = "bill_review.txt"
REVIEW_FILLED_FILE = "bill_review_filled.txt"
REVIEW_JSON_FILE = "bill_review_data.json"


def get_cutoff_from_md(md_path: Path) -> datetime:
    """从记账md文件中读取截止时间，用于确定追加起点

    格式：'截止到 2026.5.22 17:41'
    """
    if not md_path.exists():
        return datetime(2000, 1, 1)
    content = md_path.read_text(encoding="utf-8")
    match = re.search(r"截止到\s+(\d{4})\.(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})", content)
    if match:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                        int(match.group(4)), int(match.group(5)))
    return datetime(2000, 1, 1)


def fmt_amount(amount: float) -> str:
    """格式化金额：整数不加小数点"""
    if amount == int(amount):
        return str(int(amount))
    return str(amount)


def determine_sector(direction: str, is_skip: bool) -> str:
    """确定板块：进/出/理财"""
    if is_skip:
        return "理财"
    return "进" if direction == "收入" else "出"


def sanitize_name(name: str) -> str:
    """清理名称：移除+和-符号，避免干扰记账格式"""
    return name.replace("+", "").replace("-", "")


def normalize_category(category: str) -> str:
    """兼容旧映射：退款/待对冲后缀不再创建独立大类。"""
    value = (category or "").strip()
    for suffix in ("待对冲", "退款"):
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def load_mapping():
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def read_wechat_xlsx(filepath: Path) -> list[dict]:
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0] == "交易时间":
            header_idx = i
            break
    if header_idx is None:
        print(f"错误：未找到表头行 in {filepath.name}")
        return []

    headers = rows[header_idx]
    records = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0]:
            continue
        record = {}
        for j, h in enumerate(headers):
            if h and j < len(row):
                record[h] = row[j]

        dt = record.get("交易时间")
        if isinstance(dt, datetime):
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            dt_obj = dt
        elif dt:
            time_str = str(dt)
            dt_obj = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        else:
            continue

        records.append({
            "source": "微信",
            "time": time_str,
            "dt_obj": dt_obj,
            "trade_type": str(record.get("交易类型", "")).strip(),
            "counterparty": str(record.get("交易对方", "")).strip(),
            "product": str(record.get("商品", "")).strip(),
            "direction": str(record.get("收/支", "")).strip(),
            "amount": float(record.get("金额(元)", 0) or 0),
            "status": str(record.get("当前状态", "")).strip(),
        })

    return records


def read_alipay_csv(filepath: Path) -> list[dict]:
    for encoding in ["gbk", "utf-8-sig", "utf-8"]:
        try:
            with open(filepath, "r", encoding=encoding) as f:
                lines = f.readlines()
            break
        except UnicodeDecodeError:
            continue
    else:
        print(f"错误：无法解码 {filepath.name}")
        return []

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


def build_map_key(rec: dict) -> str:
    """构建映射查找key，群收款/转账按方向区分"""
    if rec["trade_type"] in ["群收款", "转账"]:
        return f"{rec['trade_type']}-{rec['direction']}-{rec['counterparty']}"
    return f"{rec['trade_type']}-{rec['counterparty']}"


def classify_record(rec: dict, mapping: dict) -> dict:
    """分类单条记录

    返回 mark 字段:
      ✓ = 自动映射（无需操作）
      ? = 未映射（必须填写）
      ! = 需确认（可变映射，映射表要求确认）
    """
    map_key = build_map_key(rec)
    direction = rec["direction"]
    product = rec["product"]

    # 跳过项
    for skip_pat in mapping.get("skip_patterns", []):
        if skip_pat in map_key or skip_pat in rec["trade_type"]:
            return {"name": None, "category": "skip", "need_confirm": False,
                    "confirm_reason": "", "mark": "⊘"}

    # 固定映射
    if map_key in mapping["fixed"]:
        info = mapping["fixed"][map_key]
        return {"name": info["name"], "category": normalize_category(info["category"]), "need_confirm": False,
                "confirm_reason": "", "mark": "✓"}

    # 可变映射
    if map_key in mapping["variable"]:
        var_info = mapping["variable"][map_key]
        if "rules" in var_info:
            for rule in var_info["rules"]:
                if rule["pattern"] in product:
                    need = rule.get("need_confirm", True)
                    return {
                        "name": rule["default_name"],
                        "category": normalize_category(rule["default_category"]),
                        "need_confirm": need,
                        "confirm_reason": var_info.get("hint", ""),
                        "mark": "!" if need else "✓",
                    }
        need = var_info.get("need_confirm", True)
        return {
            "name": var_info.get("default_name", rec["counterparty"]),
            "category": normalize_category(var_info.get("default_category", "吃")),
            "need_confirm": need,
            "confirm_reason": var_info.get("hint", ""),
            "mark": "!" if need else "✓",
        }

    # 群收款/转账 - 未在映射表中的
    if rec["trade_type"] == "群收款":
        if direction == "支出":
            return {"name": f"群收款{rec['counterparty']}", "category": "未知", "need_confirm": True,
                    "confirm_reason": f"群收款支出给{rec['counterparty']}", "mark": "?"}
        else:
            return {"name": f"群收款{rec['counterparty']}", "category": "宿舍共享", "need_confirm": True,
                    "confirm_reason": f"群收款来自{rec['counterparty']}", "mark": "?"}

    if rec["trade_type"] == "转账":
        if direction == "支出":
            return {"name": f"转账{rec['counterparty']}", "category": "未知", "need_confirm": True,
                    "confirm_reason": f"转账给{rec['counterparty']}", "mark": "?"}
        else:
            return {"name": f"转账{rec['counterparty']}", "category": "宿舍共享", "need_confirm": True,
                    "confirm_reason": f"转账来自{rec['counterparty']}", "mark": "?"}

    # 未映射
    return {
        "name": rec["counterparty"],
        "category": "未知",
        "need_confirm": True,
        "confirm_reason": f"未映射: {map_key}",
        "mark": "?",
    }


def process_records(records: list[dict], mapping: dict, cutoffs: dict = None
                    ) -> tuple[dict, list[dict], list[dict]]:
    """处理记录

    返回 (classified, need_confirm, all_review_items)
    - classified: {month_key: {category: [entries]}}
    - need_confirm: 需确认条目列表
    - all_review_items: 所有条目的核对视图（含跳过项）
    """
    classified = defaultdict(lambda: defaultdict(list))
    need_confirm = []
    all_review_items = []

    for rec in records:
        # 检查是否在截止时间之前（已有记录，跳过）
        if cutoffs:
            month_key = f"{rec['dt_obj'].year}.{rec['dt_obj'].month}"
            if month_key in cutoffs and rec["dt_obj"] <= cutoffs[month_key]:
                continue

        result = classify_record(rec, mapping)
        month_key = f"{rec['dt_obj'].year}.{rec['dt_obj'].month}"
        direction = rec["direction"]

        # 跳过项：记录到 review_items 但不计入 classified
        if result["category"] == "skip":
            # 跳过项用 trade_type 作为显示名（如"零钱通转出-到零钱"），counterparty 可能是 /
            skip_name = rec["trade_type"]
            all_review_items.append({
                "source": rec["source"],
                "time": rec["time"],
                "name": skip_name,
                "category": "跳过",
                "amount": rec["amount"],
                "direction": direction,
                "mark": "⊘",
                "counterparty": rec["counterparty"],
                "product": rec["product"],
                "trade_type": rec["trade_type"],
                "confirm_reason": "",
                "is_skip": True,
                "is_refund": False,
                "map_key": build_map_key(rec),
                "month_key": month_key,
            })
            continue

        category = result["category"]

        # 不再为退款/AA 回流创建伪大类；方向本身表达正负流水。
        is_refund = "退款" in rec.get("trade_type", "")

        entry = {
            "name": sanitize_name(result["name"]),
            "amount": rec["amount"],
            "time": rec["time"],
            "direction": direction,
            "counterparty": rec["counterparty"],
            "product": rec["product"],
            "map_key": build_map_key(rec),
            "trade_type": rec["trade_type"],
        }

        classified[month_key][category].append(entry)

        # 核对视图条目
        review_item = {
            "source": rec["source"],
            "time": rec["time"],
            "name": sanitize_name(result["name"]),
            "category": category,
            "amount": rec["amount"],
            "direction": direction,
            "mark": result["mark"],
            "counterparty": rec["counterparty"],
            "product": rec["product"],
            "trade_type": rec["trade_type"],
            "confirm_reason": result.get("confirm_reason", ""),
            "is_skip": False,
            "is_refund": is_refund,
            "map_key": build_map_key(rec),
            "month_key": month_key,
            "original_mark": result["mark"],  # 记住原始标记，用于判断是否写入fixed
        }
        all_review_items.append(review_item)

        if result["need_confirm"]:
            need_confirm.append({
                **entry,
                "confirm_reason": result["confirm_reason"],
                "suggested_name": result["name"],
                "suggested_category": category,
                "source": rec["source"],
                "mark": result["mark"],
            })

    return dict(classified), need_confirm, all_review_items


def format_review_item(item: dict) -> str:
    """格式化单条核对条目"""
    amt = fmt_amount(item["amount"])
    if item["direction"] == "收入":
        amt = f"+{amt}"

    refund_tag = " [退]" if item.get("is_refund") else ""

    line = f"{item['time'][:16]} | {item['name']}-{item['category']} | {amt}{refund_tag} {item['mark']}"

    # ? 和 ! 条目：显示原始对方·商品说明 + 修改提示
    if item["mark"] in ("?", "!"):
        context_parts = []
        if item["counterparty"]:
            context_parts.append(item["counterparty"])
        if item["product"]:
            context_parts.append(item["product"][:30])
        context = "·".join(context_parts)
        if context:
            line += f"  [{context}]"
        # ! 条目额外显示映射表提示
        if item["mark"] == "!" and item.get("confirm_reason"):
            line += f"  {item['confirm_reason']}"
        line += "  >>"

    return line


def generate_review_document(all_review_items: list[dict], output_path: Path):
    """生成核对文档：按来源分组，从新到旧展示所有映射"""
    wechat_items = [i for i in all_review_items if i["source"] == "微信" and not i.get("is_skip")]
    alipay_items = [i for i in all_review_items if i["source"] == "支付宝" and not i.get("is_skip")]
    skip_items = [i for i in all_review_items if i.get("is_skip")]

    # 从新到旧排序
    wechat_items.sort(key=lambda x: x["time"], reverse=True)
    alipay_items.sort(key=lambda x: x["time"], reverse=True)
    skip_items.sort(key=lambda x: x["time"], reverse=True)

    # 统计
    def count_marks(items):
        auto = sum(1 for i in items if i["mark"] == "✓")
        confirm = sum(1 for i in items if i["mark"] == "!")
        unmapped = sum(1 for i in items if i["mark"] == "?")
        return auto, confirm, unmapped

    auto_w, confirm_w, unmapped_w = count_marks(wechat_items)
    auto_a, confirm_a, unmapped_a = count_marks(alipay_items)

    lines = [
        "# 账单核对",
        "# ✓ 自动映射  ? 未映射(必填)  ! 需确认(映射表要求)  [退] 退款",
        "# 修改: 行末 >> 后写 名称/类别  (如 >> 宿舍聚餐/娱乐)",
        "# 只改名称: >> 新名/   只改类别: >> /新类",
        "# ✓ 条目如需修改，在行末加 >> 名称/类别",
        "",
        f"# 微信: {auto_w}条自动 | {confirm_w}条需确认 | {unmapped_w}条未映射 | {len(skip_items)}条跳过",
        f"# 支付宝: {auto_a}条自动 | {confirm_a}条需确认 | {unmapped_a}条未映射",
        "",
    ]

    if wechat_items:
        lines.append("=== 微信 ===")
        lines.append("")
        for item in wechat_items:
            lines.append(format_review_item(item))
        lines.append("")

    if alipay_items:
        lines.append("=== 支付宝 ===")
        lines.append("")
        for item in alipay_items:
            lines.append(format_review_item(item))
        lines.append("")

    if skip_items:
        lines.append("--- 跳过(资金流转，不计入) ---")
        lines.append("")
        for item in skip_items:
            amt = fmt_amount(item["amount"])
            lines.append(f"{item['time'][:16]} | {item['name']} | {amt}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    total_action = confirm_w + confirm_a + unmapped_w + unmapped_a
    print(f"\n核对文档已写入: {output_path}")
    print(f"共 {len(wechat_items) + len(alipay_items)} 条记录，{total_action} 条需要处理")


def generate_review_json(all_review_items: list[dict], classified: dict,
                         mapping: dict, cutoffs: dict) -> Path:
    """生成JSON格式的审核数据，供Web可视化页面使用"""
    items = []
    for idx, item in enumerate(all_review_items):
        items.append({
            "id": idx,
            "source": item["source"],
            "time": item["time"],
            "original_name": item.get("counterparty", ""),
            "product": item.get("product", ""),
            "amount": item["amount"],
            "direction": item["direction"],
            "sector": determine_sector(item["direction"], item.get("is_skip", False)),
            "category": item["category"],
            "description": item["name"],
            "mark": item["mark"],
            "map_key": item.get("map_key", ""),
            "trade_type": item.get("trade_type", ""),
            "counterparty": item.get("counterparty", ""),
            "is_refund": item.get("is_refund", False),
            "is_skip": item.get("is_skip", False),
            "month_key": item.get("month_key", ""),
            "confirm_reason": item.get("confirm_reason", ""),
        })

    data = {
        "generated_at": datetime.now().isoformat(),
        "cutoffs": {k: v.isoformat() for k, v in cutoffs.items()} if cutoffs else {},
        "mapping": mapping,
        "items": items,
        "classified": classified,
    }

    output_path = OUTPUT_DIR / REVIEW_JSON_FILE
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"JSON审核数据已写入: {output_path}")
    return output_path


def read_review_corrections(filepath: Path) -> list[dict]:
    """读取用户填写的核对文档，提取修改项

    解析规则:
    - 追踪 === 微信 === / === 支付宝 === 分区确定来源
    - 查找行末 >> 后的内容，格式: 名称/类别
    - 通过行首时间+金额+来源匹配到具体记录
    """
    if not filepath.exists():
        return []

    corrections = []
    current_source = ""

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line_stripped = line.strip()

            # 追踪来源分区
            if line_stripped == "=== 微信 ===":
                current_source = "微信"
                continue
            elif line_stripped == "=== 支付宝 ===":
                current_source = "支付宝"
                continue

            # 跳过注释和空行
            if line_stripped.startswith("#") or not line_stripped:
                continue

            # 查找 >> 修改标记
            if ">>" not in line_stripped:
                continue

            correction_part = line_stripped.split(">>")[-1].strip()
            if not correction_part:
                continue

            # 解析 名称/类别
            if "/" in correction_part:
                parts = correction_part.split("/", 1)
                name = parts[0].strip()
                category = parts[1].strip()
            else:
                # 没有 / 则视为只改名称
                name = correction_part.strip()
                category = ""

            if not name and not category:
                continue

            # 提取时间（行首 yyyy-MM-dd HH:mm）
            time_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", line_stripped)
            time_str = time_match.group(1) if time_match else ""

            # 提取金额和方向（第三个 | 分隔段的首个数值）
            pipe_parts = line_stripped.split("|")
            amount = 0.0
            direction = "支出"
            if len(pipe_parts) >= 3:
                amount_part = pipe_parts[2].strip().split()[0]
                if amount_part.startswith("+"):
                    amount_part = amount_part[1:]
                    direction = "收入"
                try:
                    amount = float(amount_part)
                except ValueError:
                    pass

            corrections.append({
                "time": time_str,
                "amount": amount,
                "direction": direction,
                "source": current_source,
                "name": sanitize_name(name) if name else "",
                "category": category,
            })

    return corrections


def format_entry_part(item: dict, *, offset_income: bool = False) -> str:
    """格式化单条记账条目"""
    amount_str = fmt_amount(item["amount"])
    name = item["name"]
    direction = item.get("direction", "支出")
    if direction == "收入":
        return f"{'-' if offset_income else '+'}{amount_str}{name}"
    return f"{amount_str}{name}"


def format_category_line(items: list[dict]) -> str:
    """支出类别行: 金额1名称1+金额2名称2+..."""
    if not items:
        return ""
    return "+".join(format_entry_part(item, offset_income=True) for item in items)


def format_income_line(items: list[dict]) -> str:
    """收入类别行: +金额1名称1+金额2名称2+..."""
    if not items:
        return ""
    return "".join(
        format_entry_part(item) if item.get("direction") == "收入"
        else f"-{fmt_amount(item['amount'])}{item['name']}"
        for item in items
    )


def parse_existing_line(line: str) -> str:
    """解析已有的记账行内容（###### 类别：后面的部分）"""
    match = re.match(r"######\s+\S+：(.*)$", line)
    if match:
        return match.group(1).strip()
    return ""


def _ensure_category_line(content: str, section_title: str, category: str) -> str:
    """确保指定章节下存在大类标题。"""
    pattern = rf"^###### {re.escape(category)}："
    if re.search(pattern, content, flags=re.MULTILINE):
        return content
    section_match = re.search(
        rf"^## {re.escape(section_title)}\s*$", content, flags=re.MULTILINE
    )
    if not section_match:
        suffix = "" if content.endswith("\n") else "\n"
        return f"{content}{suffix}\n## {section_title}\n###### {category}：\n"
    next_section = re.search(r"^##\s+", content[section_match.end():], flags=re.MULTILINE)
    insert_at = (
        section_match.end() + next_section.start()
        if next_section else len(content)
    )
    prefix = content[:insert_at].rstrip()
    suffix = content[insert_at:].lstrip("\n")
    return f"{prefix}\n###### {category}：\n\n{suffix}"


def update_md_file(
    month_key: str,
    classified: dict,
    md_path: Path,
    category_sections: dict[str, list[str]] | None = None,
):
    """更新记账md文件 - 追加模式，保留已有数据"""
    if not md_path.exists():
        print(f"文件不存在: {md_path}")
        return

    content = md_path.read_text(encoding="utf-8")
    month_data = classified.get(month_key, {})

    sections = category_sections or {
        "出": ["吃", "学习资料", "娱乐", "生活", "交通", "电动车"],
        "进": ["商业副业", "活动返现", "宿舍共享", "妈妈", "工资"],
        "理财": [],
    }
    category_sector = {
        category: sector
        for sector, categories in sections.items()
        for category in categories
    }
    for category, items in month_data.items():
        if category not in category_sector:
            stored_sector = next(
                (
                    item.get("sector") for item in items
                    if item.get("sector") in {"出", "进", "理财"}
                ),
                None,
            )
            category_sector[category] = stored_sector or (
                "进" if items and all(item.get("direction") == "收入" for item in items)
                else "出"
            )

    # 更新截止时间：使用该月最后一条记录的时间
    month_items = []
    for cat_items in month_data.values():
        month_items.extend(cat_items)
    if month_items:
        last_time = max(item["time"] for item in month_items)
        last_dt = datetime.strptime(last_time[:16], "%Y-%m-%d %H:%M")
        new_timestamp = f"截止到 {last_dt.strftime('%Y.%m.%d %H:%M')}"
    else:
        new_timestamp = None
    if new_timestamp:
        content = re.sub(r"^截止到\s.*$", new_timestamp, content, flags=re.MULTILINE)

    section_names = {"出": "支出", "进": "收入", "理财": "投资"}
    ordered_categories = []
    for sector in ("出", "进", "理财"):
        ordered_categories.extend(
            category for category in sections.get(sector, [])
            if category not in ordered_categories
        )
    ordered_categories.extend(
        category for category in month_data if category not in ordered_categories
    )

    for cat in ordered_categories:
        items = month_data.get(cat, [])
        if not items:
            continue
        sector = category_sector.get(cat, "出")
        section_title = section_names.get(sector, "支出")
        content = _ensure_category_line(content, section_title, cat)
        new_content = (
            format_category_line(items)
            if sector == "出" else format_income_line(items)
        )
        pattern = rf"(###### {re.escape(cat)}：)(.*)"
        match = re.search(pattern, content)
        if match:
            existing = match.group(2).strip()
            if existing and new_content:
                joiner = "+" if sector == "出" else ""
                combined = existing + joiner + new_content
            elif new_content:
                combined = new_content
            else:
                combined = existing
            content = re.sub(pattern, f"###### {cat}：{combined}", content)

    md_path.write_text(content, encoding="utf-8")
    print(f"已更新: {md_path}")


def git_commit(month_keys: list[str]):
    for mk in month_keys:
        fname = f"{mk.replace('.', '-')}.md"
        subprocess.run(["git", "-C", str(ACCOUNTING_DIR), "add", fname], check=True)
    msg = f"更新{'、'.join(month_keys)}账单"
    subprocess.run(["git", "-C", str(ACCOUNTING_DIR), "commit", "-m", msg], check=True)
    print(f"Git commit 成功: {msg}")


def main():
    apply_mode = "--apply" in sys.argv
    mapping = load_mapping()

    # 1. 读取所有账单文件
    all_records = []
    for f in INPUT_DIR.iterdir():
        if f.suffix == ".xlsx" and "微信" in f.name:
            records = read_wechat_xlsx(f)
            print(f"微信账单: {len(records)} 条记录")
            all_records.extend(records)
        elif f.suffix == ".csv" and "支付宝" in f.name:
            records = read_alipay_csv(f)
            print(f"支付宝账单: {len(records)} 条记录")
            all_records.extend(records)

    if not all_records:
        print("未找到账单文件")
        return

    # 2. 从记账文件读取各月截止时间
    cutoffs = {}
    for f in ACCOUNTING_DIR.iterdir():
        if f.suffix == ".md" and f.stem.replace("-", "").isdigit():
            parts = f.stem.split("-")
            if len(parts) == 2:
                month_key = f"{parts[0]}.{parts[1]}"
                cutoff_dt = get_cutoff_from_md(f)
                if cutoff_dt.year > 2000:
                    cutoffs[month_key] = cutoff_dt
                    print(f"  {month_key} 截止时间: {cutoff_dt.strftime('%Y.%m.%d %H:%M')}")

    # 3. 分类映射
    classified, need_confirm, all_review_items = process_records(all_records, mapping, cutoffs)

    # 4. apply模式：读取修改结果
    if apply_mode:
        filled_path = OUTPUT_DIR / REVIEW_FILLED_FILE
        corrections = read_review_corrections(filled_path)
        if corrections:
            print(f"已读取 {len(corrections)} 条用户修改")

            for correction in corrections:
                # 在 all_review_items 中找到匹配的条目
                matched_item = None
                for item in all_review_items:
                    if (item["time"].startswith(correction["time"]) and
                        abs(item["amount"] - correction["amount"]) < 0.01 and
                        item["source"] == correction["source"] and
                        not item.get("is_skip")):
                        matched_item = item
                        break

                if not matched_item:
                    print(f"  警告: 未找到匹配记录 {correction['time']} {correction['amount']}")
                    continue

                new_name = correction["name"] or matched_item["name"]
                new_category = correction["category"] or matched_item["category"]
                old_category = matched_item["category"]
                month_key = matched_item["month_key"]

                # 如果名称或类别有变化
                if new_name != matched_item["name"] or new_category != old_category:
                    # 从旧类别移除
                    if month_key in classified and old_category in classified[month_key]:
                        classified[month_key][old_category] = [
                            e for e in classified[month_key][old_category]
                            if not (e["time"] == matched_item["time"] and
                                   abs(e["amount"] - matched_item["amount"]) < 0.01)
                        ]

                    # 添加到新类别
                    classified[month_key][new_category].append({
                        "name": sanitize_name(new_name),
                        "amount": matched_item["amount"],
                        "time": matched_item["time"],
                        "direction": matched_item["direction"],
                        "counterparty": matched_item["counterparty"],
                        "product": matched_item["product"],
                        "map_key": matched_item["map_key"],
                        "trade_type": matched_item["trade_type"],
                    })

                    # 更新映射表：
                    # ? (未映射) → 写入 fixed，下次自动匹配
                    # ✓ (自动映射被修正) → 更新 fixed
                    # ! (可变映射) → 不写入 fixed，因为每次内容不同
                    if matched_item.get("original_mark") != "!":
                        mapping["fixed"][matched_item["map_key"]] = {
                            "name": sanitize_name(new_name),
                            "category": new_category,
                        }
                        print(f"  映射表更新: {matched_item['map_key']} → {sanitize_name(new_name)}/{new_category}")
                    else:
                        print(f"  可变映射不写入fixed: {matched_item['map_key']}")

            # 保存映射表
            with open(MAPPING_FILE, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)

    # 5. 输出统计
    print(f"\n=== 分类统计 ===")
    for month_key in sorted(classified.keys()):
        print(f"\n--- {month_key} ---")
        for cat in sorted(classified[month_key].keys()):
            items = classified[month_key][cat]
            total = sum(item["amount"] for item in items)
            print(f"  {cat}: {len(items)}笔, 合计 {fmt_amount(total)}")

    # 6. 非apply模式：生成核对文档 + JSON + 预览
    if not apply_mode:
        review_path = OUTPUT_DIR / REVIEW_FILE
        generate_review_document(all_review_items, review_path)
        generate_review_json(all_review_items, classified, mapping, cutoffs)

        print(f"\n=== 记账预览 ===")
        for month_key in sorted(classified.keys()):
            print(f"\n--- {month_key} ---")
            month_data = classified[month_key]
            for cat in ["吃", "学习资料", "娱乐", "生活", "交通", "电动车"]:
                line = format_category_line(month_data.get(cat, []))
                print(f"###### {cat}：{line}")
            for cat in ["商业副业", "活动返现", "宿舍共享", "妈妈", "工资"]:
                line = format_income_line(month_data.get(cat, []))
                if line or cat in ["妈妈", "工资"]:
                    print(f"###### {cat}：{line}")

        print(f"\n请核对 {review_path} 后保存为 {REVIEW_FILLED_FILE}")
        print("然后运行: uv run python workspace/accounting/bill_converter.py --apply")

    # 7. apply模式：写入md + git commit
    if apply_mode:
        for month_key in sorted(classified.keys()):
            md_path = ACCOUNTING_DIR / f"{month_key.replace('.', '-')}.md"
            update_md_file(month_key, classified, md_path)
        git_commit(sorted(classified.keys()))


if __name__ == "__main__":
    main()
