"""detect_dice_points v2: 定向裁切 + 封闭圆角方形强约束检测

基于样本图分析得到的关键事实：
1. 骰子中心 x = 610（固定，5 张样本图 0 偏差）
2. 骰子尺寸 = 53x53（固定，min=max=53）
3. 骰子在 name 下方约 +5~+26 像素（正常偏移）
4. 骰子间距约 80 像素（不会覆盖到相邻骰子）

算法改进：
1. 修正 DICE_CX = 610（原 629 不准）
2. 定向裁切：x 围绕 610（±40），y 围绕 name_cy（-0.5*name_h ~ +1.5*name_h）
   - y 搜索范围 2*name_h ≈ 70，远小于骰子间距 80，不会匹配到相邻骰子
3. 强约束筛选：尺寸 45-60、宽高比 0.8-1.2、填充率>0.5、cx 在 [595, 625]
4. 在骰子内部数亮色点

测试：对 test_samples/ 下所有样本图跑 v2，对比 v1 结果
"""
import sys
import io
import json
import base64
import re
import time
import requests
from pathlib import Path
from collections import deque

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path("workspace/yihuan_gacha").resolve()))
import yihuan_gacha as yg

API = "http://127.0.0.1:8766"
SAMPLES_DIR = Path("workspace/yihuan_gacha/test_samples")
DEBUG_DIR = SAMPLES_DIR / "debug"
V2_OUTPUT_DIR = DEBUG_DIR / "v2"
V2_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 修正后的骰子中心 x（原 DICE_CX=629 不准，实际骰子 bbox [584, 636] 中心 610）
DICE_CX_V2 = 610
DICE_SIZE = 53  # 骰子图标尺寸（固定）


