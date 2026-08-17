"""检查截图分辨率 vs 窗口实际分辨率"""
import requests
import base64
from PIL import Image
import io

API = "http://127.0.0.1:8766"
WINDOW = "异环  "

# 1. 获取窗口信息
resp = requests.get(f"{API}/screen/windows")
windows = resp.json().get("windows", [])
for w in windows:
    if "异环" in w.get("title", ""):
        print(f"窗口: {w['title']}")
        print(f"  bbox: {w['bbox']}")
        print(f"  实际尺寸: {w['width']}x{w['height']}")
        break

# 2. 截图并检查分辨率
resp = requests.post(f"{API}/screen/capture", json={"mode": "window", "window_title": WINDOW})
data = resp.json()
if data.get("success"):
    img_data = base64.b64decode(data["image"])
    img = Image.open(io.BytesIO(img_data))
    print(f"\n截图分辨率: {img.size[0]}x{img.size[1]}")
    print(f"截图DPI: {img.info.get('dpi', 'unknown')}")

    # 计算缩放比
    for w in windows:
        if "异环" in w.get("title", ""):
            win_w = w['width']
            win_h = w['height']
            scale_x = img.size[0] / win_w
            scale_y = img.size[1] / win_h
            print(f"\n缩放比: x={scale_x:.3f}, y={scale_y:.3f}")
            if abs(scale_x - 1.0) > 0.01 or abs(scale_y - 1.0) > 0.01:
                print("⚠️ 截图和窗口分辨率不一致！OCR坐标需要乘以缩放比的倒数才是实际点击坐标")
                print(f"  OCR坐标 -> 实际坐标: x * {1/scale_x:.3f}, y * {1/scale_y:.3f}")
            break
