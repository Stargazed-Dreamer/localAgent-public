"""PP-OCRv6 bbox 偏移校准 - 多尺寸网格采样工具

生成已知坐标的网格标记图，OCR 后用贪心位置匹配（不依赖文字识别），
拟合 affine 模型 raw = k*actual + b，输出 kx/ky/bx/by 及残差。

用途：
  - 仅在直接 detector A/B 已确认系统误差后拟合可选 affine
  - PaddleOCR 升级后辅助调查偏移模式

不要用本工具补偿 UVDoc：截图管线必须保持 use_doc_unwarping=False，
升级后的第一步是运行 verify_bbox_affine.py，而不是直接写入拟合参数。

用法：
  python tools/debug/sample_bbox_offset.py
  # 结果写入 temp/bbox_sampling_results.json

依赖后端运行（http://127.0.0.1:8766），调用 /ocr/file 接口。

详见 .agents/wip/ocr_bbox_offset.md 的探索指引。
"""
import json
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

API = "http://127.0.0.1:8766"
TEMP = Path("temp")


def generate_test_image(width, height, cols=5, rows=5):
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    font_size = max(24, min(width, height) // 25)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    margin_x = width * 0.1
    margin_y = height * 0.1
    step_x = (width - 2 * margin_x) / (cols - 1)
    step_y = (height - 2 * margin_y) / (rows - 1)

    labels = {}
    for i in range(rows):
        for j in range(cols):
            x = int(margin_x + j * step_x)
            y = int(margin_y + i * step_y)
            label = f"{i}{j}"
            # anchor="mm" 让文字中心对齐 (x, y)，消除字号不同导致的锚点偏移
            draw.text((x, y), label, fill="black", font=font, anchor="mm")
            labels[label] = (x, y)

    return img, labels, step_x, step_y


def ocr_image(img_path):
    with open(img_path, "rb") as f:
        # Calibration must observe the detector's raw coordinates. The normal
        # OCR endpoint applies the configured affine correction by default;
        # fitting those already-corrected boxes would produce meaningless
        # identity parameters and make the next correction drift.
        resp = requests.post(
            f"{API}/ocr/file",
            files={"file": f},
            data={"apply_bbox_fix": "false"},
            timeout=120,
        )
    return resp.json()


def match_by_position(details, labels, step_x, step_y):
    """贪心位置匹配：按距离从小到大匹配，不依赖 OCR 文字"""
    pairs = []
    for di, d in enumerate(details):
        box = d.get("box", [])
        if not box or len(box) < 2:
            continue
        cx = sum(p[0] for p in box) / len(box)
        cy = sum(p[1] for p in box) / len(box)
        for label, (lx, ly) in labels.items():
            dist = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            pairs.append((dist, di, label, cx, cy, lx, ly, d.get("text", "")))

    pairs.sort(key=lambda x: x[0])
    threshold = min(step_x, step_y) * 0.55

    used_details = set()
    used_labels = set()
    matched = []
    for dist, di, label, cx, cy, lx, ly, text in pairs:
        if di in used_details or label in used_labels:
            continue
        if dist > threshold:
            continue
        used_details.add(di)
        used_labels.add(label)
        matched.append({
            "label": label, "text": text,
            "raw_x": cx, "raw_y": cy,
            "actual_x": lx, "actual_y": ly,
            "dist": dist,
        })
    return matched


def fit_affine(matched):
    if len(matched) < 4:
        return None
    raw_x = np.array([m["raw_x"] for m in matched])
    raw_y = np.array([m["raw_y"] for m in matched])
    act_x = np.array([m["actual_x"] for m in matched])
    act_y = np.array([m["actual_y"] for m in matched])

    A = np.vstack([act_x, np.ones(len(act_x))]).T
    kx, bx = np.linalg.lstsq(A, raw_x, rcond=None)[0]
    Ay = np.vstack([act_y, np.ones(len(act_y))]).T
    ky, by = np.linalg.lstsq(Ay, raw_y, rcond=None)[0]

    pred_x = kx * act_x + bx
    pred_y = ky * act_y + by
    res_x = raw_x - pred_x
    res_y = raw_y - pred_y

    return {
        "kx": float(kx), "bx": float(bx),
        "ky": float(ky), "by": float(by),
        "bx_ratio": float(bx / matched[0]["actual_x"]) if matched[0]["actual_x"] else None,
        "x_center": float(-bx / (kx - 1)) if abs(kx - 1) > 1e-6 else None,
        "y_center": float(-by / (ky - 1)) if abs(ky - 1) > 1e-6 else None,
        "res_x_max": float(np.max(np.abs(res_x))),
        "res_y_max": float(np.max(np.abs(res_y))),
        "res_x_rms": float(np.sqrt(np.mean(res_x ** 2))),
        "res_y_rms": float(np.sqrt(np.mean(res_y ** 2))),
        "n": len(matched),
    }


def main():
    sizes = [
        (800, 600),
        (1280, 720),
        (1920, 1080),
        (2560, 1440),
    ]

    all_results = {}
    for w, h in sizes:
        size_key = f"{w}x{h}"
        print(f"\n--- {size_key} ---", flush=True)

        img, labels, step_x, step_y = generate_test_image(w, h)
        img_path = TEMP / f"grid_{size_key}.png"
        img.save(str(img_path))
        print(f"  generated: {len(labels)} labels, step=({step_x:.0f},{step_y:.0f})", flush=True)

        t0 = time.time()
        try:
            ocr = ocr_image(str(img_path))
        except Exception as e:
            print(f"  OCR FAILED: {e}", flush=True)
            continue
        elapsed = time.time() - t0
        details = ocr.get("details", [])
        print(f"  OCR: {len(details)} results, {elapsed:.1f}s", flush=True)

        matched = match_by_position(details, labels, step_x, step_y)
        print(f"  matched: {len(matched)}/{len(labels)}", flush=True)

        fit = fit_affine(matched)
        if fit:
            cx, cy = w / 2, h / 2
            x0 = fit["x_center"] or 0
            y0 = fit["y_center"] or 0
            print(f"  kx={fit['kx']:.4f}, bx={fit['bx']:.2f}, bx/W={fit['bx']/w:+.4f}, x0={x0:.1f} (cx={cx:.0f}, x0-cx={x0-cx:+.1f})")
            print(f"  ky={fit['ky']:.4f}, by={fit['by']:.2f}, by/H={fit['by']/h:+.4f}, y0={y0:.1f} (cy={cy:.0f}, y0-cy={y0-cy:+.1f})")
            print(f"  residual: X max={fit['res_x_max']:.1f}px rms={fit['res_x_rms']:.1f}px")
            print(f"            Y max={fit['res_y_max']:.1f}px rms={fit['res_y_rms']:.1f}px")

        all_results[size_key] = {"width": w, "height": h, "matched": matched, "fit": fit}

    # 汇总
    print("\n" + "=" * 95)
    print("SUMMARY: affine params vs image size")
    print("=" * 95)
    hdr = (f"{'Size':12s} {'kx':>8s} {'bx':>8s} {'bx/W':>7s} {'x0-cx':>8s} | "
           f"{'ky':>8s} {'by':>8s} {'by/H':>7s} {'y0-cy':>8s} | {'Xrms':>5s} {'Yrms':>5s}")
    print(hdr)
    print("-" * len(hdr))
    for size_key, r in all_results.items():
        if r.get("fit"):
            f = r["fit"]
            w, h = r["width"], r["height"]
            x0 = f["x_center"] or 0
            y0 = f["y_center"] or 0
            cx, cy = w / 2, h / 2
            print(f"{size_key:12s} {f['kx']:8.4f} {f['bx']:8.2f} {f['bx']/w:+7.4f} {x0-cx:+8.1f} | "
                  f"{f['ky']:8.4f} {f['by']:8.2f} {f['by']/h:+7.4f} {y0-cy:+8.1f} | "
                  f"{f['res_x_rms']:5.1f} {f['res_y_rms']:5.1f}")

    # 推荐参数（各尺寸平均）
    fits = [r["fit"] for r in all_results.values() if r.get("fit")]
    if fits:
        import statistics as st
        avg = {
            "kx": st.mean(f["kx"] for f in fits),
            "ky": st.mean(f["ky"] for f in fits),
            "bx_ratio": st.mean(f["bx"] / all_results[k]["width"] for k, f in
                                ((s, all_results[s]["fit"]) for s in all_results if all_results[s].get("fit"))),
            "by_ratio": st.mean(f["by"] / all_results[k]["height"] for k, f in
                                ((s, all_results[s]["fit"]) for s in all_results if all_results[s].get("fit"))),
        }
        print("\n推荐 config.toml [ocr] 参数（各尺寸平均）：")
        print(f"  kx = {avg['kx']:.4f}")
        print(f"  ky = {avg['ky']:.4f}")
        print(f"  bx_ratio = {avg['bx_ratio']:.4f}")
        print(f"  by_ratio = {avg['by_ratio']:.4f}")

    out_path = TEMP / "bbox_sampling_results.json"
    with open(str(out_path), "w", encoding="utf-8") as fp:
        json.dump(all_results, fp, indent=2, ensure_ascii=False)
    print(f"\nresults saved to {out_path}")


if __name__ == "__main__":
    main()