def detect_dice_points_v2(image_b64, name_cy, name_h, ocr_details=None):
    """改进版骰子检测：定向裁切 + 封闭圆角方形强约束

    返回: (result, info)
      result: 1-6=点数, 0=集点赠礼, -1=无法识别, -2=沉眠地
      info: dict with debug info
    """
    info = {}
    try:
        img_data = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_data))
        img_array = np.array(img)
    except Exception as e:
        return -1, {"error": f"图片解码失败: {e}"}

    # Step 0: OCR 检查"集点赠礼"/"沉眠地"（保持原逻辑）
    if ocr_details:
        for item in ocr_details:
            text = item.get("text", "")
            box = item.get("box", [])
            if "集点赠礼" in text and box:
                item_cy = sum(p[1] for p in box) / 4
                if abs(item_cy - name_cy) < name_h:
                    return 0, {"ocr_hit": "集点赠礼"}
            if "沉眠地" in text and box:
                item_cy = sum(p[1] for p in box) / 4
                if abs(item_cy - name_cy) < name_h:
                    return -2, {"ocr_hit": "沉眠地"}

    # Step 1: 定向裁切区域
    # x: DICE_CX_V2 ± 40 = [570, 650]（覆盖骰子宽度 53 + 余量）
    # y: name_cy - 0.5*name_h 到 name_cy + 1.5*name_h（骰子在 name 下方，总高 2*name_h ≈ 70）
    # 骰子间距约 80，2*name_h ≈ 70 < 80，不会覆盖到相邻骰子
    crop_left = max(0, int(DICE_CX_V2 - 40))
    crop_right = min(img_array.shape[1], int(DICE_CX_V2 + 40))
    crop_top = max(0, int(name_cy - name_h * 0.5))
    crop_bot = min(img_array.shape[0], int(name_cy + name_h * 1.5))

    if crop_right <= crop_left or crop_bot <= crop_top:
        return -1, {"error": "裁切区域空"}

    crop = img_array[crop_top:crop_bot, crop_left:crop_right]
    h, w = crop.shape[:2]
    info["crop_bbox"] = (crop_left, crop_top, crop_right, crop_bot)
    info["crop_size"] = (w, h)

    r, g, b = crop[:, :, 0].astype(int), crop[:, :, 1].astype(int), crop[:, :, 2].astype(int)

    # Step 2: 骰子底色掩码（同 v1）
    is_dice_base = (np.abs(r - 82) < 15) & (np.abs(g - 83) < 15) & (np.abs(b - 82) < 15) & ((g - r) >= 0)
    is_gift_text = (np.abs(r - 74) < 15) & (np.abs(g - 74) < 15) & (np.abs(b - 74) < 15) & \
                   (np.abs(r - g) < 5) & (np.abs(g - b) < 5)

    dice_base_ratio = is_dice_base.sum() / (h * w)
    gift_text_ratio = is_gift_text.sum() / (h * w)
    info["dice_base_ratio"] = round(float(dice_base_ratio), 4)
    info["gift_text_ratio"] = round(float(gift_text_ratio), 4)

    # 赠礼文字显著且骰子底色少 → 集点赠礼
    if gift_text_ratio > 0.005 and dice_base_ratio < 0.05:
        return 0, info

    if dice_base_ratio < 0.02:
        return -1, info

    # Step 3: 找所有骰子底色连通域
    visited = np.zeros((h, w), dtype=bool)
    components = []

    for y in range(h):
        for x in range(w):
            if is_dice_base[y, x] and not visited[y, x]:
                queue = deque([(y, x)])
                visited[y, x] = True
                count = 0
                min_y2, max_y2, min_x2, max_x2 = y, y, x, x
                while queue:
                    cy2, cx2 = queue.popleft()
                    count += 1
                    min_y2 = min(min_y2, cy2)
                    max_y2 = max(max_y2, cy2)
                    min_x2 = min(min_x2, cx2)
                    max_x2 = max(max_x2, cx2)
                    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        ny, nx = cy2 + dy, cx2 + dx
                        if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and is_dice_base[ny, nx]:
                            visited[ny, nx] = True
                            queue.append((ny, nx))
                components.append((count, min_y2, max_y2, min_x2, max_x2))

    info["total_components"] = len(components)

    if not components:
        return -1, info

    # Step 4: 筛选"封闭圆角方形"——用强约束
    # 骰子尺寸固定 53x53，允许 ±5 容差
    # 骰子 cx 固定 610，相对 crop 的 cx 应在 [595-crop_left, 625-crop_left]
    expected_cx_rel = DICE_CX_V2 - crop_left  # 骰子中心 x 相对 crop
    candidates = []
    for comp in components:
        count, min_y2, max_y2, min_x2, max_x2 = comp
        bbox_h = max_y2 - min_y2 + 1
        bbox_w = max_x2 - min_x2 + 1
        fill_ratio = count / (bbox_h * bbox_w)
        aspect_ratio = bbox_w / bbox_h if bbox_h > 0 else 0

        # 尺寸约束：45-60（骰子固定 53x53）
        if bbox_h < 45 or bbox_h > 60:
            continue
        if bbox_w < 45 or bbox_w > 60:
            continue
        # 宽高比约束：0.8-1.2（正方形）
        if aspect_ratio < 0.8 or aspect_ratio > 1.2:
            continue
        # 填充率约束：封闭圆角方形内部基本填满
        if fill_ratio < 0.5:
            continue
        # x 中心约束：cx 应在 [expected_cx_rel - 15, expected_cx_rel + 15]
        center_x = (min_x2 + max_x2) / 2
        if abs(center_x - expected_cx_rel) > 15:
            continue

        # y 中心（相对 crop）
        center_y = (min_y2 + max_y2) / 2
        # 距 name_cy 的 y 偏移（相对 crop）
        name_cy_rel = name_cy - crop_top
        y_offset = center_y - name_cy_rel

        candidates.append({
            "count": count, "bbox": (min_y2, max_y2, min_x2, max_x2),
            "bbox_h": bbox_h, "bbox_w": bbox_w,
            "fill_ratio": round(fill_ratio, 3),
            "aspect_ratio": round(aspect_ratio, 3),
            "center_y": round(center_y, 1),
            "center_x": round(center_x, 1),
            "y_offset": round(y_offset, 1),
        })

    info["candidates"] = candidates
    info["candidates_count"] = len(candidates)

    if not candidates:
        # 没找到符合条件的骰子方形
        # 如果有赠礼文字，可能是集点赠礼
        if gift_text_ratio > 0.003:
            return 0, info
        # 否则可能是沉眠地（无骰子图标）
        return -1, info

    # Step 5: 选最佳候选——y 偏移最接近正常范围（+5 ~ +26）的
    # 正常情况下骰子在 name 下方，y_offset 应为正值
    # 选 y_offset 最接近 name_h * 0.5（约 +16）的候选
    expected_y_offset = name_h * 0.5
    candidates.sort(key=lambda c: abs(c["y_offset"] - expected_y_offset))
    best = candidates[0]
    info["best_candidate"] = best

    min_y2, max_y2, min_x2, max_x2 = best["bbox"]

    # Step 6: 在骰子内部找亮色点（同 v1）
    inner_y1 = min_y2 + 2
    inner_y2 = max_y2 - 1
    inner_x1 = min_x2 + 2
    inner_x2 = max_x2 - 1

    if inner_y2 <= inner_y1 or inner_x2 <= inner_x1:
        return -1, info

    inner_crop = crop[inner_y1:inner_y2 + 1, inner_x1:inner_x2 + 1]
    ir, ig, ib = inner_crop[:, :, 0].astype(int), inner_crop[:, :, 1].astype(int), inner_crop[:, :, 2].astype(int)
    is_dot = (np.abs(ir - 224) < 20) & (np.abs(ig - 224) < 20) & (np.abs(ib - 226) < 20)
    dot_px = is_dot.sum()
    info["dot_pixel_count"] = int(dot_px)

    if dot_px < 2:
        info["note"] = "找到骰子但无亮色点"
        return -1, info

    # 连通域计数
    ih, iw = inner_crop.shape[:2]
    visited2 = np.zeros((ih, iw), dtype=bool)
    dot_sizes = []
    for y in range(ih):
        for x in range(iw):
            if is_dot[y, x] and not visited2[y, x]:
                queue = deque([(y, x)])
                visited2[y, x] = True
                cnt = 0
                while queue:
                    cy2, cx2 = queue.popleft()
                    cnt += 1
                    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        ny, nx = cy2 + dy, cx2 + dx
                        if 0 <= ny < ih and 0 <= nx < iw and not visited2[ny, nx] and is_dot[ny, nx]:
                            visited2[ny, nx] = True
                            queue.append((ny, nx))
                dot_sizes.append(cnt)

    info["dot_sizes"] = dot_sizes

    # 过滤噪点
    MIN_DOT_SIZE = 30
    real_dots = [s for s in dot_sizes if s >= MIN_DOT_SIZE]
    info["real_dot_count"] = len(real_dots)

    if 1 <= len(real_dots) <= 6:
        return len(real_dots), info
    else:
        return -1, info


