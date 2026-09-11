"""读取所有 docx 文件的段落结构，判断是短文段集合还是长文章"""
import os
from pathlib import Path

import docx

ROOT = Path(r"E:\<data_drive>:\<articles_root>")

docx_files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    for fn in filenames:
        p = Path(dirpath) / fn
        if p.suffix.lower() == ".docx":
            docx_files.append(p)

print(f"共 {len(docx_files)} 个 docx 文件\n")

for p in sorted(docx_files):
    rel = p.relative_to(ROOT)
    try:
        doc = docx.Document(str(p))
        # 统计非空段落
        non_empty = [para.text.strip() for para in doc.paragraphs if para.text.strip()]
        n_paras = len(non_empty)
        # 计算平均段落长度
        if n_paras > 0:
            avg_len = sum(len(t) for t in non_empty) / n_paras
            max_len = max(len(t) for t in non_empty)
        else:
            avg_len = 0
            max_len = 0
        # 判断类型：短文段集合（多段、平均短）vs 长文章（少段、平均长）
        if n_paras >= 5 and avg_len < 300:
            kind = "短文段集合"
        elif n_paras <= 3:
            kind = "极少段落"
        else:
            kind = "长文章/混合"
        print(f"[{kind}] {rel}")
        print(f"    段落数={n_paras}, 平均长度={avg_len:.0f}, 最大长度={max_len}")
        # 显示前2段
        for i, t in enumerate(non_empty[:2]):
            print(f"    [{i}] {t[:100]}")
        print()
    except Exception as e:
        print(f"[错误] {rel}: {e}\n")
