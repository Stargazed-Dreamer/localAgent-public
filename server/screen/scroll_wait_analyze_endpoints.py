"""scroll / wait / analyze 端点 — 滚动截图 + 条件等待 + 图像特征分析。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router / 常量仍由 routes 提供，本模块定义：
- ScrollCaptureRequest / ScrollCaptureResponse 模型 + scroll_capture（/scroll-capture）
- WaitForRequest / WaitForResponse 模型 + screen_wait_for（/wait-for）
- AnalyzeRequest 模型 + screen_analyze（/analyze）

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import base64
import io
import time

from fastapi import HTTPException

from lib.schema import BaseSchema
from server.model_manager.types import ModelUnavailableError

# 截图子模块
from server.screen.capture import (
    _capture_fullscreen,
    _capture_window,
    _color_name,
    _find_overlap_and_stitch,
    _images_equal,
)

# 从主模块复用共享件（router / 日志 / 常量）
from server.screen.routes import (
    _ACTION_SETTLE_DELAY,
    _FOCUS_RETRY_DELAY,
    _GO_TOP_SCROLL_AMOUNT,
    _GO_TOP_SCROLL_TIMES,
    _OVERLAY_HIDE_DELAY,
    _SCROLL_CAPTURE_MAX_HEIGHT,
    _enforce_session_permission,
    _touch_task_authorization_after_delivery,
    logger,
    router,
)

# 安全子模块
from server.screen.security import (
    emergency,
)

# 窗口子模块
from server.screen.windows import (
    _ADMIN_STATUS,
    _find_window,
    _force_focus_window,
)

# ========== 滚动截图（长界面处理） ==========

class ScrollCaptureRequest(BaseSchema):
    """滚动截图请求 — 滚动窗口并截取多张图,可选拼接为长图。

    适用于内容超出可见区域的滚动界面(如长设置对话框、长网页等)。
    """
    window_title: str | None = None
    process_name: str | None = None
    hwnd: int | None = None
    scroll_method: str = "keyboard"  # keyboard | mouse
    scroll_key: str = "pagedown"  # keyboard 模式: pagedown | arrow_down | space
    scroll_amount: int = 5  # mouse 模式: 滚轮量
    scroll_count: int = 5  # 最大滚动次数
    scroll_interval: float = 0.6  # 每次滚动后等待(秒)
    scroll_x: int | None = None  # mouse 模式滚动位置(默认窗口中心)
    scroll_y: int | None = None
    go_top_first: bool = True  # 开始前先按 Home 回到顶部
    stitch: bool = True  # 是否拼接为长图
    return_inline: bool = True  # 返回 inline image(MCP ImageContent)
    detect_end: bool = True  # 检测到底部自动停止(相邻截图相同)

class ScrollCaptureResponse(BaseSchema):
    success: bool
    segments_count: int
    stitched_image_inline: dict | None = None
    stitched_width: int | None = None
    stitched_height: int | None = None
    segment_heights: list[int] = []
    reached_bottom: bool = False
    message: str
    elapsed_ms: int


@router.post("/scroll-capture", response_model=ScrollCaptureResponse, operation_id="scroll_capture")
def scroll_capture(req: ScrollCaptureRequest):
    """滚动窗口并截取多张图,可选拼接为长图。

    适用于内容超出可见区域的长界面(如 qBittorrent 设置对话框)。
    支持 keyboard(pagedown/arrow_down) 和 mouse(滚轮) 两种滚动方式。
    自动检测到底部(相邻截图相同则停止)。

    职责边界：本端点只负责滚动+截图+拼接。前期准备（激活窗口、点击内容区聚焦、
    切到目标标签页等）由调用方用 focus_window/execute_action 等工具提前完成。
    keyboard 模式尤其依赖焦点已在可滚动区域——若焦点在左侧导航栏，PageDown/Home
    会被导航栏拦截导致标签页乱切。mouse 模式不依赖焦点，滚轮作用于鼠标下方控件。
    """
    # T15：副作用端点 403 拦截（滚动窗口属副作用操作）
    _enforce_session_permission()

    import pyautogui
    from PIL import Image

    if not _ADMIN_STATUS:
        return ScrollCaptureResponse(
            success=False, segments_count=0, message="需要管理员权限", elapsed_ms=0
        )
    if not emergency.can_operate():
        return ScrollCaptureResponse(
            success=False, segments_count=0, message="紧急停止已触发", elapsed_ms=0
        )

    t0 = time.perf_counter()

    # 解析窗口
    target_hwnd = req.hwnd
    if target_hwnd is None and req.window_title:
        win = _find_window(req.window_title, req.process_name)
        if not win:
            raise HTTPException(status_code=404, detail=f"未找到窗口: {req.window_title}")
        target_hwnd = win["hwnd"]
    elif target_hwnd is None:
        raise HTTPException(status_code=400, detail="必须提供 window_title 或 hwnd")

    # 激活窗口
    try:
        _force_focus_window(target_hwnd)
        time.sleep(_ACTION_SETTLE_DELAY)
    except Exception as e:
        logger.warning(f"激活窗口失败: {e}")

    # 计算鼠标滚动位置（默认窗口右侧 2/3 处中心，避开左侧导航栏）
    # mouse 模式在此位置滚动；keyboard 模式不使用此坐标（需调用方提前把焦点放到内容区）
    try:
        import win32gui
        left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
    except Exception:
        left, top, right, bottom = 0, 0, 800, 600
    sx = req.scroll_x if req.scroll_x is not None else left + (right - left) * 2 // 3
    sy = req.scroll_y if req.scroll_y is not None else top + (bottom - top) // 2

    # 回到顶部
    # keyboard 模式依赖焦点在可滚动区域（调用方需提前点击内容区聚焦，否则 Home/PageDown 可能被导航栏拦截）
    # mouse 模式不依赖焦点，滚轮作用于鼠标下方的控件
    if req.go_top_first:
        try:
            if req.scroll_method == "keyboard":
                pyautogui.hotkey("home")
            else:
                pyautogui.moveTo(sx, sy)
                time.sleep(_FOCUS_RETRY_DELAY)
                for _ in range(_GO_TOP_SCROLL_TIMES):
                    pyautogui.scroll(_GO_TOP_SCROLL_AMOUNT)
                    time.sleep(_OVERLAY_HIDE_DELAY)
            time.sleep(0.5)  # 回到顶部后等待界面稳定
        except Exception:
            pass

    segments = []  # PNG bytes 列表
    reached_bottom = False

    for i in range(req.scroll_count + 1):  # +1 因为第一次不滚动(当前视图)
        # 截图
        try:
            png_bytes = _capture_window(target_hwnd)
            if not png_bytes:
                png_bytes = _capture_fullscreen()
        except Exception as e:
            logger.warning(f"截图失败(第{i}次): {e}")
            continue

        # 检测是否到底部(与上次截图相同)
        if req.detect_end and segments:
            if _images_equal(segments[-1], png_bytes):
                reached_bottom = True
                logger.info(f"滚动截图: 检测到底部(第{i}次与上次相同),停止")
                break

        segments.append(png_bytes)

        # 滚动
        if i < req.scroll_count:
            try:
                if req.scroll_method == "keyboard":
                    pyautogui.hotkey(req.scroll_key)
                else:
                    # mouse 模式：向下滚（负数=向下）
                    pyautogui.moveTo(sx, sy)
                    pyautogui.scroll(-req.scroll_amount)
            except Exception as e:
                logger.warning(f"滚动失败: {e}")
            time.sleep(req.scroll_interval)

    if not segments:
        return ScrollCaptureResponse(
            success=False, segments_count=0, message="未获取到任何截图", elapsed_ms=0
        )

    # 拼接
    stitched_inline = None
    stitched_width = None
    stitched_height = None
    segment_heights = []

    images = [Image.open(io.BytesIO(png)) for png in segments]
    segment_heights = [img.height for img in images]

    if req.stitch and len(images) > 0:
        try:
            stitched = _find_overlap_and_stitch(images)
            stitched_width = stitched.width
            stitched_height = stitched.height

            # 限制长图最大高度(避免太大)
            max_h = _SCROLL_CAPTURE_MAX_HEIGHT
            if stitched.height > max_h:
                ratio = max_h / stitched.height
                stitched = stitched.resize(
                    (int(stitched.width * ratio), max_h),
                    Image.Resampling.LANCZOS
                )
                stitched_width = stitched.width
                stitched_height = stitched.height

            # 转 JPEG inline
            buf = io.BytesIO()
            stitched.save(buf, format="JPEG", quality=85)
            stitched_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            if req.return_inline:
                stitched_inline = {
                    "mcp_image_block": True,
                    "image": stitched_b64,
                    "mime_type": "image/jpeg",
                    "width": stitched_width,
                    "height": stitched_height,
                    "note": f"滚动截图长图(拼接 {len(segments)} 段,原始 {sum(segment_heights)}px → 拼接 {stitched_height}px)",
                }
        except Exception as e:
            logger.warning(f"拼接失败: {e}", exc_info=True)

    elapsed = int((time.perf_counter() - t0) * 1000)
    msg = f"截取 {len(segments)} 段"
    if stitched_inline:
        msg += f",拼接为 {stitched_width}x{stitched_height}"
    if reached_bottom:
        msg += ",已到底部"

    # T15：副作用成功后 extend idle 计时（滚动属副作用操作）
    _touch_task_authorization_after_delivery(success=True)
    return ScrollCaptureResponse(
        success=True,
        segments_count=len(segments),
        stitched_image_inline=stitched_inline,
        stitched_width=stitched_width,
        stitched_height=stitched_height,
        segment_heights=segment_heights,
        reached_bottom=reached_bottom,
        message=msg,
        elapsed_ms=elapsed,
    )


# ========== 条件等待 ==========

class WaitForRequest(BaseSchema):
    """条件等待请求。

    expected 为 dict 格式，与其他端点（execute_action/desktop_transaction/batch_actions）统一：
        {"type": "ocr_contains" | "ocr_not_contains" | "vl_contains", "text": "期望文本"}

    旧版 expected: str + condition_type 字段已废弃（breaking change）。
    """
    expected: dict  # {"type": "ocr_contains"|..., "text": "..."}；type 可省略默认 ocr_contains
    timeout: float = 30.0  # 最大等待秒数
    interval: float = 1.0  # 检查间隔
    mode: str = "fullscreen"  # fullscreen | window
    window_title: str | None = None  # mode=window 时使用
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突）
    hwnd: int | None = None  # 直接指定窗口句柄（与其他端点统一，优先于 window_title）
    region: str | None = None  # "left,top,right,bottom" 区域裁剪
    case_sensitive: bool = False
    max_size: int = 1280  # 给 VL 用的图片最长边


class WaitForResponse(BaseSchema):
    success: bool
    condition_met: bool
    elapsed_ms: int
    check_count: int = 0
    last_ocr_text: str = ""
    matched_at: float | None = None  # 匹配成功的相对时间秒
    error_code: str | None = None  # 快速失败原因（如 "ocr_unavailable"）；正常结束为 None
    message: str = ""


@router.post("/wait-for", response_model=WaitForResponse, operation_id="screen_wait_for")
def screen_wait_for(req: WaitForRequest):
    """等待画面条件满足。

    循环截图 + OCR/VL 检查，直到条件满足或超时。
    返回纯文本，无 base64，不污染上下文。

    expected 格式（dict，与其他端点统一）：
        {"type": "ocr_contains", "text": "加载完成"}  # OCR 文本中包含 text（默认）
        {"type": "ocr_not_contains", "text": "正在加载"}  # OCR 文本中不包含 text
        {"type": "vl_contains", "text": "登录成功"}  # 远程 VL 识别结果中包含 text
    """
    # 解析 expected dict
    if not isinstance(req.expected, dict):
        return WaitForResponse(
            success=False, condition_met=False, elapsed_ms=0,
            message="expected 必须为 dict 格式，如 {\"type\": \"ocr_contains\", \"text\": \"加载完成\"}"
        )
    exp_type = req.expected.get("type", "ocr_contains")
    exp_text_raw = req.expected.get("text", "")
    if not exp_text_raw or not str(exp_text_raw).strip():
        return WaitForResponse(
            success=False, condition_met=False, elapsed_ms=0,
            message="expected.text 不能为空。请提供要等待出现的文本，如 '设置' 或 '加载完成'"
        )
    if exp_type not in ("ocr_contains", "ocr_not_contains", "vl_contains"):
        return WaitForResponse(
            success=False, condition_met=False, elapsed_ms=0,
            message=f"expected.type='{exp_type}' 不支持，可选值: ocr_contains | ocr_not_contains | vl_contains"
        )
    if req.mode not in ("fullscreen", "window"):
        return WaitForResponse(
            success=False, condition_met=False, elapsed_ms=0,
            message=f"mode='{req.mode}' 不支持，可选值: fullscreen | window"
        )
    if req.mode == "window" and not req.window_title and not req.hwnd:
        return WaitForResponse(
            success=False, condition_met=False, elapsed_ms=0,
            message="mode=window 必须提供 hwnd 或 window_title。推荐：用 list_windows 获取窗口信息，hwnd 优先（避免标题歧义）"
        )

    t0 = time.perf_counter()
    deadline = t0 + req.timeout
    check_count = 0
    last_ocr_text = ""

    expected = exp_text_raw if req.case_sensitive else str(exp_text_raw).lower()

    # 解析区域（格式错误必须报 400，否则静默用全图导致检查区域错误）
    region = None
    if req.region:
        parts_raw = req.region.split(",")
        if len(parts_raw) != 4:
            raise HTTPException(status_code=400, detail=f"region 格式错误：需 'left,top,right,bottom'（4 个整数），当前 {len(parts_raw)} 段")
        try:
            region = [int(p.strip()) for p in parts_raw]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"region 含非整数: {e}（格式: 'left,top,right,bottom'）") from None

    while time.perf_counter() < deadline:
        if not emergency.can_operate():
            return WaitForResponse(
                success=False, condition_met=False,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                check_count=check_count, last_ocr_text=last_ocr_text,
                message="紧急停止已触发"
            )

        check_count += 1

        # 截图
        try:
            overlay_was_visible = False
            try:
                from server.overlay_client import overlay_client
                overlay_was_visible = overlay_client.overlay_visible
                if overlay_was_visible:
                    overlay_client.hide_overlay()
                    time.sleep(_OVERLAY_HIDE_DELAY)
            except Exception:
                pass

            try:
                if req.mode == "window" and (req.hwnd or req.window_title):
                    # 第二轮评估 P1-3：hwnd 优先于 window_title（与其他端点统一）
                    target_hwnd_wait = req.hwnd
                    if target_hwnd_wait is None and req.window_title:
                        w = _find_window(req.window_title, req.process_name)
                        if not w:
                            raise RuntimeError(f"未找到窗口: {req.window_title}")
                        target_hwnd_wait = w["hwnd"]
                    if target_hwnd_wait is not None:
                        png_bytes = _capture_window(target_hwnd_wait)
                    else:
                        png_bytes = _capture_fullscreen()
                else:
                    png_bytes = _capture_fullscreen()

                if not png_bytes:
                    raise RuntimeError("截图失败")

                from PIL import Image
                img = Image.open(io.BytesIO(png_bytes))
                if region:
                    img = img.crop((region[0], region[1], region[2], region[3]))
            finally:
                if overlay_was_visible:
                    try:
                        from server.overlay_client import overlay_client
                        overlay_client.show_overlay()
                    except Exception:
                        pass

            # 检查条件
            matched = False
            if exp_type in ("ocr_contains", "ocr_not_contains"):
                from server.ocr import _do_ocr
                ocr_resp = _do_ocr(img)
                last_ocr_text = ocr_resp.text
                text = ocr_resp.text if req.case_sensitive else ocr_resp.text.lower()
                matched = expected in text if exp_type == "ocr_contains" else expected not in text
            elif exp_type == "vl_contains":
                from server.ocr import _do_vl
                vl_resp = _do_vl(img)
                last_ocr_text = vl_resp.markdown
                text = vl_resp.markdown if req.case_sensitive else vl_resp.markdown.lower()
                matched = expected in text
            else:
                return WaitForResponse(
                    success=False, condition_met=False,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    check_count=check_count, last_ocr_text=last_ocr_text,
                    message=f"未知 expected.type: {exp_type}"
                )

            if matched:
                return WaitForResponse(
                    success=True, condition_met=True,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    check_count=check_count, last_ocr_text=last_ocr_text,
                    matched_at=round(time.perf_counter() - t0, 2),
                    message=f"条件满足: {exp_type} '{exp_text_raw}'"
                )
        except ModelUnavailableError as e:
            # 模型被存活管理器拒绝（压力/卸载/冷却）：重试无意义，快速失败
            # 而非烧完整个 timeout（design §6 消费者矩阵）
            logger.warning(f"wait_for OCR 不可用，快速失败: {e.reason}")
            return WaitForResponse(
                success=False, condition_met=False,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                check_count=check_count, last_ocr_text=last_ocr_text,
                error_code="ocr_unavailable",
                message=f"OCR 模型不可用（{e.reason}），无法检查条件；压力回落后重试"
            )
        except Exception as e:
            logger.warning(f"wait_for 第 {check_count} 次检查失败: {e}")
            last_ocr_text = f"[ERROR] {e}"

        # 等待下次检查
        time.sleep(req.interval)

    return WaitForResponse(
        success=True, condition_met=False,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        check_count=check_count, last_ocr_text=last_ocr_text,
        message=f"超时 {req.timeout}s 未满足条件"
    )


# ========== 图像特征分析 ==========

class AnalyzeRequest(BaseSchema):
    mode: str = "fullscreen"  # fullscreen | window
    window_title: str | None = None  # mode=window 时使用
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突）
    hwnd: int | None = None  # 第二轮评估 P1-3：直接指定窗口句柄（与其他端点统一，优先于 window_title）
    region: str | None = None  # "left,top,right,bottom"
    grid_size: int = 4  # 网格分区大小（NxN）
    sample_step: int = 4  # 采样步长（越大越快但越粗糙）


@router.post("/analyze", operation_id="screen_analyze")
def screen_analyze(req: AnalyzeRequest):
    """图像特征分析：返回纯文本的颜色/亮度/异常告警，无 base64。

    用于远程 VL 不可用时的画面快速判断（蓝屏/黑屏/卡加载等）。
    比 capture_screen(format=info) 更有信息量，又不会像 base64 那样污染上下文。
    """
    if req.mode not in ("fullscreen", "window"):
        return {"success": False, "message": f"mode='{req.mode}' 不支持，可选值: fullscreen | window"}
    if req.mode == "window" and not req.window_title and not req.hwnd:
        return {"success": False, "message": "mode=window 必须提供 hwnd 或 window_title。推荐：用 list_windows 获取窗口信息，hwnd 优先（避免标题歧义）"}

    import numpy as np
    from PIL import Image

    t0 = time.perf_counter()

    # 截图
    try:
        overlay_was_visible = False
        try:
            from server.overlay_client import overlay_client
            overlay_was_visible = overlay_client.overlay_visible
            if overlay_was_visible:
                overlay_client.hide_overlay()
                time.sleep(_OVERLAY_HIDE_DELAY)
        except Exception:
            pass

        try:
            if req.mode == "window" and (req.hwnd or req.window_title):
                # 第二轮评估 P1-3：hwnd 优先于 window_title（与其他端点统一）
                target_hwnd_analyze = req.hwnd
                if target_hwnd_analyze is None and req.window_title:
                    w = _find_window(req.window_title, req.process_name)
                    if not w:
                        raise RuntimeError(f"未找到窗口: {req.window_title}")
                    target_hwnd_analyze = w["hwnd"]
                if target_hwnd_analyze is not None:
                    png_bytes = _capture_window(target_hwnd_analyze)
                else:
                    png_bytes = _capture_fullscreen()
            else:
                png_bytes = _capture_fullscreen()

            if not png_bytes:
                raise RuntimeError("截图失败")

            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            if req.region:
                parts_raw = req.region.split(",")
                if len(parts_raw) != 4:
                    raise HTTPException(status_code=400, detail=f"region 格式错误：需 'left,top,right,bottom'（4 个整数），当前 {len(parts_raw)} 段")
                try:
                    parts = [int(p.strip()) for p in parts_raw]
                    img = img.crop((parts[0], parts[1], parts[2], parts[3]))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"region 含非整数: {e}（格式: 'left,top,right,bottom'）") from None
        finally:
            if overlay_was_visible:
                try:
                    from server.overlay_client import overlay_client
                    overlay_client.show_overlay()
                except Exception:
                    pass
    except Exception as e:
        return {"success": False, "message": f"截图失败: {e}"}

    w, h = img.size
    arr = np.asarray(img)

    # 缩小采样
    step = max(1, req.sample_step)
    sampled = arr[::step, ::step].reshape(-1, 3)

    # 主色统计
    # 量化到 16 级
    quantized = (sampled // 16) * 16
    unique, counts = np.unique(quantized, axis=0, return_counts=True)
    sorted_idx = np.argsort(-counts)
    top_colors = []
    total = len(sampled)
    for i in sorted_idx[:5]:
        r, g, b = unique[i]
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        name = _color_name(int(r), int(g), int(b))
        top_colors.append({
            "hex": hex_color,
            "name": name,
            "ratio": round(float(counts[i]) / total, 3),
        })

    # 亮度统计
    gray = np.dot(sampled, [0.299, 0.587, 0.114])
    brightness = {
        "min": int(gray.min()),
        "max": int(gray.max()),
        "mean": round(float(gray.mean()), 1),
        "std": round(float(gray.std()), 1),
    }

    # 颜色多样性
    unique_full = np.unique(arr.reshape(-1, 3), axis=0)
    diversity = {
        "unique_colors": int(len(unique_full)),
        "diversity_ratio": round(float(len(unique_full)) / (w * h), 4),
    }

    # 边缘密度（粗略梯度估计）
    small = arr[::max(1, step * 2), ::max(1, step * 2)]
    gray_small = np.dot(small, [0.299, 0.587, 0.114])
    if gray_small.shape[0] > 1 and gray_small.shape[1] > 1:
        dx = np.abs(np.diff(gray_small, axis=1))
        dy = np.abs(np.diff(gray_small, axis=0))
        edge_density = round(float((dx.mean() + dy.mean()) / 2), 2)
    else:
        edge_density = 0.0

    # 网格分区主色
    grid = []
    gs = max(1, req.grid_size)
    cell_w = w // gs
    cell_h = h // gs
    if cell_w > 0 and cell_h > 0:
        for gy in range(gs):
            row = []
            for gx in range(gs):
                cell = arr[gy * cell_h:(gy + 1) * cell_h, gx * cell_w:(gx + 1) * cell_w]
                cell_sampled = cell.reshape(-1, 3)[::max(1, step * 2)]
                if len(cell_sampled) > 0:
                    avg = cell_sampled.mean(axis=0).astype(int)
                    hex_c = f"#{avg[0]:02x}{avg[1]:02x}{avg[2]:02x}"
                    row.append({"hex": hex_c, "name": _color_name(*avg.tolist())})
                else:
                    row.append({"hex": "#000000", "name": "黑"})
            grid.append(row)

    # 异常告警
    alerts = []
    top_ratio = top_colors[0]["ratio"] if top_colors else 0
    top_name = top_colors[0]["name"] if top_colors else ""
    mean_b = brightness["mean"]

    # A normal white web page can be both bright and dominated by one color.
    # Require a genuinely blank image before reporting black/white screen;
    # text and UI edges make edge_density/diversity materially larger.
    blank_like = edge_density < 2.0 and diversity["diversity_ratio"] < 0.0002
    if mean_b < 10 and top_ratio > 0.9 and blank_like:
        alerts.append("⚠️ 黑屏（亮度极低且单色统治）")
    elif mean_b > 245 and top_ratio > 0.9 and blank_like:
        alerts.append("⚠️ 白屏（亮度极高且单色统治）")
    elif top_ratio > 0.95:
        if "蓝" in top_name:
            alerts.append("⚠️ 蓝屏（蓝色统治）")
        else:
            alerts.append(f"⚠️ 单色统治：{top_name} 占比 {top_ratio:.1%}")
    if mean_b > 30 and mean_b < 60 and top_ratio > 0.7:
        alerts.append("⚠️ 可能卡加载（暗色统治但非纯黑）")

    # 警告色检测（黄/红/橙）
    warn_count = 0
    for c in sampled:
        r, g, b = int(c[0]), int(c[1]), int(c[2])
        if (r > 200 and g > 150 and b < 100) or (r > 180 and g < 80 and b < 80):  # 黄/橙
            warn_count += 1
    warn_ratio = warn_count / len(sampled)
    if warn_ratio > 0.1:
        alerts.append(f"⚠️ 警告色较多（黄/红/橙占比 {warn_ratio:.1%}）")

    # 一句话总结
    if alerts:
        summary = "；".join(alerts)
    else:
        summary = f"画面正常：主色 {top_name}（{top_ratio:.0%}），亮度均值 {mean_b}，边缘密度 {edge_density}"

    return {
        "success": True,
        "image_size": [w, h],
        "top_colors": top_colors,
        "brightness": brightness,
        "diversity": diversity,
        "edge_density": edge_density,
        "grid": grid,
        "alerts": alerts,
        "summary": summary,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
