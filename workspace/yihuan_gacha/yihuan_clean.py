"""异环抽卡记录数据清洗脚本 v5
- 新JSON格式: action/type/name/rarity/count/dice_points/time(unix timestamp)
- dice_points: 1-6=掷骰点数, 0=额外奖励(集点赠礼/沉眠地), -1=未识别
- action: "掷骰" / "集点赠礼" / "沉眠地" / "抽卡"（武器池）
- 集点赠礼与沉眠地同属额外奖励类(dice_points=0)，通过 action 区分(供模拟器使用)
- 采集阶段 raw dice=-2 为沉眠地内部信号，清洗时归为 dice_points=0 + action="沉眠地"
- 审核步骤：先生成审核清单，确认后才写入最终文件
- 原始数据永不覆盖
- 不做去重：同一秒的十连抽可能产出多条相同记录，是合法数据

用法:
  python workspace/yihuan_gacha/yihuan_clean.py                    # 清洗原始数据
  python workspace/yihuan_gacha/yihuan_clean.py --fix-time         # 列出缺时间记录
  python workspace/yihuan_gacha/yihuan_clean.py --fix-time 限定棋盘 0 "2026年6月8日 23:50:00"
"""
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

RAW_DIR = Path("workspace/yihuan_gacha/raw")          # 原始数据目录（保留近3次）
SUMMARY_FILE = Path("workspace/yihuan_gacha/yihuan_gacha_summary.json")  # 采集工作文件（近1次）
REVIEW_FILE = Path("workspace/yihuan_gacha/yihuan_gacha_review.json")    # 清洗审核文件（近1次）
DATABASE_FILE = Path("workspace/yihuan_gacha/yihuan_gacha_database.json") # 最终数据库（近1次）


def find_latest_raw():
    """查找 raw/ 目录中最新的原始数据文件，找不到则回退到 summary 工作文件"""
    if RAW_DIR.exists():
        raw_files = sorted(RAW_DIR.glob("yihuan_gacha_raw_*.json"))
        if raw_files:
            return raw_files[-1], True  # (path, is_raw)
    if SUMMARY_FILE.exists():
        return SUMMARY_FILE, False
    return None, False

# ========== 稀有度映射（用户确认，作为权威来源） ==========
RARITY_MAP = {
    # S级
    '角色·安魂曲': 'S', '角色·浔': 'S', '角色·娜娜莉': 'S', '角色·早雾': 'S',
    '角色·法帝娅': 'S', '角色·真红': 'S', '角色·九原': 'S',
    '角色·残虹': 'S', '角色·伊洛伊': 'S',
    '道具·失纬棋子': 'S', '道具·质实骰子': 'S', '道具·捏造骰子': 'S',
    '弧盘·好狗狗走四方': 'S', '弧盘·预备备': 'S',
    # A级
    '角色·埃德嘉': 'A', '角色·薄荷': 'A', '角色·阿德勒': 'A',
    '角色·哈尼娅': 'A', '角色·海月': 'A', '角色·翳': 'A',
    '弧盘·拔刀': 'A', '弧盘·勿忘伞': 'A',
    '弧盘·开始净空': 'A', '弧盘·当心头顶': 'A', '弧盘·被遗忘者': 'A',
    '道具·迷迭棋子': 'A',
    '滑翔翼·好柿成双': 'A', '滑翔翼·幽兰': 'A', '滑翔翼·副手的副手': 'A',
    '滑翔翼·天际猎手': 'A', '滑翔翼·赤练': 'A', '滑翔翼·小羊直升机': 'A',
    # B级
    '弧盘·危险游戏': 'B',
    '弧盘·「电音」狂欢': 'B', '弧盘·笑口常开': 'B',
    '弧盘·成功的第一步': 'B', '弧盘·「我们。」': 'B',
}

