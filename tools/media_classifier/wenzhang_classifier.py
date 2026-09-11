"""文字篇章整理脚本
- 解析 txt/docx/json 文件（按空行/空段落分割文段）
- LLM 高并发分类（15 类）+ 特征提取（标题/体裁/关键词/标签/摘要）
- 输出 JSONL + 统计报告
- PDF 文件单独复制
- 审查拒绝的文段单独收录
- 支持断点续传
"""
import concurrent.futures
import json
import os
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path

import docx
import requests

# ============ 配置 ============
ROOT = Path(r"E:\<data_drive>:\<articles_root>")
OUTPUT = Path(r"<project_root>\output\文字篇章整理")
API = "http://127.0.0.1:8766"
CONCURRENCY = 60
PROJECT = "文字篇章整理"
MAX_INPUT_CHARS = 1500          # 文段输入截断长度
BATCH_THRESHOLD = 50            # <此字符数的文段走批量处理
BATCH_SIZE = 30                 # 每批文段数

# 15 分类
CATEGORIES = {
    1: "段子笑话", 2: "生活经验", 3: "健康养生", 4: "恋爱情感", 5: "游戏相关",
    6: "技术知识", 7: "职场就业", 8: "学术学习", 9: "人生感悟", 10: "社交人际",
    11: "网络评论", 12: "节日祝福", 13: "文学创作", 14: "擦边涩涩", 15: "其他",
}

SYSTEM_PROMPT = """你是文本分类与特征提取引擎。分析用户给出的文段，提取特征并分类。

## 分类选项（必须选一个编号）
1.段子笑话 - 搞笑段子、空耳、神评论、玩梗、谐音恶搞
2.生活经验 - 生活技巧、常识、避坑指南
3.健康养生 - 护肤、健身、医疗、中药养生
4.恋爱情感 - 恋爱聊天、约会、情感建议、表白
5.游戏相关 - Steam、游戏梗、游戏评测、游戏设备
6.技术知识 - 编程、硬件、软件、装系统、网络技术
7.职场就业 - 找工作、职业规划、面试、职场经验
8.学术学习 - 论文、学习方法、大学生活、考试
9.人生感悟 - 励志、感悟、智慧语录、哲理
10.社交人际 - 吵架、家庭、宿舍、人际关系处理
11.网络评论 - 锐评、普通评论、懂哥评论、网络热评
12.节日祝福 - 祝福语、节日贺词、吉祥话
13.文学创作 - 诗词、好文、文学性段落、创作
14.擦边涩涩 - 涩涩内容、擦边、成人话题
15.其他 - 无法归入以上类别

## 体裁选项
段子/评论/教程/观点/故事/诗歌/祝福/科普/清单/空耳/其它

## 输出格式
严格输出一行 JSON（不要 markdown 代码块，不要多余文字）：
{"category_id":1,"category_name":"段子笑话","title":"10字内标题","genre":"段子","keywords":["词1","词2","词3"],"tags":["标签1","标签2"],"summary":"30字内摘要"}

要求：
- keywords 给 3-8 个特征词，优先名词和专有名词
- tags 给 2-4 个宽泛标签
- summary 用一句话概括核心内容
- title 提炼主题，不要直接复制原文开头
- 若文段内容涉及色情/违规被你拒绝处理，输出 {"category_id":14,"refused":true,"title":"审查拒绝","genre":"其它","keywords":[],"tags":["审查拒绝"],"summary":"内容被拒绝处理"}"""

BATCH_SYSTEM_PROMPT = """你是文本分类与特征提取引擎。用户给出多条带编号的短文段，请为每条返回分类结果。

## 分类选项
1.段子笑话 2.生活经验 3.健康养生 4.恋爱情感 5.游戏相关 6.技术知识 7.职场就业 8.学术学习 9.人生感悟 10.社交人际 11.网络评论 12.节日祝福 13.文学创作 14.擦边涩涩 15.其他

## 体裁选项
段子/评论/教程/观点/故事/诗歌/祝福/科普/清单/空耳/其它

## 输出格式
严格输出 JSON 数组（不要 markdown，不要多余文字）：
[{"id":1,"category_id":1,"category_name":"段子笑话","title":"标题","genre":"段子","keywords":["词"],"tags":["标签"],"summary":"摘要"},...]

每条摘要30字内，标题10字内，keywords 2-5个，tags 1-3个。"""


