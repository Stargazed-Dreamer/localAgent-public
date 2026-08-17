"""VL 反馈闭环：原子操作后用远程 VL 描述画面状态，给 agent 视觉反馈。

第一性原理：更好处理突发状况和长程任务。操作（browser click/fill/wait/extract、
screen execute_action）执行后，agent 通常只拿到 success/message 文本，失败时只能猜
原因。本模块在 agent 主动传 verify_prompt 时，截图 + 调用 RemoteVLClient.understand，
返回 ≤200 字的结构化描述（状态/观察/建议），让 agent 据此决定下一步。

调用契约：
- 调用方（browser.py / screen/routes.py）负责截图并传 PIL.Image
- 本函数为同步阻塞（remote_vl.understand 用 urllib），异步路由请用 asyncio.to_thread 包裹
- 无论操作成功或失败都可调用，失败时描述尤其有价值
- 返回纯文本，绝不返回 base64
"""

from __future__ import annotations

import logging
import time

from PIL import Image

from .remote_vl import remote_vl

logger = logging.getLogger("localagent.feedback_vl")

# VL 调用超时（秒）。超时返回 error，不阻塞主操作响应。
_DEFAULT_TIMEOUT = 20
# 描述最大字符数（提示词里约束 VL 返回 ≤200 字）。
_MAX_DESC_CHARS = 400  # 实际 VL 返回偶尔超 200，截到 400 防极端

# Prompt B 模板：围绕"突发状况 + 长程任务"第一性原理
# 三个字段：状态判断 / 画面观察 / 下一步建议。失败时强制找原因。
_PROMPT_TEMPLATE = """你是操作反馈助手。Agent 刚执行一步浏览器/屏幕操作。
期望状态：{verify_prompt}
操作结果：{result_label} - {action_message}

观察截图，按以下格式回答（≤200 字）：
【状态】符合预期 | 不符预期 | 部分符合
【观察】画面具体看到什么（弹窗/错误提示/加载中/登录墙/验证码/跳转/空白/正常内容）
【建议】下一步动作（继续下一步 / 重试本步 / 换 selector / 关闭弹窗 / 放弃）

重点排查会阻塞长程任务的突发状况：弹窗遮挡、登录失效、验证码出现、网络错误、意外跳转、加载卡死。
若操作失败，从画面找出可能原因。若操作成功但画面异常，明确指出。只输出上述三行，不要额外解释。"""


def describe_after_action(
    image: Image.Image,
    verify_prompt: str,
    action_succeeded: bool,
    action_message: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """操作后调用 VL 描述画面状态。

    Args:
        image: 操作后截图（PIL Image）
        verify_prompt: agent 提供的期望状态描述（非空才会调用本函数）
        action_succeeded: 操作本身是否成功
        action_message: 操作的 message 字段（用于让 VL 知道操作结果）
        timeout: VL 调用超时秒数

    Returns:
        {
            "status": "ok" | "skipped" | "error",
            "description": Optional[str],   # status=ok 时为 VL 描述文本
            "reason": str,                  # status!=ok 时的原因
            "elapsed_ms": int,
        }
    """
    t0 = time.perf_counter()

    # 1. VL 可用性检查
    try:
        remote_vl.reload_config()
    except Exception as e:
        logger.warning("reload VL config 失败: %s", e)
    if not remote_vl.available:
        return {
            "status": "skipped",
            "description": None,
            "reason": "远程 VL 不可用（未启用或所有 provider 冷却中）",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }

    # 2. 拼 Prompt B
    result_label = "success" if action_succeeded else "fail"
    # action_message 可能很长（如 extract_text 返回整页文本），截断
    msg_snippet = (action_message or "")[:300]
    prompt = _PROMPT_TEMPLATE.format(
        verify_prompt=verify_prompt,
        result_label=result_label,
        action_message=msg_snippet,
    )

    # 3. 调 VL（同步阻塞，调用方用 asyncio.to_thread 包裹）
    try:
        result = remote_vl.understand(image, prompt, timeout=timeout, use_case="vl_vision")
    except Exception as e:
        logger.warning("VL 调用异常: %s", e)
        return {
            "status": "error",
            "description": None,
            "reason": f"VL 调用异常: {e}",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if result.get("status") != "ok":
        detail = result.get("detail", "VL 调用失败")
        return {
            "status": "error",
            "description": None,
            "reason": detail,
            "elapsed_ms": elapsed_ms,
        }

    answer = result.get("answer", "").strip()
    if not answer:
        return {
            "status": "error",
            "description": None,
            "reason": "VL 返回空回答",
            "elapsed_ms": elapsed_ms,
        }

    if len(answer) > _MAX_DESC_CHARS:
        answer = answer[:_MAX_DESC_CHARS] + "…"

    return {
        "status": "ok",
        "description": answer,
        "reason": "",
        "elapsed_ms": elapsed_ms,
    }
