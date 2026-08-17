"""PaddleOCR 截图 bbox 坐标验证工具

生成已知坐标的网格图（anchor="mm" 消除字号锚点混淆），OCR 后检查
返回坐标与真实坐标的偏差。用于验证截图 OCR 坐标链路。

用途：
  - PaddleOCR 升级后确认截图坐标仍正确
  - 排查"OCR 坐标不准"类问题

用法：
  python tools/debug/verify_bbox_affine.py

判定标准：
  - 每档匹配 25/25，mean≈0、rms 约 1-3px、max≤5px：坐标链路正确
  - 误差显著增大：先检查 use_doc_unwarping 是否被重新启用

依赖后端运行（http://127.0.0.1:8766），调用 /ocr/base64 接口。
OCR 返回的 box 坐标应当保持原始截图坐标系。

详见 .agents/wip/ocr_bbox_offset.md。
"""
import base64
import io

import requests
from PIL import Image, ImageDraw, ImageFont

API = "http://127.0.0.1:8766"


def make_grid(w, h, rows=5, cols=5):
    """生成网格图，每个交叉点放一个 2 位标签，返回 {label: (cx, cy)}"""
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", max(24, min(w, h) // 25))
    except Exception:
        font = ImageFont.load_default()
    labels = {}
    step_x = w / (cols + 1)
    step_y = h / (rows + 1)
    for r in range(rows):
        for c in range(cols):
            cx = int(step_x * (c + 1))
            cy = int(step_y * (r + 1))
            label = f"{r}{c}"
            # anchor="mm" 让文字中心对齐 (cx, cy)，消除字号不同导致的锚点偏移
            draw.text((cx, cy), label, fill="black", font=font, anchor="mm")
            labels[label] = (cx, cy)
    return img, labels


def ocr_image(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = requests.post(f"{API}/ocr/base64", data={"data": b64}, timeout=60)
    return resp.json()


def match_by_position(details, labels, step_x, step_y):
    pairs = []
    for di, d in enumerate(details):
        box = d.get("box", [])
        if not box or len(box) < 2:
            continue
        cx = sum(p[0] for p in box) / len(box)
        cy = sum(p[1] for p in box) / len(box)
        for label, (lx, ly) in labels.items():
            dist = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            pairs.append((dist, di, label, cx, cy, lx, ly))
    pairs.sort(key=lambda x: x[0])
    threshold = min(step_x, step_y) * 0.55
    used_d, used_l, matched = set(), set(), []
    for dist, di, label, cx, cy, lx, ly in pairs:
        if di in used_d or label in used_l or dist > threshold:
            continue
        used_d.add(di)
        used_l.add(label)
        matched.append({"label": label, "raw_x": cx, "raw_y": cy, "actual_x": lx, "actual_y": ly, "dist": dist})
    return matched


def stats(diffs):
    if not diffs:
        return "n/a"
    rms = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
    return f"rms={rms:.1f} max={max(abs(d) for d in diffs):.1f} mean={sum(diffs)/len(diffs):.1f}"


print("=" * 70)
print("PaddleOCR 截图 bbox 坐标验证")
print("=" * 70)
for size_name, (w, h) in [("800x600", (800, 600)), ("1920x1080", (1920, 1080)), ("2560x1440", (2560, 1440))]:
    img, labels = make_grid(w, h)
    rows, cols = 5, 5
    step_x, step_y = w / (cols + 1), h / (rows + 1)
    ocr = ocr_image(img)
    details = ocr.get("details", [])
    matched = match_by_position(details, labels, step_x, step_y)
    if not matched:
        print(f"{size_name}: 无匹配")
        continue
    fix_dx = [m["raw_x"] - m["actual_x"] for m in matched]
    fix_dy = [m["raw_y"] - m["actual_y"] for m in matched]
    print(f"\n{size_name}  (匹配 {len(matched)}/25 个标记)")
    print(f"  坐标偏差 X: {stats(fix_dx)}")
    print(f"  坐标偏差 Y: {stats(fix_dy)}")

print("\n" + "=" * 70)
print("判定：25/25 匹配、mean≈0、rms 约 1-3px、max≤5px → 坐标链路正确")
print("         误差显著增大时先检查 use_doc_unwarping 是否被启用")