# ============ 文件解析 ============
def split_txt_segments(text, filename):
    """txt 按空行分割，去首行标题"""
    lines = text.split("\n")
    segments = []
    current = []
    for line in lines:
        if line.strip() == "":
            if current:
                seg = "\n".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
        else:
            current.append(line)
    if current:
        seg = "\n".join(current).strip()
        if seg:
            segments.append(seg)
    # 去掉第一段如果是标题
    if segments:
        fname = Path(filename).stem
        first = segments[0].strip()
        if len(first) < 30 and (first == fname or first in fname or fname in first):
            segments = segments[1:]
    return segments


def split_docx_segments(doc, filename):
    """docx 按空段落分割"""
    segments = []
    current = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t == "":
            if current:
                seg = "\n".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
        else:
            current.append(t)
    if current:
        seg = "\n".join(current).strip()
        if seg:
            segments.append(seg)
    # 去掉第一段如果是标题
    if segments:
        fname = Path(filename).stem
        first = segments[0].strip()
        if len(first) < 30 and (first == fname or first in fname or fname in first):
            segments = segments[1:]
    return segments


def parse_all_files():
    """解析所有 txt/docx/json 文件，返回 [(source, text), ...]"""
    segments = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            rel = str(p.relative_to(ROOT))
            try:
                if ext == ".txt":
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    for seg in split_txt_segments(text, fn):
                        segments.append((rel, seg))
                elif ext == ".docx":
                    doc = docx.Document(str(p))
                    for seg in split_docx_segments(doc, fn):
                        segments.append((rel, seg))
                elif ext == ".json":
                    data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and "content" in item:
                                c = str(item["content"]).strip()
                                if c:
                                    segments.append((rel, c))
                            elif isinstance(item, str) and item.strip():
                                segments.append((rel, item.strip()))
                    elif isinstance(data, dict):
                        segments.append((rel, json.dumps(data, ensure_ascii=False)[:1000]))
            except Exception as e:
                print(f"  [跳过] {rel}: {e}")
    return segments


def preprocess(segments):
    """去重 + 过滤过短"""
    seen = set()
    cleaned = []
    for source, text in segments:
        t = text.strip()
        if len(t) < 5:
            continue
        key = t[:200]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((source, text))
    return cleaned


# ============ LLM 调用 ============
def call_llm(messages, max_tokens=16384, timeout=300):
    """调用后端 LLM 池（思考模型，max_tokens 要大）
    - 空响应重试 1 次（等 3 秒）
    - 429 等待 60 秒后重试，最多 5 次（后端会自动换 key）
    """
    payload = {
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "timeout": 180,
        "retries": 3,
        "project": PROJECT,
    }
    empty_retry = 0      # 空响应已重试次数
    rl_retry = 0         # 429 已重试次数
    MAX_RL = 5
    while True:
        try:
            r = requests.post(f"{API}/llm/pool/call", json=payload, timeout=timeout)
            if r.status_code != 200:
                if rl_retry < MAX_RL:
                    rl_retry += 1
                    time.sleep(15)
                    continue
                return None, f"HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
            if data.get("ok"):
                content = data.get("content", "")
                if not content.strip():
                    # 空响应：重试 1 次
                    if empty_retry < 1:
                        empty_retry += 1
                        time.sleep(3)
                        continue
                return content, None
            error = data.get("error", "unknown")
            if "429" in error or "rate_limited" in error:
                # 429：等 60 秒，后端自动换 key
                if rl_retry < MAX_RL:
                    rl_retry += 1
                    time.sleep(60)
                    continue
                return None, error
            return None, error
        except Exception as e:
            if rl_retry < MAX_RL:
                rl_retry += 1
                time.sleep(10)
                continue
            return None, f"{type(e).__name__}: {str(e)[:200]}"


def parse_json_response(text):
    """从 LLM 响应中解析 JSON（对象或数组）"""
    if not text:
        return None, "空响应"
    text = text.strip()
    # 去 markdown 代码块
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 直接解析
    try:
        return json.loads(text), None
    except Exception:
        pass
    # 提取 JSON 对象
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1]), None
        except Exception:
            pass
    # 提取 JSON 数组
    s = text.find("[")
    e = text.rfind("]")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1]), None
        except Exception:
            pass
    return None, f"JSON解析失败: {text[:200]}"


