"""异环抽卡记录收集器 v4 - 骰子点数识别+集点赠礼检测

改进点（相比 v3）：
- 骰子点数识别：通过图像处理检测骰子图标中的亮色圆点数量(1-6)
- 集点赠礼检测：OCR文字+颜色+形状三重检测，0=赠礼
- 优化stdout输出：展示页数/总页数、点数、名称、数量、时间
- 页码跟踪：记录已读页码，检测翻页是否真正生效
- 重复检测：如果翻页后页码未变化，自动重试
- 连续失败重试：连续3次翻页失败后停止，避免死循环
- 颜色稀有度检测：通过裁切文本区域像素判断 S/A/B 稀有度
"""
import requests
import time
import json
import random
import re
import base64
import io
import os
import sys
from pathlib import Path
from collections import deque
from datetime import datetime
from PIL import Image

API = "http://127.0.0.1:8766"
WINDOW = "异环  "
OUTPUT_DIR = Path("workspace/yihuan_gacha")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 调试模式：YIYUAN_DEBUG=1 开启后打印每步耗时（截图/OCR/翻页）
DEBUG = os.environ.get("YIYUAN_DEBUG") == "1"

# OCR 超时（秒）：CPU 推理大图约 15-40s/页，留足余量；超时说明后端异常，避免脚本永久挂死
OCR_TIMEOUT = 180

# 骰子图标中心x坐标（v2 修正：通过5张样本图分析确认实际中心x=610，bbox [584,636]）
# 原 v1 用的 629 也落在骰子区域内但非中心，导致 y 裁切范围偏移截断骰子下半部分
DICE_CX = 610


_window_bbox_cache = None
_vscreen_offset_cache = None


def _get_window_bbox():
    """获取异环窗口的bbox（带缓存）"""
    global _window_bbox_cache
    if _window_bbox_cache:
        return _window_bbox_cache
    resp = requests.get(f"{API}/screen/windows", timeout=10)
    for w in resp.json().get("windows", []):
        if w.get("title", "").strip() == WINDOW.strip():
            _window_bbox_cache = w["bbox"]
            return _window_bbox_cache
    return None


def _get_vscreen_offset():
    """获取虚拟屏幕左上角偏移（带缓存）"""
    global _vscreen_offset_cache
    if _vscreen_offset_cache is not None:
        return _vscreen_offset_cache
    import ctypes
    _vscreen_offset_cache = (
        ctypes.windll.user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        ctypes.windll.user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
    )
    return _vscreen_offset_cache


def capture_window():
    """截取异环窗口，返回 base64 图片（含重试）

    使用 force_fullscreen_crop=True 跳过 PrintWindow（DirectX 全屏游戏假成功），
    后端自动用全屏截图+裁剪获取窗口内容。"""
    for attempt in range(3):
        try:
            resp = requests.post(f"{API}/screen/capture",
                json={"mode": "window", "window_title": WINDOW, "format": "base64",
                      "force_fullscreen_crop": True}, timeout=30)
            data = resp.json()
            if not data.get("success"):
                print(f"截图失败(重试{attempt+1}/3): {data.get('detail', data)}")
                time.sleep(2)
                continue
            return data["image"]
        except Exception as e:
            print(f"截图异常(重试{attempt+1}/3): {e}")
            time.sleep(2)
    print(f"截图失败，已重试3次")
    return None


def ocr_image(image_b64):
    """OCR识别图片（带超时，防止后端挂死时脚本永久阻塞）"""
    try:
        resp = requests.post(f"{API}/ocr/base64", data={"data": image_b64}, timeout=OCR_TIMEOUT)
    except requests.exceptions.Timeout:
        print(f"[!] OCR 超时（{OCR_TIMEOUT}s），后端可能异常（GPU 推理挂死？），放弃本页")
        return None, []
    except requests.exceptions.RequestException as e:
        print(f"[!] OCR 请求异常: {e}")
        return None, []
    data = resp.json()
    if not data.get("success"):
        print(f"OCR失败: {data}")
        return None, []
    return data.get("text", ""), data.get("details", [])


def capture_and_ocr():
    """截图+OCR一步完成（调试模式下打印各步耗时）"""
    t0 = time.perf_counter()
    image = capture_window()
    if not image:
        return None, []
    t1 = time.perf_counter()
    text, details = ocr_image(image)
    t2 = time.perf_counter()
    if DEBUG:
        print(f"    [debug] 截图 {t1-t0:.1f}s | OCR {t2-t1:.1f}s | {len(details)} 项")
    return image, details


def ensure_authorization():
    """申请屏幕控制授权（采集前主动调用，防后端重启/授权过期导致点击 403）

    POST /screen/control/request 会弹窗阻塞等待用户批准（后端默认 30s 超时），
    响应 status="authorized" 表示成功。已授权时重新请求也会覆盖刷新（幂等）。
    """
    try:
        resp = requests.post(f"{API}/screen/control/request", json={
            "source": "yihuan_gacha",
            "task_description": "异环抽卡记录采集：需要自动翻页读取掷骰记录",
        }, timeout=60)
        data = resp.json()
    except Exception as e:
        print(f"[!] 授权申请失败: {e}")
        return False
    if data.get("status") == "authorized":
        print("屏幕控制授权: 已通过")
        return True
    print(f"[!] 屏幕控制授权未通过: status={data.get('status')} {data.get('message', '')}")
    return False


