#!/usr/bin/env python3
"""明日方舟小黑盒数据导入脚本

将小黑盒导出的寻访记录增量合并到方舟数据库。

小黑盒数据结构：
{
  "info": {"uid": 227446513, "lang": "zh-cn", "export_time": "...", "export_app": "小黑盒"},
  "data": {
    "timestamp": {
      "c": [[name, rarity, isNew], ...],  // 十连/单抽记录
      "p": "卡池名称"
    },
    ...
  }
}

rarity: 2=★3, 3=★4, 4=★5, 5=★6
isNew: 0=非新, 1=新干员

用法：
    python workspace/arknights_gacha/arknights_import.py                          # 从 workspace/arknights_gacha/ 导入
    python workspace/arknights_gacha/arknights_import.py --file path/to/file.json  # 指定文件
    python workspace/arknights_gacha/arknights_import.py --dry-run                 # 只预览不写入
"""

import json
import argparse
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "workspace" / "arknights_gacha"
INPUT_DIR = PROJECT_ROOT / "workspace" / "arknights_gacha"

RARITY_MAP = {2: "★3", 3: "★4", 4: "★5", 5: "★6"}


def _atomic_write_json(path: Path, data) -> None:
    """先写完同目录临时文件，再原子替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    temp_path.replace(path)


def _record_key(record: dict) -> tuple:
    """pos 区分同秒同名的十连结果，同时兼容官网和小黑盒。"""
    return (
        int(float(record.get("timestamp", 0) or 0)),
        str(record.get("name", "")),
        str(record.get("pool", "")),
        int(record.get("pos", 0) or 0),
        int(record.get("rarity", 0) or 0),
        bool(record.get("is_new", False)),
    )


def _prefer_record(first: dict, second: dict) -> dict:
    """优先保留字段更完整的官网副本，并补齐缺失字段。"""
    def score(record: dict) -> tuple:
        source_score = 2 if record.get("source") == "official" else 1 if record.get("source") else 0
        populated = sum(value not in (None, "", [], {}) for value in record.values())
        return source_score, populated

    preferred, other = (first, second) if score(first) >= score(second) else (second, first)
    merged = dict(preferred)
    for key, value in other.items():
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def load_xhh_data(filepath: Path) -> list:
    """加载小黑盒导出数据，转换为统一格式"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    info = data.get("info", {})
    uid = info.get("uid", "")
    print(f"  UID: {uid}")
    print(f"  导出时间: {info.get('export_time', '')}")
    print(f"  导出应用: {info.get('export_app', '')}")

    records = []
    raw_data = data.get("data", {})

    for ts, val in raw_data.items():
        pool = val.get("p", "未知卡池")
        chars = val.get("c", [])

        for i, char in enumerate(chars):
            if len(char) >= 3:
                name, rarity, is_new = char[0], char[1], char[2]
            elif len(char) == 2:
                name, rarity = char[0], char[1]
                is_new = 0
            else:
                continue

            ts_int = int(ts)
            records.append({
                "timestamp": ts_int,
                "time": datetime.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M:%S"),
                "pool": pool,
                "name": name,
                "rarity": rarity,
                "rarity_label": RARITY_MAP.get(rarity, "?"),
                "is_new": bool(is_new),
                "pos": i,
                "source": "xhh",
            })

    # 按时间排序
    records.sort(key=lambda x: x["timestamp"], reverse=True)
    print(f"  记录数: {len(records)}")
    return records


