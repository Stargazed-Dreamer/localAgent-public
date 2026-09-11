"""生成标注所有OCR文本框的图片"""
import base64
import io

import requests
from PIL import Image, ImageDraw, ImageFont

API = "http://127.0.0.1:8766"
WINDOW = "异环  "

# 截图
resp = requests.post(f"{API}/screen/capture", json={"mode": "window", "window_title": WINDOW})
data = resp.json()
img_data = base64.b64decode(data["image"])
img = Image.open(io.BytesIO(img_data))
print(f"截图尺寸: {img.size}")

# OCR
ocr_resp = requests.post(f"{API}/ocr/base64", data={"data": data["image"]})
ocr_data = ocr_resp.json()
details = ocr_data.get("details", [])
print(f"OCR识别到 {len(details)} 个文本块")

# 标注所有文本框
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("arial.ttf", 12)
except Exception:
    font = ImageFont.load_default()

for i, item in enumerate(details):
    text = item.get("text", "")
    box = item.get("box", [])
    if box and len(box) >= 4:
        # 画bbox（红色框）
        pts = [(int(p[0]), int(p[1])) for p in box]
        draw.polygon(pts, outline="red", width=2)

        # 计算中心点
        cx = sum(p[0] for p in box) / 4
        cy = sum(p[1] for p in box) / 4

        # 画中心点（绿色圆）
        draw.ellipse([cx-3, cy-3, cx+3, cy+3], fill="green")

        # 标注序号和中心坐标（蓝色文字）
        label = f"{i}:({cx:.0f},{cy:.0f})"
        draw.text((cx+5, cy-8), label, fill="blue", font=font)

# 保存
img.save("temp/ocr_all_boxes.png")
print("已保存到 temp/ocr_all_boxes.png")

# 打印关键元素的bbox信息
print("\n关键元素:")
for item in details:
    text = item.get("text", "")
    box = item.get("box", [])
    if text in ["限定棋盘", "标准棋盘", "常驻棋盘", "棋盘类型", "棋盘详情", "上一页", "下一页"]:
        if box:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs)/4
            cy = sum(ys)/4
            print(f"  '{text}' x=[{min(xs):.0f},{max(xs):.0f}] y=[{min(ys):.0f},{max(ys):.0f}] center=({cx:.0f},{cy:.0f})")
