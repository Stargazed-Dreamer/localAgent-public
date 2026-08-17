"""L0 M5 剪贴板传感器：监听剪贴板变化（默认关）

设计（用户反馈："还可以抓取剪贴板事件"）：
- 默认关闭，需在 ConfigDialog 勾选"采集剪贴板变化"或在 config.toml 设置 clipboard_enabled=true
- 轮询 GetClipboardSequenceNumber() 检测变化（500ms 一次，简单可靠）
- 序列号变化时打开剪贴板读取内容（文本/图片）
- 密码框检测：通过 GetForegroundWindow + GetGUIThreadInfo 检测焦点控件是否有 ES_PASSWORD 风格
  → 密码框复制自动隐藏内容（payload.is_password=true，不写实际剪贴内容）
- 文本截断到 1000 字符（避免大文本拖慢事件流）
- 图片保存为 PNG 文件到 frames/ 目录（与 ScreenCaptureSensor 一致），事件只记录路径

事件格式（events.jsonl）：
    {
        "timestamp": 12.34,
        "kind": "clipboard_change",
        "payload": {
            "format": "text" | "image",
            "text": "剪贴的文本内容",  # format=text 时存在
            "image_path": "frames/clip_xxx.png",  # format=image 时存在
            "is_password": false,  # 密码框复制时 true，不写 text/image_path
            "truncated": false  # 文本截断时 true
        }
    }

线程模型：
- start() 起 daemon 线程轮询
- stop() 设 stop_event + join 线程
- win32clipboard 调用需要主线程？不需要，OpenClipboard 在任意线程都可以调用，
  但需要确保 CloseClipboard 配对调用（否则其他进程无法访问剪贴板）
"""

import io
import logging
import threading
from typing import TYPE_CHECKING

from lib.recorder.types import KIND_CLIPBOARD_CHANGE

if TYPE_CHECKING:
    from lib.recorder.controller import RecordingController

logger = logging.getLogger(__name__)

# 轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 0.5

# 文本截断长度（字符数）
MAX_TEXT_LENGTH = 1000

# Windows 剪贴板格式常量
CF_UNICODETEXT = 13
CF_DIB = 8  # Device-Independent Bitmap

# ES_PASSWORD 风格位（用于密码框检测）
ES_PASSWORD = 0x0020


