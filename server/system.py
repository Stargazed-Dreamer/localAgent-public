"""系统级控制路由 - 防休眠开关等

通过 Windows SetThreadExecutionState API 阻止系统/显示器休眠。
适合长时间运行爬虫、训练任务等场景。
"""

import ctypes
import logging
import sys
import time

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.system")
router = APIRouter(prefix="/system", tags=["System"])


# ========== Windows SetThreadExecutionState 常量 ==========

# 仅在 Windows 下可用
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040  # Windows Vista+，允许"离开模式"（系统不休眠但可屏保）

# 是否支持离开模式（Vista+）
_AWAYMODE_SUPPORTED = sys.platform == "win32"


# ========== 防休眠管理器 ==========

class KeepAwakeManager:
    """防休眠开关管理器

    状态机：
        - enable=True, keep_display_on=False → 阻止系统休眠（显示器可关）
        - enable=True, keep_display_on=True  → 阻止系统+显示器休眠
        - enable=False                       → 恢复默认（系统可正常休眠）
    """

    def __init__(self):
        self._enabled = False
        self._keep_display_on = False
        self._reason: str | None = None
        self._enabled_at: float | None = None  # 启用时间戳
        self._last_error: str | None = None
        self._supported = sys.platform == "win32"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def keep_display_on(self) -> bool:
        return self._keep_display_on

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def enabled_at(self) -> float | None:
        return self._enabled_at

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _apply(self) -> bool:
        """将当前状态应用到系统"""
        if not self._supported:
            self._last_error = f"当前平台 {sys.platform} 不支持 SetThreadExecutionState"
            return False

        try:
            if self._enabled:
                flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                if self._keep_display_on:
                    flags |= ES_DISPLAY_REQUIRED
            else:
                # 恢复默认：仅 ES_CONTINUOUS 清除所有标志
                flags = ES_CONTINUOUS

            # SetThreadExecutionState 返回之前的 state（0 表示失败）
            result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
            if result == 0:
                self._last_error = "SetThreadExecutionState 返回 0（调用失败）"
                logger.error(f"防休眠设置失败: flags=0x{flags:08X}")
                return False

            self._last_error = None
            return True
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"防休眠设置异常: {e}")
            return False

    def enable(self, keep_display_on: bool = False, reason: str | None = None) -> bool:
        """启用防休眠"""
        self._enabled = True
        self._keep_display_on = keep_display_on
        self._reason = reason
        self._enabled_at = time.time()

        ok = self._apply()
        if ok:
            mode = "系统+显示器" if keep_display_on else "仅系统"
            logger.info(f"防休眠已启用（{mode}）" + (f"，原因: {reason}" if reason else ""))
        return ok

    def disable(self) -> bool:
        """关闭防休眠，恢复系统默认"""
        was_enabled = self._enabled
        self._enabled = False
        self._keep_display_on = False
        self._reason = None
        self._enabled_at = None

        ok = self._apply()
        if ok and was_enabled:
            logger.info("防休眠已关闭，系统恢复默认休眠策略")
        return ok

    def status(self) -> dict:
        """返回当前状态字典"""
        duration = None
        if self._enabled and self._enabled_at:
            duration = round(time.time() - self._enabled_at, 0)

        return {
            "supported": self._supported,
            "enabled": self._enabled,
            "keep_display_on": self._keep_display_on,
            "reason": self._reason,
            "enabled_at": self._enabled_at,
            "enabled_duration_seconds": duration,
            "last_error": self._last_error,
        }


keep_awake = KeepAwakeManager()


# ========== Pydantic 模型 ==========

class KeepAwakeRequest(BaseSchema):
    enable: bool
    keep_display_on: bool = False  # 是否同时阻止显示器休眠（默认只阻止系统休眠）
    reason: str | None = None  # 启用原因（便于记录，如 "爬虫运行中"）


class KeepAwakeResponse(BaseSchema):
    success: bool
    enabled: bool
    keep_display_on: bool
    reason: str | None = None
    message: str


class SystemStatusResponse(BaseSchema):
    keep_awake_supported: bool
    keep_awake_enabled: bool
    keep_awake_keep_display_on: bool
    keep_awake_reason: str | None = None
    keep_awake_enabled_duration_seconds: float | None = None
    keep_awake_last_error: str | None = None


# ========== 路由 ==========

@router.get("/status", response_model=SystemStatusResponse, operation_id="system_status")
async def system_status():
    """查询系统控制模块状态"""
    s = keep_awake.status()
    return SystemStatusResponse(
        keep_awake_supported=s["supported"],
        keep_awake_enabled=s["enabled"],
        keep_awake_keep_display_on=s["keep_display_on"],
        keep_awake_reason=s["reason"],
        keep_awake_enabled_duration_seconds=s["enabled_duration_seconds"],
        keep_awake_last_error=s["last_error"],
    )


@router.get("/keep-awake", response_model=KeepAwakeResponse, operation_id="keep_awake_status")
async def keep_awake_status():
    """查询防休眠开关当前状态"""
    s = keep_awake.status()
    if not s["supported"]:
        return KeepAwakeResponse(
            success=False, enabled=False, keep_display_on=False,
            message=f"当前平台不支持防休眠: {sys.platform}",
        )
    return KeepAwakeResponse(
        success=True,
        enabled=s["enabled"],
        keep_display_on=s["keep_display_on"],
        reason=s["reason"],
        message="防休眠已启用" if s["enabled"] else "防休眠已关闭",
    )


@router.post("/keep-awake", response_model=KeepAwakeResponse, operation_id="set_keep_awake")
async def set_keep_awake(req: KeepAwakeRequest):
    """设置防休眠开关

    - enable=true: 阻止系统休眠（爬虫/训练任务运行时使用）
    - enable=false: 恢复系统默认休眠策略
    - keep_display_on: 是否同时阻止显示器休眠（默认 false，显示器可关闭节能）
    - reason: 启用原因，便于记录（如 "爬虫运行中"）
    """
    if not keep_awake.supported:
        raise HTTPException(
            status_code=400,
            detail=f"当前平台 {sys.platform} 不支持 SetThreadExecutionState，仅 Windows 可用",
        )

    if req.enable:
        ok = keep_awake.enable(keep_display_on=req.keep_display_on, reason=req.reason)
        mode = "系统+显示器" if req.keep_display_on else "仅系统"
        msg = f"防休眠已启用（{mode}）" if ok else f"防休眠启用失败: {keep_awake.last_error}"
        return KeepAwakeResponse(
            success=ok,
            enabled=keep_awake.enabled,
            keep_display_on=keep_awake.keep_display_on,
            reason=keep_awake.reason,
            message=msg,
        )
    else:
        ok = keep_awake.disable()
        msg = "防休眠已关闭，系统恢复默认休眠策略" if ok else f"关闭失败: {keep_awake.last_error}"
        return KeepAwakeResponse(
            success=ok,
            enabled=keep_awake.enabled,
            keep_display_on=keep_awake.keep_display_on,
            reason=keep_awake.reason,
            message=msg,
        )


# ========== 启动/关闭钩子 ==========

def on_shutdown():
    """模块关闭时恢复系统默认休眠策略，避免进程退出后仍锁着"""
    if keep_awake.enabled:
        keep_awake.disable()
        logger.info("进程退出，已自动恢复系统默认休眠策略")