def click_at(x, y, element_text="", require_confirm=False):
    """点击指定坐标（窗口相对坐标）；403 时自动申请授权并重试一次"""
    resp = requests.post(f"{API}/screen/action", json={
        "action": "click", "x": int(x), "y": int(y),
        "window_title": WINDOW, "element_text": element_text,
        "require_confirm": require_confirm
    })
    if resp.status_code == 403:
        # 授权丢失/过期（后端重启或 idle 超时撤销）→ 申请后重试一次
        print("  [!] 点击被拒（403 未授权），尝试申请屏幕控制授权...")
        if ensure_authorization():
            resp = requests.post(f"{API}/screen/action", json={
                "action": "click", "x": int(x), "y": int(y),
                "window_title": WINDOW, "element_text": element_text,
                "require_confirm": require_confirm
            })
    if resp.status_code == 422:
        errors = resp.json().get("detail", [])
        err_info = "; ".join(
            f"{e.get('loc',['?'][-1])}: {e.get('msg','')}" for e in errors
        )
        print(f"  点击({x},{y}) '{element_text}': 参数验证失败 - {err_info}")
        return False
    data = resp.json()
    success = data.get("success", False)
    if not success:
        print(f"  点击({x},{y}) '{element_text}': FAILED status={data.get('status')} msg={data.get('message', '')}")
    return success


def find_text_center(details, target_text, y_min=0, y_max=9999):
    """在OCR结果中查找指定文本的中心坐标"""
    for item in details:
        text = item.get("text", "")
        box = item.get("box", [])
        if target_text in text and box:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs) / 4 + random.randint(-3, 3)
            cy = sum(ys) / 4 + random.randint(-3, 3)
            if y_min <= cy <= y_max:
                return cx, cy, text
    return None


def detect_rarity_by_color(image_b64, bbox):
    """通过裁切文本区域检测颜色来判断稀有度
    S级=金黄色(253,181,11), A级=粉紫色(231,63,189), B级=灰色(106,106,106)
    """
    try:
        from PIL import Image
        import numpy as np

        img_data = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_data))
        img_array = np.array(img)

        x_min = int(min(p[0] for p in bbox))
        x_max = int(max(p[0] for p in bbox))
        y_min = int(min(p[1] for p in bbox))
        y_max = int(max(p[1] for p in bbox))

        x_min = max(0, x_min - 3)
        x_max = min(img_array.shape[1], x_max + 3)
        y_min = max(0, y_min - 3)
        y_max = min(img_array.shape[0], y_max + 3)

        if x_max <= x_min or y_max <= y_min:
            return "?"

        crop = img_array[y_min:y_max, x_min:x_max]
        r, g, b = crop[:,:,0], crop[:,:,1], crop[:,:,2]
        is_bg = (r > 200) & (g > 200) & (b > 200)
        is_border = (r < 40) & (g < 40) & (b < 40)
        is_text = ~is_bg & ~is_border

        text_pixels = crop[is_text]
        if len(text_pixels) < 5:
            return "?"

        avg_r = text_pixels[:, 0].mean()
        avg_g = text_pixels[:, 1].mean()
        avg_b = text_pixels[:, 2].mean()

        if avg_r > 200 and avg_g > 100 and avg_b < 80:
            return "S"
        elif avg_r > 180 and avg_b > 100 and avg_g < 100:
            return "A"
        elif abs(avg_r - avg_g) < 30 and abs(avg_g - avg_b) < 30 and avg_r < 180:
            return "B"
        elif avg_r > 200 and avg_g > 80 and avg_b < 100:
            return "S"
        elif avg_r > 150 and avg_b > 80 and avg_g < avg_b:
            return "A"
        else:
            return "?"
    except Exception as e:
        print(f"  颜色检测失败: {e}")
        return "?"


