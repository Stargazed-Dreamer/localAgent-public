"""扫描 文字篇章 文件夹，统计文件类型、数量，并读取 docx 样本"""
import os
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(r"E:\<data_drive>:\<articles_root>")

# 统计文件类型
ext_count = defaultdict(int)
all_files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    for fn in filenames:
        p = Path(dirpath) / fn
        ext = p.suffix.lower()
        ext_count[ext] += 1
        all_files.append(p)

print("=== 文件类型统计 ===")
for ext, n in sorted(ext_count.items(), key=lambda x: -x[1]):
    print(f"  {ext or '(无后缀)':15s} {n:4d}")
print(f"  总计: {len(all_files)} 个文件")

# 按目录统计
print("\n=== 各目录文件数 ===")
dir_count = defaultdict(int)
for p in all_files:
    rel = p.parent.relative_to(ROOT)
    dir_count[str(rel)] += 1
for d, n in sorted(dir_count.items()):
    print(f"  {d:50s} {n:3d}")

# 读取几个 docx 文件样本
print("\n=== docx 文件样本 ===")
try:
    import docx
except ImportError:
    print("python-docx 未安装")
    sys.exit(0)

docx_files = [p for p in all_files if p.suffix.lower() == ".docx"]
sample_docx = docx_files[:5]
for p in sample_docx:
    print(f"\n--- {p.relative_to(ROOT)} ---")
    try:
        doc = docx.Document(str(p))
        for i, para in enumerate(doc.paragraphs[:15]):
            text = para.text.strip()
            if text:
                print(f"  [{i}] {text[:120]}")
    except Exception as e:
        print(f"  读取失败: {e}")