# ========== 名称修正映射 ==========
NAME_FIXES = {
    '弧盘"「我们。': '弧盘·「我们。」',
    '弧盘"「我们。」': '弧盘·「我们。」',
    '弧盘「我们。」': '弧盘·「我们。」',
    '弧盘「我们。': '弧盘·「我们。」',
    '弧盘·"「我们。」': '弧盘·「我们。」',
    '弧盘·"「我们。': '弧盘·「我们。」',
    '弧盘·"「我们。」1': '弧盘·「我们。」',
    '弧盘·"「我们。√1': '弧盘·「我们。」',
    '弧盘·"「我们。|1': '弧盘·「我们。」',
    '弧盘·"「我们。』1': '弧盘·「我们。」',
    '弧盘·「我们。」1': '弧盘·「我们。」',
    '弧盘·「我们。」√1': '弧盘·「我们。」',
    '弧盘·「我们。」|1': '弧盘·「我们。」',
    '弧盘·「我们。」』1': '弧盘·「我们。」',
    '弧盘·「我们。|1': '弧盘·「我们。」',
    '弧盘·「我们。』1': '弧盘·「我们。」',
    '弧盘·「我们。√1': '弧盘·「我们。」',
    '弧盘：「电音」狂欢': '弧盘·「电音」狂欢',
    '弧盘「电音」狂欢': '弧盘·「电音」狂欢',
    '弧盘■笑口常开': '弧盘·笑口常开',
    '盘·美口常开': '弧盘·笑口常开',
    '强盘·成功的第一步': '弧盘·成功的第一步',
    '弧盘·成功的第': '弧盘·成功的第一步',
    '弧盘·成功的第 一步': '弧盘·成功的第一步',
    '道具·-迷迭棋子': '道具·迷迭棋子',
    '角色·-埃德嘉': '角色·埃德嘉',
    '角色·-哈尼娅': '角色·哈尼娅',
    '角色·-阿德勒': '角色·阿德勒',
    # 以下为OCR噪音变体，直接映射比正则更可靠
    '弧盘·「我们。」1': '弧盘·「我们。」',
    '弧盘·「我们。」11': '弧盘·「我们。」',
    '弧盘·「我们。1': '弧盘·「我们。」',
    '弧盘·「我们。11': '弧盘·「我们。」',
    '弧盘·「我们。』1': '弧盘·「我们。」',
    '弧盘·「我们。|1': '弧盘·「我们。」',
    '弧盘·笨口常开': '弧盘·笑口常开',
    '弧盘·?笑口常开': '弧盘·笑口常开',
    '弧盘·▪成功的第一步1': '弧盘·成功的第一步',
    '弧盘·成功的第一步1': '弧盘·成功的第一步',
    '弧盘·被遗忘者1': '弧盘·被遗忘者',
    '角色·埃德嘉1': '角色·埃德嘉',
    '弧盘·：「我们。」': '弧盘·「我们。」',
    # 武器池 OCR 变体（多右引号/带√/带J）
    '弧盘·「我们。」」': '弧盘·「我们。」',
    '弧盘·「我们。」√': '弧盘·「我们。」',
    '弧盘·「我们。」J': '弧盘·「我们。」',
    # 武器池 OCR 变体（补」前的变体，clean_name 先查映射后补」）
    '弧盘·「我们。√': '弧盘·「我们。」',
    '弧盘·「我们。J': '弧盘·「我们。」',
    # 全角？混入变体（2026-08-26 采集出现 63 条）
    '弧盘·？「我们。」': '弧盘·「我们。」',
    # 异体字变体：凈(U+51C6繁体) vs 净(U+51C0)
    '弧盘·开始凈空': '弧盘·开始净空',
    # 无·分隔符变体（去引号后产生）
    '弧盘「我们。」1': '弧盘·「我们。」',
    '弧盘「我们。」11': '弧盘·「我们。」',
    '弧盘「我们。1': '弧盘·「我们。」',
    '弧盘「我们。11': '弧盘·「我们。」',
    '弧盘「我们。』1': '弧盘·「我们。」',
    '弧盘「我们。|1': '弧盘·「我们。」',
    '弧盘「我们。」': '弧盘·「我们。」',
    '弧盘「我们。': '弧盘·「我们。」',
    '弧盘·成功的第一步1': '弧盘·成功的第一步',
}

# 噪音关键词
NOISE_KEYWORDS = ['操作：', 'click坐标', '未来相同操作', 'UD:', '是否重复播放']