def detect_dice_points(image_b64, name_cy, name_h, ocr_details=None):
    """检测骰子点数（图像处理，v2 算法）

    v2 改进（2026-07-09）：定向裁切 + 封闭圆角方形强约束
    - 修正 DICE_CX=610（原 629 非中心）
    - 定向裁切：x 围绕 610（±40），y 围绕 name_cy（-0.5*name_h ~ +1.5*name_h）
      y 搜索范围 2*name_h ≈ 70 < 骰子间距 80，不会匹配到相邻骰子
    - 强约束筛选：尺寸 45-60、宽高比 0.8-1.2、填充率>0.5、cx 在 [595,625]
    - 解决了 v1 y 裁切范围太窄截断骰子下半部分导致点数偏少的问题

    算法：
    1. OCR检查"集点赠礼"/"沉眠地"文字
    2. 定向裁切骰子区域
    3. 区分骰子底色(RGB~82,83,82,G略高)和赠礼文字(RGB~74,74,74,RGB几乎相等)
    4. 找所有骰子底色连通域，用强约束筛选"封闭圆角方形"
    5. 选 y 偏移最接近 name_h*0.5 的候选（骰子通常在 name 下方半个 name_h）
    6. 在骰子内部找亮色像素(RGB~224,224,226) → 连通域计数(过滤<30px噪点) = 点数

    返回: 1-6=点数, 0=集点赠礼, -1=无法识别, -2=沉眠地

    注: -2 是采集阶段的内部信号(保留 raw 数据区分信息)，清洗时会归为
        dice_points=0 + action="沉眠地"(与集点赠礼同属额外奖励类，供模拟器使用)
    """
    try:
        from PIL import Image
        import numpy as np

        img_data = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_data))
        img_array = np.array(img)

        # Step 1: OCR检查"集点赠礼"和"沉眠地"
        if ocr_details:
            for item in ocr_details:
                text = item.get("text", "")
                box = item.get("box", [])
                if "集点赠礼" in text and box:
                    item_cy = sum(p[1] for p in box) / 4
                    if abs(item_cy - name_cy) < name_h:
                        return 0
                if "沉眠地" in text and box:
                    item_cy = sum(p[1] for p in box) / 4
                    if abs(item_cy - name_cy) < name_h:
                        return -2

        # Step 2: 定向裁切区域（v2 改进）
        # x: DICE_CX ± 40 = [570, 650]（覆盖骰子宽度 53 + 余量）
        # y: name_cy - 0.5*name_h 到 name_cy + 1.5*name_h
        #    骰子在 name 下方约 +5~+26 像素，y 搜索范围 2*name_h ≈ 70 < 骰子间距 80
        dice_left = max(0, int(DICE_CX - 40))
        dice_right = min(img_array.shape[1], int(DICE_CX + 40))
        dice_top = max(0, int(name_cy - name_h * 0.5))
        dice_bot = min(img_array.shape[0], int(name_cy + name_h * 1.5))

        if dice_right <= dice_left or dice_bot <= dice_top:
            return -1

        crop = img_array[dice_top:dice_bot, dice_left:dice_right]
        h, w = crop.shape[:2]
        total = h * w

        r, g, b = crop[:,:,0].astype(int), crop[:,:,1].astype(int), crop[:,:,2].astype(int)

        # Step 3: 区分骰子底色和赠礼文字
        # 骰子底色(82,83,82): G通道略高(G-R>=0)
        # 赠礼文字(74,74,74): RGB几乎相等(|R-G|<5, |G-B|<5)
        is_dice_base = (np.abs(r - 82) < 15) & (np.abs(g - 83) < 15) & (np.abs(b - 82) < 15) & \
                       ((g - r) >= 0)
        is_gift_text = (np.abs(r - 74) < 15) & (np.abs(g - 74) < 15) & (np.abs(b - 74) < 15) & \
                       (np.abs(r - g) < 5) & (np.abs(g - b) < 5)

        dice_base_ratio = is_dice_base.sum() / total
        gift_text_ratio = is_gift_text.sum() / total

        # 赠礼文字显著且骰子底色少 → 集点赠礼
        if gift_text_ratio > 0.005 and dice_base_ratio < 0.05:
            return 0

        if dice_base_ratio < 0.02:
            return -1

        # Step 4: 找所有骰子底色连通域，用强约束筛选"封闭圆角方形"（v2 改进）
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
                        for dy, dx in [(-1,0),(1,0),(0,-1),(0,1)]:
                            ny, nx = cy2+dy, cx2+dx
                            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and is_dice_base[ny, nx]:
                                visited[ny, nx] = True
                                queue.append((ny, nx))
                    components.append((count, min_y2, max_y2, min_x2, max_x2))

        if not components:
            return -1

        # 强约束筛选：骰子尺寸固定 53x53，cx 固定 610
        expected_cx_rel = DICE_CX - dice_left  # 骰子中心 x 相对 crop
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

            # y 偏移（相对 name_cy）
            center_y = (min_y2 + max_y2) / 2
            name_cy_rel = name_cy - dice_top
            y_offset = center_y - name_cy_rel

            candidates.append({
                "count": count, "bbox": (min_y2, max_y2, min_x2, max_x2),
                "y_offset": y_offset,
            })

        if not candidates:
            # 没找到符合条件的骰子方形
            if gift_text_ratio > 0.003:
                return 0
            return -1

        # Step 5: 选最佳候选——y 偏移最接近 name_h*0.5（骰子通常在 name 下方半个 name_h）
        expected_y_offset = name_h * 0.5
        candidates.sort(key=lambda c: abs(c["y_offset"] - expected_y_offset))
        best = candidates[0]

        min_y2, max_y2, min_x2, max_x2 = best["bbox"]

        # Step 6: 在骰子内部找亮色点
        inner_y1 = min_y2 + 2
        inner_y2 = max_y2 - 1
        inner_x1 = min_x2 + 2
        inner_x2 = max_x2 - 1

        if inner_y2 <= inner_y1 or inner_x2 <= inner_x1:
            return -1

        inner_crop = crop[inner_y1:inner_y2+1, inner_x1:inner_x2+1]
        ir, ig, ib = inner_crop[:,:,0].astype(int), inner_crop[:,:,1].astype(int), inner_crop[:,:,2].astype(int)

        is_dot = (np.abs(ir - 224) < 20) & (np.abs(ig - 224) < 20) & (np.abs(ib - 226) < 20)
        dot_px = is_dot.sum()

        ih, iw = inner_crop.shape[:2]

        if dot_px < 2:
            return -1

        # 连通域计数
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
                        for dy, dx in [(-1,0),(1,0),(0,-1),(0,1)]:
                            ny, nx = cy2+dy, cx2+dx
                            if 0 <= ny < ih and 0 <= nx < iw and not visited2[ny, nx] and is_dot[ny, nx]:
                                visited2[ny, nx] = True
                                queue.append((ny, nx))
                    dot_sizes.append(cnt)

        # 过滤噪点：真实点数圆点≥30px，角落噪点<25px
        MIN_DOT_SIZE = 30
        real_dots = [s for s in dot_sizes if s >= MIN_DOT_SIZE]
        num_real = len(real_dots)

        if 1 <= num_real <= 6:
            return num_real
        else:
            return -1

    except Exception as e:
        print(f"  骰子检测异常: {e}")
        return -1


