"""浏览器错误模型（评估文档 P0 第 5 项）。

包含统一错误码常量、phase 常量、BrowserErrorResponse 模型和错误分类函数。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 01）。
代码与原定义完全一致，仅调整 import 路径。
"""


from lib.schema import BaseSchema

# ---- 错误模型 ----

# error_code 标准取值（评估文档 P0 第 5 项）
BROWSER_ERROR_CODES = {
    "TAB_NOT_FOUND": "未找到匹配的标签页",
    "ELEMENT_NOT_FOUND": "未找到匹配的元素（CSS/role/text 等选择器未命中）",
    "AMBIGUOUS_TAB": "URL 匹配到多个标签页，需指定 tab_id 或 session_id",
    "MULTIPLE_MATCHES": "匹配到多个元素，需细化 target",
    "INVALID_SELECTOR": "CSS 选择器语法错误",
    "TIMEOUT": "等待超时",
    "NAVIGATION_COMPLETED_WAIT_TIMEOUT": "导航已完成但后续等待超时",
    "STALE_NODE": "DOM 已变化，旧 node_id 不再有效",
    "UNSUPPORTED_ACTION": "不支持的 action 类型",
    "BROWSER_DISCONNECTED": "调试浏览器未连接",
    "EXECUTION_ERROR": "Playwright 执行异常",
    "INTERNAL_ERROR": "服务端内部错误",
    # 2026-08-06 新增（spec: browser-dialog-handling）
    "DIALOG_NOT_FOUND": "dialog 队列为空，无 dialog 可处理",
    "DIALOG_ALREADY_DISMISSED": "dialog 已被超时兜底自动 dismiss，无法再 accept/dismiss",
    "BLOCKED_BY_DIALOG": "操作被 pending dialog 阻塞，需先调 handle_dialog 处理",
}

# phase 标准取值
BROWSER_PHASES = {"connect", "locate", "act", "wait", "verify"}


class BrowserErrorResponse(BaseSchema):
    """统一浏览器错误响应（评估文档 P0 第 5 项）。

    新路由失败时返回此结构（HTTP 200 + success=false），方便 agent 解析 error_code
    而非自然语言。原始 Playwright 错误消息保留在 debug_detail。

    - error_code: TAB_NOT_FOUND / MULTIPLE_MATCHES / INVALID_SELECTOR / TIMEOUT /
                  STALE_NODE / UNSUPPORTED_ACTION / BROWSER_DISCONNECTED /
                  EXECUTION_ERROR / INTERNAL_ERROR
    - phase: connect / locate / act / wait / verify（错误发生在哪个阶段）
    - debug_detail: 原始 Playwright 错误消息（用于诊断，agent 不应解析）
    """
    success: bool = False
    error_code: str
    error_message: str  # 人类可读简述（= BROWSER_ERROR_CODES[error_code]）
    phase: str
    debug_detail: str | None = None
    elapsed_ms: int


def _classify_error(err_text: str, phase: str, elapsed: int) -> BrowserErrorResponse:
    """从 Playwright 错误文本推断 error_code，构造 BrowserErrorResponse。"""
    et = (err_text or "").lower()
    if "target closed" in et or "browser has been closed" in et or "connection reset" in et:
        code = "BROWSER_DISCONNECTED"
    elif "ambiguous_tab" in et:
        code = "AMBIGUOUS_TAB"
    elif "navigation_completed" in et:
        code = "NAVIGATION_COMPLETED_WAIT_TIMEOUT"
    elif "timeout" in et or "waiting for selector" in et:
        code = "TIMEOUT"
    elif "strict mode violation" in et or "resolved to" in et:
        code = "MULTIPLE_MATCHES"
    elif "selector=" in et or "is not a valid selector" in et or "css.escape" in et:
        code = "INVALID_SELECTOR"
    elif "stale" in et or "detached" in et:
        code = "STALE_NODE"
    elif "no matching tab" in et or "tab_not_found" in et:
        code = "TAB_NOT_FOUND"
    else:
        code = "EXECUTION_ERROR"
    return BrowserErrorResponse(
        error_code=code,
        error_message=BROWSER_ERROR_CODES[code],
        phase=phase,
        debug_detail=err_text,
        elapsed_ms=elapsed,
    )
