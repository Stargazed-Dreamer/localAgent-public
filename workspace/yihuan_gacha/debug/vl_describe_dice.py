"""用远程 VL 模型描述骰子图标的完整性和截断情况

对 test_samples/ 下的样本图，选取典型记录（不同点数），裁切骰子区域附近
的宽区域图，调 /vision/understand 让 VL 描述：
1. 骰子图标是否完整
2. 下半部分是否被截断
3. 实际点数

只返回 VL 的文本答案，不返回 base64（避免污染上下文）。

注意：远程 VL (ModelScope Qwen3-VL-235B) RPM=3，每次调用间隔 25 秒。
"""
import sys
import io
import json
import time
import base64
import re
import requests
from pathlib import Path
from collections import deque

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path("workspace/yihuan_gacha").resolve()))
import yihuan_gacha as yg

API = "http://127.0.0.1:8766"
SAMPLES_DIR = Path("workspace/yihuan_gacha/test_samples")
DEBUG_DIR = SAMPLES_DIR / "debug"
VL_OUTPUT_DIR = DEBUG_DIR / "vl_describe"
VL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# RPM=3 → 每次调用至少间隔 25 秒（留余量）
VL_INTERVAL_SEC = 25
# 429 时等待 70 秒重试
VL_RETRY_WAIT_SEC = 70
VL_MAX_RETRIES = 2

# 只选 3 条最关键记录（6点、5点、集点赠礼），减少 VL 调用次数
TARGETS = [
    ("sample_20260709_032731_001.png", 3, "6点_笑口常开"),
    ("sample_20260709_032803_003.png", 4, "5点_真红S"),
    ("sample_20260709_032752_002.png", 1, "集点赠礼_埃德嘉"),
]


def ocr_image(img_path: Path) -> dict:
    with open(img_path, "rb") as f:
        img_bytes = f.read()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
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


def crop_for_vl(img_array: np.ndarray, name_cy: float, name_h: float) -> Image.Image:
    """裁切一个宽区域供 VL 观察

    宽度：DICE_CX 左右各 name_h*3.5（覆盖整个骰子图标 + 周围）
    高度：name_cy 上下各 name_h*2.5（确保看到完整骰子 + 上下行边界）
    """
    cx = yg.DICE_CX
    half_w = name_h * 3.5
    half_h = name_h * 2.5
    left = max(0, int(cx - half_w))
    right = min(img_array.shape[1], int(cx + half_w))
    top = max(0, int(name_cy - half_h))
    bot = min(img_array.shape[0], int(name_cy + half_h))
    crop = img_array[top:bot, left:right]
    return Image.fromarray(crop)


def crop_dice_only(img_array: np.ndarray, name_cy: float, name_h: float) -> Image.Image:
    """只裁切 detect_dice_points 实际用的窄区域（用于对比）"""
    half_w = name_h * 1.9
    half_h = name_h * 0.9
    left = max(0, int(yg.DICE_CX - half_w))
    right = min(img_array.shape[1], int(yg.DICE_CX + half_w))
    top = max(0, int(name_cy - half_h))
    bot = min(img_array.shape[0], int(name_cy + half_h))
    crop = img_array[top:bot, left:right]
    return Image.fromarray(crop)


def ask_vl(image: Image.Image, question: str) -> str:
    """调 VL，带 RPM 限流和 429 重试"""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    for attempt in range(VL_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{API}/vision/understand",
                json={"image": img_b64, "question": question},
                timeout=120,
            )
            if resp.status_code == 503:
                # VL 冷却中
                detail = ""
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    pass
                if attempt < VL_MAX_RETRIES:
                    print(f"    VL 503 冷却中 ({detail[:80]}), 等 {VL_RETRY_WAIT_SEC}s 重试...", flush=True)
                    time.sleep(VL_RETRY_WAIT_SEC)
                    continue
                return f"[VL冷却中] {detail}"
            resp.raise_for_status()
            return resp.json().get("answer", "(空)")
        except requests.exceptions.Timeout:
            if attempt < VL_MAX_RETRIES:
                print(f"    VL 超时, 等 {VL_RETRY_WAIT_SEC}s 重试...", flush=True)
                time.sleep(VL_RETRY_WAIT_SEC)
                continue
            return "[VL超时]"
        except Exception as e:
            if attempt < VL_MAX_RETRIES:
                print(f"    VL 异常 {type(e).__name__}: {e}, 等 {VL_RETRY_WAIT_SEC}s 重试...", flush=True)
                time.sleep(VL_RETRY_WAIT_SEC)
                continue
            return f"[VL异常] {type(e).__name__}: {e}"
    return "[VL全部重试失败]"


