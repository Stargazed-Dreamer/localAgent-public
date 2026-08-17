#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiMo LRC 歌词分析器
使用 MiMo API 分析歌词的情感、主题、风格等。
支持 MuseArc 数据库关联（保证歌词与歌曲的对应关系）和独立 LRC 文件。

用法:
  # 分析 MuseArc 库中的歌词（通过数据库关联歌曲信息）
  uv run python tools/media_classifier/mimo_lrc_analyze.py --musearc-db F:\\codex\\MuseArc\\realLib\\db\\musearc.db --musearc-lib F:\\codex\\MuseArc\\realLib

  # 分析测试目录中的歌词
  uv run python tools/media_classifier/mimo_lrc_analyze.py --test-dir "E:\\<data_drive>:\<projects_root>\\歌曲分类\\测试文件"

  # 两者同时
  uv run python tools/media_classifier/mimo_lrc_analyze.py --musearc-db ... --musearc-lib ... --test-dir ...
"""

import json
import os
import re
import sys
import time
import argparse
import sqlite3
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from server.llm_pool import call_via_backend, check_backend_pool, print_backend_pool_status

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MAX_WORKERS = 30       # 并发 HTTP 调用数（后端统一管理 key 级并发）

STATE_FILE = PROJECT_ROOT / "temp" / "mimo_lrc_state.json"
OUTPUT_FILE = PROJECT_ROOT / "temp" / "mimo_lrc_results.json"

# ==================== API 调用 ====================

def call_mimo(prompt, project="LRC歌词分析"):
    """通过后端代理调用 MiMo API（project 用于 per-project token 统计）"""
    result = call_via_backend(prompt, max_tokens=4096, temperature=0.5, timeout=120,
                              project=project, http_timeout=300)
    if result:
        return result.strip()
    return None

def init_llm_pool():
    """检查后端 LLM 池是否可用"""
    if not check_backend_pool():
        print("后端 LLM 池不可用，退出。")
        return False
    result = call_mimo("回复OK")
    if result and "OK" in result.upper():
        print("[API 检查] 后端池可用")
        return True
    print("[API 检查] 后端池调用测试失败！")
    return False

# ==================== LRC 解析 ====================

def parse_lrc(content):
    """解析 LRC 文件，提取纯歌词文本（去除时间标签和元数据）"""
    lines = content.splitlines()
    lyrics = []
    for line in lines:
        # 去除时间标签 [mm:ss.xx] 或 [mm:ss]
        text = re.sub(r'\[[\d:.]+\]', '', line).strip()
        # 跳过空的行
        if not text:
            continue
        # 跳过元数据 [ti:...], [ar:...], [al:...], [by:...], [offset:...]
        if re.match(r'^\[a-z]{2}:', text, re.I):
            continue
        lyrics.append(text)
    return '\n'.join(lyrics)

def read_lrc_safe(filepath):
    """安全读取 LRC 文件"""
    for enc in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1'):
        try:
            return Path(filepath).read_text(encoding=enc)
        except (UnicodeDecodeError, Exception):
            continue
    return None

# ==================== MuseArc 数据库关联 ====================

def get_musearc_lrc_files(db_path, lib_path):
    """从 MuseArc 数据库获取 LRC 文件列表及关联的歌曲信息"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cursor = conn.execute("""
        SELECT
            l.lyrics_id,
            l.storage_relpath,
            l.lyrics_title,
            l.lyrics_artist,
            l.lyrics_album,
            l.line_count,
            t.title as track_title,
            t.artist as track_artist,
            t.album as track_album,
            t.track_id
        FROM lyrics l
        LEFT JOIN track_lyrics tl ON l.lyrics_id = tl.lyrics_id AND tl.is_primary = 1
        LEFT JOIN tracks t ON tl.track_id = t.track_id
        WHERE l.deleted_at IS NULL
        ORDER BY l.lyrics_id
    """)

    results = []
    missing = 0
    for row in cursor:
        # storage_relpath 格式如 "data/lyrics/XX/lrc_XXXX.lrc"
        lrc_path = Path(lib_path) / row['storage_relpath']
        if not lrc_path.exists():
            missing += 1
            continue

        # 优先使用 track 信息，回退到 lyrics 自带信息
        title = row['track_title'] or row['lyrics_title'] or ''
        artist = row['track_artist'] or row['lyrics_artist'] or ''
        album = row['track_album'] or row['lyrics_album'] or ''

        results.append({
            'lyrics_id': row['lyrics_id'],
            'track_id': row['track_id'],
            'lrc_path': str(lrc_path),
            'storage_relpath': row['storage_relpath'],
            'title': title,
            'artist': artist,
            'album': album,
            'line_count': row['line_count'] or 0,
        })

    conn.close()
    print(f"  数据库查询完成: {len(results)} 个有效 LRC 文件, {missing} 个文件缺失")
    return results

def get_test_lrc_files(directory):
    """从测试目录获取 LRC 文件（文件名即歌曲名）"""
    results = []
    for path in sorted(Path(directory).rglob('*.lrc')):
        title = path.stem
        # 去除常见后缀如 (2), (3)
        title = re.sub(r'\s*\(\d+\)\s*$', '', title)
        results.append({
            'lyrics_id': None,
            'track_id': None,
            'lrc_path': str(path),
            'storage_relpath': None,
            'title': title,
            'artist': '',
            'album': '',
            'line_count': 0,
        })
    return results

# ==================== 歌词分析 ====================

def analyze_lyrics(lyrics, title="", artist=""):
    """调用 MiMo 分析歌词"""
    prompt = f"""请分析以下歌词，返回JSON格式的分析结果。

