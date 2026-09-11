"""可视化 v2 找到的骰子，对比 v1 的裁切区域。
目的：直观确认 v2 找到的是完整骰子，v1 截断了骰子下半部分。
"""
import sys
import io
import base64
import requests
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path("workspace/yihuan_gacha").resolve()))
import yihuan_gacha as yg
from detect_dice_v2 import detect_dice_points_v2, DICE_CX_V2, parse_records_with_boxes

API = "http://127.0.0.1:8766"
SAMPLES_DIR = Path("workspace/yihuan_gacha/test_samples")

# 选 4 个典型的不一致 case
TARGET_CASES = [
    ("sample_20260709_032731_001.png", 0),  # v1=3 → v2=5
    ("sample_20260709_032803_003.png", 0),  # v1=4 → v2=6
    ("sample_20260709_032818_004.png", 2),  # v1=4 → v2=6
    ("sample_20260709_032832_005.png", 0),  # v1=2 → v2=4
]

def ocr_image(img_path):
    resp = requests.post(f"{API}/ocr/path/json", json={"path": str(img_path)}, timeout=30)
    return resp.json()

for img_name, target_idx in TARGET_CASES:
    img_path = SAMPLES_DIR / img_name
    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("ascii")
    img = Image.open(img_path).convert("RGB")

    details = ocr_image(img_path).get("details", [])
    records = parse_records_with_boxes(details, img_b64)

    if target_idx >= len(records):
        continue
    rec = records[target_idx]
    if "_name_cy" not in rec:
        continue

    name_cy = rec["_name_cy"]
    name_h = rec["_name_h"]
    name_box = rec["_name_box"]

    v2_dice, v2_info = detect_dice_points_v2(img_b64, name_cy, name_h, details)
    v1_dice = rec.get("dice", "?")

    # 在原图上标注
    draw_img = img.copy()
    draw = ImageDraw.Draw(draw_img)

    # 1. 标注 name 框（绿色）
    name_xs = [p[0] for p in name_box]
    name_ys = [p[1] for p in name_box]
    draw.rectangle([min(name_xs), min(name_ys), max(name_xs), max(name_ys)],
                   outline=(0, 255, 0), width=3)
    draw.text((min(name_xs), min(name_ys) - 25),
              f"name cy={name_cy:.0f} h={name_h:.0f}", fill=(0, 255, 0))

    # 2. 标注 v1 裁切区域（红色虚线框）
    v1_half_w = name_h * 1.9
    v1_half_h = name_h * 0.9
    v1_left = max(0, int(yg.DICE_CX - v1_half_w))
    v1_right = int(yg.DICE_CX + v1_half_w)
    v1_top = max(0, int(name_cy - v1_half_h))
    v1_bot = int(name_cy + v1_half_h)
    draw.rectangle([v1_left, v1_top, v1_right, v1_bot],
                   outline=(255, 0, 0), width=2)
    draw.text((v1_left, v1_top - 25),
              f"v1 crop (dice={v1_dice})", fill=(255, 0, 0))

    # 3. 标注 v2 裁切区域（蓝色框）
    v2_crop = v2_info.get("crop_bbox", (0, 0, 0, 0))
    draw.rectangle([v2_crop[0], v2_crop[1], v2_crop[2], v2_crop[3]],
                   outline=(0, 0, 255), width=2)
    draw.text((v2_crop[0], v2_crop[3] + 5),
              f"v2 crop (dice={v2_dice})", fill=(0, 0, 255))

    # 4. 标注 v2 找到的骰子（黄色粗框）
    best = v2_info.get("best_candidate", {})
    if best:
        bbox = best["bbox"]  # (min_y, max_y, min_x, max_x) 相对 crop
        crop_top = v2_crop[1]
        crop_left = v2_crop[0]
        dice_y1 = crop_top + bbox[0]
        dice_y2 = crop_top + bbox[1]
        dice_x1 = crop_left + bbox[2]
        dice_x2 = crop_left + bbox[3]
        draw.rectangle([dice_x1, dice_y1, dice_x2, dice_y2],
                       outline=(255, 255, 0), width=4)
        draw.text((dice_x1, dice_y2 + 5),
                  f"v2 dice bbox {bbox[2]}-{bbox[3]}x{bbox[0]}-{bbox[1]}",
                  fill=(255, 255, 0))

    # 5. 标注 DICE_CX 竖线
    draw.line([(yg.DICE_CX, 0), (yg.DICE_CX, img.size[1])], fill=(128, 128, 128), width=1)
    draw.text((yg.DICE_CX + 2, 10), f"DICE_CX v1={yg.DICE_CX}", fill=(128, 128, 128))
    draw.line([(DICE_CX_V2, 0), (DICE_CX_V2, img.size[1])], fill=(0, 128, 128), width=1)
    draw.text((DICE_CX_V2 + 2, 30), f"DICE_CX v2={DICE_CX_V2}", fill=(0, 128, 128))

    # 裁切局部区域放大保存
    zoom_left = max(0, int(DICE_CX_V2 - 100))
    zoom_right = min(img.size[0], int(DICE_CX_V2 + 100))
    zoom_top = max(0, int(name_cy - 80))
    zoom_bot = min(img.size[1], int(name_cy + 80))
    zoom_img = draw_img.crop((zoom_left, zoom_top, zoom_right, zoom_bot))
    # 放大 2 倍
    zoom_img = zoom_img.resize((zoom_img.size[0] * 2, zoom_img.size[1] * 2), Image.Resampling.NEAREST)

    out_path = SAMPLES_DIR / "debug" / "v2" / f"compare_{img_name.replace('.png','')}_idx{target_idx}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    zoom_img.save(out_path)
    print(f"{img_name}#{target_idx} {rec.get('name')[:20]}: v1={v1_dice} → v2={v2_dice}, y_off={best.get('y_offset','-')}, 保存到 {out_path.name}")

print("\n可视化完成，请查看 debug/v2/ 目录下的 compare_*.png")
