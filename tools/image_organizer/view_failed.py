"""查看失败文件"""
import sqlite3
conn = sqlite3.connect(r'f:\<project_root>\output\image_organizer\files.db')
rows = conn.execute(
    'SELECT source_path, status, error FROM files WHERE status="failed" OR status="florence_done"'
).fetchall()
for r in rows:
    print(f"\n状态: {r[1]}")
    print(f"路径: {r[0]}")
    print(f"错误: {r[2]}")