def classify_single(seg_id, source, text):
    """分类单个文段"""
    input_text = text[:MAX_INPUT_CHARS]
    if len(text) > MAX_INPUT_CHARS:
        input_text += "\n...(截断)"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"## 文段\n{input_text}"},
    ]
    content, err = call_llm(messages)
    if err:
        return {
            "id": seg_id, "source_file": source, "original_text": text,
            "char_count": len(text), "error": err, "raw_response": content or "",
        }
    result, parse_err = parse_json_response(content)
    if parse_err or not isinstance(result, dict):
        return {
            "id": seg_id, "source_file": source, "original_text": text,
            "char_count": len(text),
            "error": f"审查拒绝或解析失败: {parse_err}",
            "raw_response": (content or "")[:500],
        }
    cat_id = result.get("category_id", 15)
    try:
        cat_id = int(cat_id)
    except (TypeError, ValueError):
        cat_id = 15
    if cat_id not in CATEGORIES:
        cat_id = 15
    refused = result.get("refused", False)
    return {
        "id": seg_id, "source_file": source,
        "category_id": cat_id, "category_name": CATEGORIES[cat_id],
        "title": result.get("title", ""),
        "genre": result.get("genre", "其它"),
        "keywords": result.get("keywords", []) or [],
        "tags": result.get("tags", []) or [],
        "summary": result.get("summary", ""),
        "original_text": text, "char_count": len(text),
        "error": "审查拒绝" if refused else None,
        "refused": refused,
    }


def classify_batch(batch_id, source, items):
    """批量分类短文段，items=[(seg_id, text), ...]"""
    lines = []
    for i, (sid, text) in enumerate(items, 1):
        lines.append(f"[{i}] {text[:100]}")
    input_text = "\n".join(lines)
    messages = [
        {"role": "system", "content": BATCH_SYSTEM_PROMPT},
        {"role": "user", "content": f"请分类以下{len(items)}条短文段：\n{input_text}"},
    ]
    content, err = call_llm(messages, max_tokens=32768)
    results = []
    if err:
        for sid, text in items:
            results.append({
                "id": sid, "source_file": source, "original_text": text,
                "char_count": len(text), "error": err, "raw_response": "",
            })
        return results
    parsed, parse_err = parse_json_response(content)
    if parse_err or not isinstance(parsed, list):
        for sid, text in items:
            results.append({
                "id": sid, "source_file": source, "original_text": text,
                "char_count": len(text),
                "error": f"批量解析失败: {parse_err}",
                "raw_response": (content or "")[:200],
            })
        return results
    by_id = {}
    for item in parsed:
        if isinstance(item, dict) and "id" in item:
            try:
                by_id[int(item["id"])] = item
            except (TypeError, ValueError):
                pass
    for i, (sid, text) in enumerate(items, 1):
        item = by_id.get(i)
        if item:
            cat_id = item.get("category_id", 15)
            try:
                cat_id = int(cat_id)
            except (TypeError, ValueError):
                cat_id = 15
            if cat_id not in CATEGORIES:
                cat_id = 15
            results.append({
                "id": sid, "source_file": source,
                "category_id": cat_id, "category_name": CATEGORIES[cat_id],
                "title": item.get("title", ""),
                "genre": item.get("genre", "其它"),
                "keywords": item.get("keywords", []) or [],
                "tags": item.get("tags", []) or [],
                "summary": item.get("summary", ""),
                "original_text": text, "char_count": len(text),
                "error": None,
            })
        else:
            results.append({
                "id": sid, "source_file": source, "original_text": text,
                "char_count": len(text), "error": "批量结果缺失", "raw_response": "",
            })
    return results


# ============ 主流程 ============
def load_done_ids(result_file, reject_file):
    done = set()
    for f in [result_file, reject_file]:
        if f.exists():
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    try:
                        done.add(json.loads(line)["id"])
                    except Exception:
                        pass
    return done


def copy_pdfs():
    pdf_dir = OUTPUT / "PDF文件"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        for fn in filenames:
            if fn.lower().endswith(".pdf"):
                src = Path(dirpath) / fn
                rel = src.relative_to(ROOT)
                dst = pdf_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    n += 1
                except Exception as e:
                    print(f"  [PDF复制失败] {rel}: {e}")
    return n