歌曲：{title}
歌手：{artist}

歌词：
{lyrics}

请返回以下JSON格式（直接返回JSON，不要加```标记或任何解释文字）：
{{
  "emotion": "主要情感（快乐/悲伤/激昂/平静/忧郁/温暖/热血/伤感/轻松/思念/愤怒/恐惧/迷茫/释然）",
  "emotion_secondary": "次要情感（同上选项）",
  "theme": ["主题标签，从以下选择：爱情/励志/古风/叙事/友情/亲情/自然/人生/社会/梦想/离别/思念/青春/孤独/战争/宗教/童趣/故乡/旅行/成长"],
  "style": "语言风格（口语化/文艺/直白/诗意/古雅/现代/民谣/说唱/电子/摇滚）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "summary": "一句话概括歌词内容",
  "mood_score": 5,
  "energy_level": 5,
  "language": "歌词主要语言（中文/英文/日文/韩文/粤语/混合/纯音乐/无歌词）",
  "imagery": ["意象1", "意象2"],
  "narrative_type": "叙事类型（抒情/叙事/说理/意识流/对话/独白）",
  "suitable_scene": "适合场景（通勤/学习/运动/放松/聚会/睡前/驾驶/工作）"
}}

注意：mood_score 和 energy_level 是1-10的整数（1=最低，10=最高）。"""

    result = call_mimo(prompt)
    if not result:
        return None

    # 去除 markdown 标记
    result = re.sub(r'^```\s*(?:json)?\s*\n?', '', result)
    result = re.sub(r'\n?```\s*$', '', result)

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        # 尝试提取 JSON 部分
        match = re.search(r'\{[\s\S]*\}', result)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"raw_response": result, "parse_error": True}

def process_one_lrc(f):
    """处理单个 LRC 文件"""
    content = read_lrc_safe(f['lrc_path'])
    if not content:
        return None

    lyrics = parse_lrc(content)
    if not lyrics.strip():
        return None

    # 限制歌词长度（避免 token 过多）
    if len(lyrics) > 8000:
        lyrics = lyrics[:8000] + "\n...(歌词过长，已截断)"

    analysis = analyze_lyrics(lyrics, f['title'], f['artist'])
    if not analysis:
        return None

    return {
        'lyrics_id': f.get('lyrics_id'),
        'track_id': f.get('track_id'),
        'lrc_path': f['lrc_path'],
        'storage_relpath': f.get('storage_relpath'),
        'title': f['title'],
        'artist': f['artist'],
        'album': f.get('album', ''),
        'line_count': f.get('line_count', 0),
        'analysis': analysis,
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    }

# ==================== 状态管理 ====================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_results():
    if OUTPUT_FILE.exists():
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    return []