def clean_name(name):
    """清洗道具名称

    原则：通用正则只做最基础的清理（去引号、去噪音字符、去时间），
    具体名称修正全部靠 NAME_FIXES 映射表，不要写越来越复杂的正则。
    OCR千差万别，正则改不完，个例直接加映射更可靠。
    """
    # 先移除引号（各种双引号），避免引号变体导致NAME_FIXES匹配失败
    for ch in ['"', '\u201c', '\u201d', '\u201e', '\u201f', '\uff02']:
        name = name.replace(ch, '')
    # 移除OCR噪音字符（?、▪、>等）
    name = re.sub(r'[?▪►◀▲▼◆◇★☆□☒☑>]', '', name)
    # 替换常见分隔符变体
    name = name.replace('■', '·')
    name = name.replace('：', '·')
    name = name.replace('·-', '·')
    name = name.replace('··', '·')
    # 去除名称中混入的时间信息（OCR错误导致时间粘在名称后面）
    name = re.sub(r'\d*\d{4}年\d{1,2}月\d{1,2}日.*$', '', name)
    name = re.sub(r'\d{2,4}\d?月\d{1,2}日.*$', '', name)
    # 查修正映射（个例优先，比正则更可靠）
    if name in NAME_FIXES:
        return NAME_FIXES[name]
    # 通用后处理
    if name.startswith('弧盘·成功的第') and '一步' not in name:
        name = '弧盘·成功的第一步'
    if '我们。' in name and '」' not in name:
        name = name.replace('我们。', '我们。」')
    prefixes = ['弧盘', '角色', '道具', '滑翔翼']
    for prefix in prefixes:
        if name.startswith(prefix) and '·' not in name:
            name = name.replace(prefix, prefix + '·', 1)
            break
    return name

def parse_name(full_name):
    """从完整名称解析出 type 和 name"""
    if '·' in full_name:
        parts = full_name.split('·', 1)
        return parts[0], parts[1]
    for prefix in ['弧盘', '角色', '道具', '滑翔翼']:
        if full_name.startswith(prefix):
            return prefix, full_name[len(prefix):]
    return '未知', full_name

def parse_time_to_unix(time_str):
    """将时间字符串转为Unix时间戳"""
    if not time_str or time_str == '?':
        return 0
    time_str = re.sub(r'\s+', ' ', time_str).strip()
    time_str = re.sub(r'(\d+)\s*月', r'\1月', time_str)
    time_str = re.sub(r'(\d+)\s*日', r'\1日', time_str)
    
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2}):(\d{2})', time_str)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)), int(m.group(6)))
            return int(dt.timestamp())
        except ValueError:
            pass
    
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', time_str)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return int(dt.timestamp())
        except ValueError:
            pass
    
    return 0

def is_noise(record):
    name = record.get('name', '')
    for kw in NOISE_KEYWORDS:
        if kw in name:
            return True
    if name in ['田', '：', '■', '「', '」', '']:
        return True
    if len(name) <= 1:
        return True
    return False

def validate_times(records, board_type):
    """验证时间正确性：不超过今天、按OCR顺序单调非递减（允许连续相同时间）
    返回警告列表，同时打印到控制台"""
    warnings = []
    now_ts = int(datetime.now().timestamp())
    today_str = datetime.now().strftime('%Y-%m-%d')

    prev_time = 0
    for i, r in enumerate(records):
        t = r['time']
        if t == 0:
            continue
        # 检查1: 时间不超过当前时间
        if t > now_ts:
            dt_str = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')
            msg = f"[{board_type}] 第{i}条 时间超出今天: {dt_str} (记录: {r['type']}·{r['name']})"
            warnings.append(('future', i, r, msg))
        # 检查2: 时间单调非递减（允许相等）
        if t < prev_time:
            prev_str = datetime.fromtimestamp(prev_time).strftime('%Y-%m-%d %H:%M:%S')
            curr_str = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')
            msg = f"[{board_type}] 第{i}条 时间倒退: {prev_str} -> {curr_str} (记录: {r['type']}·{r['name']})"
            warnings.append(('decrease', i, r, msg))
        prev_time = t

    if warnings:
        print(f"\n  [!] 时间验证发现 {len(warnings)} 个问题:")
        for kind, idx, r, msg in warnings:
            print(f"    {msg}")
    else:
        print(f"  时间验证通过: 所有时间 ≤ {today_str}，且单调非递减")

    return warnings