def main():
    QUESTION = (
        "这是游戏抽卡记录界面中某一行的裁切图。请仔细观察图中右侧的骰子图标（一个圆角方形图标，"
        "上面有圆点表示点数），回答以下问题：\n"
        "1. 骰子图标是否完整可见？如果不是，哪一部分被截断或遮挡了？\n"
        "2. 骰子图标的下半部分是否完整？下半部分的圆点是否清晰可见？\n"
        "3. 你能数出骰子上有几个圆点吗？点数是多少？\n"
        "4. 骰子图标的底色是什么颜色？圆点是什么颜色？\n"
        "5. 如果骰子图标不存在（比如显示的是\"集点赠礼\"或\"沉眠地\"文字），请说明。\n"
        "请逐条回答，简明扼要。"
    )

    # 先检查 VL 是否可用
    print("检查 VL 状态...", flush=True)
    try:
        health = requests.get(f"{API}/health", timeout=5).json()
        vl_ok = health.get("ocr", {}).get("vl_available", False)
        if not vl_ok:
            print(f"VL 不可用 (vl_available=False)，等待 70s 让冷却结束...", flush=True)
            time.sleep(70)
    except Exception as e:
        print(f"健康检查失败: {e}", flush=True)

    results = []
    last_call_time = 0

    for i, (sample_file, rec_idx, label) in enumerate(TARGETS):
        img_path = SAMPLES_DIR / sample_file
        if not img_path.exists():
            print(f"  跳过(文件不存在): {sample_file}", flush=True)
            continue

        print(f"\n=== [{i+1}/{len(TARGETS)}] {label}  ({sample_file} #{rec_idx}) ===", flush=True)

        img = Image.open(img_path).convert("RGB")
        img_array = np.array(img)

        ocr_json = ocr_image(img_path)
        details = ocr_json.get("details", [])
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
        records = parse_records_with_boxes(details, img_b64)

        if rec_idx >= len(records) or "_name_cy" not in records[rec_idx]:
            print(f"  记录 #{rec_idx} 不存在或无 name_box", flush=True)
            continue

        rec = records[rec_idx]
        name_cy = rec["_name_cy"]
        name_h = rec["_name_h"]
        collector_dice = rec.get("dice", "?")

        print(f"  记录: {rec.get('rarity')} {rec.get('name')} x{rec.get('quantity')} 采集器dice={collector_dice}", flush=True)
        print(f"  name_cy={name_cy:.1f} name_h={name_h:.1f} DICE_CX={yg.DICE_CX}", flush=True)

        # 只裁切宽区域（减少 VL 调用次数）
        wide_crop = crop_for_vl(img_array, name_cy, name_h)
        wide_path = VL_OUTPUT_DIR / f"{label}_wide.png"
        wide_crop.save(wide_path)
        print(f"  宽区域已保存: {wide_path}  ({wide_crop.size[0]}x{wide_crop.size[1]})", flush=True)

        # RPM 限流：距上次调用不足 25s 则等待
        now = time.time()
        wait = VL_INTERVAL_SEC - (now - last_call_time)
        if wait > 0 and last_call_time > 0:
            print(f"  RPM 限流: 等待 {wait:.0f}s...", flush=True)
            time.sleep(wait)

        print(f"  正在调 VL...", flush=True)
        last_call_time = time.time()
        answer = ask_vl(wide_crop, QUESTION)
        print(f"  VL 回答: {answer}", flush=True)

        results.append({
            "label": label,
            "sample": sample_file,
            "rec_idx": rec_idx,
            "record": f"{rec.get('rarity')} {rec.get('name')} x{rec.get('quantity')}",
            "collector_dice": collector_dice,
            "name_cy": name_cy,
            "name_h": name_h,
            "wide_crop_size": list(wide_crop.size),
            "vl_answer": answer,
        })

        # 增量保存（防止中途失败丢失结果）
        summary_path = VL_OUTPUT_DIR / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n汇总: {VL_OUTPUT_DIR / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
