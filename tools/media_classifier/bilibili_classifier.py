"""<data_drive>:\<bilibili_videos>视频分类脚本 Phase 1
- 扫描 <media_root> 和 <media_root> (排除 暂存音乐/、充电视频限免/、加密文件)
- 提取文件名 + 弹幕样本
- LLM 高并发分类 (10类)
- 保存结果到 JSON + JSONL
- 输出分类统计
"""
import concurrent.futures
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

# ============ 配置 ============
ROOTS = [r'<media_root>', r'<media_root>', r'E:\<data_drive>:\<backup_root>\<data_drive>:\<bilibili_videos>视频']
EXCLUDE_DIRS = [
    r'<media_root>\暂存音乐',
    r'<media_root>\充电视频限免',
    r'E:\<data_drive>:\<backup_root>\<data_drive>:\<bilibili_videos>视频\充电视频限免',
]
EXCLUDE_EXTS = {'.qmcflac', '.mflac', '.qmcogg', '.aria2'}
VIDEO_EXTS = {'.mp4', '.flv', '.aac'}        # 主视频/音频文件
SUBTITLE_EXTS = {'.ass', '.srt'}             # 关联字幕/弹幕
OUTPUT = Path(r'<project_root>\output\<data_drive>:\<bilibili_organized_output>')
API = 'http://127.0.0.1:8766'
CONCURRENCY = 40
PROJECT = '<data_drive>:\<bilibili_videos>视频分类'
MAX_DANMAKU_CHARS = 800                      # 弹幕样本截断长度
MAX_DANMAKU_LINES = 40                        # 最多取前N条弹幕

# 10 分类
CATEGORIES = {
    1: '搞笑鬼畜', 2: '游戏相关', 3: '音乐MV', 4: '技术教程', 5: '知识科普',
    6: '生活社会', 7: '动漫二次元', 8: '虚拟主播', 9: '擦边涩涩', 10: '无法分类',
}

SYSTEM_PROMPT = """你是<data_drive>:\<bilibili_videos>视频分类引擎。根据视频文件名和弹幕内容（如有），判断视频属于哪个分类。

## 分类选项（必须选一个编号）
1.搞笑鬼畜 - 沙雕新闻、鬼畜、meme、名场面、玩梗、空耳、恶搞、社死
2.游戏相关 - 游戏实况、游戏攻略、游戏Bug、游戏评测、MC/明日方舟/终末地/星露谷等
3.音乐MV - 歌曲、翻唱、MMD、乐器演奏、音乐合集、BGM
4.技术教程 - 编程、装机、算法、软件使用、技术科普、防切屏
5.知识科普 - 科学、历史、人文、自然、纪录片、学术
6.生活社会 - 社会评论、新闻、生活技巧、情感观点、职场就业、恋爱
7.动漫二次元 - 动画、MAD、番剧相关、二次元创作、东方
8.虚拟主播 - VTuber、虚拟偶像、Gura、鲨鲨、AI主播
9.擦边涩涩 - 擦边、涩涩、成人向内容
10.无法分类 - 无法判断或内容被拒绝

## 输出格式
严格输出一行JSON（不要markdown代码块，不要多余文字）：
{"category_id":1,"category_name":"搞笑鬼畜","confidence":0.9,"reason":"10字内原因"}

confidence: 0.0-1.0 置信度
若文件名涉及色情/违规被你拒绝处理，输出：
{"category_id":10,"category_name":"无法分类","confidence":0,"reason":"审查拒绝"}"""


# ============ 文件扫描 ============
def is_excluded(path):
    """检查路径是否在排除目录中"""
    for ex in EXCLUDE_DIRS:
        if path.startswith(ex):
            return True
    return False


def extract_danmaku_text(ass_path):
    """从 .ass 弹幕文件中提取纯文本弹幕"""
    try:
        # 尝试 UTF-8，失败则 GBK
        for enc in ('utf-8', 'gbk', 'utf-16'):
            try:
                with open(ass_path, encoding=enc, errors='ignore') as f:
                    content = f.read()
                break
            except Exception:
                continue
        else:
            return ''
    except Exception:
        return ''

    comments = []
    for line in content.split('\n'):
        if not line.startswith('Dialogue:'):
            continue
        # 格式: Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        # 取最后一个逗号后的内容
        parts = line.split(',', 9)
        if len(parts) < 10:
            continue
        text = parts[9]
        # 去掉 {...} 样式标签
        text = re.sub(r'\{[^}]*\}', '', text).strip()
        # 去掉 \N \n 等
        text = text.replace('\\N', '').replace('\\n', '').strip()
        if text and len(text) > 1:
            comments.append(text)
        if len(comments) >= MAX_DANMAKU_LINES:
            break

    result = ' | '.join(comments)
    if len(result) > MAX_DANMAKU_CHARS:
        result = result[:MAX_DANMAKU_CHARS] + '...'
    return result