# ========== 主流程 ==========
def run_clean():
    """执行清洗主流程

    支持武器池(弧盘研募)记录：
    - 武器池名称无"弧盘·"前缀，清洗时自动补前缀以便RARITY_MAP查找
    - 武器池 action="抽卡"（非掷骰/集点赠礼），dice_points=-1
    - 武器池保留 research_type 字段（研募类型，如"奇迹盒盒"）
    - 武器池与已有数据库合并（支持单独采集武器池而不丢失角色池数据）
    """
    raw_path, is_raw = find_latest_raw()
    if raw_path is None:
        print("[!] 未找到原始数据文件，请先运行采集脚本")
        return

    source_label = "raw/" + raw_path.name if is_raw else "summary工作文件"
    print(f"读取原始数据: {raw_path} ({source_label})")

    with open(raw_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # 读取已有数据库，用于合并（武器池可单独采集，需保留角色池数据）
    existing_db = {}
    if DATABASE_FILE.exists():
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            existing_db = json.load(f)
        if existing_db:
            print(f"已有数据库: {', '.join(f'{k}:{len(v)}' for k, v in existing_db.items())}")

    review_data = {}
    all_names = Counter()
    unknown_names = set()

    for board_type, records in raw.items():
        is_weapon = (board_type == "弧盘研募")
        print(f"\n{'='*50}")
        print(f"处理 {board_type}: 原始 {len(records)} 条" + (" [武器池]" if is_weapon else ""))

        cleaned = []
        for r in records:
            if is_noise(r):
                continue

            raw_name = r.get('name', '')
            raw_dice = r.get('dice', -1)  # v4: 整数 (0=赠礼, 1-6=点数, -1=未识别)
            raw_count = r.get('count', r.get('quantity', 1))
            raw_date = r.get('date', r.get('time', ''))
            raw_rarity = r.get('rarity', '?')

            # 武器池名称无"弧盘·"前缀，补前缀以便清洗和RARITY_MAP查找
            if is_weapon and not raw_name.startswith("弧盘·"):
                raw_name = f"弧盘·{raw_name}"

            clean = clean_name(raw_name)
            item_type, item_name = parse_name(clean)

            if is_weapon:
                # 武器池：普通抽卡，无骰子机制
                dice_points = -1
                action = '抽卡'
                research_type = r.get('research_type', '')
            else:
                # 角色池：兼容旧格式(v3: dice="集点赠礼"/"骰子")和新格式(v4: dice=0/1-6/-1/-2)
                # 集点赠礼与沉眠地同属额外奖励类(dice_points=0)，通过 action 区分
                # 采集阶段 dice=-2 为沉眠地内部信号，清洗时归为 dice_points=0 + action="沉眠地"
                if isinstance(raw_dice, int):
                    if raw_dice == -2:
                        # 沉眠地内部信号 → 归为 0
                        dice_points = 0
                        action = '沉眠地'
                    else:
                        dice_points = raw_dice
                        action = '集点赠礼' if dice_points == 0 else '掷骰'
                elif raw_dice == '集点赠礼':
                    dice_points = 0
                    action = '集点赠礼'
                elif raw_dice == '沉眠地':
                    dice_points = 0
                    action = '沉眠地'
                else:
                    dice_points = -1
                    action = '掷骰'

                count = int(raw_count) if isinstance(raw_count, (int, str)) and str(raw_count).isdigit() else 1

                # 沉眠地识别（旧采集数据中 dice=-1，需根据名称+数量推断）
                if dice_points == -1 and '失纬棋子' in clean and count >= 30:
                    dice_points = 0
                    action = '沉眠地'
                research_type = ''

            # 数量警告：只有道具（棋子/骰子）允许数量>1，角色/弧盘/滑翔翼通常为1
            if item_type in ['角色', '弧盘', '滑翔翼'] and count > 1:
                print(f"  [!] 数量可疑: {clean} x{count}（{item_type}类通常为1）")
            unix_time = parse_time_to_unix(raw_date)

            rarity = RARITY_MAP.get(clean, raw_rarity)
            if rarity == '?':
                if item_type == '角色':
                    rarity = 'A'
                elif item_type in ['道具', '滑翔翼']:
                    rarity = 'A'
                else:
                    unknown_names.add(clean)

            all_names[clean] += 1

            record = {
                'action': action,
                'type': item_type,
                'name': item_name,
                'rarity': rarity,
                'count': count,
                'dice_points': dice_points,
                'time': unix_time,
            }
            # 武器池额外保留 research_type 字段（不影响角色池记录）
            if is_weapon:
                record['research_type'] = research_type
            cleaned.append(record)

        print(f"  清洗后: {len(cleaned)} 条")

        # 按时间排序（time=0的排最后）
        cleaned.sort(key=lambda x: (x['time'] == 0, x['time']))

        # 十连组时间均摊：同时间戳的记录反序后+1s递增
        # 游戏历史页按倒序显示（最新在前），OCR读取的顺序也是倒序
        # 反序后得到正序（最旧在前），符合游戏内实际抽取顺序
        i = 0
        while i < len(cleaned):
            t = cleaned[i]['time']
            if t == 0:
                i += 1
                continue
            j = i + 1
            while j < len(cleaned) and cleaned[j]['time'] == t:
                j += 1
            group_size = j - i
            if group_size >= 2:
                # 反序：OCR最后一条（最旧）获得最早时间
                for k in range(group_size):
                    cleaned[j - 1 - k]['time'] = t + k
            i = j

        # 确保时间严格递增（处理不同组之间的时间冲突）
        for k in range(1, len(cleaned)):
            if cleaned[k]['time'] > 0 and cleaned[k-1]['time'] > 0:
                if cleaned[k]['time'] <= cleaned[k-1]['time']:
                    cleaned[k]['time'] = cleaned[k-1]['time'] + 1

        # 时间正确性验证
        time_warnings = validate_times(cleaned, board_type)

        # 合并已有数据库（增量更新）
        # 策略：raw 是全量数据（游戏保留最近6个月），旧库用于补充 raw 丢失的旧记录
        # 1. 保留旧库中早于 raw 最早记录的记录（raw 丢失的超过6个月的旧记录）
        # 2. 用 raw 替换角色池数据（raw 范围内的旧库记录全部丢弃，因为 raw 已有）
        # 3. 对 raw 中 dice_points==-1 的记录，从旧库按 3元组+±120s 查找 dice 修正
        #
        # 为什么不用模糊时间匹配去重：
        # - 时间戳重分配会因 raw 条数变化而偏移，±Ns 窗口无法同时避免误杀和漏判
        # - 旧版清洗脚本可能把"集点赠礼"识别为"掷骰"，4元组不匹配导致去重失效
        # - raw 是权威全量数据，直接替换最可靠
        if board_type in existing_db:
            old_records = existing_db[board_type]

            raw_min_time = min((r['time'] for r in cleaned if r['time'] > 0), default=0)

            # 旧库中早于 raw 最早记录 - 120s 的保留（raw 丢失的旧记录）
            # 120s 容差：时间戳重分配偏移最大约60秒，2倍容差确保安全
            if raw_min_time > 0:
                cutoff = raw_min_time - 120
                preserved = [r for r in old_records if 0 < r['time'] < cutoff]
            else:
                preserved = list(old_records)

            # 对 raw 中 dice_points==-1 的记录，从旧库(raw范围内)查找 dice 修正
            # 用 (type, name, count) + ±120s 模糊匹配，只修正 dice 值，不影响记录保留
            old_in_range = [r for r in old_records if raw_min_time == 0 or r['time'] >= cutoff]
            old_dice_by_3 = {}
            for r in old_in_range:
                if r.get('dice_points', -1) != -1:
                    k3 = (r.get('type'), r.get('name'), r.get('count'))
                    old_dice_by_3.setdefault(k3, []).append((r['time'], r['dice_points']))

            dice_fixed = 0
            for r in cleaned:
                if r.get('dice_points') == -1 and r['time'] > 0:
                    k3 = (r.get('type'), r.get('name'), r.get('count'))
                    if k3 in old_dice_by_3:
                        for ot, odp in old_dice_by_3[k3]:
                            if abs(ot - r['time']) <= 120:
                                r['dice_points'] = odp
                                dice_fixed += 1
                                break

            merged = preserved + cleaned
            merged.sort(key=lambda x: (x['time'] == 0, x['time']))
            review_data[board_type] = merged

            parts = [f"raw{len(cleaned)}条"]
            if preserved:
                parts.append(f"旧库保留{len(preserved)}条(raw丢失)")
            if dice_fixed:
                parts.append(f"dice修正{dice_fixed}条")
            print(f"  合并: {' + '.join(parts)} = {len(merged)}条")
        else:
            review_data[board_type] = cleaned

    # 保留已有数据库中本次未采集的卡池（如单独采集武器池时保留角色池数据）
    for board_type, records in existing_db.items():
        if board_type not in review_data:
            review_data[board_type] = records
            print(f"  保留已有数据: {board_type} ({len(records)}条)")

    # ========== 保存审核文件 ==========
    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)
    print(f"\n审核文件已保存: {REVIEW_FILE}")

    # ========== 打印名称审核清单 ==========
    print(f"\n{'='*60}")
    print("名称审核清单（请确认以下名称是否正确）:")
    print(f"{'='*60}")

    for name, count in sorted(all_names.items(), key=lambda x: -x[1]):
        item_type, item_name = parse_name(name)
        rarity = RARITY_MAP.get(name, '?')
        flag = " [需确认]" if name in unknown_names else ""
        print(f"  [{rarity}] {item_type}·{item_name} x{count}{flag}")

    if unknown_names:
        print(f"\n[!] 以下名称无法确定稀有度:")
        for name in sorted(unknown_names):
            print(f"    {name}")

    # ========== 统计 ==========
    for board_type, records in review_data.items():
        is_weapon = (board_type == "弧盘研募")
        by_rarity = Counter(r['rarity'] for r in records)
        by_action = Counter(r['action'] for r in records)
        no_time = sum(1 for r in records if r['time'] == 0)
        total = len(records)

        print(f"\n{board_type}: {total} 条")
        for r in ['S', 'A', 'B', '?']:
            if by_rarity.get(r, 0) > 0:
                print(f"  {r}级: {by_rarity[r]} 条")

        if is_weapon:
            # 武器池：研募类型分布 + 额外奖励统计
            research_types = Counter(r.get('research_type', '') for r in records)
            rt_str = " | ".join(f"{k}:{v}" for k, v in research_types.items() if k)
            if rt_str:
                print(f"  研募类型: {rt_str}")
            s_count = by_rarity.get('S', 0)
            a_count = by_rarity.get('A', 0)
            b_count = by_rarity.get('B', 0)
            shiwei = s_count * 40 + a_count * 4
            midie = b_count * 20
            print(f"  额外奖励: 失纬棋子{shiwei} | 迷迭棋子{midie}")
        else:
            # 角色池：骰子统计
            unknown_dice = sum(1 for r in records if r.get('dice_points', -1) == -1)
            chenmian = by_action.get('沉眠地', 0)
            dice_dist = Counter()
            for r in records:
                dp = r.get('dice_points', -1)
                if isinstance(dp, int) and 1 <= dp <= 6:
                    dice_dist[dp] += 1
            dice_dist_str = " ".join(f"{k}点:{v}" for k, v in sorted(dice_dist.items()))
            parts = [f"掷骰: {by_action.get('掷骰', 0)}"]
            if chenmian:
                parts.append(f"沉眠地: {chenmian}")
            parts.append(f"集点赠礼: {by_action.get('集点赠礼', 0)}")
            parts.append(f"未识别点数: {unknown_dice}")
            print(f"  {' | '.join(parts)}")
            if dice_dist_str:
                print(f"  点数分布: {dice_dist_str}")

        if no_time:
            print(f"  [!] {no_time} 条记录缺少时间")

