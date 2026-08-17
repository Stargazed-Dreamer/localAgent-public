"""VL 反馈闭环：操作后按需调用视觉模型描述画面。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 05）。
代码与原定义完全一致，仅调整 import 路径：
- get_browser_config：`from .config import` → `from ..config import`（包嵌套层级变化）
- describe_after_action：`from .vl.feedback_vl import` → `from ..vl.feedback_vl import`

迁移内容：
- _maybe_vl_feedback：操作后按需调用 VL 反馈，返回 (vl_description, vl_skipped, vl_skip_reason)

本模块不注册端点（_maybe_vl_feedback 被 action/click/fill/wait/extract 等端点调用），
故不 import routes.router，无循环导入风险。routes.py 末尾 import 本模块仅为触发加载，
让 ``from server.browser.vl_feedback import _maybe_vl_feedback`` 在包初始化后可用。
"""

import asyncio
import logging
import os

from ..config import get_browser_config
from ..vl.feedback_vl import describe_after_action

logger = logging.getLogger("localagent.browser")


async def _maybe_vl_feedback(
    verify_prompt: str | None,
    shot_path: str | None,
    action_succeeded: bool,
    action_message: str,
) -> tuple[str | None, bool | None, str | None]:
    """操作后按需调用 VL 反馈。返回 (vl_description, vl_skipped, vl_skip_reason)。

    - verify_prompt 为空 → 全部返回 None（向后兼容，零开销）
    - config feedback_vl_enabled=false → 跳过
    - 截图文件不存在 → 跳过（操作前页面可能已不可用）
    - 否则调 feedback_vl.describe_after_action，返回 ≤200 字描述
    - 无论成败都调用（失败时描述尤其有价值）
    - 最后删除临时截图文件
    """
    if not verify_prompt:
        return None, None, None
    cfg = get_browser_config()
    if not cfg.get("feedback_vl_enabled", True):
        return None, True, "feedback_vl_enabled=false（config 关闭）"
    if not shot_path or not os.path.exists(shot_path):
        return None, True, "截图未生成（页面可能已不可用）"
    try:
        from PIL import Image
        # 用 with + copy 确保文件句柄在 finally 删除前关闭（Windows 不允许删除已打开的文件）
        with Image.open(shot_path) as f:
            image = f.copy()
        result = await asyncio.to_thread(
            describe_after_action,
            image, verify_prompt, action_succeeded, action_message,
        )
        if result["status"] == "ok":
            return result["description"], False, None
        return None, True, result["reason"]
    except Exception as e:
        logger.warning("VL 反馈异常: %s", e)
        return None, True, f"VL 反馈异常: {e}"
    finally:
        try:
            if shot_path and os.path.exists(shot_path):
                os.remove(shot_path)
        except Exception:
            pass
