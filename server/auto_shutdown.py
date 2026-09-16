"""自动关机模块 — REST 端点封装关机流程

agent 在 Trae 会话里自己跑 loop（sleep + 检测），满足条件时调
POST /auto_shutdown/trigger；120s 倒计时内可调 POST /auto_shutdown/cancel 中止。

端点内部封装：
- 截屏（temp/auto_shutdown/<task_id>/final_screenshot.png，失败不阻塞）
- 推 inbox 通知（失败不阻塞）
- 调 Windows shutdown 命令（subprocess.Popen 异步触发）

设计取舍：
- 关机命令绕过 command_guard（用户睡前无法审批，已明确同意关机）
- trigger_count 内存中不持久化（后端重启归零，inbox 有历史记录可查）
- 端点幂等（重复 trigger 递增 count，重复 cancel 返回 success=True）
- 120s 倒计时（非 60s）：多 60s 宽限用户刹停
- 关机前 agent 应自行关闭有窗口的程序（模拟器/全屏应用会阻碍关机），端点不强制
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.auto_shutdown")

router = APIRouter(prefix="/auto_shutdown", tags=["自动关机"])


# ========== 模块常量 ==========

TRIGGER_COMMAND: list[str] = ["shutdown", "/s", "/t", "120"]
CANCEL_COMMAND: list[str] = ["shutdown", "/a"]
CANCEL_HINT: str = "shutdown /a"

# 项目根目录（server/ 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _PROJECT_ROOT / "temp" / "auto_shutdown"


def _resolve_api_base() -> str:
    """从 config.toml [server] 动态拼 API base（模块加载时读一次端口）。"""
    try:
        from server.config import get_server_config
        cfg = get_server_config()
        return f"http://{cfg['host']}:{cfg['port']}"
    except Exception:
        return "http://127.0.0.1:8766"


API_BASE = _resolve_api_base()


# ========== 模块级状态 ==========

_trigger_count: int = 0
_last_trigger_at: float | None = None
_last_trigger_task_id: str | None = None


def get_status() -> dict:
    """auto_shutdown 状态概览（供 /health 调用）"""
    return {
        "trigger_count": _trigger_count,
        "last_trigger_at": _last_trigger_at,
        "last_trigger_task_id": _last_trigger_task_id,
    }


def reset_state() -> None:
    """重置模块级状态（测试隔离用）"""
    global _trigger_count, _last_trigger_at, _last_trigger_task_id
    _trigger_count = 0
    _last_trigger_at = None
    _last_trigger_task_id = None


# ========== Pydantic 模型 ==========

class TriggerRequest(BaseSchema):
    task_id: str = Field(..., description="任务唯一 ID，用于截屏目录隔离")
    reason: str = Field("", description="关机原因（agent 描述触发条件）")
    screenshot: bool = Field(True, description="是否截屏存档（默认 True）")
    dry_run: bool = Field(True, description="干跑模式，不真调 shutdown 命令（T04：默认 True 安全优先，测试忘传也不会真关机）")


class CancelRequest(BaseSchema):
    dry_run: bool = Field(True, description="干跑模式，不真调 shutdown /a（T04：默认 True 安全优先）")


# ========== 内部辅助函数 ==========

def _capture_screenshot(task_id: str) -> str | None:
    """关机瞬间截屏（mss + PIL），返回文件路径，失败返回 None"""
    try:
        import mss
        from PIL import Image
        screenshot_dir = STATE_DIR / task_id
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / "final_screenshot.png"
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # 全屏（所有显示器合集）
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            img.save(str(screenshot_path), "PNG")
        return str(screenshot_path)
    except Exception as e:
        logger.warning(f"[auto_shutdown] 截屏失败: {e}")
        return None


async def _push_inbox(
    source: str,
    category: str,
    title: str,
    description: str = "",
    payload: dict | None = None,
    api_base: str = API_BASE,
) -> bool:
    """推送到 inbox（POST /inbox），失败返回 False 不抛异常。

    T04：sync urllib.request → async httpx.AsyncClient，避免阻塞事件循环。
    """
    try:
        data = {
            "source": source,
            "category": category,
            "title": title,
            "description": description,
            "payload": payload or {},
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_base}/inbox",
                json=data,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"[auto_shutdown] inbox 推送失败: {e}")
        return False


def _trigger_shutdown_command() -> tuple[bool, str | None]:
    """调 shutdown /s /t 120 异步触发关机，返回 (success, error)"""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(TRIGGER_COMMAND, creationflags=creationflags)
        return True, None
    except Exception as e:
        return False, str(e)


def _cancel_shutdown_command() -> tuple[bool, str | None]:
    """调 shutdown /a 中止关机倒计时，返回 (success, error)"""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(CANCEL_COMMAND, creationflags=creationflags)
        return True, None
    except Exception as e:
        return False, str(e)


# ========== 端点实现 ==========

@router.post("/trigger", operation_id="auto_shutdown_trigger")
async def trigger_shutdown(req: TriggerRequest) -> dict:
    """触发关机（120s 倒计时，可调 /auto_shutdown/cancel 中止）

    内部依次：权限检查 → 递增 trigger_count → 截屏（可选）→ 推 inbox → 调 shutdown 命令（除非 dry_run）
    截屏 / inbox 失败均不阻塞关机。

    权限：需 watchdog 模式且勾选允许关机（SessionManager.can_shutdown()）。
    dry_run 模式也检查权限（避免测试时绕过权限，但 dry_run=True 仍允许无权限测试调用流程）。
    """
    global _trigger_count, _last_trigger_at, _last_trigger_task_id

    # T19：关机权限检查（watchdog 模式 + shutdown_permitted=True 才允许）
    # dry_run 模式跳过权限检查（测试用），生产路径必须拦截
    if not req.dry_run:
        try:
            from server.screen.session import get_session_manager
            session = get_session_manager()
            if not session.can_shutdown():
                status = session.status()
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "shutdown_not_permitted",
                        "message": "需 watchdog 模式且勾选允许关机",
                        "current_mode": status.get("mode", "no_permission"),
                        "shutdown_permitted": status.get("shutdown_permitted", False),
                    },
                )
        except HTTPException:
            raise
        except Exception as e:
            # SessionManager 不可用时 fail-closed
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "session_manager_unavailable",
                    "message": f"SessionManager 不可用，无法验证关机权限: {e}",
                },
            ) from e

    # 递增状态
    _trigger_count += 1
    _last_trigger_at = time.time()
    _last_trigger_task_id = req.task_id

    # 截屏（失败不阻塞，防御性 try/except 兜底 helper 自身 bug）
    screenshot_path: str | None = None
    if req.screenshot:
        try:
            screenshot_path = _capture_screenshot(req.task_id)
        except Exception as e:
            logger.warning(f"[auto_shutdown] 截屏异常: {e}")
            screenshot_path = None

    # 推 inbox（失败不阻塞，防御性 try/except 兜底 helper 自身 bug）
    description = (
        f"任务 {req.task_id} 触发关机（120s 倒计时）\n"
        f"原因: {req.reason or '(未指定)'}\n"
        f"截屏: {screenshot_path or '失败/未启用'}\n"
        f"取消: 在 120s 内执行 {CANCEL_HINT} 或调 POST /auto_shutdown/cancel"
    )
    try:
        await _push_inbox(
            source=f"auto_shutdown:{req.task_id}",
            category="info",
            title="关机已触发",
            description=description,
            payload={
                "task_id": req.task_id,
                "reason": req.reason,
                "screenshot": screenshot_path,
                "dry_run": req.dry_run,
                "trigger_count": _trigger_count,
            },
        )
    except Exception as e:
        logger.warning(f"[auto_shutdown] inbox 推送异常: {e}")

    # 调 shutdown 命令（dry_run 跳过）
    if req.dry_run:
        return {
            "success": True,
            "task_id": req.task_id,
            "trigger_count": _trigger_count,
            "shutdown_scheduled": True,
            "dry_run": True,
            "screenshot_path": screenshot_path,
            "cancel_command": CANCEL_HINT,
        }

    ok, err = _trigger_shutdown_command()
    # T04：关机命令失败时推 inbox 问用户（简化看门狗权限检查：
    # 若 shutdown 命令因权限不足失败，通知用户手动授权或处理）
    if not ok:
        try:
            await _push_inbox(
                source=f"auto_shutdown:{req.task_id}",
                category="warning",
                title="关机命令执行失败",
                description=f"shutdown 命令返回错误：{err}\n请用户手动处理或检查关机权限。",
                payload={
                    "task_id": req.task_id,
                    "error": err,
                    "trigger_count": _trigger_count,
                },
            )
        except Exception as e:
            logger.warning(f"[auto_shutdown] 失败通知推送异常: {e}")
    return {
        "success": ok,
        "task_id": req.task_id,
        "trigger_count": _trigger_count,
        "shutdown_scheduled": ok,
        "dry_run": False,
        "screenshot_path": screenshot_path,
        "cancel_command": CANCEL_HINT,
        "error": err,
    }


@router.post("/cancel", operation_id="auto_shutdown_cancel")
async def cancel_shutdown(req: CancelRequest) -> dict:
    """中止关机倒计时（shutdown /a）

    幂等：重复调用不报错。推 inbox 通知（失败不阻塞）。
    """
    if not req.dry_run:
        ok, err = _cancel_shutdown_command()
    else:
        ok, err = True, None

    # 推 inbox（失败不阻塞）
    await _push_inbox(
        source="auto_shutdown:cancel",
        category="info",
        title="关机已取消",
        description=f"用户/agent 取消了关机倒计时（dry_run={req.dry_run}）",
        payload={"dry_run": req.dry_run, "success": ok},
    )

    return {
        "success": ok,
        "dry_run": req.dry_run,
        "cancel_command": CANCEL_HINT,
        "error": err,
    }