# ========== 时间补全接口 ==========
def fix_time(board_type: str, index: int, time_str: str):
    """补全缺时间记录
    Args:
        board_type: 棋盘类型（限定棋盘/标准棋盘）
        index: 缺时间记录的序号（从0开始）
        time_str: 人类可读时间，如 "2026年6月8日 23:50:00"
    """
    if not DATABASE_FILE.exists():
        print(f"[!] 数据库文件不存在: {DATABASE_FILE}")
        return

    with open(DATABASE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if board_type not in data:
        print(f"[!] 未知棋盘类型: {board_type}，可选: {list(data.keys())}")
        return

    records = data[board_type]
    missing = [(i, r) for i, r in enumerate(records) if r['time'] == 0]

    if index < 0 or index >= len(missing):
        print(f"[!] 序号超出范围，共 {len(missing)} 条缺时间记录 (0-{len(missing)-1})")
        return

    unix_time = parse_time_to_unix(time_str)
    if unix_time == 0:
        print(f"[!] 无法解析时间: {time_str}")
        print(f"    支持格式: 2026年6月8日 23:50:00 或 2026年6月8日")
        return

    real_idx, record = missing[index]
    old_time = record['time']
    record['time'] = unix_time

    # 重新排序
    records.sort(key=lambda x: (x['time'] == 0, x['time']))
    data[board_type] = records

    with open(DATABASE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    dt_str = datetime.fromtimestamp(unix_time).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[OK] {board_type} #{index} [{record['rarity']}] {record['type']}·{record['name']} x{record['count']}")
    print(f"     time: 0 -> {unix_time} ({dt_str})")

def list_missing_time():
    """列出所有缺时间的记录"""
    target = DATABASE_FILE if DATABASE_FILE.exists() else REVIEW_FILE
    if not target.exists():
        print(f"[!] 无数据文件")
        return

    with open(target, "r", encoding="utf-8") as f:
        data = json.load(f)

    total_missing = 0
    for board_type, records in data.items():
        missing = [(i, r) for i, r in enumerate(records) if r['time'] == 0]
        if missing:
            print(f"\n{board_type} - {len(missing)} 条缺时间:")
            for idx, (real_idx, r) in enumerate(missing):
                # 找前后有时间记录
                prev_t = None
                for j in range(real_idx - 1, -1, -1):
                    if records[j]['time'] > 0:
                        prev_t = records[j]['time']
                        break
                next_t = None
                for j in range(real_idx + 1, len(records)):
                    if records[j]['time'] > 0:
                        next_t = records[j]['time']
                        break
                prev_str = datetime.fromtimestamp(prev_t).strftime('%Y-%m-%d %H:%M:%S') if prev_t else '-'
                next_str = datetime.fromtimestamp(next_t).strftime('%Y-%m-%d %H:%M:%S') if next_t else '-'
                print(f"  #{idx} [{r['rarity']}] {r['type']}·{r['name']} x{r['count']} ({r['action']})")
                print(f"       前: {prev_str}  后: {next_str}")
            total_missing += len(missing)

    if total_missing == 0:
        print("\n所有记录时间完整！")
    else:
        print(f"\n共 {total_missing} 条缺时间")
        print(f"补全命令: python workspace/yihuan_gacha/yihuan_clean.py --fix-time <棋盘类型> <序号> \"<时间>\"")
        print(f"示例: python workspace/yihuan_gacha/yihuan_clean.py --fix-time 限定棋盘 0 \"2026年6月8日 23:50:00\"")

# ========== 命令行入口 ==========
if __name__ == "__main__":
    import sys
    if '--fix-time' in sys.argv:
        idx = sys.argv.index('--fix-time')
        args = sys.argv[idx+1:]
        if len(args) == 0:
            list_missing_time()
        elif len(args) == 3:
            fix_time(args[0], int(args[1]), args[2])
        else:
            print("用法:")
            print("  python workspace/yihuan_gacha/yihuan_clean.py --fix-time")
            print("  python workspace/yihuan_gacha/yihuan_clean.py --fix-time 限定棋盘 0 \"2026年6月8日 23:50:00\"")
    else:
        run_clean()
