"""异环抽卡数据审核探查脚本
读取清洗后的抽卡数据，输出统计摘要和异常检测。

用法:
  uv run python yihuan_review.py            # 默认含时间倒序明细
  uv run python yihuan_review.py --brief    # 仅统计摘要，不打印明细
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REVIEW_FILE = Path("workspace/yihuan_gacha/yihuan_gacha_review.json")
DATABASE_FILE = Path("workspace/yihuan_gacha/yihuan_gacha_database.json")

RARITY_ORDER = {'S': 0, 'A': 1, 'B': 2, '?': 3}


def _pad_cn(s, width):
    """中英文混合字符串右填充到指定宽度（中文占2格）"""
    display_len = sum(2 if ord(c) > 0x7f else 1 for c in s)
    return s + ' ' * max(0, width - display_len)


def print_detail_timeline(data):
    """分棋盘按时间倒序打印所有记录"""
    for board, records in data.items():
        # 按时间倒序
        sorted_recs = sorted(records, key=lambda x: (-x.get('time', 0), x.get('time', 0) == 0))
        print(f"\n{'='*78}")
        print(f"【{board}】共 {len(records)} 条（时间倒序）")
        print(f"{'='*78}")
        hdr = f"  {'序号':>4}  {_pad_cn('时间', 19)}  {_pad_cn('稀有度', 5)}  {_pad_cn('类型', 6)}  {_pad_cn('名称', 18)}  {'数量':>4}  {'点数':>4}  操作"
        print(hdr)
        print(f"  {'----':>4}  {'-'*19}  {'-'*5}  {'-'*6}  {'-'*18}  {'----':>4}  {'----':>4}  ----")
        for i, r in enumerate(sorted_recs):
            t = r.get('time', 0)
            if t > 0:
                dt_str = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')
            else:
                dt_str = '(无时间)'
            rarity = r.get('rarity', '?')
            rtype = r.get('type', '?')
            name = r.get('name', '?')
            count = r.get('count', 1)
            dp = r.get('dice_points', -1)
            action = r.get('action', '')
            if dp == 0 and action == '沉眠地':
                dp_str = '沉眠地'
            elif dp == 0:
                dp_str = '赠礼'
            elif dp >= 1:
                dp_str = str(dp)
            else:
                dp_str = '?'
            action = r.get('action', '?')
            line = f"  {i+1:>4}  {_pad_cn(dt_str, 19)}  {_pad_cn(rarity, 5)}  {_pad_cn(rtype, 6)}  {_pad_cn(name, 18)}  {count:>4}  {dp_str:>4}  {action}"
            print(line)
        print()


def main():
    brief_mode = '--brief' in sys.argv

    # 选择数据源：优先数据库
    data_file = DATABASE_FILE if DATABASE_FILE.exists() else REVIEW_FILE
    if not data_file.exists():
        print(f"[!] 未找到数据文件: {DATABASE_FILE} 或 {REVIEW_FILE}")
        return

    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"数据源: {data_file.name}\n")

    # 默认打印时间倒序明细
    if not brief_mode:
        print_detail_timeline(data)

    total_all = 0
    all_items = Counter()  # (name, rarity) -> count
    name_boards = defaultdict(set)  # name -> 出现的棋盘集合
    anomalies = []  # 异常记录

    for board, records in data.items():
        total_all += len(records)
        board_dice = Counter()
        board_gift = 0
        board_chenmian = 0
        board_unknown_dice = 0
        board_no_time = 0
        board_rarity_unknown = 0

        for r in records:
            name = r['name']
            rarity = r['rarity']
            dp = r.get('dice_points', -1)
            action = r.get('action', '')
            count = r.get('count', 1)
            t = r.get('time', 0)

            # 物品统计（仅掷骰/沉眠地，不含集点赠礼）
            if action not in ('集点赠礼',):
                all_items[(name, rarity)] += count

            name_boards[name].add(board)

            # 额外奖励(dp=0): 集点赠礼 或 沉眠地，通过 action 区分
            if action == '集点赠礼':
                board_gift += 1
            elif action == '沉眠地':
                board_chenmian += 1
            elif dp == -1:
                board_unknown_dice += 1
                if action not in ('抽卡',):
                    anomalies.append(('dice_unknown', board, r))
            elif isinstance(dp, int) and 1 <= dp <= 6:
                board_dice[dp] += 1

            # 稀有度未知
            if rarity == '?':
                board_rarity_unknown += 1
                anomalies.append(('rarity_unknown', board, r))

            # 缺时间
            if t == 0:
                board_no_time += 1
                anomalies.append(('no_time', board, r))

        # === 棋盘概览 ===
        print(f"{'='*50}")
        print(f"【{board}】共 {len(records)} 条")
        print()

        # 物品统计
        board_items = Counter()
        for r in records:
            if r.get('action') != '集点赠礼':
                board_items[(r['name'], r['rarity'])] += r.get('count', 1)

        if board_items:
            sorted_items = sorted(board_items.items(),
                                  key=lambda x: (RARITY_ORDER.get(x[0][1], 9), -x[1]))
            print("  物品统计:")
            for (name, rarity), cnt in sorted_items:
                print(f"    [{rarity}] {name} x{cnt}")
            print()

        # 骰子点数分布
        total_dice = sum(board_dice.values())
        dice_str = "  ".join(f"{k}点:{v}" for k, v in sorted(board_dice.items()))
        print(f"  骰子点数: {dice_str}")
        extra_parts = [f"集点赠礼: {board_gift}"]
        if board_chenmian:
            extra_parts.append(f"沉眠地: {board_chenmian}")
        extra_parts.append(f"未识别: {board_unknown_dice}")
        print(f"  {'  '.join(extra_parts)}")

        # 点数均匀性检查
        if total_dice > 0:
            for dp, cnt in board_dice.items():
                if cnt / total_dice > 0.25:
                    pct = cnt / total_dice * 100
                    print(f"  [!] {dp}点占比 {pct:.1f}% (>25%)")

        # 集点赠礼占比
        total_records = len(records)
        if total_records > 0:
            gift_pct = board_gift / total_records * 100
            flag = " [!]" if abs(gift_pct - 10) > 8 else ""
            print(f"  集点赠礼占比: {gift_pct:.1f}%{flag}")

        if board_rarity_unknown:
            print(f"  [!] 稀有度未知: {board_rarity_unknown} 条")
        if board_no_time:
            print(f"  [!] 缺少时间: {board_no_time} 条")

        print()

    # === 汇总 ===
    print(f"{'='*50}")
    print(f"总计: {total_all} 条记录，{len(data)} 个棋盘")
    print()

    # 全局物品统计
    if all_items:
        sorted_all = sorted(all_items.items(),
                            key=lambda x: (RARITY_ORDER.get(x[0][1], 9), -x[1]))
        print("全局物品统计:")
        for (name, rarity), cnt in sorted_all:
            print(f"  [{rarity}] {name} x{cnt}")
        print()

    # 同名出现在不同棋盘
    cross_board = {n: bs for n, bs in name_boards.items() if len(bs) > 1}
    if cross_board:
        print("同名出现在多个棋盘:")
        for name, boards in sorted(cross_board.items()):
            print(f"  {name}: {', '.join(sorted(boards))}")
        print()

    # 异常汇总
    if anomalies:
        print(f"异常记录汇总 ({len(anomalies)} 条):")
        by_type = defaultdict(list)
        for kind, board, r in anomalies:
            by_type[kind].append((board, r))
        for kind, items in by_type.items():
            label = {'rarity_unknown': '稀有度未知',
                     'dice_unknown': '点数未识别',
                     'no_time': '缺少时间'}[kind]
            print(f"  {label}: {len(items)} 条")
            for board, r in items[:5]:  # 最多显示5条
                print(f"    [{board}] [{r['rarity']}] {r['name']} x{r.get('count',1)}")
            if len(items) > 5:
                print(f"    ... 还有 {len(items)-5} 条")
        print()
    else:
        print("未发现异常记录。")


if __name__ == "__main__":
    main()
    input("press any key...")
