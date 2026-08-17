"""OCR routes - classic PaddleOCR + remote VL (ModelScope Qwen3-VL-235B)."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image

from lib.schema import BaseSchema
from server.vl.remote_vl import remote_vl, vl_status_fields

logger = logging.getLogger("localagent.ocr")
router = APIRouter(prefix="/ocr", tags=["OCR"])

# 长图分块时相邻块的纵向重叠像素，避免文字被切断
_SPLIT_OVERLAP = 200


class ModelManager:
    """Manage OCR models and persistence.

    可观测性字段（P1-A 评估文档要求）：
    - _load_started_at / _load_completed_at：首次加载开始/完成时间戳
    - _load_elapsed_ms：模型加载耗时（首次）
    - _last_inference_ms：最近一次推理耗时
    - _inference_count：累计推理次数
    - _last_error：最近一次加载/推理异常信息
    """

    def __init__(self):
        self._ocr_engine = None
        self._pir_patched = False
        self._env_initialized = False
        self.keep_models = True
        # 从 [models] external_dir 读取统一模型路径；发布时 external_dir 留空则回退到 weights/paddlex
        from server.config import get_models_config
        self.model_cache_dir = get_models_config()["paddleocr_dir"]
        # P1-A 可观测性
        self._load_started_at: float | None = None
        self._load_completed_at: float | None = None
        self._load_elapsed_ms: int | None = None
        self._last_inference_ms: int | None = None
        self._inference_count: int = 0
        self._last_error: str | None = None

    def _reset_load_stats(self) -> None:
        """重置加载统计（卸载模型后调用，使下次调用重新计为 cold_start）"""
        self._load_started_at = None
        self._load_completed_at = None
        self._load_elapsed_ms = None

    def _ensure_env(self):
        """初始化 PaddleOCR 运行环境（makedirs + env），懒加载避免 import 副作用。"""
        if self._env_initialized:
            return
        self._env_initialized = True
        os.makedirs(self.model_cache_dir, exist_ok=True)
        os.environ["PADDLEX_HOME"] = self.model_cache_dir

    def _apply_pir_patch(self):
        """PaddleStaticRunner 默认启用 new_ir (PIR)，但 PIR + oneDNN 有 bug 会导致 OCR 推理失败。

        此 monkey-patch 强制 disable new_ir。原本在 main.py 启动时同步执行，
        但 import paddlex 耗时 4.3s 会阻塞后端启动，故移到首次 OCR 调用时懒加载。
        """
        if self._pir_patched:
            return
        try:
            from paddlex.inference.models.runners.paddle_static.runner import PaddleStaticRunnerConfig
            for field_name, field_info in PaddleStaticRunnerConfig.model_fields.items():
                if field_name == "enable_new_ir":
                    field_info.default = False
                    break
            self._pir_patched = True
            logger.info("PaddleStaticRunner enable_new_ir 已禁用（PIR monkey-patch）")
        except Exception as e:
            logger.warning("PaddleStaticRunner monkey-patch 失败（可能 paddlex 版本不同）: %s", e)
            self._pir_patched = True

    def _detect_paddle_device(self) -> str:
        try:
            import paddle
            if paddle.is_compiled_with_cuda() and paddle.device.is_compiled_with_cuda():
                logger.info("PaddlePaddle GPU available, using GPU inference")
                return "gpu"
        except Exception:
            pass
        logger.info("PaddlePaddle GPU unavailable, using CPU inference")
        return "cpu"

    def get_ocr(self):
        if self._ocr_engine is None:
            self._ensure_env()
            self._apply_pir_patch()
            try:
                from paddleocr import PaddleOCR

                device = self._detect_paddle_device()
                logger.info("Loading classic PaddleOCR (device=%s)...", device)
                # P1-A：记录加载开始时间（仅首次进入加载流程时设置）
                if self._load_started_at is None:
                    self._load_started_at = time.perf_counter()
                t0 = self._load_started_at
                # UVDoc improves photographed/scanned <data_drive>:\Documents, but it geometrically
                # warps ordinary screenshots. PaddleOCR returns boxes in that warped
                # coordinate space, which makes them unusable for screen actions.
                self._ocr_engine = PaddleOCR(
                    use_doc_unwarping=False,
                    use_textline_orientation=True,
                    lang="ch",
                    device=device,
                )
                self._load_completed_at = time.perf_counter()
                self._load_elapsed_ms = int((self._load_completed_at - t0) * 1000)
                logger.info("Classic PaddleOCR loaded in %dms", self._load_elapsed_ms)
                self._last_error = None
            except Exception as e:
                self._last_error = f"PaddleOCR 加载失败: {e}"
                self._load_completed_at = time.perf_counter()
                if self._load_started_at is not None:
                    self._load_elapsed_ms = int((self._load_completed_at - self._load_started_at) * 1000)
                logger.exception("Classic PaddleOCR 加载失败")
                raise
        return self._ocr_engine

    def unload_ocr(self):
        if self._ocr_engine is not None:
            del self._ocr_engine
            self._ocr_engine = None
            # 卸载后重置加载统计，使下次调用重新计为 cold_start
            self._reset_load_stats()
            logger.info("Classic PaddleOCR unloaded")

    @property
    def ocr_loaded(self) -> bool:
        return self._ocr_engine is not None

    @property
    def model_ready(self) -> bool:
        """OCR 模型已加载就绪（可立即推理）"""
        return self._ocr_engine is not None

    @property
    def cold_start(self) -> bool:
        """下次调用会触发首次加载（未加载 + 未在加载中）"""
        return self._ocr_engine is None and self._load_started_at is None

    @property
    def loading(self) -> bool:
        """正在加载中（加载已开始但未完成）"""
        return self._ocr_engine is None and self._load_started_at is not None and self._load_completed_at is None

    @property
    def failed(self) -> bool:
        """最近一次加载失败（无引擎 + 有错误）"""
        return self._ocr_engine is None and self._last_error is not None

    def record_inference(self, elapsed_ms: int) -> None:
        """记录一次推理耗时"""
        self._last_inference_ms = elapsed_ms
        self._inference_count += 1

    def get_vl_client(self):
        from server.config import get_vision_config

        cfg = get_vision_config()
        if not cfg.get("vl_enabled", False):
            raise RuntimeError("远程 VL 未启用 (vl_enabled=false)")
        remote_vl.reload_config()
        if not remote_vl.available:
            raise RuntimeError("远程 VL 不可用（所有 provider 冷却中或未配置）")
        return remote_vl

    def unload_vl(self):
        # 远程 VL 无本地资源需释放，保持 no-op。
        logger.info("远程 VL 无需卸载（远端 API）")

    def unload_all(self):
        self.unload_vl()
        self.unload_ocr()


models = ModelManager()


def _split_long_image(img: Image.Image, max_height: int = 3000) -> list[Image.Image]:
    w, h = img.size
    if h <= max_height:
        return [img]
    pieces = []
    y = 0
    while y < h:
        bottom = min(y + max_height, h)
        pieces.append(img.crop((0, y, w, bottom)))
        y = bottom - _SPLIT_OVERLAP if bottom < h else bottom
    return pieces


def _run_classic_ocr(img: Image.Image) -> list[dict]:
    import numpy as np

    ocr = models.get_ocr()
    img_array = np.array(img.convert("RGB"))
    # P1-A：记录推理耗时（仅含 predict 调用，不含图片预处理）
    t_inf = time.perf_counter()
    results = ocr.predict(img_array)
    models.record_inference(int((time.perf_counter() - t_inf) * 1000))
    items = []
    if results:
        for result in results:
            texts = result.get("rec_texts", [])
            scores = result.get("rec_scores", [])
            polys = result.get("rec_polys", [])
            for i, text in enumerate(texts):
                confidence = float(scores[i]) if i < len(scores) else 0.0
                box = polys[i] if i < len(polys) else []
                items.append(
                    {
                        "text": text,
                        "confidence": round(confidence, 4),
                        "box": [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in box] if box is not None and len(box) > 0 else [],
                    }
                )
    return items


class VLResponse(BaseSchema):
    success: bool
    markdown: str
    blocks: list[dict]
    image_size: list[int]
    elapsed_ms: int
    # P1-A 可观测性
    model_ready: bool = True  # 远程 VL 始终 ready（远端 API）
    cold_start: bool = False
    inference_ms: int | None = None


class OCRResponse(BaseSchema):
    success: bool
    text: str
    details: list[dict]
    image_size: list[int]
    split_count: int = 1
    elapsed_ms: int
    # P1-A 可观测性
    model_ready: bool = False
    cold_start: bool = False
    load_elapsed_ms: int | None = None  # 本次调用触发的加载耗时（cold_start 时非空）
    inference_ms: int | None = None  # 本次推理耗时（单图最近一片）


class ModelControlResponse(BaseSchema):
    success: bool
    message: str


class OCRStatusResponse(BaseSchema):
    ocr_loaded: bool
    keep_models: bool
    vl_model_enabled: bool
    vl_provider: str = ""
    vl_model: str = ""
    vl_available: bool = False
    # sensitive use_case 真实可用性（考虑 privacy_warning 过滤）
    vl_usable_for_sensitive: bool = False
    allow_privacy_warning_for_sensitive: bool = False
    # P1-A 可观测性
    model_ready: bool = False
    cold_start: bool = True
    loading: bool = False
    failed: bool = False
    load_elapsed_ms: int | None = None
    last_inference_ms: int | None = None
    inference_count: int = 0
    last_error: str | None = None


class VLPathRequest(BaseSchema):
    path: str


class VLFileRequest(BaseSchema):
    file: str


class OCRPathRequest(BaseSchema):
    path: str
    max_height: int = 3000
    apply_bbox_fix: bool = True


class OCRFileRequest(BaseSchema):
    file: str
    max_height: int = 3000
    apply_bbox_fix: bool = True


class KeepModelsRequest(BaseSchema):
    keep: bool = True


class UnloadModelsRequest(BaseSchema):
    engine: str | None = None


class PreloadModelsRequest(BaseSchema):
    engine: str = "vl"


@router.get("/status", response_model=OCRStatusResponse, operation_id="ocr_status")
async def ocr_status():
    from server.config import get_vision_config

    cfg = get_vision_config()
    # vl_status_fields() 返回的 providers/provider_exhausted/daily_exhausted 系列
    # 用于 /health，不属于 OCRStatusResponse；forbid 模式下需过滤多余字段
    _vl_fields = {
        k: v for k, v in vl_status_fields().items()
        if k in OCRStatusResponse.model_fields
    }
    return OCRStatusResponse(
        ocr_loaded=models.ocr_loaded,
        keep_models=models.keep_models,
        vl_model_enabled=cfg.get("vl_enabled", False),
        **_vl_fields,
        # P1-A 可观测性
        model_ready=models.model_ready,
        cold_start=models.cold_start,
        loading=models.loading,
        failed=models.failed,
        load_elapsed_ms=models._load_elapsed_ms,
        last_inference_ms=models._last_inference_ms,
        inference_count=models._inference_count,
        last_error=models._last_error,
    )


@router.post("/vl/file", response_model=VLResponse, operation_id="vl_file_form")
async def vl_file(file: UploadFile = File(...)):  # noqa: B008
    contents = await file.read()
    try:
        img = Image.open(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return await asyncio.to_thread(_do_vl, img)


def _do_vl(img: Image.Image) -> VLResponse:
    t0 = time.perf_counter()
    w, h = img.size
    try:
        result = models.get_vl_client().ocr(img)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"远程 VL 识别失败: {e}") from None

    if result.get("status") != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "远程 VL 识别失败"))

    elapsed = int((time.perf_counter() - t0) * 1000)
    markdown_text = result.get("markdown") or result.get("text") or ""
    blocks = result.get("blocks") or ([{"label": "ocr", "content": markdown_text, "bbox": []}] if markdown_text else [])
    # P1-A：远程 VL 始终 ready（远端 API），inference_ms 含网络往返
    return VLResponse(
        success=True, markdown=markdown_text, blocks=blocks, image_size=[w, h], elapsed_ms=elapsed,
        model_ready=True, cold_start=False, inference_ms=elapsed,
    )


@router.post("/file", response_model=OCRResponse, operation_id="ocr_file_form")
async def ocr_file(
    file: UploadFile = File(...),  # noqa: B008
    max_height: int = Form(3000),  # noqa: B008
    apply_bbox_fix: bool = Form(True),  # noqa: B008
):  # noqa: B008
    contents = await file.read()
    try:
        img = Image.open(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return await asyncio.to_thread(_do_ocr, img, max_height, apply_bbox_fix)


@router.post("/base64", response_model=OCRResponse, operation_id="ocr_base64_form")
async def ocr_base64(
    data: str = Form(...),  # noqa: B008
    max_height: int = Form(3000),  # noqa: B008
    apply_bbox_fix: bool = Form(True),  # noqa: B008
):
    try:
        img_bytes = base64.b64decode(data)
        img = Image.open(io.BytesIO(img_bytes))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return await asyncio.to_thread(_do_ocr, img, max_height, apply_bbox_fix)


def _do_ocr(
    img: Image.Image,
    max_height: int = 3000,
    apply_bbox_fix: bool = True,
) -> OCRResponse:
    t0 = time.perf_counter()
    w, h = img.size
    pieces = _split_long_image(img, max_height=max_height)

    # P1-A：在调用 _run_classic_ocr 之前记录 cold_start 状态（_run_classic_ocr 会触发 get_ocr 加载）
    was_cold_start = models.cold_start
    load_ms_before = models._load_elapsed_ms

    from server.config import get_ocr_config

    ocr_cfg = get_ocr_config()
    kx = ocr_cfg["kx"]
    ky = ocr_cfg["ky"]
    bx_ratio = ocr_cfg["bx_ratio"]
    by_ratio = ocr_cfg["by_ratio"]
    need_fix = apply_bbox_fix and (
        kx != 1.0 or ky != 1.0 or bx_ratio != 0.0 or by_ratio != 0.0
    )

    all_details = []
    y_offset = 0.0
    for piece in pieces:
        items = _run_classic_ocr(piece)
        # 仿射修正须按片应用：偏移量与该片尺寸成正比，故在 y_offset 拼接前修正
        if need_fix:
            pw, ph = piece.size
            bx = bx_ratio * pw
            by = by_ratio * ph
            for item in items:
                box = item.get("box", [])
                if box:
                    item["box"] = [
                        [round((pt[0] + bx) / kx, 1), round((pt[1] + by) / ky, 1)]
                        for pt in box
                    ]
        for item in items:
            item["box"] = [[pt[0], round(pt[1] + y_offset, 1)] for pt in item["box"]]
        all_details.extend(items)
        y_offset += piece.size[1] - _SPLIT_OVERLAP

    full_text = "\n".join(item["text"] for item in all_details)
    elapsed = int((time.perf_counter() - t0) * 1000)

    # P1-A：本次调用触发的加载耗时（仅 cold_start 时非空）
    triggered_load_ms = None
    if was_cold_start and models._load_elapsed_ms is not None and models._load_elapsed_ms != load_ms_before:
        triggered_load_ms = models._load_elapsed_ms

    return OCRResponse(
        success=True,
        text=full_text,
        details=all_details,
        image_size=[w, h],
        split_count=len(pieces),
        elapsed_ms=elapsed,
        model_ready=models.model_ready,
        cold_start=was_cold_start,
        load_elapsed_ms=triggered_load_ms,
        inference_ms=models._last_inference_ms,
    )


@router.post("/models/keep/json", response_model=ModelControlResponse, operation_id="ocr_set_keep_models")
async def set_keep_models_json(req: KeepModelsRequest):
    models.keep_models = req.keep
    return ModelControlResponse(success=True, message=f"模型常驻内存已{'开启' if req.keep else '关闭'}")


@router.post("/models/unload/json", response_model=ModelControlResponse, operation_id="ocr_unload_models")
async def unload_models_json(req: UnloadModelsRequest):
    msg = await asyncio.to_thread(_unload_models_sync, req.engine)
    return ModelControlResponse(success=True, message=msg)


def _unload_models_sync(engine: str | None) -> str:
    """同步卸载模型（阻塞调用，供 asyncio.to_thread 包裹）"""
    if engine == "ocr":
        models.unload_ocr()
        return "经典 PaddleOCR 模型已卸载"
    elif engine == "vl":
        models.unload_vl()
        return "远程 VL 无需卸载（远端 API）"
    else:
        models.unload_all()
        return "经典 PaddleOCR 模型已卸载；远程 VL 无需卸载"


@router.post("/models/preload/json", response_model=ModelControlResponse, operation_id="ocr_preload_models")
async def preload_models_json(req: PreloadModelsRequest):
    msg = await asyncio.to_thread(_preload_models_sync, req.engine)
    if msg is None:
        return ModelControlResponse(success=False, message=f"未知引擎: {req.engine}")
    return ModelControlResponse(success=True, message=msg)


def _preload_models_sync(engine: str) -> str | None:
    """同步预加载模型（阻塞调用，供 asyncio.to_thread 包裹）；未知引擎返回 None"""
    if engine == "vl":
        models.get_vl_client()
        return "远程 VL 已就绪"
    elif engine == "ocr":
        models.get_ocr()
        return "经典 PaddleOCR 模型预加载完成"
    return None


@router.post("/vl/file/json", response_model=VLResponse, operation_id="vl_file")
def vl_file_json(req: VLFileRequest):
    p = Path(req.file)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.file}")
    try:
        img = Image.open(p).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return _do_vl(img)


@router.post("/vl/path/json", response_model=VLResponse, operation_id="vl_path")
def vl_path_json(req: VLPathRequest):
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.path}")
    try:
        img = Image.open(p).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return _do_vl(img)


@router.post("/file/json", response_model=OCRResponse, operation_id="ocr_file")
def ocr_file_json(req: OCRFileRequest):
    p = Path(req.file)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.file}")
    try:
        img = Image.open(p)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return _do_ocr(img, req.max_height, req.apply_bbox_fix)


@router.post("/path/json", response_model=OCRResponse, operation_id="ocr_path")
def ocr_path_json(req: OCRPathRequest):
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.path}")
    try:
        img = Image.open(p)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}") from None
    return _do_ocr(img, req.max_height, req.apply_bbox_fix)
