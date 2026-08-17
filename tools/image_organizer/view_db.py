"""查看 DB 内容"""
import sqlite3, json
conn = sqlite3.connect(r'f:\<project_root>\output\image_organizer\files.db')
rows = conn.execute(
    'SELECT filename, caption, detailed_caption, ocr_text, width, height, exif_data, '
    'main_category, dimensions, tags, confidence, florence_raw FROM files WHERE status="llm_done"'
).fetchall()
for r in rows:
    print(f"\n=== {r[0]} ===")
    print(f"  分类: {r[7]}")
    print(f"  尺寸: {r[4]}x{r[5]}")
    print(f"  caption: {r[1]}")
    print(f"  detailed: {r[2][:120] if r[2] else None}...")
    print(f"  ocr: {r[3][:80] if r[3] else None}...")
    print(f"  exif: {r[6]}")
    print(f"  dimensions: {r[8]}")
    print(f"  tags: {r[9]}")
    print(f"  confidence: {r[10]}")
    print(f"  florence_raw存在: {bool(r[11])}")
