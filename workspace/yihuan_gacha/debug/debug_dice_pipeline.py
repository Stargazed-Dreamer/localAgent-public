"""dice 识别中间过程可视化调试工具

对 test_samples/ 下的每张样本图：
1. 调后端 OCR 获取所有文字框
2. 复用采集脚本的 parse_page_records 拿到记录列表
3. 对每条记录，重写 detect_dice_points 输出所有中间步骤：
   - 骰子区域裁切
   - 骰子底色掩码 (is_dice_base)
   - 赠礼文字掩码 (is_gift_text)
   - 最大连通域包围盒
   - 内部亮色点掩码 (is_dot)
   - 连通域大小列表 (dot_sizes)
   - 最终识别结果
4. 汇总成一张大图，保存到 test_samples/debug/

用法：
    .venv\\Scripts\\python.exe tools\\yihuan_gacha\\debug_dice_pipeline.py [sample_xxx.png ...]

不指定文件则处理 test_samples/ 下所有 sample_*.png。
"""
import sys
import os
import io
import json
import base64
import re
import requests
from pathlib import Path
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 复用采集脚本里的解析逻辑
sys.path.insert(0, str(Path("workspace/yihuan_gacha").resolve()))
import yihuan_gacha as yg

API = "http://127.0.0.1:8766"
SAMPLES_DIR = Path("workspace/yihuan_gacha/test_samples")
DEBUG_DIR = SAMPLES_DIR / "debug"