def load_official_data(filepath: Path) -> list:
    """加载官网采集数据，转换为统一格式"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    records = []
    for rec in data:
        records.append({
            "timestamp": int(rec.get("timestamp", 0)),
            "time": rec.get("time", ""),
            "pool": rec.get("pool", ""),
            "name": rec.get("name", ""),
            "rarity": rec.get("rarity", 0),
            "rarity_label": rec.get("rarity_label", ""),
            "is_new": rec.get("is_new", False),
            "pos": rec.get("pos", 0),
            "source": rec.get("source", "official"),
            "poolId": rec.get("poolId", ""),
            "charId": rec.get("charId", ""),
        })

    return records


def merge_records(existing: list, new_records: list) -> list:
    """增量合并：按 (timestamp, name, pool, pos) 去重。"""
    deduped = []
    index_by_key = {}
    for rec in existing:
        key = _record_key(rec)
        if key in index_by_key:
            index = index_by_key[key]
            deduped[index] = _prefer_record(deduped[index], rec)
        else:
            index_by_key[key] = len(deduped)
            deduped.append(rec)
    existing = deduped
    added = 0

    for rec in new_records:
        key = _record_key(rec)
        if key not in index_by_key:
            index_by_key[key] = len(existing)
            existing.append(rec)
            added += 1
        else:
            index = index_by_key[key]
            existing[index] = _prefer_record(existing[index], rec)

    # 重新排序
    existing.sort(key=lambda x: x["timestamp"], reverse=True)
    return existing, added


def build_pool_registry(records: list) -> list:
    """构建卡池注册表：名称、时间段、UP干员（留空待补充）

    卡池架构设计：
    - 每个卡池一条记录
    - 字段：name, start_time, end_time, up_operators, category, pull_count
    - category: 限定/标准/中坚/联动/联合/甄选 等分类（从名称推断）
    - up_operators: 留空，后续手动或自动补充
    """
    from collections import defaultdict

    pool_stats = defaultdict(lambda: {
        "timestamps": [],
        "pull_count": 0,
        "six_star": [],
        "five_star": [],
    })

    for rec in records:
        pool = rec["pool"]
        stats = pool_stats[pool]
        stats["timestamps"].append(rec["timestamp"])
        stats["pull_count"] += 1
        if rec["rarity"] == 5:
            stats["six_star"].append(rec["name"])
        elif rec["rarity"] == 4:
            stats["five_star"].append(rec["name"])

    registry = []
    for pool_name, stats in pool_stats.items():
        ts_list = stats["timestamps"]
        start_ts = min(ts_list)
        end_ts = max(ts_list)

        # 从卡池名称推断分类
        category = _infer_pool_category(pool_name)

        # 六星去重
        six_star_unique = list(dict.fromkeys(stats["six_star"]))

        registry.append({
            "name": pool_name,
            "start_time": datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"),
            "end_time": datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d"),
            "category": category,
            "pull_count": stats["pull_count"],
            "up_operators": [],  # 留空，后续补充
            "six_star_pulled": six_star_unique,
        })

    # 按开始时间排序（最新在前）
    registry.sort(key=lambda x: x["start_time"], reverse=True)
    return registry


def _infer_pool_category(pool_name: str) -> str:
    """从卡池名称推断分类"""
    if "限定" in pool_name or "庆典" in pool_name:
        return "限定寻访"
    elif "中坚甄选" in pool_name:
        return "中坚甄选"
    elif "中坚" in pool_name:
        return "中坚寻访"
    elif "标准" in pool_name or "常驻" in pool_name:
        return "标准寻访"
    elif "联合" in pool_name:
        return "联合行动"
    elif "甄选" in pool_name:
        return "定向甄选"
    elif "跨年" in pool_name:
        return "特殊寻访"
    else:
        # 大部分活动池属于限定寻访
        return "活动寻访"


def generate_summary(records: list) -> dict:
    """生成统计摘要"""
    if not records:
        return {"total": 0}

    rarity_count = {}
    six_star = []
    pool_count = {}

    for rec in records:
        r = rec["rarity_label"]
        rarity_count[r] = rarity_count.get(r, 0) + 1
        if rec["rarity"] == 5:
            six_star.append(rec["name"])
        pool = rec["pool"] or "未知卡池"
        pool_count[pool] = pool_count.get(pool, 0) + 1

    return {
        "total": len(records),
        "rarity_count": rarity_count,
        "six_star_count": len(six_star),
        "six_star_names": list(dict.fromkeys(six_star)),
        "pool_count": pool_count,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_summary(summary: dict):
    """打印统计摘要"""
    print("\n" + "=" * 50)
    print("  明日方舟寻访记录统计")
    print("=" * 50)
    print("  总抽数: %d" % summary["total"])

    if summary["total"] == 0:
        return

    print("\n  稀有度分布:")
    for r in ["★6", "★5", "★4", "★3"]:
        count = summary["rarity_count"].get(r, 0)
        if count > 0:
            pct = count / summary["total"] * 100
            print("    %s: %d (%.1f%%)" % (r, count, pct))

    if summary["six_star_names"]:
        print("\n  六星干员: %s" % ", ".join(summary["six_star_names"]))

    print("\n  卡池分布:")
    for pool, count in sorted(summary["pool_count"].items(), key=lambda x: -x[1]):
        print("    %s: %d抽" % (pool, count))

    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="明日方舟小黑盒数据导入")
    parser.add_argument("--file", type=str, help="指定小黑盒导出文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args()

    # 确定输入文件
    if args.file:
        filepath = Path(args.file)
    else:
        # 自动查找 workspace/arknights_gacha/ 下的 JSON 文件
        json_files = list(INPUT_DIR.glob("*.json"))
        if not json_files:
            print("错误: workspace/arknights_gacha/ 目录下没有 JSON 文件")
            return
        if len(json_files) == 1:
            filepath = json_files[0]
        else:
            # 优先找包含 UID 的文件
            uid_files = [f for f in json_files if f.stem.isdigit()]
            filepath = uid_files[0] if uid_files else json_files[0]

    if not filepath.exists():
        print(f"错误: 文件不存在 {filepath}")
        return

    print(f"[1/4] 读取小黑盒数据: {filepath.name}")
    xhh_records = load_xhh_data(filepath)

    # 加载已有数据
    db_path = OUTPUT_DIR / "gacha_records.json"
    if db_path.exists():
        print(f"\n[2/4] 加载已有数据: {db_path.name}")
        existing = load_official_data(db_path)
        print(f"  已有记录: {len(existing)}条")
    else:
        print("\n[2/4] 无已有数据，创建新数据库")
        existing = []

    # 增量合并
    print(f"\n[3/4] 增量合并...")
    merged, added = merge_records(existing, xhh_records)
    print(f"  新增: {added}条, 总计: {len(merged)}条")

    if args.dry_run:
        print("\n[dry-run] 不写入文件")
        summary = generate_summary(merged)
        print_summary(summary)
        return

    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _atomic_write_json(db_path, merged)
    print(f"  已保存: {db_path.name}")

    # 生成卡池注册表
    pool_registry = build_pool_registry(merged)
    pool_path = OUTPUT_DIR / "pool_registry.json"
    _atomic_write_json(pool_path, pool_registry)
    print(f"  卡池注册表: {pool_path.name} ({len(pool_registry)}个卡池)")

    # 生成统计
    summary = generate_summary(merged)
    summary_path = OUTPUT_DIR / "summary.json"
    _atomic_write_json(summary_path, summary)

    print_summary(summary)

    # 打印卡池注册表
    print("\n卡池注册表:")
    for pool in pool_registry:
        up_str = ", ".join(pool["up_operators"]) if pool["up_operators"] else "(待补充)"
        print(f"  {pool['name']} [{pool['category']}]")
        print(f"    {pool['start_time']} ~ {pool['end_time']} | {pool['pull_count']}抽 | UP: {up_str}")
        if pool["six_star_pulled"]:
            print(f"    出金: {', '.join(pool['six_star_pulled'])}")


if __name__ == "__main__":
    main()
