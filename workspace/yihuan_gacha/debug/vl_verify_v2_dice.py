"""用 VL 验证 v2 找到的骰子裁切图，确认点数正确。
选 4 个不一致 case，裁切 v2 找到的骰子区域给 VL 数点数。
"""
import sys
import io
import base64
import time
import requests
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path("workspace/yihuan_gacha").resolve()))
import yihuan_gacha as yg
from detect_dice_v2 import detect_dice_points_v2, DICE_CX_V2, parse_records_with_boxes

API = "http://127.0.0.1:8766"
SAMPLES_DIR = Path("workspace/yihuan_gacha/test_samples")

TARGET_CASES = [
    ("sample_20260709_032731_001.png", 0, 3, 5),  # v1=3 → v2=5
    ("sample_20260709_032803_003.png", 0, 4, 6),  # v1=4 → v2=6
    ("sample_20260709_032818_004.png", 2, 4, 6),  # v1=4 → v2=6
    ("sample_20260709_032832_005.png", 0, 2, 4),  # v1=2 → v2=4
]

QUESTION = (
    "这是游戏抽卡记录中的一个骰子图标裁切图。"
    "请数一数骰子上有几个圆点（点数）？只回答数字（1-6）。"
    "如果不是骰子（比如是文字），回答 0。"
)

def ocr_image(img_path):
    resp = requests.post(f"{API}/ocr/path/json", json={"path": str(img_path)}, timeout=30)
    return resp.json()

def vl_understand(img_b64, question):
    """调用远程 VL 理解图片"""
    resp = requests.post(
        f"{API}/vision/understand",
        json={"image": img_b64, "question": question},
        timeout=60,
    )
    return resp.json()

results = []
for img_name, target_idx, v1_dice, v2_dice in TARGET_CASES:
    img_path = SAMPLES_DIR / img_name
    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("ascii")
    img = Image.open(img_path).convert("RGB")
    img_array = np.array(img)

    details = ocr_image(img_path).get("details", [])
    records = parse_records_with_boxes(details, img_b64)

    if target_idx >= len(records):
        continue
    rec = records[target_idx]
    if "_name_cy" not in rec:
        continue

    name_cy = rec["_name_cy"]
    name_h = rec["_name_h"]

    v2_result, v2_info = detect_dice_points_v2(img_b64, name_cy, name_h, details)
    best = v2_info.get("best_candidate", {})

    if not best:
        print(f"{img_name}#{target_idx}: 无 v2 候选")
        continue

    # 裁切 v2 找到的骰子区域（加 5 像素边距）
    bbox = best["bbox"]  # (min_y, max_y, min_x, max_x) 相对 crop
    crop_bbox = v2_info["crop_bbox"]  # (crop_left, crop_top, crop_right, crop_bot)
    crop_left = crop_bbox[0]
    crop_top = crop_bbox[1]
    dice_y1 = crop_top + bbox[0] - 5
    dice_y2 = crop_top + bbox[1] + 5
    dice_x1 = crop_left + bbox[2] - 5
    dice_x2 = crop_left + bbox[3] + 5
    dice_crop = img.crop((dice_x1, dice_y1, dice_x2, dice_y2))
    # 放大 3 倍便于 VL 识别
    dice_crop = dice_crop.resize((dice_crop.size[0] * 3, dice_crop.size[1] * 3), Image.Resampling.LANCZOS)

    # 保存裁切图
    out_path = SAMPLES_DIR / "debug" / "v2" / f"vl_dice_{img_name.replace('.png','')}_idx{target_idx}.png"
    dice_crop.save(out_path)

    # 转 base64 给 VL
    buf = io.BytesIO()
    dice_crop.save(buf, format="PNG")
    dice_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    print(f"\n{img_name}#{target_idx} {rec.get('name')[:20]}:")
    print(f"  v1={v1_dice}, v2={v2_dice}, y_off={best.get('y_offset')}")
    print(f"  调用 VL ...")

    try:
        vl_resp = vl_understand(dice_b64, QUESTION)
        vl_text = vl_resp.get("text", "") or vl_resp.get("answer", "") or str(vl_resp)
        print(f"  VL 回答: {vl_text}")
        results.append((img_name, target_idx, v1_dice, v2_dice, vl_text))
    except Exception as e:
        print(f"  VL 失败: {e}")
        results.append((img_name, target_idx, v1_dice, v2_dice, f"ERROR: {e}"))

    # RPM 限流：25s 间隔
    print(f"  等待 25s (RPM 限流)...")
    time.sleep(25)

print("\n=== VL 验证汇总 ===")
print(f"{'sample':<35} {'v1':>4} {'v2':>4} {'VL':<30}")
for img_name, idx, v1, v2, vl in results:
    print(f"{img_name}#{idx:<26} {v1:>4} {v2:>4} {vl:<30}")
