"""L0 M2 屏幕捕获器：定时截图 + click 多帧采样 + 帧哈希去重

设计（D023 / D024 / D025 / D035）：
- 定时截图（详细 500ms / 粗略 2s）+ click 触发多帧采样（50/100/150ms）
- click 后 N 帧一组，组内哈希全相同才去重（D023：N 帧一样留 1 张，有变化全留）
- 定时截图路径 pHash 相邻帧去重（D035：均值哈希 + 汉明距离，跳过相似帧）
- PNG 无损编码 + 10 帧批量写盘（减少 I/O）
- 三种截图模式（D017 / D024）：
  - fullscreen：mss 截主屏（L0 简化，多屏拼接留给 L1）
  - monitor：mss 截指定显示器
  - window：PrintWindow 截窗口内容（即使被遮挡），最小化时返回 None 暂停截图
- capture_fn 注入：测试时传 fake 返回 PIL Image，生产根据 mode 用真实实现

线程模型：
- start() 起 daemon 线程做定时截图
- trigger_burst() 起 daemon 线程做多帧采样（不阻塞调用方）
- _capture_lock 串行化截图调用（避免定时与 burst 并发抢系统资源）
- _buffer_lock 保护帧缓冲列表
- _seq_lock 保护帧序号递增
- stop() 设置 stop_event + join 线程 + flush 缓冲
"""

import hashlib
import io
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from lib.recorder.controller import RecordingController

# 默认采样间隔（秒）
DEFAULT_INTERVAL_DETAILED = 0.5  # 详细模式 500ms
DEFAULT_INTERVAL_COARSE = 2.0    # 粗略模式 2s

# click 多帧采样时刻（秒，相对 click 时刻的偏移）
DEFAULT_BURST_INTERVALS = (0.05, 0.10, 0.15)  # 50ms / 100ms / 150ms

# 批量写盘阈值
DEFAULT_BATCH_SIZE = 10

# pHash 默认阈值（D035）：汉明距离 <= 此值视为相似帧，跳过。
# 0 = 禁用去重；默认 5（64-bit hash 中允许 5 bit 不同 ≈ 7.8% 差异）
DEFAULT_PHASH_THRESHOLD = 5

# aHash 缩放尺寸（8×8 = 64 像素 = 64-bit hash）
_PHASH_SIZE = 8


class ClickBurstDedup:
    """click 多帧采样去重器（spec D023）。

    规则：
    - 所有帧哈希完全相同 → 只留第 1 张
    - 任一帧不同 → 全部保留

    "有变化全留"的目的是捕捉瞬态变化（如复选框切换后又变回 [A,B,A]）。
    """

    @staticmethod
    def dedup(frames: list[bytes]) -> list[bytes]:
        """对帧列表去重。

        Args:
            frames: PNG bytes 列表

        Returns:
            去重后的列表（保持原顺序）
        """
        if len(frames) <= 1:
            return list(frames)
        hashes = {hashlib.md5(f).digest() for f in frames}
        if len(hashes) == 1:
            return [frames[0]]
        return list(frames)


def compute_phash(img: Image.Image) -> int:
    """计算图片的均值哈希（aHash，D035）。

    算法：resize 到 8×8 灰度 → 取均值 → 每个像素 >= 均值置 1，否则 0 → 64-bit 整数。

    Args:
        img: PIL Image（任意尺寸/模式）

    Returns:
        64-bit 整数哈希值
    """
    small = img.convert("L").resize((_PHASH_SIZE, _PHASH_SIZE), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float64)
    mean = arr.mean()
    bits = (arr >= mean).flatten()
    # 64-bit 整数（bit 0 = 左上角像素）
    h = 0
    for bit in bits:
        h = (h << 1) | int(bit)
    return h


def phash_distance(h1: int, h2: int) -> int:
    """计算两个 64-bit 哈希的汉明距离（D035）。

    Args:
        h1: 哈希 1
        h2: 哈希 2

    Returns:
        汉明距离（不同 bit 数，0 = 完全相同，64 = 完全相反）
    """
    return bin(h1 ^ h2).count("1")


