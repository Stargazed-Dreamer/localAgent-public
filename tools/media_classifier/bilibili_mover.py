"""<data_drive>:\<bilibili_videos>视频分类移动脚本 Phase 2
- 读取 classification_results.jsonl
- 按分类+置信度分层 move 到 <media_root>视频整理\\
- 高置信(>=0.8) 和 待确认(<0.8) 分开存放
- 重复文件(同名同大小)跳过，优先保留I盘
- 生成 move_mapping.json 支持回滚
"""
import json
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

# ============ 配置 ============
JSONL = Path(r'<project_root>\output\<data_drive>:\<bilibili_organized_output>\classification_results.jsonl')
OUTPUT = Path(r'<project_root>\output\<data_drive>:\<bilibili_organized_output>')
MAPPING_FILE = OUTPUT / 'move_mapping.json'
DEST_ROOT = Path(r'<media_root>视频整理')
CONFIDENCE_THRESHOLD = 0.8

# ============ 主流程 ============
def main():
    # 读取分类结果
    items = []
    with open(JSONL, encoding='utf-8') as f:
        for line in f:
            items.append(json.loads(line))
    print(f'读取分类结果: {len(items)} 条')

    # 按 I盘优先 排序（I盘先处理，E盘后处理，E盘重复自动跳过）
    items.sort(key=lambda x: (0 if x['source_path'][0] == 'I' else 1, x['id']))

    # 统计
    moves = []
    skipped_duplicates = []
    errors = []
    moved_count = 0
    skipped_count = 0
    error_count = 0

    # 跟踪已移动的文件名（用于去重）
    # key: (tier, category, filename), value: size
    moved_tracker = {}

    start = time.time()

    for i, item in enumerate(items, 1):
        source = item['source_path']
        filename = item['filename']
        category = item.get('category_name', '无法分类')
        confidence = item.get('confidence', 0)
        tier = '高置信' if confidence >= CONFIDENCE_THRESHOLD else '待确认'
        associated = item.get('associated_files', [])

        # 检查源文件是否存在
        if not os.path.exists(source):
            errors.append({
                'source': source,
                'reason': '源文件不存在',
                'item_id': item.get('id'),
            })
            error_count += 1
            if i % 100 == 0:
                print(f'  进度: {i}/{len(items)} | 已移动:{moved_count} 跳过:{skipped_count} 错误:{error_count}')
            continue

        src_size = os.path.getsize(source)

        # 构建目标路径
        dest_dir = DEST_ROOT / tier / category
        dest_path = dest_dir / filename

        # 检查重复（同名同大小）
        tracker_key = (tier, category, filename)
        if tracker_key in moved_tracker:
            existing_size = moved_tracker[tracker_key]
            if existing_size == src_size:
                # 大小一致，跳过
                skipped_duplicates.append({
                    'source': source,
                    'reason': f'重复文件(同名同大小)，已存在: {dest_path}',
                    'size': src_size,
                    'item_id': item.get('id'),
                })
                skipped_count += 1
                # 也跳过关联文件
                continue
            else:
                # 大小不同，加后缀
                stem, ext = os.path.splitext(filename)
                dest_path = dest_dir / f'{stem}_dup2{ext}'
                counter = 2
                while dest_path.exists() or (tier, category, dest_path.name) in moved_tracker:
                    counter += 1
                    dest_path = dest_dir / f'{stem}_dup{counter}{ext}'
                filename = dest_path.name
                tracker_key = (tier, category, filename)

        # 检查目标是否已存在（可能上次运行中断）
        if dest_path.exists():
            existing_size = os.path.getsize(dest_path)
            if existing_size == src_size:
                # 目标已存在且大小一致，跳过
                moved_tracker[tracker_key] = src_size
                skipped_duplicates.append({
                    'source': source,
                    'reason': f'目标已存在(同名同大小): {dest_path}',
                    'size': src_size,
                    'item_id': item.get('id'),
                })
                skipped_count += 1
                continue
            else:
                # 大小不同，加后缀
                stem, ext = os.path.splitext(filename)
                dest_path = dest_dir / f'{stem}_dup2{ext}'
                counter = 2
                while dest_path.exists():
                    counter += 1
                    dest_path = dest_dir / f'{stem}_dup{counter}{ext}'
                filename = dest_path.name
                tracker_key = (tier, category, filename)

        # 创建目标目录
        dest_dir.mkdir(parents=True, exist_ok=True)

        # 记录关联文件移动
        assoc_moves = []
        for assoc_source in associated:
            if not os.path.exists(assoc_source):
                continue
            assoc_filename = os.path.basename(assoc_source)
            assoc_dest = dest_dir / assoc_filename
            # 检查关联文件是否已存在
            if assoc_dest.exists() and os.path.getsize(assoc_dest) == os.path.getsize(assoc_source):
                continue  # 已存在，跳过
            assoc_moves.append({
                'source': assoc_source,
                'destination': str(assoc_dest),
            })

        # 移动主文件
        try:
            shutil.move(str(source), str(dest_path))
            moved_tracker[tracker_key] = src_size
            moved_count += 1
        except Exception as e:
            errors.append({
                'source': source,
                'reason': f'移动失败: {e}',
                'item_id': item.get('id'),
            })
            error_count += 1
            if i % 100 == 0:
                print(f'  进度: {i}/{len(items)} | 已移动:{moved_count} 跳过:{skipped_count} 错误:{error_count}')
            continue

        # 移动关联文件
        for am in assoc_moves:
            try:
                shutil.move(am['source'], am['destination'])
            except Exception as e:
                errors.append({
                    'source': am['source'],
                    'reason': f'关联文件移动失败: {e}',
                    'item_id': item.get('id'),
                })

        # 记录映射
        moves.append({
            'source': source,
            'destination': str(dest_path),
            'category': category,
            'confidence': confidence,
            'confidence_tier': tier,
            'size': src_size,
            'item_id': item.get('id'),
            'associated_moves': assoc_moves,
        })

        if i % 100 == 0:
            el = time.time() - start
            print(f'  进度: {i}/{len(items)} | 已移动:{moved_count} 跳过:{skipped_count} 错误:{error_count} | {el:.0f}s')

    # 保存映射JSON
    mapping = {
        'total': len(items),
        'moved': moved_count,
        'skipped_duplicates': skipped_count,
        'errors': error_count,
        'moves': moves,
        'skipped_duplicates_list': skipped_duplicates,
        'errors_list': errors,
    }
    with open(MAPPING_FILE, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    el = time.time() - start
    print(f'\n=== 移动完成! 耗时: {el:.0f}s ===')
    print(f'总条目: {len(items)}')
    print(f'已移动: {moved_count}')
    print(f'跳过(重复): {skipped_count}')
    print(f'错误: {error_count}')
    print(f'\n映射文件: {MAPPING_FILE}')

    # 打印分类统计
    tier_cat = defaultdict(lambda: defaultdict(int))
    for m in moves:
        tier_cat[m['confidence_tier']][m['category']] += 1
    print('\n=== 移动统计 ===')
    for tier in ['高置信', '待确认']:
        if tier in tier_cat:
            print(f'\n{tier}:')
            for cat, n in sorted(tier_cat[tier].items(), key=lambda x: -x[1]):
                print(f'  {cat:8s} {n:5d}')


if __name__ == '__main__':
    main()
