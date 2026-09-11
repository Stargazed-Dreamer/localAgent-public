"""生成不同缩放倍率的标注图片供用户选择"""
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
W, H = img.size

# OCR
ocr_resp = requests.post(f"{API}/ocr/base64", data={"data": data["image"]})
ocr_data = ocr_resp.json()
details = ocr_data.get("details", [])

# 中心点（截图中心）
CX, CY = W // 2, H // 2  # 1280, 720

# 根据用户数据计算缩放比:
# Point1: OCR=(460,488) -> Correct=(615,493)
#   sx = (615-1280)/(460-1280) = 0.811
#   sy = (493-720)/(488-720) = 0.978
# Point2: OCR=(722,487) -> Correct=(816,495)
#   sx = (816-1280)/(722-1280) = 0.832
#   sy = (495-720)/(487-720) = 0.966
# Average: sx≈0.82, sy≈0.97

scale_variants = [
    (0.80, 0.95, "sx=0.80_sy=0.95"),
    (0.82, 0.97, "sx=0.82_sy=0.97"),
    (0.85, 1.00, "sx=0.85_sy=1.00"),
    (0.88, 1.00, "sx=0.88_sy=1.00"),
]

try:
    font = ImageFont.truetype("arial.ttf", 14)
except Exception:
    font = ImageFont.load_default()

for sx, sy, label in scale_variants:
    test_img = img.copy()
    draw = ImageDraw.Draw(test_img)

    for i, item in enumerate(details):
        text = item.get("text", "")
        box = item.get("box", [])
        if box and len(box) >= 4:
            # 原始OCR坐标
            orig_pts = [(p[0], p[1]) for p in box]

            # 中心缩放: corrected = center + (ocr - center) * scale
            scaled_pts = []
            for px, py in orig_pts:
                new_x = CX + (px - CX) * sx
                new_y = CY + (py - CY) * sy
                scaled_pts.append((int(new_x), int(new_y)))

            # 画缩放后的bbox（绿色框）
            draw.polygon(scaled_pts, outline="lime", width=2)

            # 也画原始bbox（红色框，细线）
            orig_int_pts = [(int(p[0]), int(p[1])) for p in orig_pts]
            draw.polygon(orig_int_pts, outline="red", width=1)

            # 计算缩放后中心点
            cx = sum(p[0] for p in scaled_pts) / 4
            cy = sum(p[1] for p in scaled_pts) / 4

            # 画中心点
            draw.ellipse([cx-4, cy-4, cx+4, cy+4], fill="lime")

            # 标注文字
            short_text = text[:8] if len(text) > 8 else text
            draw.text((cx+8, cy-8), short_text, fill="yellow", font=font)

    # 添加图例
    draw.text((20, 20), f"RED=OCR原始  GREEN=缩放后 {label}", fill="white", font=font)

    fname = f"temp/ocr_scale_{label}.png"
    test_img.save(fname)
    print(f"已保存: {fname}")

print("\n完成！请查看4张图片，选择最准确的缩放倍率。")
print("红色框=OCR原始坐标，绿色框=缩放后坐标")