class ScreenCaptureSensor:
    """M2 屏幕捕获器。

    Args:
        controller: RecordingController 引用
        mode: 截图模式，fullscreen / monitor / window
        monitor_index: mss 显示器索引（mode=monitor 时用，0=主屏）
        window_hwnd: 目标窗口句柄（mode=window 时用）
        interval: 定时截图间隔（秒），默认 0.5（详细模式）
        burst_intervals: click 多帧采样时刻（秒，相对 click 时刻偏移），默认 (0.05, 0.10, 0.15)
        batch_size: 批量写盘阈值，默认 10
        capture_fn: 截图函数，返回 PIL Image 或 None（截图失败/窗口最小化）。
                    None 时根据 mode 自动构造默认实现。
        on_before_capture: 截图前回调（无参），用于截图瞬间隐藏 FloatingBar 等遮挡元素。
            None 时不调用。回调必须线程安全（ScreenCaptureSensor 在子线程调用）。
        on_after_capture: 截图后回调（无参），用于恢复隐藏的 FloatingBar。
            None 时不调用。即使截图失败（capture_fn 返回 None）也会调用。
        phash_threshold: 定时截图 pHash 相邻帧去重阈值（D035）。
            汉明距离 <= 此值视为相似帧，跳过不写盘。0 = 禁用去重。默认 5。
            仅对定时截图路径生效，click burst 路径不受影响（已有 ClickBurstDedup）。
    """

    def __init__(
        self,
        controller: "RecordingController",
        mode: str = "fullscreen",
        monitor_index: int = 0,
        window_hwnd: int | None = None,
        interval: float = DEFAULT_INTERVAL_DETAILED,
        burst_intervals: tuple = DEFAULT_BURST_INTERVALS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        capture_fn: Callable[[], Image.Image | None] | None = None,
        on_before_capture: Callable[[], None] | None = None,
        on_after_capture: Callable[[], None] | None = None,
        phash_threshold: int = DEFAULT_PHASH_THRESHOLD,
    ) -> None:
        self.controller = controller
        self.mode = mode
        self.monitor_index = monitor_index
        self.window_hwnd = window_hwnd
        self.interval = float(interval)
        self.burst_intervals = tuple(burst_intervals)
        self.batch_size = int(batch_size)
        self.phash_threshold = int(phash_threshold)
        self._capture_fn = capture_fn or self._build_default_capture_fn()
        # 截图前后回调（用于截图瞬间隐藏 FloatingBar）
        self._on_before_capture = on_before_capture
        self._on_after_capture = on_after_capture
        # 线程与同步
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._capture_lock = threading.Lock()  # 串行化截图调用
        self._buffer_lock = threading.Lock()   # 保护帧缓冲
        self._seq_lock = threading.Lock()      # 保护帧序号
        self._frame_buffer: list[tuple[bytes, float, float]] = []  # [(png, mono, abs), ...]
        self._frame_seq = 0
        self._started = False
        # pHash 相邻帧去重状态（D035）：上一帧的 hash，None 表示首帧或 reset 后
        self._last_phash: int | None = None
        # pHash 去重统计（供测试/调试读取）
        self._phash_skipped_count = 0

    def _build_default_capture_fn(self) -> Callable[[], Image.Image | None]:
        """根据 mode 构造默认 capture_fn。"""
        if self.mode == "window":
            hwnd = self.window_hwnd
            if hwnd is None:
                raise ValueError("mode='window' 需要 window_hwnd")
            return lambda: _capture_window(hwnd)
        elif self.mode == "monitor":
            idx = self.monitor_index
            return lambda: _capture_mss_monitor(idx)
        else:  # fullscreen
            # L0 简化：fullscreen 等同于截主屏（多屏拼接留给 L1）
            return lambda: _capture_mss_monitor(0)

    def start(self) -> None:
        """启动定时截图线程。"""
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("ScreenCaptureSensor.controller 未注入")
        self._started = True
        self._stop_event.clear()
        # 重置 pHash 状态（D035）：新录制会话不继承上一会话的 hash
        self._last_phash = None
        self._phash_skipped_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止截图线程 + flush 缓冲。幂等。"""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._flush_buffer()

    def trigger_burst(self) -> None:
        """异步触发 click 多帧采样。

        在子线程中按 burst_intervals 时刻截 N 帧，
        完成后调 ClickBurstDedup.dedup 去重并立即写盘。
        不阻塞调用方。
        """
        if not self._started:
            return
        t = threading.Thread(target=self.capture_burst, daemon=True)
        t.start()

    def capture_burst(self) -> None:
        """同步执行 click 多帧采样 + 去重 + 写盘。

        生产环境由 trigger_burst() 在子线程中调用；
        测试可直接同步调用以避免定时线程干扰。
        """
        self._run_burst_impl()

    def _run(self) -> None:
        """定时截图循环。"""
        while not self._stop_event.is_set():
            self._capture_and_buffer()
            self._stop_event.wait(self.interval)

    def _run_burst_impl(self) -> None:
        """click 多帧采样实现。"""
        frames: list[tuple[bytes, float, float]] = []  # [(png, mono, abs), ...]
        prev_offset = 0.0
        for offset in self.burst_intervals:
            # 等到相对 offset 时刻
            wait_s = offset - prev_offset
            if wait_s > 0 and self._stop_event.wait(wait_s):
                return  # stop 信号
            prev_offset = offset
            img = self._safe_capture()
            if img is None:
                continue
            png = _encode_png(img)
            mono, abs_t = self.controller.timestamp.now()
            frames.append((png, mono, abs_t))
        if not frames:
            return
        # 去重（仅对 PNG bytes）
        pngs = [f[0] for f in frames]
        kept_pngs = ClickBurstDedup.dedup(pngs)
        if len(kept_pngs) == len(frames):
            # 有变化，全留
            for png, mono, abs_t in frames:
                self._write_single_frame(png, mono, abs_t)
        else:
            # 全相同，只留第一张（dedup 返回 [frames[0]]）
            png0, mono0, abs0 = frames[0]
            self._write_single_frame(png0, mono0, abs0)

    def _capture_and_buffer(self) -> None:
        """定时截图：截一张 → pHash 去重（D035）→ PNG 编码 → 加入缓冲（满 batch_size 时 flush）。"""
        img = self._safe_capture()
        if img is None:
            return
        # pHash 相邻帧去重（D035）：与上一帧汉明距离 <= 阈值则跳过本帧
        if self.phash_threshold > 0:
            current_phash = compute_phash(img)
            if self._last_phash is not None:
                dist = phash_distance(current_phash, self._last_phash)
                if dist <= self.phash_threshold:
                    # 与上一帧相似，跳过不写盘
                    self._phash_skipped_count += 1
                    return
            self._last_phash = current_phash
        png = _encode_png(img)
        mono, abs_t = self.controller.timestamp.now()
        with self._buffer_lock:
            self._frame_buffer.append((png, mono, abs_t))
            should_flush = len(self._frame_buffer) >= self.batch_size
        if should_flush:
            self._flush_buffer()

    def _safe_capture(self) -> Image.Image | None:
        """加锁调用 capture_fn，异常时返回 None。

        截图前后调用 on_before_capture / on_after_capture 回调，
        用于截图瞬间隐藏 FloatingBar（避免出现在截图中）。
        即使截图失败也调用 on_after_capture 恢复显示。
        """
        # 截图前隐藏 overlay（如 FloatingBar）
        if self._on_before_capture is not None:
            try:
                self._on_before_capture()
            except Exception:
                pass
        try:
            with self._capture_lock:
                try:
                    return self._capture_fn()
                except Exception:
                    return None
        finally:
            # 截图后恢复 overlay（即使截图失败也恢复）
            if self._on_after_capture is not None:
                try:
                    self._on_after_capture()
                except Exception:
                    pass

    def _flush_buffer(self) -> None:
        """flush 帧缓冲到盘。"""
        with self._buffer_lock:
            items = self._frame_buffer
            self._frame_buffer = []
        # 释放锁后写盘
        for png, mono, abs_t in items:
            self._write_single_frame(png, mono, abs_t)

    def _write_single_frame(self, png: bytes, mono: float, abs_t: float) -> None:
        """写一帧到盘 + 上报 screen_frame 事件 + 累加 frame_count。"""
        seq = self._next_seq()
        frame_name = f"frame_{int(mono * 1000):08d}_{seq:06d}.png"
        frame_path = self.controller.package.frames_dir / frame_name
        frame_path.write_bytes(png)
        try:
            rel_path = frame_path.relative_to(self.controller.package.root)
            path_str = str(rel_path).replace("\\", "/")
        except ValueError:
            path_str = frame_path.name
        payload = {
            "frame_path": path_str,
            "frame_seq": seq,
            "mode": self.mode,
        }
        self.controller.emit_event("screen_frame", payload)
        self.controller.increment_frame_count(1)

    def _next_seq(self) -> int:
        """取下一个帧序号（线程安全）。"""
        with self._seq_lock:
            seq = self._frame_seq
            self._frame_seq += 1
            return seq


# ========== 默认 capture_fn 实现（真实系统 API） ==========

def _capture_mss_monitor(monitor_index: int = 0) -> Image.Image | None:
    """mss 截取指定显示器。

    Args:
        monitor_index: 显示器索引，0=主屏，1+=副屏

    Returns:
        PIL Image 或 None（截图失败）
    """
    import mss
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_index >= len(monitors):
                monitor_index = 0
            monitor = monitors[monitor_index]
            shot = sct.grab(monitor)
            # mss 返回 BGRA，转 RGB
            return Image.frombytes("RGB", shot.size, bytes(shot.bgra), "raw", "BGRX")
    except Exception:
        return None


def _capture_window(hwnd: int) -> Image.Image | None:
    """PrintWindow 截取窗口内容（即使被遮挡）。

    Args:
        hwnd: 窗口句柄

    Returns:
        PIL Image 或 None（窗口最小化/截图失败）
    """
    from ctypes import windll

    import win32gui
    import win32ui

    try:
        # 最小化时跳过（D024）
        if win32gui.IsIconic(hwnd):
            return None

        # 取窗口矩形
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None

        # 创建兼容 DC + bitmap
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bitmap)

        # PrintWindow（PW_RENDERFULLCONTENT = 0x00000002，能截到浏览器等内容）
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0x00000002)
        if result != 1:
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            win32gui.DeleteObject(bitmap.GetHandle())
            return None

        # 取 bitmap bits → PIL Image
        bmp_info = bitmap.GetInfo()
        bmp_bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits, "raw", "BGRX", 0, 1,
        )

        # 清理
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        win32gui.DeleteObject(bitmap.GetHandle())

        return img
    except Exception:
        return None


def _encode_png(img: Image.Image) -> bytes:
    """PIL Image → PNG bytes。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
