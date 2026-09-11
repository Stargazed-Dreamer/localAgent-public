"""browser_evaluate 端点 — 只读 JS 执行 + 写操作安全拦截。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 04）。
代码与原定义完全一致，仅调整 import 路径：
- BROWSER_ERROR_CODES / BrowserErrorResponse / _classify_error：从 server.browser.error_model import
- _build_playwright_script / _execute_playwright：从 server.browser.playwright_executor import
- router / _get_session_page：从 server.browser.routes import

迁移内容：
- BrowserEvaluateRequest / BrowserEvaluateResponse 模型
- _WRITE_PATTERNS_RE 写操作正则黑名单
- _READ_ALLOWLIST 只读白名单（占位，目前为空）
- _normalize_expression 表达式归一化（去注释 + 还原转义）
- _is_write_expression 写操作判定
- browser_evaluate 端点

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import asyncio
import re

from lib.schema import BaseSchema

# 从本包各子模块复用共享件（router / 错误模型 / playwright 子进程执行器 / 辅助函数）
from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse, _classify_error
from .playwright_executor import _build_playwright_script, _execute_playwright
from .routes import router


class BrowserEvaluateRequest(BaseSchema):
    """只读 JS 执行请求（评估文档 P1 第 1 项）。

    在页面上下文中执行 JavaScript 表达式。服务端会做基础写操作拦截
    （禁止 location/document.cookie/localStorage 赋值、document.write、fetch/XHR
    发起等），但无法保证完全沙箱化——agent 应只用于读取 DOM/状态。

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则走 url_pattern 子进程模型
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）
    - expression: JS 表达式（最后一条语句的值作为返回值，自动 JSON.stringify）
    - return_json: True（默认）= 把返回值 JSON.stringify；False=返回原始 toString
    - max_length: 返回值截断长度（默认 5000）
    """
    session_id: str | None = None
    url_pattern: str | None = None
    tab_id: str | None = None  # 无会话模式：CDP target id 精确消歧
    expression: str
    return_json: bool = True
    max_length: int = 5000


class BrowserEvaluateResponse(BaseSchema):
    success: bool
    value: str | None = None
    elapsed_ms: int
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


_WRITE_PATTERNS_RE = [
    # P0-3 修复：用正则替代子串匹配，区分读/写
    # 赋值：xxx = ... （但排除 == !== >= <= 等比较运算符）
    re.compile(r'location\.href\s*=(?![=])', re.IGNORECASE),
    re.compile(r'window\.location\s*=(?![=])', re.IGNORECASE),
    re.compile(r'\blocation\s*=(?![=])', re.IGNORECASE),
    re.compile(r'location\.(assign|replace)\s*\(', re.IGNORECASE),
    # 属性访问绕过：location['href'] / location["href"]
    re.compile(r'\blocation\s*\[', re.IGNORECASE),
    re.compile(r'document\.(cookie|domain|title)\s*=(?![=])', re.IGNORECASE),
    re.compile(r'document\.(cookie|domain|title)\s*\[', re.IGNORECASE),
    re.compile(r'document\.write(?:ln)?\s*\(', re.IGNORECASE),
    re.compile(r'(localStorage|sessionStorage)\.(setItem|removeItem|clear)\s*\(', re.IGNORECASE),
    re.compile(r'(localStorage|sessionStorage)\s*\[', re.IGNORECASE),
    re.compile(r'\bfetch\s*\(', re.IGNORECASE),
    re.compile(r'\bXMLHttpRequest\b', re.IGNORECASE),
    re.compile(r'navigator\.sendBeacon\s*\(', re.IGNORECASE),
    re.compile(r'\.(innerHTML|outerHTML|textContent|innerText)\s*=(?![=])', re.IGNORECASE),
    re.compile(r'\.(innerHTML|outerHTML|textContent|innerText)\s*\[', re.IGNORECASE),
    re.compile(r'\.(removeChild|appendChild|insertBefore|replaceChild)\s*\(', re.IGNORECASE),
    re.compile(r'\bdelete\s+', re.IGNORECASE),
    re.compile(r'\beval\s*\(', re.IGNORECASE),
    # new Function(...) 与 Function(...) 均可创建可执行代码（不加 new 也能调用）
    re.compile(r'\bnew\s+Function\s*\(', re.IGNORECASE),
    re.compile(r'\bFunction\s*\(', re.IGNORECASE),
    # setTimeout/setInterval 任意形式调用都拦（之前只拦字符串形式，箭头函数可绕过）
    re.compile(r'\bsetTimeout\s*\(', re.IGNORECASE),
    re.compile(r'\bsetInterval\s*\(', re.IGNORECASE),
    # 异步任务绕过：queueMicrotask / requestIdleCallback / Promise.then
    re.compile(r'\bqueueMicrotask\s*\(', re.IGNORECASE),
    re.compile(r'\brequestIdleCallback\s*\(', re.IGNORECASE),
    re.compile(r'Promise\.resolve\s*\(\s*\)\s*\.then', re.IGNORECASE),
    # 属性重定义绕过：Object.defineProperty(document, 'cookie', ...)
    re.compile(r'Object\.defineProperty\s*\(', re.IGNORECASE),
    # postMessage / WebSocket / EventSource 等网络发送通道
    re.compile(r'\bpostMessage\s*\(', re.IGNORECASE),
    re.compile(r'\bnew\s+WebSocket\s*\(', re.IGNORECASE),
    re.compile(r'\bnew\s+EventSource\s*\(', re.IGNORECASE),
    # window.open / document.location = 等导航写入
    re.compile(r'window\.open\s*\(', re.IGNORECASE),
    re.compile(r'document\.location\s*=(?![=])', re.IGNORECASE),
]

# 只读白名单（即使命中写正则也允许的表达式，目前为空，留作扩展）
_READ_ALLOWLIST = set()


def _normalize_expression(expression: str) -> str:
    """对表达式做归一化以对抗简单绕过：

    1. 去除 /* ... */ 块注释和 // 行注释（避免 `location./* */href =` 绕过）
    2. 还原 \\uXXXX / \\xXX 转义（避免 `location\\u002ehref` 绕过）

    返回归一化后的表达式。原表达式不动，仅用于写入检测。
    """
    if not expression:
        return expression
    # 去注释
    expr = re.sub(r'/\*.*?\*/', '', expression, flags=re.DOTALL)
    expr = re.sub(r'//[^\n]*', '', expr)

    # 还原 \uXXXX / \xXX 转义
    def _unescape(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)

    expr = re.sub(r'\\u([0-9a-fA-F]{4})', _unescape, expr)
    expr = re.sub(r'\\x([0-9a-fA-F]{2})', _unescape, expr)
    return expr


def _is_write_expression(expression: str) -> tuple[bool, str | None]:
    """P0-3：判定表达式是否包含写操作。

    Returns: (is_write, matched_pattern)
    - is_write=True 时 matched_pattern 为触发的正则 pattern 字符串
    - is_write=False 时 matched_pattern 为 None

    设计原则：
    1. 赋值用 `=(?![=])` 排除 == !== >= <= 等比较运算符
    2. 允许 location.href/pathname/search/hash/origin/host/hostname/port/protocol 读取
    3. 允许 document.title/referrer/URL/domain/readyState 读取（但赋值拦截）
    4. 函数调用（fetch/setItem/removeChild 等）按调用括号匹配
    5. 对抗绕过：normalize 后再匹配（去注释、还原 \\uXXXX/\\xXX 转义）；
       方括号属性访问、Function/setTimeout(arrow) 等已知绕过路径单列正则
    """
    normalized = _normalize_expression(expression)
    for pat in _WRITE_PATTERNS_RE:
        if pat.search(normalized):
            return True, pat.pattern
    return False, None


@router.post("/evaluate", response_model=BrowserEvaluateResponse, operation_id="browser_evaluate")
async def browser_evaluate(req: BrowserEvaluateRequest):
    """Execute read-only JavaScript in the page context.

    Use for quick reads that snapshot/action can't express — e.g.
    document.readyState, document.title, window.scrollY, computed styles,
    or aggregating data attributes across elements.

    The server blocks obvious write patterns (location/document.cookie/
    localStorage/fetch/XHR/innerHTML=) but cannot guarantee full sandboxing.
    Agents should only use this for reads.

    Example — get readyState and title:
      browser_evaluate(expression="JSON.stringify({state: document.readyState, title: document.title})")
    Example — get all data-id attributes from cards:
      browser_evaluate(expression="JSON.stringify([...document.querySelectorAll('[data-id]')].map(e => e.dataset.id))")
    """
    # P0-3 修复：用正则区分读/写，避免 location.href 等只读表达式被误判
    is_write, matched = _is_write_expression(req.expression)
    if is_write:
        return BrowserEvaluateResponse(
            success=False, elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="act",
                debug_detail=f"expression contains blocked write pattern: {matched}",
                elapsed_ms=0,
            ),
        )

    import time as _time
    start = _time.perf_counter()

    # 优先用持久 session（热态 <250ms）
    if req.session_id:
        from .session.manager import get_session_manager
        mgr = await get_session_manager()
        sess = mgr.get_session(req.session_id)
        page = await mgr.get_page(req.session_id) if sess else None
        if page is None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserEvaluateResponse(
                success=False, elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="TAB_NOT_FOUND",
                    error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                    phase="connect",
                    debug_detail=f"session_id {req.session_id} 不存在或已失效",
                    elapsed_ms=elapsed,
                ),
            )
        # 2026-08-06 Ticket 03：dialog 阻塞预检
        from .dialog_endpoints import _check_dialog_block
        block = _check_dialog_block(sess)
        if block is not None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserEvaluateResponse(
                success=False, elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="BLOCKED_BY_DIALOG",
                    error_message=BROWSER_ERROR_CODES["BLOCKED_BY_DIALOG"],
                    phase="act",
                    debug_detail="操作被 pending dialog 阻塞，请先调 /browser/handle_dialog 处理",
                    elapsed_ms=elapsed,
                ),
                blocked_by_dialog=True,
                blocking_dialog=block["blocking_dialog"],
            )
        try:
            if req.return_json:
                wrapped = '(function(){ try { return JSON.stringify(eval(' + repr(req.expression) + ')); } catch(e){ return "ERROR:" + e.message; } })()'
            else:
                wrapped = '(function(){ try { return String(eval(' + repr(req.expression) + ')); } catch(e){ return "ERROR:" + e.message; } })()'
            result = await page.evaluate(wrapped)
            if isinstance(result, str) and result.startswith("ERROR:"):
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserEvaluateResponse(
                    success=False, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="EXECUTION_ERROR",
                        error_message=BROWSER_ERROR_CODES["EXECUTION_ERROR"],
                        phase="act",
                        debug_detail=result[6:],
                        elapsed_ms=elapsed,
                    ),
                )
            text = result if isinstance(result, str) else str(result)
            if len(text) > req.max_length:
                text = text[:req.max_length] + '...[truncated]'
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserEvaluateResponse(success=True, value=text, elapsed_ms=elapsed)
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "act", elapsed)
            return BrowserEvaluateResponse(success=False, elapsed_ms=elapsed, error=err)

    # 无 session：走 exec_python 子进程模型（冷启动）
    # 修复幽灵参数：无会话模式支持 tab_id 精确消歧。
    _tab_id_url = None
    if req.tab_id:
        from .routes import _resolve_tab_id_to_url
        _tab_id_url = await asyncio.to_thread(_resolve_tab_id_to_url, req.tab_id)
        if _tab_id_url is None:
            _elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserEvaluateResponse(
                success=False, elapsed_ms=_elapsed, error=BrowserErrorResponse(
                    error_code="TAB_NOT_FOUND",
                    error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                    phase="locate",
                    debug_detail=f"tab_id {req.tab_id} 无法解析为 URL（可能已关闭或调试浏览器未运行）",
                    elapsed_ms=_elapsed,
                ),
            )
    params = {
        "url_pattern": req.url_pattern,
        "tab_id": _tab_id_url,
        "expression": req.expression,
        "return_json": req.return_json,
        "max_length": req.max_length,
    }
    body = """
