"""检查窗口坐标转换"""
import requests

API = "http://127.0.0.1:8766"
WINDOW = "异环  "

# 获取窗口bbox
resp = requests.get(f"{API}/screen/windows")
windows = resp.json().get("windows", [])
for w in windows:
    if "异环" in w.get("title", ""):
        bbox = w['bbox']
        print(f"窗口bbox: {bbox}")
        print(f"  left={bbox['left']}, top={bbox['top']}, right={bbox['right']}, bottom={bbox['bottom']}")
        print(f"  width={w['width']}, height={w['height']}")
        break

# 模拟点击坐标转换
# OCR识别到"标准棋盘" center=(694, 646)
# screen.py 中的转换: abs_x = bbox["left"] + x, abs_y = bbox["top"] + y
ocr_x, ocr_y = 694, 646
abs_x = bbox["left"] + ocr_x
abs_y = bbox["top"] + ocr_y
print(f"\nOCR坐标: ({ocr_x}, {ocr_y})")
print(f"转换后屏幕绝对坐标: ({abs_x}, {abs_y})")
print(f"  abs_x = {bbox['left']} + {ocr_x} = {abs_x}")
print(f"  abs_y = {bbox['top']} + {ocr_y} = {abs_y}")

# 但窗口left=0, top=0，所以转换后坐标就是OCR坐标本身
# 问题：OCR坐标是截图内的像素坐标，但窗口可能不是从(0,0)开始的
# 如果窗口在副屏，left可能不是0

# 检查：截图是窗口截图，OCR坐标是截图内的坐标
# 点击时用window_title，screen.py会加上窗口偏移
# 如果窗口在主屏(0,0)，偏移就是0，坐标就是OCR坐标
# 这应该是正确的

# 但用户说偏左偏下10px，可能是窗口边框/标题栏的偏移？
# 游戏全屏时没有边框，但窗口模式可能有

print(f"\n窗口类名: {w.get('class_name', 'unknown')}")
print(f"窗口标题: '{w.get('title', '')}'")

# 检查是否有标题栏偏移
# UnrealWindow 类型的窗口通常没有标题栏（游戏全屏/无边框窗口）
