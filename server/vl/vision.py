"""Vision routes - remote VL understanding & element location.

职责：远程 VL 图像理解（understand）与无文字元素坐标定位（locate）。
定位分工见 docs/environment-constraints.md「Computer Use 定位策略」。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time

from fastapi import APIRouter, HTTPException
from PIL import Image

from lib.schema import BaseSchema

from .remote_vl import remote_vl, vl_status_fields

logger = logging.getLogger("localagent.vl.vision")
router = APIRouter(prefix="/vision", tags=["Vision"])


class UnderstandRequest(BaseSchema):
    """远程 VL 图像理解请求。

    图片来源（按优先级，三选一）:
    1. image: base64 编码的图片
    2. window_title + process_name: 自动截取指定窗口
    3. 省略以上字段: 截取全屏
    """
    image: str | None = None
    window_title: str | None = None
    process_name: str | None = None
    question: str
    bbox: list[float] | None = None


class UnderstandResponse(BaseSchema):
    success: bool
    answer: str
    elapsed_ms: int


class LocateRequest(BaseSchema):
    """VL 元素定位请求。

    两种图片来源(按优先级):
    1. image: base64 编码的图片
    2. window_title + process_name: 自动截取指定窗口（无 window_title 时截全屏）
    """
    target: str  # 目标描述,如"下载标签"、"保存按钮"、".!qB 复选框"
    image: str | None = None  # base64 图片(与 window_title 二选一)
    window_title: str | None = None  # 窗口标题(模糊匹配)
    process_name: str | None = None  # 进程名过滤(如 "qbittorrent.exe")


class LocateResponse(BaseSchema):
    success: bool
    found: bool
    x: int | None = None  # 像素坐标(基于原图/原窗口尺寸)
    y: int | None = None
    nx: float | None = None  # 归一化坐标(0-1000)
    ny: float | None = None
    image_size: list[int] | None = None  # [width, height]
    description: str = ""
    elapsed_ms: int


class VisionStatusResponse(BaseSchema):
    vl_model_enabled: bool
    vl_provider: str = ""
    vl_model: str = ""
    vl_available: bool = False
    # sensitive use_case 真实可用性（考虑 privacy_warning 过滤）
    vl_usable_for_sensitive: bool = False
    allow_privacy_warning_for_sensitive: bool = False


def _crop_for_bbox(image: Image.Image, bbox: list[float] | None) -> Image.Image:
    if not bbox or len(bbox) != 4:
        return image
    try:
        x1, y1, x2, y2 = [float(v) for v in bbox]
    except Exception:
        return image
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.0:
        x1 *= image.width
        x2 *= image.width
        y1 *= image.height
        y2 *= image.height
    left = max(0, min(image.width, int(round(min(x1, x2)))))
    right = max(0, min(image.width, int(round(max(x1, x2)))))
    top = max(0, min(image.height, int(round(min(y1, y2)))))
    bottom = max(0, min(image.height, int(round(max(y1, y2)))))
    if right - left < 2 or bottom - top < 2:
        return image
    return image.crop((left, top, right, bottom))


def _acquire_image(
    image_b64: str | None,
    window_title: str | None,
    process_name: str | None,
) -> tuple[Image.Image, tuple[int, int] | None]:
    """统一的图像获取 helper（供 understand/locate 共用）。

    按优先级获取图片：
    1. image_b64 不为空 → 直接 base64 解码
    2. window_title 不为空 → 自动截取指定窗口（带 process_name 过滤）
    3. 以上都为空 → 截取全屏

    Returns:
        (PIL.Image, window_offset) - window_offset 为 (left, top) 或 None
        （窗口截图时为窗口在屏幕上的偏移，全屏时为主显示器原点偏移，
         用于把图像内坐标转换为屏幕坐标；base64 直传时为 None）
    """
    if image_b64:
        try:
            return Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB"), None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"图片解码失败: {e}") from None
    if window_title:
        from server.screen import _capture_window, _find_window
        win = _find_window(window_title, process_name)
        if not win:
            raise HTTPException(status_code=404, detail=f"未找到窗口: {window_title}")
        png_bytes = _capture_window(win["hwnd"])
        if not png_bytes:
            raise HTTPException(status_code=500, detail=f"窗口截图失败: {window_title}")
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        return image, (win["bbox"]["left"], win["bbox"]["top"])
    # 全屏截图
    from server.screen import _capture_fullscreen, _get_fullscreen_offset
    png_bytes = _capture_fullscreen()
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return image, _get_fullscreen_offset()


@router.get("/status", response_model=VisionStatusResponse, operation_id="vision_status")
async def vision_status():
    from server.config import get_vision_config

    cfg = get_vision_config()
    # T-Pydantic forbid：vl_status_fields() 返回 9+ 字段，但 VisionStatusResponse 只声明 6 个，
    # 多出的 providers/provider_exhausted/daily_exhausted 等不属 /vision/status 响应（透传给 /health）。
    # forbid 会报 Extra inputs are not permitted，过滤只保留 schema 声明的字段。
    _vl_fields = {
        k: v for k, v in vl_status_fields().items()
        if k in VisionStatusResponse.model_fields
    }
    return VisionStatusResponse(
        vl_model_enabled=cfg.get("vl_enabled", False),
        **_vl_fields,
    )


@router.get("/providers", operation_id="vl_providers")
async def vl_providers():
    """返回 VL providers 详情（per-key_id 运行时状态）

    暴露 remote_vl.status().providers 列表，供客户端"模型池"面板的 VL 子面板展示。
    每个 provider 含: key_id/label/model/base_url/available/cooldown_remaining/
    active_count/max_concurrency/last_error/privacy_warning。
    """
    from server.vl.remote_vl import remote_vl
    remote_vl.reload_config()
    vstatus = remote_vl.status()
    return {
        "enabled": vstatus.get("enabled", False),
        "available": vstatus.get("available", False),
        "vl_usable_for_sensitive": vstatus.get("vl_usable_for_sensitive", False),
        "allow_privacy_warning_for_sensitive": vstatus.get("allow_privacy_warning_for_sensitive", False),
        "daily_exhausted": vstatus.get("daily_exhausted", False),
        "daily_exhausted_at": vstatus.get("daily_exhausted_at", ""),
        "daily_exhausted_reason": vstatus.get("daily_exhausted_reason", ""),
        "providers": vstatus.get("providers", []),
    }


@router.post("/understand", response_model=UnderstandResponse, operation_id="understand_image")
async def understand_image(req: UnderstandRequest):
    from server.config import get_vision_config

    cfg = get_vision_config()
    if not cfg.get("vl_enabled", False):
        raise HTTPException(status_code=503, detail="远程 VL 未启用 (vl_enabled=false)")
    remote_vl.reload_config()
    if not remote_vl.available:
        raise HTTPException(status_code=503, detail="远程 VL 不可用（所有 provider 冷却中或未配置）")

    # 支持直接传 base64、指定窗口标题自动截图、或全屏截图
    # 3-6: _capture_window 内含 time.sleep×2 + 提窗 + PrintWindow + PNG 编码
    # （最坏 0.5s+），与 VL 调用一样移入线程，避免阻塞事件循环
    image, _ = await asyncio.to_thread(_acquire_image, req.image, req.window_title, req.process_name)

    focused = _crop_for_bbox(image, req.bbox)
    t0 = time.perf_counter()
    # The image sent to VL is cropped when bbox is supplied. Passing the
    # original-image bbox along with that crop describes a different coordinate
    # system and can make VL focus on the wrong region. The crop itself already
    # conveys the focus, so omit the stale coordinates.
    result = await asyncio.to_thread(
        remote_vl.understand, focused, req.question, None, use_case="vl_vision"
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "远程 VL 理解失败"))
    return UnderstandResponse(success=True, answer=result.get("answer", ""), elapsed_ms=int((time.perf_counter() - t0) * 1000))


@router.post("/locate", response_model=LocateResponse, operation_id="vision_locate")
async def vision_locate(req: LocateRequest):
    """远程 VL 元素定位兜底。

    普通文字控件应优先使用浏览器 DOM locator 或本地 screen_ocr bbox；本端点
    面向纯图标、无文字画布和 OCR 无法消歧的目标。
    自动处理 Qwen3-VL 的 0-1000 归一化坐标,返回原图像素坐标。
    支持直接传 base64 图片,或指定窗口标题自动截图。
    """
    from server.config import get_vision_config

    cfg = get_vision_config()
    if not cfg.get("vl_enabled", False):
        raise HTTPException(status_code=503, detail="远程 VL 未启用 (vl_enabled=false)")
    remote_vl.reload_config()
    if not remote_vl.available:
        raise HTTPException(status_code=503, detail="远程 VL 不可用（所有 provider 冷却中或未配置）")

    t0 = time.perf_counter()

    # 统一图像获取（base64 → 窗口截图 → 全屏截图）；3-6: 截图重活移入线程
    image, window_offset = await asyncio.to_thread(
        _acquire_image, req.image, req.window_title, req.process_name
    )

    # 调用 VL locate
    result = await asyncio.to_thread(remote_vl.locate, image, req.target, use_case="vl_vision")
    elapsed = int((time.perf_counter() - t0) * 1000)

    if result.get("status") != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "VL 定位失败"))

    found = result.get("found", False)
    x = result.get("x")
    y = result.get("y")

    # 如果是窗口截图,把窗口内坐标转为屏幕坐标(方便直接用于 execute_action)
    if found and window_offset and x is not None and y is not None:
        x = x + window_offset[0]
        y = y + window_offset[1]

    return LocateResponse(
        success=True,
        found=found,
        x=x,
        y=y,
        nx=result.get("nx"),
        ny=result.get("ny"),
        image_size=result.get("image_size"),
        description=result.get("description", ""),
        elapsed_ms=elapsed,
    )


def on_shutdown() -> None:
    # 远程 VL 无本地资源需释放（旧 qwen_vl_bridge.stop() 已废弃）
    pass