class ClipboardSensor:
    """M5 剪贴板传感器（默认关）。

    Args:
        controller: RecordingController 引用
        poll_interval: 轮询间隔（秒），默认 0.5
    """

    def __init__(
        self,
        controller: "RecordingController",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.controller = controller
        self.poll_interval = float(poll_interval)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_seq = -1  # 上次见到的序列号，-1 表示首次启动
        self._started = False

    def start(self) -> None:
        """启动剪贴板轮询线程。"""
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("ClipboardSensor.controller 未注入")
        self._started = True
        self._stop_event.clear()
        # 初始化序列号（首次启动不触发"变化"事件）
        self._last_seq = self._get_sequence_number()
        self._thread = threading.Thread(target=self._run, daemon=True, name="clipboard-sensor")
        self._thread.start()

    def stop(self) -> None:
        """停止轮询线程。幂等。"""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        """轮询主循环：每 poll_interval 秒检查序列号变化。"""
        while not self._stop_event.is_set():
            try:
                self._check_and_emit()
            except Exception:
                # 任何异常不应终止轮询线程
                logger.exception("ClipboardSensor 轮询异常")
            self._stop_event.wait(self.poll_interval)

    def _check_and_emit(self) -> None:
        """检查剪贴板序列号，变化时读取内容并上报事件。"""
        seq = self._get_sequence_number()
        if seq == self._last_seq:
            return  # 无变化
        # 首次启动时不触发（_last_seq 初始化时已设置）
        is_first = self._last_seq == -1
        self._last_seq = seq
        if is_first:
            return

        # 检测焦点控件是否密码框
        is_password = self._is_foreground_password()

        # 读取剪贴板内容
        if is_password:
            # 密码框复制：不读取实际内容，只标记 is_password=true
            self._emit_event({
                "format": "password_masked",
                "is_password": True,
                "text": "",
            })
            return

        # 尝试读取文本优先，其次图片
        text = self._read_text()
        if text is not None:
            truncated = len(text) > MAX_TEXT_LENGTH
            payload_text = text[:MAX_TEXT_LENGTH] if truncated else text
            self._emit_event({
                "format": "text",
                "text": payload_text,
                "truncated": truncated,
                "is_password": False,
            })
            return

        image_path = self._read_image_and_save()
        if image_path is not None:
            self._emit_event({
                "format": "image",
                "image_path": image_path,
                "is_password": False,
            })
            return

        # 其他格式（如文件列表）暂不支持，上报 format=unknown
        self._emit_event({
            "format": "unknown",
            "is_password": False,
        })

    def _emit_event(self, payload: dict) -> None:
        """上报 clipboard_change 事件到 controller。"""
        self.controller.emit_event(KIND_CLIPBOARD_CHANGE, payload)

    # ========== Windows 剪贴板 API 封装 ==========

    def _get_sequence_number(self) -> int:
        """获取当前剪贴板序列号（不打开剪贴板，速度快）。"""
        try:
            import win32clipboard
            return int(win32clipboard.GetClipboardSequenceNumber())
        except Exception:
            return 0

    def _read_text(self) -> str | None:
        """打开剪贴板读取 CF_UNICODETEXT，失败返回 None。"""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                data = win32clipboard.GetClipboardData(CF_UNICODETEXT)
                return str(data) if data is not None else None
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return None

    def _read_image_and_save(self) -> str | None:
        """读取 CF_DIB 图片并保存为 PNG 到 frames/ 目录，返回相对路径。

        Returns:
            相对录制包根目录的路径（如 "frames/clip_000123_000001.png"），失败返回 None
        """
        try:
            import win32clipboard
            from PIL import Image
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(CF_DIB):
                    return None
                data = win32clipboard.GetClipboardData(CF_DIB)
                if data is None or len(data) < 40:
                    return None
                # DIB → PIL Image
                # BITMAPINFOHEADER (40 bytes) + 像素数据
                # 用 io.BytesIO 包装，PIL 读取 BMP 格式（DIB 就是去掉文件头的 BMP）
                # 标准 BMP 文件头 14 字节，加到 DIB 前面
                bmp_header = b"BM" + (len(data) + 14).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (14 + 40).to_bytes(4, "little")
                img = Image.open(io.BytesIO(bmp_header + data))
                img = img.convert("RGB")
                # 保存到 frames/
                mono, _ = self.controller.timestamp.now()
                seq = int(mono * 1000)
                # 用 controller 的帧序号（与 ScreenCaptureSensor 共享）
                # 简化：用时间戳作为文件名
                frame_name = f"clip_{seq:08d}.png"
                frames_dir = self.controller.package.frames_dir
                frame_path = frames_dir / frame_name
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(str(frame_path), format="PNG")
                try:
                    rel = frame_path.relative_to(self.controller.package.root)
                    return str(rel).replace("\\", "/")
                except ValueError:
                    return frame_path.name
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return None

    def _is_foreground_password(self) -> bool:
        """检测当前焦点控件是否是密码框（ES_PASSWORD 风格）。

        通过 GetForegroundWindow + GetGUIThreadInfo 获取焦点控件句柄，
        再 GetWindowLong(GWL_STYLE) 检查 ES_PASSWORD 位。
        失败时返回 False（宁可漏报密码框，不误报普通控件）。
        """
        try:
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False

            # 获取焦点控件的线程
            thread_id, _ = win32process.GetWindowThreadProcessId(hwnd)
            if not thread_id:
                return False

            # GetGUIThreadInfo 获取焦点控件句柄（hwndFocus）
            # GUITHREADINFO 结构：cbSize(4) + flags(4) + hwndActive(8) + hwndFocus(8) + ...
            import ctypes
            from ctypes import wintypes

            class GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND),
                    ("rcCursor", wintypes.RECT),
                    ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND),
                ]

            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if not ctypes.windll.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return False

            focus_hwnd = info.hwndFocus
            if not focus_hwnd:
                return False

            # 检查 ES_PASSWORD 风格位
            GWL_STYLE = -16
            style = win32gui.GetWindowLong(focus_hwnd, GWL_STYLE)
            return bool(style & ES_PASSWORD)
        except Exception:
            return False