async def main():
    async with async_playwright() as p:
        browser = await asyncio.wait_for(
            p.chromium.connect_over_cdp(_cdp_endpoint()), timeout=10
        )
        context = browser.contexts[0]
        page, _tab_err = await _find_page(context, _PARAMS["url_pattern"], _PARAMS.get("tab_id"))
        if _tab_err:
            print(f'ERROR:{_tab_err}')
            return
        if not page:
            print('ERROR:TAB_NOT_FOUND')
            return
        try:
            if _PARAMS["return_json"]:
                wrapped = '(function(){ try { return JSON.stringify(eval(' + repr(_PARAMS["expression"]) + ')); } catch(e){ return "ERROR:" + e.message; } })()'
            else:
                wrapped = '(function(){ try { return String(eval(' + repr(_PARAMS["expression"]) + ')); } catch(e){ return "ERROR:" + e.message; } })()'
            result = await page.evaluate(wrapped)
            if isinstance(result, str) and result.startswith("ERROR:"):
                print(f'ERROR:{result[6:]}')
                return
            text = result if isinstance(result, str) else str(result)
            if len(text) > _PARAMS["max_length"]:
                text = text[:_PARAMS["max_length"]] + '...[truncated]'
            print('OK:' + text)
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, 30)
    if success:
        return BrowserEvaluateResponse(
            success=True, value=message, elapsed_ms=elapsed,
        )
    err = _classify_error(message, "act", elapsed)
    return BrowserEvaluateResponse(success=False, elapsed_ms=elapsed, error=err)