def generate_report():
    result_file = OUTPUT / "all_segments.jsonl"
    reject_file = OUTPUT / "_审查拒绝.jsonl"
    cat_stats = defaultdict(int)
    genre_stats = defaultdict(int)
    source_stats = defaultdict(int)
    total = 0
    rejected = 0
    if result_file.exists():
        with open(result_file, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    total += 1
                    cat_stats[obj.get("category_name", "未知")] += 1
                    genre_stats[obj.get("genre", "未知")] += 1
                    source_stats[obj.get("source_file", "未知")] += 1
                except Exception:
                    pass
    if reject_file.exists():
        with open(reject_file, encoding="utf-8") as f:
            for line in f:
                rejected += 1
    report = {
        "total_classified": total,
        "total_rejected": rejected,
        "total": total + rejected,
        "categories": dict(sorted(cat_stats.items(), key=lambda x: -x[1])),
        "genres": dict(sorted(genre_stats.items(), key=lambda x: -x[1])),
        "top_sources": dict(sorted(source_stats.items(), key=lambda x: -x[1])[:20]),
    }
    report_file = OUTPUT / "_统计报告.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n=== 统计报告 ===")
    print(f"已分类: {total}  审查拒绝/错误: {rejected}  总计: {total + rejected}")
    print("\n分类分布:")
    for cat, n in report["categories"].items():
        pct = n / total * 100 if total else 0
        print(f"  {cat:8s} {n:5d} ({pct:.1f}%)")
    print(f"\n报告已保存: {report_file}")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result_file = OUTPUT / "all_segments.jsonl"
    reject_file = OUTPUT / "_审查拒绝.jsonl"

    # 断点续传
    done_ids = load_done_ids(result_file, reject_file)
    print(f"已处理: {len(done_ids)} 条")

    # 解析文件
    print("解析文件中...")
    segments = parse_all_files()
    print(f"原始文段: {len(segments)} 条")
    segments = preprocess(segments)
    print(f"预处理后: {len(segments)} 条")

    # 分配 ID，过滤已处理
    all_tasks = []
    for i, (source, text) in enumerate(segments, 1):
        seg_id = f"{i:05d}"
        if seg_id in done_ids:
            continue
        all_tasks.append((seg_id, source, text))
    print(f"待处理: {len(all_tasks)} 条")
    if not all_tasks:
        print("全部已处理，生成报告...")
        copy_pdfs()
        generate_report()
        return

    # 分离短文段（批量）和长文段（单独）
    single_tasks = []
    batch_groups = defaultdict(list)
    for seg_id, source, text in all_tasks:
        if len(text) < BATCH_THRESHOLD:
            batch_groups[source].append((seg_id, text))
        else:
            single_tasks.append((seg_id, source, text))
    batch_tasks = []
    bc = 0
    for source, items in batch_groups.items():
        for i in range(0, len(items), BATCH_SIZE):
            bc += 1
            batch_tasks.append((f"batch_{bc:04d}", source, items[i:i + BATCH_SIZE]))

    batch_seg_count = sum(len(b[2]) for b in batch_tasks)
    print(f"单独处理: {len(single_tasks)} 条")
    print(f"批量处理: {batch_seg_count} 条 ({len(batch_tasks)} 批)")
    print(f"并发数: {CONCURRENCY}")
    print()

    result_fp = open(result_file, "a", encoding="utf-8")
    reject_fp = open(reject_file, "a", encoding="utf-8")
    lock = threading.Lock()
    completed = [0]
    total_tasks = len(single_tasks) + len(batch_tasks)
    start = time.time()

    def write_result(result):
        with lock:
            if result.get("error") and "category_id" not in result:
                reject_fp.write(json.dumps(result, ensure_ascii=False) + "\n")
                reject_fp.flush()
            else:
                result_fp.write(json.dumps(result, ensure_ascii=False) + "\n")
                result_fp.flush()
            completed[0] += 1
            c = completed[0]
            if c % 50 == 0 or c == total_tasks:
                el = time.time() - start
                rate = c / el if el > 0 else 0
                eta = (total_tasks - c) / rate if rate > 0 else 0
                print(f"  进度: {c}/{total_tasks} ({c / total_tasks * 100:.1f}%) "
                      f"速率: {rate:.1f}/s ETA: {eta:.0f}s")

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = []
        for t in single_tasks:
            f = ex.submit(classify_single, t[0], t[1], t[2])
            f._kind = "single"
            futures.append(f)
        for t in batch_tasks:
            f = ex.submit(classify_batch, t[0], t[1], t[2])
            f._kind = "batch"
            futures.append(f)
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
                if f._kind == "single":
                    write_result(res)
                else:
                    for r in res:
                        write_result(r)
            except Exception as e:
                print(f"  [异常] {e}")

    result_fp.close()
    reject_fp.close()
    el = time.time() - start
    print(f"\nLLM 处理完成! 耗时: {el:.1f}s")

    print("\n复制 PDF 文件...")
    n = copy_pdfs()
    print(f"复制 PDF: {n} 个")

    generate_report()


if __name__ == "__main__":
    main()