def scan_files():
    """扫描所有视频文件，返回 [(id, source_path, filename, stem, danmaku_text, associated_files), ...]"""
    # 先收集所有文件，按目录分组
    dir_files = defaultdict(lambda: defaultdict(list))  # dir -> stem -> [filenames]
    for root in ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            if is_excluded(dirpath + os.sep) or is_excluded(dirpath):
                continue
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in EXCLUDE_EXTS:
                    continue
                stem = os.path.splitext(fn)[0]
                dir_files[dirpath][stem].append(fn)

    # 构建视频条目
    items = []
    item_id = 0
    for dirpath in sorted(dir_files.keys()):
        stems = dir_files[dirpath]
        for stem in sorted(stems.keys()):
            files = stems[stem]
            exts = {os.path.splitext(f)[1].lower() for f in files}
            # 找主视频文件
            primary = None
            for vext in VIDEO_EXTS:
                for f in files:
                    if os.path.splitext(f)[1].lower() == vext:
                        primary = f
                        break
                if primary:
                    break
            # 找关联字幕文件
            associated = []
            danmaku_text = ''
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in SUBTITLE_EXTS:
                    associated.append(f)
                    if ext == '.ass' and not danmaku_text:
                        danmaku_text = extract_danmaku_text(os.path.join(dirpath, f))

            if primary:
                # 视频文件 + 关联字幕
                item_id += 1
                items.append({
                    'id': f'{item_id:04d}',
                    'source_path': os.path.join(dirpath, primary),
                    'filename': primary,
                    'stem': stem,
                    'has_danmaku': bool(danmaku_text),
                    'danmaku_excerpt': danmaku_text,
                    'associated_files': [os.path.join(dirpath, f) for f in associated],
                })
            elif associated:
                # 孤立字幕文件（无配对视频）
                item_id += 1
                items.append({
                    'id': f'{item_id:04d}',
                    'source_path': os.path.join(dirpath, associated[0]),
                    'filename': associated[0],
                    'stem': stem,
                    'has_danmaku': bool(danmaku_text),
                    'danmaku_excerpt': danmaku_text,
                    'associated_files': [os.path.join(dirpath, f) for f in associated[1:]],
                })
    return items


# ============ LLM 调用 ============
def call_llm(messages, max_tokens=65536, timeout=120, client_retries=4):
    """调用后端 LLM 池，含客户端 429 重试"""
    payload = {
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': max_tokens,
        'timeout': 60,
        'retries': 3,
        'project': PROJECT,
    }
    for attempt in range(client_retries):
        try:
            r = requests.post(f'{API}/llm/pool/call', json=payload, timeout=timeout)
            if r.status_code != 200:
                if attempt < client_retries - 1:
                    time.sleep(8)
                    continue
                return None, f'HTTP {r.status_code}: {r.text[:200]}'
            data = r.json()
            if data.get('ok'):
                return data.get('content', ''), None
            error = data.get('error', 'unknown')
            if ('429' in error or 'rate_limited' in error) and attempt < client_retries - 1:
                time.sleep(12)
                continue
            return None, error
        except Exception as e:
            if attempt < client_retries - 1:
                time.sleep(5)
                continue
            return None, f'{type(e).__name__}: {str(e)[:200]}'
    return None, '客户端重试耗尽(429)'


def parse_json_response(text):
    """从 LLM 响应中解析 JSON"""
    if not text:
        return None, '空响应'
    text = text.strip()
    # 去 markdown 代码块
    if text.startswith('```'):
        lines = text.split('\n')
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        return json.loads(text), None
    except Exception:
        pass
    s = text.find('{')
    e = text.rfind('}')
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1]), None
        except Exception:
            pass
    return None, f'JSON解析失败: {text[:200]}'


def classify_item(item):
    """分类单个视频条目"""
    filename = item['filename']
    danmaku = item.get('danmaku_excerpt', '')

    # 构建用户消息
    user_msg = f'## 文件名\n{filename}'
    if danmaku:
        user_msg += f'\n\n## 弹幕样本\n{danmaku}'

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_msg},
    ]
    content, err = call_llm(messages)
    result = dict(item)
    if err:
        result['category_id'] = 10
        result['category_name'] = '无法分类'
        result['confidence'] = 0
        result['reason'] = f'错误: {err}'
        result['error'] = err
        result['raw_response'] = content or ''
        return result

    parsed, parse_err = parse_json_response(content)
    if parse_err or not isinstance(parsed, dict):
        result['category_id'] = 10
        result['category_name'] = '无法分类'
        result['confidence'] = 0
        result['reason'] = f'解析失败: {parse_err}'
        result['error'] = parse_err
        result['raw_response'] = (content or '')[:300]
        return result

    cat_id = parsed.get('category_id', 10)
    try:
        cat_id = int(cat_id)
    except (TypeError, ValueError):
        cat_id = 10
    if cat_id not in CATEGORIES:
        cat_id = 10

    result['category_id'] = cat_id
    result['category_name'] = CATEGORIES[cat_id]
    result['confidence'] = parsed.get('confidence', 0.5)
    result['reason'] = parsed.get('reason', '')
    result['error'] = None
    result.pop('raw_response', None)
    return result


