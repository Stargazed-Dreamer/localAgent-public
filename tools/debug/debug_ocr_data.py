"""调试：查看OCR返回的完整数据，输出到文件"""

import requests

API = "http://127.0.0.1:8766"
WINDOW = "异环  "

resp = requests.post(f"{API}/screen/capture", json={"mode": "window", "window_title": WINDOW})
data = resp.json()
ocr_resp = requests.post(f"{API}/ocr/base64", data={"data": data["image"]})
ocr_data = ocr_resp.json()

details = ocr_data.get("details", [])
details.sort(key=lambda x: min(p[1] for p in x.get("box", [[0,0]])))

lines = []
for i, item in enumerate(details):
    text = item.get("text", "")
    box = item.get("box", [])
    if box:
        cy = sum(p[1] for p in box) / 4
        cx = sum(p[0] for p in box) / 4
        lines.append(f"[{i:2d}] y={cy:7.1f} x={cx:7.1f}  '{text}'")

with open("temp/ocr_debug.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"已写入 {len(lines)} 行到 temp/ocr_debug.txt")