def parse_page_records(details, image_b64=None):
    """解析当前页的抽卡记录（含颜色稀有度+骰子点数检测）"""
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

    # 按y坐标分组
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

    # 识别数据行
    name_prefixes = ["弧盘", "角色", "道具", "滑翔翼"]
    time_pattern = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
    time_suffix_pattern = re.compile(r'\d{2}:\d{2}:\d{2}')

    records = []
    header_found = False

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

        has_name = any(
            prefix in item["text"]
            for item in row
            for prefix in name_prefixes
        )

        if not has_name:
            continue

        # 解析行
        row_texts = [item["text"] for item in row]
        full_text = " ".join(row_texts)

        # 提取道具名称
        name = ""
        name_box = None
        for item in row:
            for prefix in name_prefixes:
                if prefix in item["text"]:
                    idx = row.index(item)
                    name_parts = [item["text"]]
                    name_box = item["box"]
                    if "·" not in item["text"] and "「" not in item["text"]:
                        for j in range(idx + 1, len(row)):
                            next_text = row[j]["text"]
                            name_parts.append(next_text)
                            if "·" in next_text or "」" in next_text:
                                break
                    else:
                        if item["text"].endswith("。") or item["text"].endswith("「"):
                            for j in range(idx + 1, len(row)):
                                next_text = row[j]["text"]
                                if next_text in ["」", "」"] or len(next_text) <= 2:
                                    name_parts.append(next_text)
                                else:
                                    break

                    name = "".join(name_parts)
                    break
            if name:
                break

        # 清理名称
        name = name.replace('"', '·').replace(""", '·').replace(""", '·')
        if not re.match(r'^(弧盘|角色|道具|滑翔翼)·', name):
            for prefix in name_prefixes:
                if prefix in name and "·" not in name:
                    name = name.replace(prefix, prefix + "·", 1)
                    break

        # 提取时间
        date_part = ""
        time_part = ""
        for text in row_texts:
            m = time_pattern.search(text)
            if m:
                date_part = m.group()
            m2 = time_suffix_pattern.search(text)
            if m2:
                time_part = m2.group()
        time_str = f"{date_part} {time_part}".strip()

        # 提取数量：在行文本中找独立整数（非日期/时间部分）
        quantity = 1
        date_time_texts = set()
        for text in row_texts:
            if time_pattern.search(text) or time_suffix_pattern.search(text):
                date_time_texts.add(text)
            # "X月X日" 这种部分日期文本也排除
            if re.search(r'\d{1,2}月\d{1,2}日', text):
                date_time_texts.add(text)
        for text in row_texts:
            t = text.strip()
            if t in date_time_texts:
                continue
            # 独立整数（1-99），不是日期/时间的一部分
            if re.match(r'^\d{1,2}$', t):
                q = int(t)
                if q >= 1:
                    quantity = q
                    break

        # 如果名称末尾粘着数字（OCR合并），尝试提取
        # 先剥离名称中的时间信息，再提取数量
        # 例如 "道具·-迷迭棋子302026年5月7日13:56:47" → 先去时间 → "道具·-迷迭棋子30" → 提取30
        if quantity == 1:
            # 剥离名称中的时间信息
            name_no_time = re.sub(r'\d*\d{4}年\d{1,2}月\d{1,2}日.*$', '', name)
            name_no_time = re.sub(r'\d{2,4}\d?月\d{1,2}日.*$', '', name_no_time)
            # 从剥离时间后的名称末尾提取数字
            name_qty_match = re.search(r'^(.*[^\d])(\d{1,2})$', name_no_time)
            if name_qty_match:
                candidate_name = name_qty_match.group(1)
                candidate_qty = int(name_qty_match.group(2))
                # 确认名称去掉数字后仍是合法名称（以已知前缀开头）
                if any(candidate_name.startswith(p) for p in name_prefixes) and candidate_qty > 1:
                    name = candidate_name
                    quantity = candidate_qty

        # 检测稀有度
        rarity = "?"
        name_cy = 0
        name_h = 0
        if name_box and image_b64:
            rarity = detect_rarity_by_color(image_b64, name_box)
            name_ys = [p[1] for p in name_box]
            name_cy = sum(name_ys) / 4
            name_h = max(name_ys) - min(name_ys)

        # 检测骰子点数（图像处理）
        dice = -1
        if image_b64 and name_cy and name_h:
            dice = detect_dice_points(image_b64, name_cy, name_h, details)

        if name:
            records.append({
                "name": name,
                "quantity": quantity,
                "time": time_str,
                "rarity": rarity,
                "dice": dice,
            })

    return records


def parse_weapon_page_records(details, image_b64=None):
    """解析弧盘研募页面的记录（三列：弧盘列表/研募类型/研募时间）

    与角色池 parse_page_records 的区别：
    - 无骰子列、无数量列（每条记录 count=1, dice_points=-1）
    - 名称无"弧盘·"前缀（武器池全是弧盘，UI不显示前缀）
    - 新增 research_type 字段（研募类型，如"奇迹盒盒"）
    - 三列布局：名称(cx<1100) / 研募类型(1100<=cx<1600) / 时间(cx>=1600)
    """
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

    # 按y坐标分组（同角色池逻辑）
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

    # 识别数据行
    time_pattern = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
    time_suffix_pattern = re.compile(r'\d{2}:\d{2}:\d{2}')

    # 武器池页面噪音文本（需跳过）
    skip_texts = {"上一页", "下一页", "研募详情", "研募列表", "规则说明",
                  "研募记录", "弧盘列表", "研募类型", "研募时间"}

    records = []
    header_found = False

    for row in rows:
        row_text = " ".join(item["text"] for item in row)
        # 表头行（弧盘列表/研募类型/研募时间）
        if "弧盘列表" in row_text or "研募类型" in row_text:
            header_found = True
            continue

        if not header_found:
            continue

        # 跳过导航/说明文本
        if any(t in row_text for t in ["上一页", "下一页", "研募详情", "研募列表",
                                        "规则说明", "研募记录", "可在本页面查询"]):
            continue
        # 跳过页码 "10/14"
        if re.match(r'^\d+/\d+$', row_text.strip()):
            continue

        # 按列提取
        # 名称列: cx < 1100
        # 研募类型列: 1100 <= cx < 1600
        # 时间列: cx >= 1600
        name_parts = []
        name_box = None
        research_type = ""
        date_part = ""
        time_part = ""

        for item in row:
            cx = item["cx"]
            text = item["text"]
            if cx < 1100:
                name_parts.append(text)
                if name_box is None:
                    name_box = item["box"]
            elif 1100 <= cx < 1600:
                research_type = text
            else:
                m = time_pattern.search(text)
                if m:
                    date_part = m.group()
                m2 = time_suffix_pattern.search(text)
                if m2:
                    time_part = m2.group()

        name = "".join(name_parts)
        time_str = f"{date_part} {time_part}".strip()

        # 必须有名称和时间才算有效记录
        if not name or not date_part:
            continue

        # 检测稀有度（颜色检测，同角色池）
        rarity = "?"
        if name_box and image_b64:
            rarity = detect_rarity_by_color(image_b64, name_box)

        records.append({
            "name": name,
            "quantity": 1,
            "time": time_str,
            "rarity": rarity,
            "dice": -1,  # 武器池无骰子
            "research_type": research_type,
        })

    return records


