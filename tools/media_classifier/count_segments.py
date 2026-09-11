"""统计所有文件的文段数量（按空行/空段落分割）"""
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"E:\<data_drive>:\<articles_root>")
import docx


def split_txt_segments(text):
    """txt 文件：按空行分割，第一行通常是标题"""
    lines = text.split("\n")
    # 去掉第一行标题（如果第一行后跟空行）
    segments = []
    current = []
    for i, line in enumerate(lines):
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
    return segments


def split_docx_segments(doc):
    """docx 文件：按空段落分割"""
    segments = []
    current = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text == "":
            if current:
                seg = "\n".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
        else:
            current.append(text)
    if current:
        seg = "\n".join(current).strip()
        if seg:
            segments.append(seg)
    return segments


stats = defaultdict(lambda: {"files": 0, "segments": 0, "min_seg": 999999, "max_seg": 0})
all_segments_info = []  # (file_path, n_segments)
total_segments = 0
files_by_type = defaultdict(list)

for dirpath, dirnames, filenames in os.walk(ROOT):
    for fn in filenames:
        p = Path(dirpath) / fn
        ext = ext = p.suffix.lower()
        files_by_type[ext].append(p)

# txt
for p in files_by_type.get(".txt", []):
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
        segs = split_txt_segments(text)
        # 去掉第一段如果是标题（短且等于文件名）
        if segs and len(segs[0]) < 30:
            segs = segs[1:]
        n = len(segs)
        total_segments += n
        all_segments_info.append((str(p.relative_to(ROOT)), n, "txt"))
        stats["txt"]["files"] += 1
        stats["txt"]["segments"] += n
        if n < stats["txt"]["min_seg"]:
            stats["txt"]["min_seg"] = n
        if n > stats["txt"]["max_seg"]:
            stats["txt"]["max_seg"] = n
    except Exception as e:
        print(f"txt 读取失败 {p}: {e}")

# docx
for p in files_by_type.get(".docx", []):
    try:
        doc = docx.Document(str(p))
        segs = split_docx_segments(doc)
        n = len(segs)
        total_segments += n
        all_segments_info.append((str(p.relative_to(ROOT)), n, "docx"))
        stats["docx"]["files"] += 1
        stats["docx"]["segments"] += n
        if n < stats["docx"]["min_seg"]:
            stats["docx"]["min_seg"] = n
        if n > stats["docx"]["max_seg"]:
            stats["docx"]["max_seg"] = n
    except Exception as e:
        print(f"docx 读取失败 {p}: {e}")

# pdf/json/avif 只统计数量
for ext in [".pdf", ".json", ".avif"]:
    if ext in files_by_type:
        stats[ext]["files"] = len(files_by_type[ext])
        stats[ext]["segments"] = 0  # 待提取

print("=== 文段统计 ===")
for t in ["txt", "docx", "pdf", "json", "avif"]:
    s = stats[t]
    print(f"{t:6s}: 文件数={s['files']:3d}, 文段数={s['segments']:5d}", end="")
    if t in ("txt", "docx") and s["files"] > 0:
        print(f", 单文件范围={s['min_seg']}~{s['max_seg']}", end="")
    print()

print(f"\n可处理文段总数（txt+docx）: {stats['txt']['segments'] + stats['docx']['segments']}")
print(f"PDF 文件数（需 docviewer 提取）: {stats['pdf']['files']}")

# 列出文段最多的文件
print("\n=== 文段最多的 10 个文件 ===")
all_segments_info.sort(key=lambda x: -x[1])
for path, n, t in all_segments_info[:10]:
    print(f"  {n:5d} 段  [{t}] {path}")

# 估算 LLM 调用
total = stats['txt']['segments'] + stats['docx']['segments']
print("\n=== LLM 调用估算 ===")
print(f"文段总数: {total}")
print("并发数: 60 (池上限), 实际可用 key 20 个 × 3 = 60")
print(f"假设每次调用 3 秒: {total / 60 * 3 / 60:.1f} 分钟")
print(f"假设每次调用 5 秒: {total / 60 * 5 / 60:.1f} 分钟")
