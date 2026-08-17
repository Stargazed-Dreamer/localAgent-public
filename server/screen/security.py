"""安全机制：紧急停止、危险关键词检查、自动跳过坐标缓存。

拆分自原 server/screen.py。依赖 windows._find_window（_check_window_bounds 调用）。
外部模块通过 `from server.screen import emergency, _check_danger, _check_window_bounds, _auto_skip_coords, clear_auto_skip_cache` 访问。
"""

import logging
import time
import unicodedata
from collections import OrderedDict

from server.config import get_screen_config
from server.screen.windows import _find_window

logger = logging.getLogger("localagent.screen")


# ========== 安全配置常量 ==========

DANGER_KEYWORDS_BLOCK = [
    "关机", "重启", "注销", "格式化",
    "Shut down", "Restart", "Sign out", "Format",
]
DANGER_KEYWORDS_CONFIRM = [
    "删除", "Delete", "移入回收站", "卸载", "Uninstall",
    "支付", "购买", "充值", "确认付款",
    "确认删除", "确定删除",
]

# 合并 config 追加的危险关键词（懒加载，避免 import 副作用）
_keywords_extended = False


def _ensure_keywords_loaded():
    """从 config 追加危险关键词到内置列表（仅执行一次）"""
    global _keywords_extended
    if _keywords_extended:
        return
    _keywords_extended = True
    try:
        _cfg_screen = get_screen_config()
        DANGER_KEYWORDS_BLOCK.extend(_cfg_screen.get("danger_keywords_block", []))
        DANGER_KEYWORDS_CONFIRM.extend(_cfg_screen.get("danger_keywords_confirm", []))
    except Exception:
        pass


# ========== 紧急停止管理器 ==========

class EmergencyStopManager:
    """全局紧急停止：Ctrl+` 触发后在配置的冷却时间内禁止Agent操作"""

    def __init__(self):
        self._stopped = False
        self._cooldown_until = 0.0
        self._cooldown_seconds = 10
        self._hotkey_listener = None
        self._hotkey_key = "<ctrl>+`"

    @property
    def cooldown_seconds(self) -> int:
        return self._cooldown_seconds

    def start(self):
        """启动全局快捷键监听（幂等：重复调用会先停止旧监听器）"""
        # 从 config 读取 hotkey 和 cooldown（启动时一次性读取，避免回调中做 I/O）
        try:
            cfg = get_screen_config()
            self._hotkey_key = cfg.get("emergency_hotkey", self._hotkey_key)
            self._cooldown_seconds = cfg.get("emergency_cooldown_seconds", self._cooldown_seconds)
        except Exception:
            pass

        # 幂等：先停止旧监听器，避免泄漏
        self.stop()

        try:
            from pynput.keyboard import GlobalHotKeys
            self._hotkey_listener = GlobalHotKeys({
                self._hotkey_key: self._trigger
            })
            self._hotkey_listener.start()
            logger.info(f"紧急停止快捷键 {self._hotkey_key} 已注册")
        except Exception as e:
            logger.warning(f"注册全局快捷键失败（可能需要管理员权限）: {e}")

    def stop(self):
        """停止监听"""
        if self._hotkey_listener:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

    def _trigger(self):
        """紧急停止回调（使用 start() 时缓存的 cooldown，不做 config I/O）"""
        self._stopped = True
        self._cooldown_until = time.time() + self._cooldown_seconds
        logger.warning(f"紧急停止已触发！{self._cooldown_seconds}秒内禁止Agent操作")
        # T16：撤销授权改调 SessionManager（替代 overlay_client.release_task_control）
        # hide_overlay 由 OverlayClient._on_session_event 订阅 release 事件自动处理，
        # 不在此处重复调用（避免冗余 + 维持会话管理层与 GUI 渲染层职责分离）
        try:
            from server.screen.session import get_session_manager
            get_session_manager().release("emergency_stop")
        except Exception:
            pass

    def can_operate(self) -> bool:
        """检查当前是否允许Agent操作"""
        if self._stopped and time.time() > self._cooldown_until:
            self._stopped = False
            logger.info("紧急停止冷却结束，Agent操作已恢复")
        return not self._stopped

    @property
    def is_stopped(self) -> bool:
        return self._stopped


emergency = EmergencyStopManager()


# ========== 自动跳过确认坐标缓存 ==========

_AUTO_SKIP_MAXLEN = 1000
_auto_skip_coords: OrderedDict[tuple[str, int, int], bool] = OrderedDict()


def _record_skip(key: tuple[str, int, int]) -> None:
    """记录跳过项，超出上限淘汰最老的（LRU）"""
    _auto_skip_coords[key] = True
    if len(_auto_skip_coords) > _AUTO_SKIP_MAXLEN:
        _auto_skip_coords.popitem(last=False)


def clear_auto_skip_cache():
    """清空自动跳过确认的坐标缓存"""
    _auto_skip_coords.clear()


# ========== 安全检查 ==========

def _check_danger(text: str | None = None, keys: list[str] | None = None,
                  element_text: str | None = None) -> str:
    """安全检查文本中的危险关键词，返回 "safe" / "confirm" / "block"

    仅检查传入的 text/keys/element_text，不基于坐标做 OCR（点击位置 OCR 检查
    曾作为 TODO 规划但未实现，已移除避免误导）。
    """
    _ensure_keywords_loaded()
    check_texts = []
    if text:
        check_texts.append(text)
    if keys:
        check_texts.extend(keys)
    if element_text:
        check_texts.append(element_text)

    def normalized(value: str) -> tuple[str, str]:
        # NFKC 归一化：将全角字符（ｇｕāｎｊī）和兼容形式折叠为半角等价形式，
        # 避免 Unicode 同形字符绕过危险关键词检查。先 casefold 再 normalize。
        folded = unicodedata.normalize("NFKC", value.casefold())
        compact = "".join(ch for ch in folded if not ch.isspace() and ch not in "-_")
        return folded, compact

    for t in check_texts:
        folded, compact = normalized(t)
        for kw in DANGER_KEYWORDS_BLOCK:
            kw_folded, kw_compact = normalized(kw)
            if kw_folded in folded or kw_compact in compact:
                return "block"
        for kw in DANGER_KEYWORDS_CONFIRM:
            kw_folded, kw_compact = normalized(kw)
            if kw_folded in folded or kw_compact in compact:
                return "confirm"

    return "safe"


def _check_window_bounds(window_title: str, x: int, y: int, process_name: str | None = None) -> bool:
    """检查坐标是否在指定窗口范围内"""
    win = _find_window(window_title, process_name)
    if not win:
        return False
    bbox = win["bbox"]
    return (bbox["left"] <= x <= bbox["right"] and
            bbox["top"] <= y <= bbox["bottom"])