def get_current_page_info(details):
    """获取当前页码"""
    for item in details:
        text = item.get("text", "")
        m = re.search(r'(\d+)/(\d+)', text)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def switch_board_type(details, target_type, max_retries=5):
    """切换棋盘类型（含重试逻辑，多次点击+验证）"""
    for attempt in range(max_retries):
        # 重新获取当前状态（第2次起需要重新截图）
        if attempt > 0:
            print(f"  切换重试第{attempt}次...")
            time.sleep(2)
            _, details = capture_and_ocr()

        # 查找当前棋盘名称
        current_board = None
        for item in details:
            text = item.get("text", "")
            if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
                current_board = text
                break

        if current_board == target_type:
            print(f"  已在目标棋盘: {target_type}")
            return True

        if not current_board:
            print("  未找到当前棋盘类型")
            continue

        # 点击当前棋盘名称打开下拉框（尝试多次点击）
        result = find_text_center(details, current_board, y_min=300, y_max=600)
        if not result:
            print("  未找到棋盘类型下拉框")
            continue

        cx, cy, _ = result
        print(f"  当前棋盘: {current_board}, 点击 ({cx:.0f},{cy:.0f})")
        # 多点几次确保打开下拉框
        for click_try in range(3):
            click_at(cx, cy, "打开下拉框", require_confirm=False)
            time.sleep(1.5)
            # 重新截图检查下拉框是否打开
            _, check_details = capture_and_ocr()
            # 检查是否出现了目标选项（说明下拉框已打开）
            for item in check_details:
                text = item.get("text", "")
                if text == target_type or (text in ["限定棋盘", "常驻棋盘", "标准棋盘"] and text != current_board):
                    # 下拉框已打开，跳出点击循环
                    break
            else:
                # 没找到其他选项，继续尝试点击
                continue
            break

        # 重新截图找选项
        _, new_details = capture_and_ocr()
        target = find_text_center(new_details, target_type)
        if target:
            cx, cy, _ = target
            print(f"  点击 {target_type} ({cx:.0f},{cy:.0f})")
            click_at(cx, cy, target_type, require_confirm=False)
            time.sleep(2.5)

            # 验证是否切换成功（多次验证）
            verified = False
            for verify_try in range(3):
                _, verify_details = capture_and_ocr()
                for item in verify_details:
                    text = item.get("text", "")
                    if text == target_type:
                        print(f"  切换成功: {target_type}")
                        verified = True
                        break
                if verified:
                    break
                print(f"  验证第{verify_try+1}次未检测到 {target_type}，等待重试...")
                time.sleep(1.5)

            if verified:
                return True
            print(f"  切换验证失败，未检测到 {target_type}")
        else:
            print(f"  未找到 {target_type} 选项")
            # 尝试按ESC或点击空白区域关闭下拉框
            click_at(500, 300, "关闭下拉框", require_confirm=False)
            time.sleep(1)

    print(f"  切换棋盘失败，已重试{max_retries}次")
    return False


def _print_record(r):
    """打印单条记录（紧凑格式）"""
    rarity = r.get("rarity", "?")
    dice = r.get("dice", -1)
    name = r.get("name", "?")
    qty = r.get("quantity", 1)
    t = r.get("time", "")
    # 点数显示: 0=赠礼, 1-6=点数, -1=???, -2=沉眠地
    if dice == 0:
        dice_str = "赠礼"
    elif dice == -2:
        dice_str = "沉眠地"
    elif 1 <= dice <= 6:
        dice_str = f" {dice}点"
    else:
        dice_str = " ???"
    print(f"  [{rarity}]{dice_str} {name} x{qty}  {t}")


def _print_weapon_record(r):
    """打印武器池单条记录（紧凑格式，无骰子）"""
    rarity = r.get("rarity", "?")
    name = r.get("name", "?")
    t = r.get("time", "")
    research_type = r.get("research_type", "")
    print(f"  [{rarity}] {name} ({research_type})  {t}")


def load_existing_database():
    """加载已有数据库，用于重复检测"""
    db_path = OUTPUT_DIR / "yihuan_gacha_database.json"
    if not db_path.exists():
        return {}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build_record_set(db_data):
    """从数据库构建记录指纹集合，用于快速判断重复
    指纹 = (棋盘类型, 名称, 时间戳)
    名称统一为不含前缀的格式（数据库中name不含前缀，采集脚本含前缀）
    时间统一为Unix时间戳字符串
    """
    record_set = set()
    for board_type, records in db_data.items():
        for r in records:
            # 数据库格式: name="成功的第一步", time=1777115077
            name = r.get("name", "")
            t = r.get("time", 0)
            # 去掉类型前缀（如"弧盘·"），统一为纯名称
            for prefix in ["弧盘·", "角色·", "道具·", "滑翔翼·"]:
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            record_set.add((board_type, name, str(t)))
    return record_set


def _normalize_record_for_match(record):
    """将采集脚本的记录归一化为指纹元组 (棋盘类型, 纯名称, 时间戳)"""
    name = record.get("name", "")
    # 去掉类型前缀
    for prefix in ["弧盘·", "角色·", "道具·", "滑翔翼·"]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    # 时间转为Unix时间戳
    time_str = record.get("time", "")
    ts = 0
    if time_str:
        m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2}):(\d{2})', str(time_str))
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)), int(m.group(6)))
                ts = int(dt.timestamp())
            except ValueError:
                pass
    return name, str(ts)


