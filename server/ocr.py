"""OCR routes - classic PaddleOCR + remote VL (ModelScope Qwen3-VL-235B)."""

from __future__ import annotations

import asyncio
import base64
import gc
import io
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image

from lib.schema import BaseSchema
from server.vl.remote_vl import remote_vl, vl_status_fields

logger = logging.getLogger("localagent.ocr")
router = APIRouter(prefix="/ocr", tags=["OCR"])

# 长图分块时相邻块的纵向重叠像素，避免文字被切断
_SPLIT_OVERLAP = 200


def _get_models_cfg() -> dict:
    from server.config import get_models_config
    return get_models_config()


def _fmt_ratio(ratio: float | None) -> str:
    return "unknown" if ratio is None else f"{ratio:.2f}"


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
        # keep_models 语义所有权已迁移到 ModelLifecycleManager（design §9 写回链）。
        # 此属性改为 property 回读管理器生效值（默认 True，与旧行为一致）。
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
        # 模型存活管理器接入（T2，design §8 继承 ocr-vram-guard §5.4/§5.5）
        self._load_lock = threading.Lock()          # 串行化冷启动加载 + 与卸载互斥
        self._unloading = False                     # 卸载意图标志：置位后新获取一律拒绝
        self._in_flight = 0                         # 进行中的推理数（卸载前排空用）
        self._in_flight_lock = threading.Lock()
        self._loaded_at: float | None = None        # wall-clock 加载完成时间戳（驱动协议 loaded_at）
        # GPU→CPU 条件降级（2026-08-31）：显存被占满时压力拒绝态下降级到 CPU 继续服务
        # _active_device 是"最近一次决策的设备"，OcrDriver.resource 动态读它，
        # 供 admission/逐出按实际归属分流。每次加载前由 _decide_device() 决策，
        # 卸载后复位为 config 期望设备。迟滞状态（_fallback_active/_fallback_hold_until）
        # 跨卸载保留——防止 GPU 水位在阈值附近时反复横跳（卸载/重载各 1~2s 代价）。
        self._active_device: Literal["gpu", "cpu"] = self._configured_device()
        self._fallback_active: bool = False
        self._fallback_hold_until: float = 0.0

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
        # paddlex >=3.7 的 paddlex.utils.cache.CACHE_DIR 读 PADDLE_PDX_CACHE_HOME，
        # 且是模块 import 时固化的模块级常量，必须在 from paddleocr import 之前设置。
        # 只设 PADDLEX_HOME（旧版变量）会静默回落到 ~/.paddlex，模型下到用户目录
        # （docs/deployment.md 记录为"待决策"，本机已实测此修复生效）。
        os.environ["PADDLE_PDX_CACHE_HOME"] = self.model_cache_dir

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
        """推理设备选择：config.toml [models] paddle_device 驱动（当前默认 gpu）。

        [已过时 · 2026-08-31 推翻，勿再据此禁用 GPU]
        2026-08-26 曾记录：paddlepaddle-gpu 3.2.2 (cu126) 在 RTX 4070 Laptop +
        NVIDIA 驱动（Driver API 13.3）下，首次 GPU 推理单核满转 + GPU 100% 空转
        永不返回（称大图小图均挂死），据此把默认值降为 cpu，并推断需升级
        paddlepaddle 或回退驱动才能恢复 GPU。

        该结论已于 2026-08-31 实机复测推翻：同样的 paddle 3.2.2、同样的驱动
        （Driver API 13.3）、同样的 RTX 4070 Laptop，三档分辨率各 3 轮共 9 次
        GPU 预测全部正常返回，零挂死。PP-OCRv6_medium 实测：
            GPU  320x96 0.05s / 1920x1080 0.51s / 2560x1440 0.76s
            CPU  320x96 0.23s / 1920x1080 3.81s / 2560x1440 5.40s
        加速比 4.6x~7.5x，故默认值已改回 gpu。

        挂死成因未复现、无法归因，不排除间歇性可能。若再次出现挂死，把
        config.toml 的 paddle_device 改回 "cpu" 即可立即回退（CPU 路径稳定）。
        """
        from server.config import get_models_config

        device = str(get_models_config().get("paddle_device", "gpu")).strip().lower()
        if device in ("cpu", "gpu"):
            logger.info("PaddleOCR 推理设备: %s（config [models] paddle_device）", device)
            return device
        # auto：保留旧版自动检测行为
        try:
            import paddle
            _paddle_dynamic: Any = paddle  # 桩未导出私有函数，走 Any 规避
            if _paddle_dynamic.is_compiled_with_cuda() and paddle.device.is_compiled_with_cuda():
                logger.info("PaddlePaddle GPU available, using GPU inference")
                return "gpu"
        except Exception:
            pass
        logger.info("PaddlePaddle GPU unavailable, using CPU inference")
        return "cpu"

    def _configured_device(self) -> Literal["gpu", "cpu"]:
        """config 期望设备（auto 归并为 gpu 意图）；供初始化 / 卸载复位用"""
        from server.config import get_models_config

        d = str(get_models_config().get("paddle_device", "gpu")).strip().lower()
        return "cpu" if d == "cpu" else "gpu"

    def _can_fallback_to_cpu(self) -> bool:
        """降级现场判定：主机内存够 且 CPU 压力不高（psutil 现场采样，降级是低频事件）。

        阈值实测依据（2026-08-31 游戏满载实验）：可用内存最低 0.46 GB（必然 swap/OOM，
        不可降级）、常态 14.5 GB（充裕）；CPU 中位 23%、峰值 47.7%（通常达标）。
        """
        from server.config import get_models_config

        cfg = get_models_config()
        min_avail_gb = float(cfg.get("gpu_fallback_min_avail_gb", 2.0))
        max_cpu_pct = float(cfg.get("gpu_fallback_max_cpu_pct", 70.0))
        import psutil

        vm = psutil.virtual_memory()
        avail_gb = vm.available / 1024**3
        cpu_pct = psutil.cpu_percent(interval=0.5)
        ok = avail_gb >= min_avail_gb and cpu_pct <= max_cpu_pct
        logger.info(
            "OCR 降级条件评估: avail=%.2fGB(>=%.2f) cpu=%.1f%%(<=%.1f%%) → %s",
            avail_gb, min_avail_gb, cpu_pct, max_cpu_pct, ok,
        )
        return ok

    def _decide_device(self, manager: Any) -> str:
        """加载前设备决策 = _detect_paddle_device()（config/auto 解析）+ 压力降级/迟滞叠加。

        必须在 admit() 之前调用：决策写入 _active_device，OcrDriver.resource 动态
        读它——降级后 admission 走 cpu monitor（默认 disabled → 放行），GPU 逐出器
        也不再选中 OCR。GPU 压力信号直接读 gpu monitor 的 REFUSING 态
        （不能走 check_admission：其按 driver.resource 选 monitor，未加载时
        resource 为 "cpu" 会恒放行，pressure_refusing 根本到不了降级判断）。

        只对 GPU REFUSING 降级；cooldown/reload_degraded/awaiting_first_sample
        是模型自身状态，换设备解决不了，交由 admit() 原样拒绝。
        """
        device = self._detect_paddle_device()
        if device != "gpu":
            self._active_device = "cpu"
            return device  # config 指定 cpu，或 auto 检测无 GPU
        from server.model_manager.types import PressureState

        monitors = getattr(manager, "monitors", None)
        gpu_mon = monitors.get("gpu") if monitors else None
        if not getattr(manager, "enabled", True) or gpu_mon is None or not gpu_mon.cfg.enabled:
            self._active_device = "gpu"
            return "gpu"
        now = time.time()
        if gpu_mon.state is PressureState.REFUSING:
            if self._can_fallback_to_cpu():
                self._active_device = "cpu"
                self._fallback_active = True
                hold_sec = float(_get_models_cfg().get("gpu_fallback_hold_sec", 300.0))
                self._fallback_hold_until = now + hold_sec
                logger.warning(
                    "GPU 压力拒绝态（ratio=%s），OCR 降级到 CPU（内存/CPU 达标），"
                    "保持期 %.0fs",
                    _fmt_ratio(gpu_mon.ratio), hold_sec,
                )
                return "cpu"
            logger.warning(
                "GPU 压力拒绝态（ratio=%s）且降级条件不满足（内存/CPU 超阈值），维持拒绝",
                _fmt_ratio(gpu_mon.ratio),
            )
            self._active_device = "gpu"
            return "gpu"  # admit 会按 pressure_refusing 拒绝
        if self._fallback_active:
            # 迟滞：降级后至少保持 hold 秒，且 GPU 水位回落到 low_watermark 以下
            # 才允许回 GPU，避免水位在阈值附近反复横跳（每次切换 1~2s 加载代价）
            ratio = gpu_mon.ratio
            hold_ok = now >= self._fallback_hold_until
            ratio_ok = ratio is not None and ratio < gpu_mon.cfg.low_watermark
            if hold_ok and ratio_ok:
                self._fallback_active = False
                self._active_device = "gpu"
                logger.info(
                    "GPU 水位回落（ratio=%s < %.2f）且已过保持期，OCR 恢复 GPU（本次加载生效）",
                    _fmt_ratio(ratio), gpu_mon.cfg.low_watermark,
                )
                return "gpu"
            self._active_device = "cpu"
            return "cpu"  # 降级 episode 延续（不满足恢复条件）
        self._active_device = "gpu"
        return "gpu"

    def get_ocr(self):
        """获取 OCR 引擎（懒加载 + 管理器门控，design §8 继承 ocr-vram-guard §5.4）。

        三条硬规则：
        1. _unloading 置位后新获取一律拒绝（即使引擎对象还在）——保证排空收敛；
        2. 锁内复查（防 TOCTOU：外层看到未加载→等锁→卸载完成→锁内误加载）；
        3. 局部变量返回（防"返回时属性已被置 None"）。
        拒绝时抛 ModelUnavailableError（全局异常处理器转 503）。
        """
        from server.model_manager import get_model_manager
        from server.model_manager.types import ModelUnavailableError

        engine = self._ocr_engine                      # 局部快照
        if engine is not None and not self._unloading:
            return engine                              # 快速路径
        manager = get_model_manager()
        # 设备决策提前到 admit 之前：GPU 压力拒绝时降级到 CPU（条件允许），
        # _active_device 随之切换，admit 才会走 cpu monitor 放行而非直接拒绝
        device = self._decide_device(manager)
        manager.admit("ocr")                           # REFUSING/冷却/降级 → 抛异常
        with self._load_lock:                          # 顺带修复并发冷启动双加载
            if self._unloading:                        # 卸载窗口：拒绝新获取
                raise ModelUnavailableError("ocr", "unloading_in_progress")
            engine = self._ocr_engine
            if engine is not None:
                return engine
            manager.admit("ocr")                       # 锁内复查（等待期间状态可能已变）
            self._ensure_env()
            self._apply_pir_patch()
            try:
                from paddleocr import PaddleOCR

                device = self._decide_device(manager)  # 锁内重新决策（等待期间状态可能已变）
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
                self._loaded_at = time.time()          # 驱动协议 loaded_at（wall-clock）
                logger.info("Classic PaddleOCR loaded in %dms", self._load_elapsed_ms)
                self._last_error = None
                manager.note_load_success("ocr")
                engine = self._ocr_engine
            except Exception as e:
                self._last_error = f"PaddleOCR 加载失败: {e}"
                self._load_completed_at = time.perf_counter()
                if self._load_started_at is not None:
                    self._load_elapsed_ms = int((self._load_completed_at - self._load_started_at) * 1000)
                logger.exception("Classic PaddleOCR 加载失败")
                manager.note_load_failure("ocr", str(e)[:200])  # per-model 冷却/降级
                raise
        return engine

    def unload_ocr(self, drain_timeout: float = 10.0) -> dict:
        """增强卸载（design §8 继承 ocr-vram-guard §5.5）：真正释放显存。

        步骤：置 _unloading 意图（新获取立即拒绝）→ 排空 _in_flight（轮询 0.2s）→
        超时记 forced（在途推理持局部引用可跑完）→ del + gc.collect +
        paddle.device.cuda.empty_cache（实测回收 ~434/597MiB）→ forced 时安排
        延迟补释放（在途张量结束后才回池）→ 重置加载统计。

        线程归属硬规则：本方法可达秒级阻塞，调用方必须经 asyncio.to_thread
        包裹（管理器逐出/手动端点均已遵守），严禁在事件循环线程同步执行。

        Returns:
            {"unloaded": bool, "forced": bool, "detail": str}
        """
        with self._load_lock:
            if self._unloading:
                return {"unloaded": False, "forced": False, "detail": "already_unloading"}
            self._unloading = True
        forced = False
        try:
            if self._ocr_engine is None:
                self._reset_load_stats()
                self._loaded_at = None
                return {"unloaded": True, "forced": False, "detail": "already_unloaded"}
            # 排空在途推理（局部引用语义保证在途 predict 可跑完）
            deadline = time.time() + drain_timeout
            while self.in_flight_count() > 0 and time.time() < deadline:
                time.sleep(0.2)
            if self.in_flight_count() > 0:
                forced = True
                logger.warning(
                    "OCR 卸载排空超时（%d 个在途推理），强制卸载", self.in_flight_count()
                )
            del self._ocr_engine
            self._ocr_engine = None
            self._loaded_at = None
            self._release_paddle_cache(forced=forced)
            # 卸载后重置加载统计，使下次调用重新计为 cold_start
            self._reset_load_stats()
            # 设备复位为 config 期望设备，下次加载重新决策；
            # 迟滞状态（_fallback_active/_fallback_hold_until）有意保留——
            # 防止卸载后 GPU 水位仍在阈值附近时立刻回 GPU 造成抖动
            self._active_device = self._configured_device()
            logger.info("Classic PaddleOCR unloaded (forced=%s)", forced)
            return {"unloaded": True, "forced": forced, "detail": "ok"}
        finally:
            self._unloading = False

    def _release_paddle_cache(self, forced: bool = False) -> None:
        """归还 Paddle 显存：gc.collect + cuda.empty_cache（CPU-only 环境安全降级）"""
        gc.collect()
        try:
            import paddle
            _paddle_dynamic: Any = paddle  # 桩未导出私有函数，走 Any 规避
            if _paddle_dynamic.is_compiled_with_cuda():
                paddle.device.cuda.empty_cache()
        except Exception as e:
            logger.debug("paddle empty_cache 跳过（CPU-only 或 paddle 未加载）: %s", e)
        if forced:
            self._schedule_forced_release()

    def _schedule_forced_release(self, delay: float = 30.0) -> None:
        """强制卸载后的延迟补释放（design §5.5 步骤 3）。

        在途推理结束后张量才回池，第一次 empty_cache 收不走它们；
        delay 秒后若仍未重载，则补一次 gc.collect + empty_cache。
        """
        def _supplement():
            if self._ocr_engine is not None:
                return  # 期间已重载，跳过补释放
            gc.collect()
            try:
                import paddle
                _paddle_dynamic: Any = paddle  # 桩未导出私有函数，走 Any 规避
                if _paddle_dynamic.is_compiled_with_cuda():
                    paddle.device.cuda.empty_cache()
                logger.info("OCR 强制卸载延迟补释放完成")
            except Exception:
                pass

        timer = threading.Timer(delay, _supplement)
        timer.daemon = True
        timer.start()

    @contextmanager
    def in_flight_scope(self):
        """标记一次在途推理（卸载前排空依赖此计数）"""
        with self._in_flight_lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def in_flight_count(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    @property
    def ocr_loaded(self) -> bool:
        return self._ocr_engine is not None

    @property
    def keep_models(self) -> bool:
        """keep_models 读出口（design §9 写回链）。

        语义所有权已迁移到 ModelLifecycleManager.set_keep_models()；此处回读
        管理器 per-model 配置（restore_preload），供 /health.ocr、/ocr/status、
        GUI 消费者零改动地拿到生效值。管理器未注册时回退 True（与旧行为一致）。
        """
        try:
            from server.model_manager import get_model_manager
            return get_model_manager().keep_models("ocr")
        except Exception:
            return True  # 管理器未初始化时安全回退

    @property
    def model_ready(self) -> bool:
        """OCR 模型已加载就绪（可立即推理）"""
        return self._ocr_engine is not None

    @property
    def active_device(self) -> Literal["gpu", "cpu"]:
        """当前实际（或最近决策的）推理设备，供 OcrDriver.resource 动态归属与运维观察"""
        return self._active_device

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
    # in_flight_scope：卸载前排空依赖此计数（design §5.5 步骤 2）
    t_inf = time.perf_counter()
    with models.in_flight_scope():
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
    active_device: str = "gpu"  # 当前实际（或最近决策的）推理设备（GPU→CPU 降级可见）
    load_elapsed_ms: int | None = None
    last_inference_ms: int | None = None
    inference_count: int = 0
    last_error: str | None = None
    # design §10 兼容委托：附加 ModelLifecycleManager 状态摘要
    mlm_state: dict = {}


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


def _get_mlm_state() -> dict:
    """附加 ModelLifecycleManager 状态摘要（design §10 兼容委托）。"""
    try:
        from server.model_manager import get_model_manager
        return get_model_manager().health_summary()
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


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
        active_device=models.active_device,
        load_elapsed_ms=models._load_elapsed_ms,
        last_inference_ms=models._last_inference_ms,
        inference_count=models._inference_count,
        last_error=models._last_error,
        # design §10 兼容委托：附加 ModelLifecycleManager 状态摘要
        mlm_state=_get_mlm_state(),
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
    # design §9 写回链：委托管理器 per-model 配置（restore_preload）
    from server.model_manager import get_model_manager
    get_model_manager().set_keep_models("ocr", req.keep)
    return ModelControlResponse(success=True, message=f"模型常驻内存已{'开启' if req.keep else '关闭'}")


@router.post("/models/unload/json", response_model=ModelControlResponse, operation_id="ocr_unload_models")
async def unload_models_json(req: UnloadModelsRequest):
    # design §10 兼容委托：OCR 卸载走管理器（manual_unload 经驱动 unload → models.unload_ocr）
    if req.engine == "vl":
        models.unload_vl()
        return ModelControlResponse(success=True, message="远程 VL 无需卸载（远端 API）")
    if req.engine in (None, "ocr"):
        from server.model_manager import get_model_manager
        result = await get_model_manager().manual_unload("ocr")
        if result.get("status") == "error":
            return ModelControlResponse(success=False, message=result.get("detail", "卸载失败"))
        if req.engine is None:
            models.unload_vl()
            return ModelControlResponse(success=True, message="经典 PaddleOCR 模型已卸载；远程 VL 无需卸载")
        return ModelControlResponse(success=True, message="经典 PaddleOCR 模型已卸载")
    return ModelControlResponse(success=False, message=f"未知引擎: {req.engine}")


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