# ============ 主流程 ============
def load_done_ids(jsonl_file):
    """加载已处理的ID"""
    done = set()
    if jsonl_file.exists():
        with open(jsonl_file, encoding='utf-8') as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    done.add(obj['id'])
                except Exception:
                    pass
    return done


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    jsonl_file = OUTPUT / 'classification_results.jsonl'
    summary_file = OUTPUT / 'classification_summary.json'

    # 断点续传
    done_ids = load_done_ids(jsonl_file)
    print(f'已处理: {len(done_ids)} 条')

    # 扫描文件
    print('扫描文件中...')
    items = scan_files()
    print(f'总视频条目: {len(items)}')

    # 过滤已处理
    pending = [it for it in items if it['id'] not in done_ids]
    print(f'待处理: {len(pending)} 条')

    # 统计有弹幕的
    has_danmaku = sum(1 for it in pending if it['has_danmaku'])
    print(f'有弹幕: {has_danmaku} 条, 无弹幕(仅文件名): {len(pending) - has_danmaku} 条')
    print(f'并发数: {CONCURRENCY}')
    print()

    if not pending:
        print('全部已处理，生成统计...')
        generate_summary(jsonl_file, summary_file)
        return

    # 并发分类
    result_fp = open(jsonl_file, 'a', encoding='utf-8')
    lock = threading.Lock()
    completed = [0]
    total = len(pending)
    start = time.time()
    cat_counter = Counter()

    def write_result(result):
        with lock:
            # 去掉 danmaku_excerpt 节省空间（保留 has_danmaku 标记）
            save = {k: v for k, v in result.items() if k != 'danmaku_excerpt'}
            result_fp.write(json.dumps(save, ensure_ascii=False) + '\n')
            result_fp.flush()
            completed[0] += 1
            cat_counter[result.get('category_name', '未知')] += 1
            c = completed[0]
            if c % 50 == 0 or c == total:
                el = time.time() - start
                rate = c / el if el > 0 else 0
                eta = (total - c) / rate if rate > 0 else 0
                print(f'  进度: {c}/{total} ({c / total * 100:.1f}%) '
                      f'速率: {rate:.1f}/s ETA: {eta:.0f}s')

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(classify_item, it): it for it in pending}
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
                write_result(res)
            except Exception as e:
                print(f'  [异常] {e}')
                # 记录失败项
                item = futures[f]
                err_result = dict(item)
                err_result['category_id'] = 10
                err_result['category_name'] = '无法分类'
                err_result['confidence'] = 0
                err_result['reason'] = f'异常: {e}'
                err_result['error'] = str(e)
                write_result(err_result)

    result_fp.close()
    el = time.time() - start
    print(f'\n分类完成! 耗时: {el:.1f}s')
    generate_summary(jsonl_file, summary_file)


def generate_summary(jsonl_file, summary_file):
    """生成统计摘要"""
    cat_stats = Counter()
    confidence_buckets = {'high(>=0.8)': 0, 'mid(0.5-0.8)': 0, 'low(<0.5)': 0, 'error': 0}
    total = 0
    errors = 0
    samples = defaultdict(list)

    if jsonl_file.exists():
        with open(jsonl_file, encoding='utf-8') as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    total += 1
                    cat = obj.get('category_name', '未知')
                    cat_stats[cat] += 1
                    conf = obj.get('confidence', 0)
                    if obj.get('error'):
                        errors += 1
                        confidence_buckets['error'] += 1
                    elif conf >= 0.8:
                        confidence_buckets['high(>=0.8)'] += 1
                    elif conf >= 0.5:
                        confidence_buckets['mid(0.5-0.8)'] += 1
                    else:
                        confidence_buckets['low(<0.5)'] += 1
                    # 每类收集5个样本
                    if len(samples[cat]) < 5:
                        samples[cat].append({
                            'filename': obj.get('filename', ''),
                            'reason': obj.get('reason', ''),
                            'confidence': conf,
                        })
                except Exception:
                    pass

    summary = {
        'total': total,
        'errors': errors,
        'categories': {cat: cat_stats[cat] for cat in sorted(cat_stats.keys(), key=lambda c: -cat_stats[c])},
        'confidence_distribution': confidence_buckets,
        'samples_per_category': dict(samples),
    }
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f'\n=== 分类统计 (共 {total} 条) ===')
    print(f'错误/异常: {errors}')
    print('\n分类分布:')
    for cat, n in summary['categories'].items():
        pct = n / total * 100 if total else 0
        bar = '#' * int(pct / 2)
        print(f'  {cat:8s} {n:5d} ({pct:5.1f}%) {bar}')
    print('\n置信度分布:')
    for k, v in confidence_buckets.items():
        print(f'  {k:15s} {v}')
    print(f'\n摘要已保存: {summary_file}')
    print(f'详细结果: {jsonl_file}')


if __name__ == '__main__':
    main()