def check_duplicate_records(new_records, existing_set, board_name, threshold=2):
    """检查新记录与已有数据库的重复情况
    threshold: 重复记录数达到此值则判定为已采集过，停止该卡池
    返回: (重复条数, 是否应停止)
    """
    dup_count = 0
    for r in new_records:
        name, ts = _normalize_record_for_match(r)
        if (board_name, name, ts) in existing_set:
            dup_count += 1
    should_stop = dup_count >= threshold
    return dup_count, should_stop


def cleanup_old_raw(keep=3):
    """清理旧原始数据，只保留最近N次"""
    raw_dir = OUTPUT_DIR / "raw"
    if not raw_dir.exists():
        return
    raw_files = sorted(raw_dir.glob("yihuan_gacha_raw_*.json"))
    if len(raw_files) > keep:
        for old_file in raw_files[:-keep]:
            old_file.unlink()
            print(f"  清理旧原始数据: {old_file.name}")


def collect_board(board_name, max_pages=None, existing_set=None):
    """收集指定棋盘类型的所有页面（含页码跟踪+重复检测+自动终止+连续失败重试）"""
    print(f"\n{'='*60}")
    print(f"开始收集: {board_name}")
    print(f"{'='*60}")

    all_records = []
    seen_pages = set()  # 页码跟踪：防止重复读取同一页
    consecutive_failures = 0  # 连续失败重试计数器

    image, details = capture_and_ocr()
    if not image:
        return []

    # 验证当前棋盘类型是否匹配
    current_board = None
    for item in details:
        text = item.get("text", "")
        if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
            current_board = text
            break
    if current_board and current_board != board_name:
        print(f"[!] 警告: 期望棋盘'{board_name}'，实际'{current_board}'，切换可能失败!")
        print(f"[!] 跳过收集以避免重复数据")
        return []

    page, total = get_current_page_info(details)
    print(f"当前页: {page}/{total}")

    # 切换棋盘后游戏自动将页面置1，如果不在第1页说明棋盘切换失败
    if page and page > 1:
        print(f"[!] 警告: 切换棋盘后不在第1页(当前第{page}页)，棋盘切换可能失败!")
        print(f"[!] 当前仍在之前的棋盘，跳过收集以避免重复数据")
        return []

    if max_pages and total:
        total = min(total, max_pages)

    if total is None:
        print("无法确定总页数，只读取当前页")
        total = 1

    # 读取当前页
    records = parse_page_records(details, image)

    # 自动终止检测：第一页与已有数据库重复则停止
    if existing_set and records:
        dup_count, should_stop = check_duplicate_records(records, existing_set, board_name)
        if should_stop:
            print(f"[自动终止] 第1页有{dup_count}/{len(records)}条记录与数据库重复")
            print(f"[自动终止] {board_name}已采集过，跳过此卡池")
            return []
        elif dup_count > 0:
            print(f"  第1页有{dup_count}/{len(records)}条重复记录，但未达终止阈值，继续采集")

    all_records.extend(records)
    if page:
        seen_pages.add(page)
    print(f"第{page}/{total}页: {len(records)}条")
    for r in records:
        _print_record(r)

    # 翻页读取
    MAX_PAGE_RETRIES = 5  # 单次翻页最大重试次数
    while True:
        # 找到下一页按钮
        result = find_text_center(details, "下一页")
        if not result:
            print("找不到'下一页'按钮，停止翻页")
            break

        cx, cy, _ = result

        # 翻页重试
        page_turned = False
        for retry in range(MAX_PAGE_RETRIES):
            # 每次重试都重新找按钮位置（UI可能变化）
            if retry > 0:
                time.sleep(1.5)
                _, fresh_details = capture_and_ocr()
                if fresh_details:
                    result2 = find_text_center(fresh_details, "下一页")
                    if result2:
                        cx, cy, _ = result2
                    else:
                        print(f"  重试{retry}次时找不到'下一页'按钮")
                        continue

            success = click_at(cx, cy, "下一页", require_confirm=False)
            if not success:
                print(f"  翻页点击失败(重试{retry+1}/{MAX_PAGE_RETRIES})")
                time.sleep(2)
                continue

            time.sleep(1.5)

            # 重新截图OCR
            image, details = capture_and_ocr()
            if not image:
                print("截图失败，等待重试...")
                time.sleep(2)
                continue

            # 检查页码
            cur_page, _ = get_current_page_info(details)
            if cur_page is None:
                print(f"  无法识别页码(重试{retry+1}/{MAX_PAGE_RETRIES})")
                continue

            # 检查页码是否变化
            if cur_page in seen_pages:
                print(f"  页码{cur_page}已读取过，翻页未生效(重试{retry+1}/{MAX_PAGE_RETRIES})")
                time.sleep(1)
                continue

            page_turned = True
            break

        if not page_turned:
            if not image:
                break
            print(f"翻页失败，已重试{MAX_PAGE_RETRIES}次，停止")
            break

        seen_pages.add(cur_page)
        consecutive_failures = 0

        records = parse_page_records(details, image)
        all_records.extend(records)
        print(f"第{cur_page}/{total}页: {len(records)}条")
        for r in records:
            _print_record(r)

        # 检查是否到最后一页
        if cur_page >= total:
            print(f"已到达最后一页({total})")
            break

    print(f"\n{board_name}汇总: 共 {len(all_records)} 条记录")
    return all_records