def ocr_image(img_path: Path) -> dict:
    """调后端 OCR，返回 details 列表"""
    with open(img_path, "rb") as f:
        img_bytes = f.read()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    resp = requests.post(
        f"{API}/ocr/path/json",
        json={"path": str(img_path)},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def visualize_record_debug(img_array: np.ndarray, name_box, name_cy: float, name_h: float, ocr_details, label: str):
    """对单条记录输出所有中间步骤的可视化

    返回 dict:
      panels: [(title, pil_image), ...]  各步骤的小图
      info: dict  统计信息
      result: int  最终识别结果
    """
    panels = []
    info = {}

    # 原图行裁切（带 name_box 标记）
    x_min = int(min(p[0] for p in name_box))
    x_max = int(max(p[0] for p in name_box))
    y_min = int(min(p[1] for p in name_box))
    y_max = int(max(p[1] for p in name_box))
    row_top = max(0, y_min - 20)
    row_bot = min(img_array.shape[0], y_max + 20)
    row_left = max(0, x_min - 80)
    row_right = min(img_array.shape[1], x_max + 200)
    row_crop = img_array[row_top:row_bot, row_left:row_right].copy()
    row_pil = Image.fromarray(row_crop)
    draw = ImageDraw.Draw(row_pil)
    # 标记 name_box（相对 row_crop 的坐标）
    nx1 = x_min - row_left
    ny1 = y_min - row_top
    nx2 = x_max - row_left
    ny2 = y_max - row_top
    draw.rectangle([nx1, ny1, nx2, ny2], outline=(255, 0, 0), width=2)
    # 标记 DICE_CX 列（相对位置）
    dx = yg.DICE_CX - row_left
    if 0 <= dx < row_pil.width:
        draw.line([(dx, 0), (dx, row_pil.height)], fill=(0, 255, 0), width=1)
    panels.append(("row+name_box+DICE_CX", row_pil))

    # === 复刻 detect_dice_points ===
    half_w = name_h * 1.9
    half_h = name_h * 0.9
    dice_left = max(0, int(yg.DICE_CX - half_w))
    dice_right = int(yg.DICE_CX + half_w)
    dice_top = max(0, int(name_cy - half_h))
    dice_bot = int(name_cy + half_h)
    info["dice_bbox"] = (dice_left, dice_top, dice_right, dice_bot)
    info["name_cy"] = name_cy
    info["name_h"] = name_h
    info["DICE_CX"] = yg.DICE_CX

    if dice_right <= dice_left or dice_bot <= dice_top:
        info["error"] = "dice_bbox 空区域"
        return {"panels": panels, "info": info, "result": -1}

    crop = img_array[dice_top:dice_bot, dice_left:dice_right]
    h, w = crop.shape[:2]
    total = h * w
    panels.append(("dice_crop", Image.fromarray(crop)))

    r, g, b = crop[:, :, 0].astype(int), crop[:, :, 1].astype(int), crop[:, :, 2].astype(int)

    # OCR 检查"集点赠礼"/"沉眠地"
    ocr_hit = None
    if ocr_details:
        for item in ocr_details:
            text = item.get("text", "")
            box = item.get("box", [])
            if "集点赠礼" in text and box:
                item_cy = sum(p[1] for p in box) / 4
                if abs(item_cy - name_cy) < name_h:
                    ocr_hit = "集点赠礼"
                    break
            if "沉眠地" in text and box:
                item_cy = sum(p[1] for p in box) / 4
                if abs(item_cy - name_cy) < name_h:
                    ocr_hit = "沉眠地"
                    break
    info["ocr_hit"] = ocr_hit
    if ocr_hit:
        info["result_by_ocr"] = 0 if ocr_hit == "集点赠礼" else -2

    # is_dice_base / is_gift_text
    is_dice_base = (np.abs(r - 82) < 15) & (np.abs(g - 83) < 15) & (np.abs(b - 82) < 15) & ((g - r) >= 0)
    is_gift_text = (np.abs(r - 74) < 15) & (np.abs(g - 74) < 15) & (np.abs(b - 74) < 15) & \
                   (np.abs(r - g) < 5) & (np.abs(g - b) < 5)

    dice_base_ratio = is_dice_base.sum() / total
    gift_text_ratio = is_gift_text.sum() / total
    info["dice_base_ratio"] = round(dice_base_ratio, 4)
    info["gift_text_ratio"] = round(gift_text_ratio, 4)

    # 可视化掩码：黑底，命中像素用原色
    mask_dice = np.zeros_like(crop)
    mask_dice[is_dice_base] = [0, 255, 0]
    panels.append(("is_dice_base (绿)", Image.fromarray(mask_dice)))

    mask_gift = np.zeros_like(crop)
    mask_gift[is_gift_text] = [255, 0, 255]
    panels.append(("is_gift_text (紫)", Image.fromarray(mask_gift)))

    # 早退判定
    if gift_text_ratio > 0.005 and dice_base_ratio < 0.10:
        info["result_by_ratio"] = 0
        return {"panels": panels, "info": info, "result": 0}
    if dice_base_ratio < 0.05:
        info["result_by_ratio"] = -1
        return {"panels": panels, "info": info, "result": -1}

    # 最大连通域
    visited = np.zeros((h, w), dtype=bool)
    best_count = 0
    best_bounds = None
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
                if count > best_count:
                    best_count = count
                    best_bounds = (min_y2, max_y2, min_x2, max_x2)

    if best_bounds is None:
        info["result_no_component"] = -1
        return {"panels": panels, "info": info, "result": -1}

    min_y2, max_y2, min_x2, max_x2 = best_bounds
    bbox_h = max_y2 - min_y2 + 1
    bbox_w = max_x2 - min_x2 + 1
    fill_ratio = best_count / (bbox_h * bbox_w)
    info["component"] = {
        "count": best_count, "bbox": best_bounds,
        "bbox_h": bbox_h, "bbox_w": bbox_w, "fill_ratio": round(fill_ratio, 3),
    }

    # 可视化最大连通域（在 dice_crop 上画红框）
    comp_vis = crop.copy()
    comp_pil = Image.fromarray(comp_vis)
    draw = ImageDraw.Draw(comp_pil)
    draw.rectangle([min_x2, min_y2, max_x2, max_y2], outline=(255, 0, 0), width=2)
    panels.append(("max_component (红框)", comp_pil))

    if fill_ratio < 0.5 and gift_text_ratio > 0.003:
        info["result_by_fill"] = 0
        return {"panels": panels, "info": info, "result": 0}

    # 内部亮色点
    inner_y1 = min_y2 + 2
    inner_y2 = max_y2 - 1
    inner_x1 = min_x2 + 2
    inner_x2 = max_x2 - 1
    if inner_y2 <= inner_y1 or inner_x2 <= inner_x1:
        info["result_no_inner"] = -1
        return {"panels": panels, "info": info, "result": -1}

    inner_crop = crop[inner_y1:inner_y2 + 1, inner_x1:inner_x2 + 1]
    panels.append(("inner_crop", Image.fromarray(inner_crop)))

    ir, ig, ib = inner_crop[:, :, 0].astype(int), inner_crop[:, :, 1].astype(int), inner_crop[:, :, 2].astype(int)
    is_dot = (np.abs(ir - 224) < 20) & (np.abs(ig - 224) < 20) & (np.abs(ib - 226) < 20)
    dot_px = is_dot.sum()
    info["dot_pixel_count"] = int(dot_px)

    dot_vis = np.zeros_like(inner_crop)
    dot_vis[is_dot] = [255, 255, 0]
    panels.append(("is_dot (黄)", Image.fromarray(dot_vis)))

    if dot_px < 2:
        info["result_no_dot"] = -1
        return {"panels": panels, "info": info, "result": -1}

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
    MIN_DOT_SIZE = 30
    real_dots = [s for s in dot_sizes if s >= MIN_DOT_SIZE]
    info["real_dot_count"] = len(real_dots)
    info["filtered_dots"] = [s for s in dot_sizes if s < MIN_DOT_SIZE]

    if 1 <= len(real_dots) <= 6:
        result = len(real_dots)
    else:
        result = -1
    info["final_result"] = result
    return {"panels": panels, "info": info, "result": result}


def compose_overview(img_path: Path, img_array: np.ndarray, ocr_json: dict, records: list):
    """把一张图的所有记录的中间步骤拼成大图"""
    details = ocr_json.get("details", [])
    rows = []

    for i, rec in enumerate(records):
        name = rec.get("name", "")
        rarity = rec.get("rarity", "?")
        dice = rec.get("dice", -1)
        name_box = rec.get("_name_box")
        name_cy = rec.get("_name_cy", 0)
        name_h = rec.get("_name_h", 0)
        if not name_box or not name_cy or not name_h:
            continue

        debug = visualize_record_debug(img_array, name_box, name_cy, name_h, details, f"#{i}")
        rows.append((i, name, rarity, dice, debug))

    if not rows:
        return None

    # 每条记录占一行，列数 = panels 数 + 1（最左边是记录信息）
    n_cols = max(len(r[4]["panels"]) for r in rows) + 1
    cell_w = 220
    cell_h = 140
    pad = 8
    header_h = 24

    total_w = n_cols * (cell_w + pad) + pad
    total_h = header_h + len(rows) * (cell_h + pad) + pad

    canvas = Image.new("RGB", (total_w, total_h), (245, 245, 245))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype("arial.ttf", 12)
        font_b = ImageFont.truetype("arialbd.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        font_b = font

    # 表头
    draw.text((pad, 4), f"样本: {img_path.name}", fill=(0, 0, 0), font=font_b)
    headers = ["记录"] + [p[0] for p in rows[0][4]["panels"]]
    for c, h in enumerate(headers):
        x = pad + c * (cell_w + pad)
        draw.text((x, 8), h, fill=(50, 50, 50), font=font)

    for r_idx, (i, name, rarity, dice, debug) in enumerate(rows):
        y = header_h + r_idx * (cell_h + pad)
        # 第一列：记录信息
        x = pad
        info_text = f"#{i}  {rarity}\n{name[:18]}\n采集器dice={dice}\n调试dice={debug['result']}"
        # 多行
        for li, line in enumerate(info_text.split("\n")):
            draw.text((x, y + li * 16), line, fill=(0, 0, 0), font=font)
        # 关键统计
        info = debug["info"]
        stats = []
        if "dice_base_ratio" in info:
            stats.append(f"base={info['dice_base_ratio']}")
        if "gift_text_ratio" in info:
            stats.append(f"gift={info['gift_text_ratio']}")
        if "component" in info:
            c = info["component"]
            stats.append(f"comp={c['count']}px fill={c['fill_ratio']}")
        if "dot_pixel_count" in info:
            stats.append(f"dotpx={info['dot_pixel_count']}")
        if "dot_sizes" in info:
            stats.append(f"sizes={info['dot_sizes']}")
        if "real_dot_count" in info:
            stats.append(f"real={info['real_dot_count']}")
        for si, s in enumerate(stats):
            draw.text((x, y + 80 + si * 14), s, fill=(0, 100, 0), font=font)

        # 其他列：panel 图
        for c, (title, pil) in enumerate(debug["panels"]):
            x = pad + (c + 1) * (cell_w + pad)
            # 缩放
            scale = min((cell_w - 4) / pil.width, (cell_h - 4) / pil.height)
            new_w = max(1, int(pil.width * scale))
            new_h = max(1, int(pil.height * scale))
            pil_s = pil.resize((new_w, new_h), Image.LANCZOS)
            canvas.paste(pil_s, (x + (cell_w - new_w) // 2, y + (cell_h - new_h) // 2))

    return canvas


def main():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        files = [Path(a) for a in sys.argv[1:]]
    else:
        files = sorted(SAMPLES_DIR.glob("sample_*.png"))

    if not files:
        print(f"未找到样本图。请先用 sample_capture_gui.py 采集。")
        print(f"期望路径: {SAMPLES_DIR}/sample_*.png")
        return

    print(f"将处理 {len(files)} 张样本图")

    for img_path in files:
        print(f"\n=== {img_path.name} ===")
        img = Image.open(img_path).convert("RGB")
        img_array = np.array(img)
        print(f"  尺寸: {img_array.shape[1]}x{img_array.shape[0]}")

        # 调 OCR
        try:
            ocr_json = ocr_image(img_path)
        except Exception as e:
            print(f"  OCR 失败: {e}")
            continue
        details = ocr_json.get("details", [])
        print(f"  OCR 文字框: {len(details)}")

        # 准备 image_b64 给 parse_page_records 用
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")

        # 调采集脚本的 parse_page_records
        records = yg.parse_page_records(details, img_b64)
        print(f"  识别记录: {len(records)}")
        for i, r in enumerate(records):
            print(f"    [{i}] {r.get('rarity')} {r.get('name')} x{r.get('quantity')} dice={r.get('dice')} t={r.get('time')}")

        # parse_page_records 不返回 name_box/cy/h，我们需要重新跑一遍拿到这些
        # 这里直接重新解析一遍，把 name_box/cy/h 塞进 records
        # 复刻 parse_page_records 的分组逻辑（简化版）
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
        time_pattern = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
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

            # 找 name_box
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

        # 生成 overview
        overview = compose_overview(img_path, img_array, ocr_json, records)
        if overview is None:
            print(f"  无可视化记录，跳过")
            continue

        out_path = DEBUG_DIR / f"{img_path.stem}_debug.png"
        overview.save(out_path)
        print(f"  → {out_path}")

        # 同时保存每条记录的 info.json
        info_dir = DEBUG_DIR / img_path.stem
        info_dir.mkdir(exist_ok=True)
        info_data = []
        for i, rec in enumerate(records):
            if "_name_box" not in rec:
                continue
            debug = visualize_record_debug(img_array, rec["_name_box"], rec["_name_cy"], rec["_name_h"], details, f"#{i}")
            info_data.append({
                "idx": i,
                "name": rec.get("name"),
                "rarity": rec.get("rarity"),
                "quantity": rec.get("quantity"),
                "time": rec.get("time"),
                "collector_dice": rec.get("dice"),
                "debug_result": debug["result"],
                "info": debug["info"],
            })
        with open(info_dir / "info.json", "w", encoding="utf-8") as f:
            json.dump(info_data, f, ensure_ascii=False, indent=2)
        print(f"  → {info_dir}/info.json")

    print(f"\n完成。可视化结果在: {DEBUG_DIR}")


if __name__ == "__main__":
    main()