def ocr_image(img_path: Path) -> dict:
    resp = requests.post(f"{API}/ocr/path/json", json={"path": str(img_path)}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_records_with_boxes(details: list, img_b64: str):
    """复刻 parse_page_records + 解析 name_box/cy/h"""
    records = yg.parse_page_records(details, img_b64)

    items = []
    for item in details:
        text = item.get("text", "").strip()
        box = item.get("box", [])
        if not text or not box:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = sum(xs) / 4
        cy = sum(ys) / 4
        items.append({"text": text, "cx": cx, "cy": cy, "box": box})

    items.sort(key=lambda x: x["cy"])
    rows = []
    current_row = []
    current_y = -999
    for item in items:
        if abs(item["cy"] - current_y) > 25:
            if current_row:
                rows.append(current_row)
            current_row = [item]
            current_y = item["cy"]
        else:
            current_row.append(item)
    if current_row:
        rows.append(current_row)
    for row in rows:
        row.sort(key=lambda x: x["cx"])

    name_prefixes = ["弧盘", "角色", "道具", "滑翔翼"]
    header_found = False
    rec_idx = 0
    for row in rows:
        row_text = " ".join(item["text"] for item in row)
        if "道具名称" in row_text or "获得时间" in row_text:
            header_found = True
            continue
        if not header_found:
            continue
        if any(t in row_text for t in ["上一页", "下一页"]):
            continue
        if re.match(r'^\d+/\d+$', row_text.strip()):
            continue
        has_name = any(prefix in item["text"] for item in row for prefix in name_prefixes)
        if not has_name:
            continue
        name_box = None
        for item in row:
            for prefix in name_prefixes:
                if prefix in item["text"]:
                    name_box = item["box"]
                    break
            if name_box:
                break
        if name_box and rec_idx < len(records):
            name_ys = [p[1] for p in name_box]
            records[rec_idx]["_name_box"] = name_box
            records[rec_idx]["_name_cy"] = sum(name_ys) / 4
            records[rec_idx]["_name_h"] = max(name_ys) - min(name_ys)
            rec_idx += 1
    return records


def main():
    files = sorted(SAMPLES_DIR.glob("sample_*.png"))
    if not files:
        print("未找到样本图")
        return

    print(f"处理 {len(files)} 张样本图\n")
    print(f"{'sample':<12} {'idx':>3} {'rarity':>4} {'name':<22} {'qty':>3} {'v1':>4} {'v2':>4} {'base_r':>7} {'gift_r':>7} {'cand':>4} {'y_off':>6} {'dot_sizes':<30} {'flag'}")
    print("-" * 160)

    total = 0
    v1_v2_match = 0
    v1_v2_mismatch = 0
    mismatches = []

    for img_path in files:
        sample_name = img_path.name.replace("sample_20260709_", "s")
        img = Image.open(img_path).convert("RGB")
        img_array = np.array(img)

        ocr_json = ocr_image(img_path)
        details = ocr_json.get("details", [])
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
        records = parse_records_with_boxes(details, img_b64)

        for i, rec in enumerate(records):
            if "_name_cy" not in rec:
                continue
            name_cy = rec["_name_cy"]
            name_h = rec["_name_h"]
            v1_dice = rec.get("dice", "?")

            v2_dice, v2_info = detect_dice_points_v2(img_b64, name_cy, name_h, details)

            base_r = v2_info.get("dice_base_ratio", "-")
            gift_r = v2_info.get("gift_text_ratio", "-")
            cand = v2_info.get("candidates_count", "-")
            sizes = v2_info.get("dot_sizes", [])
            best = v2_info.get("best_candidate", {})

            flag = ""
            if v1_dice != v2_dice:
                flag = f"MISMATCH(v1={v1_dice}→v2={v2_dice})"
                v1_v2_mismatch += 1
                mismatches.append((sample_name, i, rec.get("name"), v1_dice, v2_dice, v2_info))
            else:
                v1_v2_match += 1
                flag = "OK"

            y_off = best.get("y_offset", "-")
            if isinstance(y_off, float):
                flag += f"  [y_off={y_off}]"

            total += 1
            name = (rec.get("name") or "")[:20]
            print(f"{sample_name:<12} {i:>3} {rec.get('rarity','?'):>4} {name:<22} {rec.get('quantity','?'):>3} {str(v1_dice):>4} {str(v2_dice):>4} {str(base_r):>7} {str(gift_r):>7} {str(cand):>4} {str(y_off):>6} {str(sizes):<30} {flag}")

    print(f"\n=== 汇总 ===")
    print(f"总记录: {total}, v1/v2 一致: {v1_v2_match}, 不一致: {v1_v2_mismatch}")

    if mismatches:
        print(f"\n--- 不一致详情 ---")
        for s, i, name, v1, v2, info in mismatches:
            print(f"  {s}#{i} {name}: v1={v1} -> v2={v2}")
            print(f"    base_r={info.get('dice_base_ratio')} gift_r={info.get('gift_text_ratio')}")
            print(f"    candidates={info.get('candidates_count')} best={info.get('best_candidate', {})}")
            print(f"    dot_sizes={info.get('dot_sizes')} real={info.get('real_dot_count')}")


if __name__ == "__main__":
    main()