def collect_weapon_pool(max_pages=None, existing_set=None):
    """收集弧盘研募池的所有页面（无棋盘切换/无骰子，普通抽卡）

    与 collect_board 的区别：
    - 无棋盘类型切换（武器池只有一个池，无需切换）
    - 无棋盘类型验证（不检查"限定棋盘"等文本）
    - 不要求从第1页开始（武器池不切换，页码可能任意）
    - 使用 parse_weapon_page_records 解析三列布局
    """
    board_name = "弧盘研募"
    print(f"\n{'='*60}")
    print(f"开始收集: {board_name}")
    print(f"{'='*60}")

    all_records = []
    seen_pages = set()

    image, details = capture_and_ocr()
    if not image:
        return []

    # 验证当前页面是弧盘研募记录页（检查表头特征）
    is_weapon_page = any(
        "弧盘列表" in item.get("text", "") or "研募类型" in item.get("text", "")
        for item in details
    )
    if not is_weapon_page:
        print("[!] 警告: 当前页面不是弧盘研募记录页！")
        print("[!] 请先在游戏中导航到 弧盘研募 → 研募记录 页面")
        return []

    page, total = get_current_page_info(details)
    print(f"当前页: {page}/{total}")

    if max_pages and total:
        total = min(total, max_pages)

    if total is None:
        print("无法确定总页数，只读取当前页")
        total = 1

    # 读取当前页
    records = parse_weapon_page_records(details, image)

    # 自动终止检测：第一页与已有数据库重复则停止
    if existing_set and records:
        dup_count, should_stop = check_duplicate_records(records, existing_set, board_name)
        if should_stop:
            print(f"[自动终止] 第{page}页有{dup_count}/{len(records)}条记录与数据库重复")
            print(f"[自动终止] {board_name}已采集过，跳过此卡池")
            return []
        elif dup_count > 0:
            print(f"  第{page}页有{dup_count}/{len(records)}条重复记录，但未达终止阈值，继续采集")

    all_records.extend(records)
    if page:
        seen_pages.add(page)
    print(f"第{page}/{total}页: {len(records)}条")
    for r in records:
        _print_weapon_record(r)

    # 翻页读取（同 collect_board 的翻页逻辑）
    MAX_PAGE_RETRIES = 5
    while True:
        result = find_text_center(details, "下一页")
        if not result:
            print("找不到'下一页'按钮，停止翻页")
            break

        cx, cy, _ = result

        page_turned = False
        for retry in range(MAX_PAGE_RETRIES):
            if retry > 0:
                time.sleep(1.5)
                _, fresh_details = capture_and_ocr()
                if fresh_details:
                    result2 = find_text_center(fresh_details, "下一页")
                    if result2:
                        cx, cy, _ = result2
                    else:
                        print(f"  重试{retry}次时找不到'下一页'按钮")
                        continue

            success = click_at(cx, cy, "下一页", require_confirm=False)
            if not success:
                print(f"  翻页点击失败(重试{retry+1}/{MAX_PAGE_RETRIES})")
                time.sleep(2)
                continue

            time.sleep(1.5)

            image, details = capture_and_ocr()
            if not image:
                print("截图失败，等待重试...")
                time.sleep(2)
                continue

            cur_page, _ = get_current_page_info(details)
            if cur_page is None:
                print(f"  无法识别页码(重试{retry+1}/{MAX_PAGE_RETRIES})")
                continue

            if cur_page in seen_pages:
                print(f"  页码{cur_page}已读取过，翻页未生效(重试{retry+1}/{MAX_PAGE_RETRIES})")
                time.sleep(1)
                continue

            page_turned = True
            break

        if not page_turned:
            if not image:
                break
            print(f"翻页失败，已重试{MAX_PAGE_RETRIES}次，停止")
            break

        seen_pages.add(cur_page)

        records = parse_weapon_page_records(details, image)
        all_records.extend(records)
        print(f"第{cur_page}/{total}页: {len(records)}条")
        for r in records:
            _print_weapon_record(r)

        if cur_page >= total:
            print(f"已到达最后一页({total})")
            break

    print(f"\n{board_name}汇总: 共 {len(all_records)} 条记录")
    return all_records


def print_summary(result):
    """打印汇总统计"""
    print("\n" + "=" * 70)
    print("异环抽卡记录汇总")
    print("=" * 70)

    for board_type, records in result.items():
        print(f"\n{'='*50}")
        print(f"【{board_type}】共 {len(records)} 条记录")
        print(f"{'='*50}")

        is_weapon = (board_type == "弧盘研募")

        by_rarity = {"S": [], "A": [], "B": [], "?": []}
        for r in records:
            rarity = r.get("rarity", "?")
            if rarity not in by_rarity:
                by_rarity[rarity] = []
            by_rarity[rarity].append(r)

        for rarity in ["S", "A", "B", "?"]:
            items = by_rarity.get(rarity, [])
            if not items:
                continue
            print(f"\n  --- {rarity}级 ({len(items)}条) ---")
            for i, r in enumerate(items, 1):
                name = r.get("name", "?")
                t = r.get("time", "?")
                if is_weapon:
                    research_type = r.get("research_type", "")
                    print(f"  {i:3d}. {name} ({research_type})  {t}")
                else:
                    dice = r.get("dice", -1)
                    qty = r.get("quantity", 1)
                    if dice == 0:
                        dice_str = "[赠礼]"
                    elif dice == -2:
                        dice_str = "[沉眠地]"
                    elif 1 <= dice <= 6:
                        dice_str = f"[{dice}点]"
                    else:
                        dice_str = "[???]"
                    print(f"  {i:3d}. {dice_str:6s} {name} x{qty}  {t}")

        s_count = len(by_rarity.get("S", []))
        a_count = len(by_rarity.get("A", []))
        b_count = len(by_rarity.get("B", []))

        print(f"\n  统计: S级{s_count} | A级{a_count} | B级{b_count}")

        if is_weapon:
            # 武器池：显示研募类型分布 + 额外奖励统计
            research_types = {}
            for r in records:
                rt = r.get("research_type", "")
                research_types[rt] = research_types.get(rt, 0) + 1
            rt_str = " | ".join(f"{k}:{v}" for k, v in research_types.items() if k)
            if rt_str:
                print(f"  研募类型: {rt_str}")
            # 额外奖励：S=40失纬棋子, A=4失纬, B=20迷迭棋子
            shiwei = s_count * 40 + a_count * 4
            midie = b_count * 20
            print(f"  额外奖励: 失纬棋子{shiwei} | 迷迭棋子{midie}")
        else:
            # 角色池：骰子统计
            gift_count = sum(1 for r in records if r.get("dice") == 0)
            dice_count = sum(1 for r in records if isinstance(r.get("dice"), int) and 1 <= r.get("dice") <= 6)
            chenmian_count = sum(1 for r in records if r.get("dice") == -2)
            unknown_dice = sum(1 for r in records if r.get("dice", -1) == -1)
            dice_dist = {}
            for r in records:
                d = r.get("dice", -1)
                if isinstance(d, int) and 1 <= d <= 6:
                    dice_dist[d] = dice_dist.get(d, 0) + 1
            dice_dist_str = " ".join(f"{k}点:{v}" for k, v in sorted(dice_dist.items()))
            parts = [f"掷骰: {dice_count}"]
            if chenmian_count:
                parts.append(f"沉眠地: {chenmian_count}")
            parts.append(f"集点赠礼: {gift_count}")
            parts.append(f"未识别: {unknown_dice}")
            print(f"  {' | '.join(parts)}")
            if dice_dist_str:
                print(f"  点数分布: {dice_dist_str}")

        print(f"  总抽数: {len(records)}")