def save_results(results):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description='MiMo LRC 歌词分析器')
    parser.add_argument('--musearc-db', help='MuseArc 数据库路径')
    parser.add_argument('--musearc-lib', help='MuseArc 库目录路径')
    parser.add_argument('--test-dir', action='append', default=[],
                        help='测试LRC目录（可多次指定）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只扫描不调用API')
    parser.add_argument('--force', action='store_true',
                        help='强制重新处理已处理的文件')
    parser.add_argument('--no-check', action='store_true',
                        help='跳过 API 健康检查')
    parser.add_argument('--output', help='输出文件路径（默认 temp/mimo_lrc_results.json）')
    args = parser.parse_args()

    global OUTPUT_FILE
    if args.output:
        OUTPUT_FILE = Path(args.output)

    # 收集所有 LRC 文件
    lrc_files = []

    if args.musearc_db and args.musearc_lib:
        print("从 MuseArc 数据库加载 LRC 文件...")
        lrc_files.extend(get_musearc_lrc_files(args.musearc_db, args.musearc_lib))

    if args.test_dir:
        for d in args.test_dir:
            print(f"从测试目录加载: {d}")
            files = get_test_lrc_files(d)
            lrc_files.extend(files)
            print(f"  找到 {len(files)} 个 LRC 文件")

    if not lrc_files:
        print("未找到任何 LRC 文件")
        return

    # 去重（按 lrc_path）
    seen = set()
    unique_files = []
    for f in lrc_files:
        if f['lrc_path'] not in seen:
            seen.add(f['lrc_path'])
            unique_files.append(f)
    lrc_files = unique_files

    print(f"\n共 {len(lrc_files)} 个 LRC 文件待处理")

    # 加载状态和已有结果
    state = load_state()
    results = load_results()

    # 过滤已处理
    to_process = []
    for f in lrc_files:
        if not args.force and f['lrc_path'] in state:
            continue
        to_process.append(f)

    print(f"已处理: {len(lrc_files) - len(to_process)}, 待处理: {len(to_process)}")

    if args.dry_run:
        print("\n[预览模式] 待处理文件:")
        for f in to_process[:30]:
            title = f['title'] or Path(f['lrc_path']).stem
            print(f"  - {title} - {f['artist']} ({os.path.basename(f['lrc_path'])})")
        if len(to_process) > 30:
            print(f"  ... 还有 {len(to_process) - 30} 个")
        return

    if not args.no_check:
        if not init_llm_pool():
            print("并发池不可用，退出。使用 --no-check 跳过检查。")
            sys.exit(1)

    # 并发处理
    print(f"开始处理 {len(to_process)} 个 LRC 文件（并发数={MAX_WORKERS}）...", flush=True)
    processed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for f in to_process:
            future = pool.submit(process_one_lrc, f)
            futures[future] = f

        for future in as_completed(futures):
            f = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                    state[f['lrc_path']] = {
                        'title': f['title'],
                        'artist': f['artist'],
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                    }
                    processed += 1
                    title = f['title'] or Path(f['lrc_path']).stem
                    emotion = result.get('analysis', {}).get('emotion', '?')
                    print(f"  [{processed}/{len(to_process)}] {title} - {f['artist']} → {emotion}", flush=True)

                    # 每10个保存一次
                    if processed % 10 == 0:
                        save_results(results)
                        save_state(state)
                else:
                    failed += 1
                    title = f['title'] or Path(f['lrc_path']).stem
                    print(f"  [失败] {title} - {f['artist']}", flush=True)
            except Exception as e:
                failed += 1
                print(f"  [异常] {f['lrc_path']}: {e}", flush=True)

    # 保存最终结果
    save_results(results)
    save_state(state)

    print(f"\n{'='*60}")
    print(f"完成！")
    print(f"  成功处理: {processed}")
    print(f"  失败: {failed}")
    print(f"  总结果数: {len(results)}")
    print(f"  结果文件: {OUTPUT_FILE}")
    print(f"  状态文件: {STATE_FILE}")

    # 打印 token 统计
    if not args.dry_run:
        try:
            print_backend_pool_status()
        except Exception:
            pass
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