if __name__ == "__main__":
    # 行缓冲输出：后台运行（RunCommand/终端重定向）时 print 立即可见，不再被块缓冲吞掉
    sys.stdout.reconfigure(line_buffering=True)
    weapon_mode = "--weapon" in sys.argv
    force_mode = "--force" in sys.argv

    print("异环抽卡记录收集器 v5")
    if DEBUG:
        print("  [调试模式] 每步打印截图/OCR 耗时（YIYUAN_DEBUG=1）")
    if weapon_mode:
        print("  [武器池模式] 仅采集弧盘研募（请先在游戏中打开 弧盘研募→研募记录 页面）")
    if force_mode:
        print("  [强制模式] 跳过重复检测，全量重新采集")

    # 检查后端
    resp = requests.get(f"{API}/health")
    health = resp.json()
    admin = health.get("screen", {}).get("admin_privileges", False)
    print(f"后端状态: {health.get('status')}, 管理员权限: {admin}")

    if not admin:
        print("警告: 没有管理员权限，键鼠操作可能失败！")

    # 采集前申请屏幕控制授权（后端重启后授权会丢失；弹窗会等你批准一次）
    if not ensure_authorization():
        print("无法获得屏幕控制授权，退出。请在弹窗中批准后重试。")
        sys.exit(1)

    # 加载已有数据库，用于自动终止检测（--force 跳过）
    db_data = load_existing_database()
    existing_set = build_record_set(db_data) if (db_data and not force_mode) else None
    if existing_set:
        total_existing = sum(len(v) for v in db_data.values())
        print(f"已有数据库: {total_existing} 条记录（{', '.join(f'{k}:{len(v)}' for k, v in db_data.items())}）")
    else:
        print("无已有数据库（或强制模式），全量采集")

    # 显示覆盖层
    requests.post(f"{API}/screen/overlay", json={"action": "show", "message": "Agent正在读取抽卡记录..."})

    result = {}

    if weapon_mode:
        # 武器池模式：仅采集弧盘研募（无棋盘切换/无骰子）
        result["弧盘研募"] = collect_weapon_pool(existing_set=existing_set)
    else:
        # 角色池模式：采集限定棋盘 + 标准棋盘（现有逻辑）
        # 限定棋盘（先确认当前棋盘类型，必要时切换）
        _, init_details = capture_and_ocr()
        current_board = None
        if init_details:
            for item in init_details:
                text = item.get("text", "")
                if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
                    current_board = text
                    break

        if current_board == "限定棋盘":
            result["限定棋盘"] = collect_board("限定棋盘", existing_set=existing_set)
        elif current_board and init_details:
            print(f"当前在{current_board}，切换到限定棋盘...")
            if switch_board_type(init_details, "限定棋盘"):
                time.sleep(2)
                result["限定棋盘"] = collect_board("限定棋盘", existing_set=existing_set)
            else:
                print("切换到限定棋盘失败，跳过")
                result["限定棋盘"] = []
        else:
            print("无法确定当前棋盘类型，尝试直接收集限定棋盘")
            result["限定棋盘"] = collect_board("限定棋盘", existing_set=existing_set)

        # 切换到标准棋盘
        _, details = capture_and_ocr()
        if details and switch_board_type(details, "标准棋盘"):
            time.sleep(2)
            result["标准棋盘"] = collect_board("标准棋盘", existing_set=existing_set)
        else:
            result["标准棋盘"] = []

    # 隐藏覆盖层
    requests.post(f"{API}/screen/overlay", json={"action": "hide"})

    # === 第一步：保存原始数据到 raw/ 目录（保留近3次） ===
    raw_dir = OUTPUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = raw_dir / f"yihuan_gacha_raw_{timestamp}.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n原始数据已保存到 {raw_path}")

    # 清理旧原始数据，只保留最近3次
    cleanup_old_raw(keep=3)

    # === 第二步：保存工作文件（单次覆盖） ===
    output_path = OUTPUT_DIR / "yihuan_gacha_summary.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"工作文件已保存: {output_path}")

    # 打印汇总
    print_summary(result)
