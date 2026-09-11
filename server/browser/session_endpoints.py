"""浏览器持久 session 端点：create / list / close。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 03）。
代码与原定义完全一致，仅调整 import 路径：
- get_session_manager：`from .browser_session import` → `from .session.manager import`

迁移内容：
- Pydantic 模型：BrowserSessionCreateRequest/Response、BrowserSessionListResponse、
  BrowserSessionCloseRequest/Response
- 端点函数：browser_session_create / browser_session_list / browser_session_close

策略说明（Ticket 04 补迁）：
本文件的端点函数通过 @router.post/@router.get 装饰器注册到 routes.py 的 router。
routes.py 末尾的 `from . import session_endpoints` 触发本模块加载，执行装饰器，
把端点注册到 router。Ticket 06 删除 browser.py 后本文件成为唯一定义。
"""

from fastapi import HTTPException

from lib.schema import BaseSchema

from .routes import router
from .session.manager import get_session_manager

# ========== 请求/响应模型 ==========

class BrowserSessionCreateRequest(BaseSchema):
    """创建持久浏览器会话。

    持久 session 解决"每次 DOM 操作都重新 connect_over_cdp + 启动子进程"
    导致的 0.5–1.4 秒固定开销。服务端维持长连接 Playwright Page 句柄，
    后续 snapshot/action/wait_for/evaluate 调用传入 session_id 即可复用，
    热态延迟 <250ms。

    - url_pattern: 复用现有 page（url 包含 pattern）
    - url: 新建 page 并 goto url（优先级低于 url_pattern）
    - 都不传：新建专用空白 tab（不复用已有页面，避免误绑用户真实 tab/扩展窗口页；
      2026-09-03 修复，详见 session/manager.py create_session 注释）

    返回 session_id（调用方持有）+ tab_id（CDP target id）+ url + title。
    session idle 超时（默认 900s = 15min，见 [browser].session_idle_timeout_secs）
    或 page 关闭时自动清理（idle 超时/LRU 淘汰时同时关闭浏览器 tab）。
    """
    url_pattern: str | None = None
    url: str | None = None


class BrowserSessionCreateResponse(BaseSchema):
    success: bool
    session_id: str
    tab_id: str
    # 修复问题①：create_session 返回 dict 含 tab_id_resolved，但本模型原缺该字段，
    # 因 BaseSchema extra="forbid" 触发 ValidationError → 端点 500。补齐字段即可。
    tab_id_resolved: bool = False  # tab_id 是否经 CDP /json/list 反查成功（跨调用定位可靠性）
    url: str
    title: str
    created: bool
    elapsed_ms: int
    # P2-1: 自动匹配的站点经验（评估文档 P2 第 1 项）
    site_lessons: list[dict] = []  # [{domain, aliases, body}]
    site_lessons_hint: str | None = None  # 简短提示，None=无 lessons


class BrowserSessionListResponse(BaseSchema):
    success: bool
    sessions: list[dict]
    elapsed_ms: int


class BrowserSessionCloseRequest(BaseSchema):
    session_id: str
    close_page: bool = True  # True=同时关闭浏览器标签（默认，避免 tab 累积）


class BrowserSessionCloseResponse(BaseSchema):
    success: bool
    closed: bool
    elapsed_ms: int


# ========== 端点函数（@router 装饰器注册到 routes.py 的 router） ==========

@router.post("/sessions", response_model=BrowserSessionCreateResponse, operation_id="browser_session_create")
async def browser_session_create(req: BrowserSessionCreateRequest):
    """Create a persistent browser session.

    Returns a session_id to pass to snapshot/action/wait_for/evaluate for hot-path
    reuse (~250ms vs 0.5–1.4s cold start). The server holds a long-lived Playwright
    Page handle; idle sessions are cleaned up after `session_idle_timeout_secs`
    (default 900s = 15min, page also closed on cleanup).

    Use this at the start of a multi-step browser task, then pass session_id to
    subsequent calls. Fall back to no-session mode if the session expires.

    Tip — 创建 session 时会自动按 URL 匹配并注入 site_lessons（如有），无需手动查询。
    新网站经验可用 browser_write_lesson 写入 sites/<domain>.md，下次访问自动加载。

    Example — attach to an existing bilibili tab:
      browser_session_create(url_pattern="bilibili.com")
    Example — open a fresh tab to a URL:
      browser_session_create(url="https://www.bilibili.com")
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    try:
        info = await mgr.create_session(url_pattern=req.url_pattern, url=req.url)
        elapsed = int((_time.perf_counter() - start) * 1000)
        return BrowserSessionCreateResponse(
            success=True, elapsed_ms=elapsed, **info,
        )
    except Exception as e:
        elapsed = int((_time.perf_counter() - start) * 1000)
        raise HTTPException(
            status_code=500,
            detail=f"创建会话失败: {e} (elapsed_ms={elapsed})",
        ) from e


@router.get("/sessions", response_model=BrowserSessionListResponse, operation_id="browser_session_list")
async def browser_session_list():
    """List all active browser sessions.

    Each entry includes session_id, tab_id, url, title, last_used_at, idle_secs,
    and console_logs_buffered (number of logs captured since session start).
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    sessions = mgr.list_sessions()
    elapsed = int((_time.perf_counter() - start) * 1000)
    return BrowserSessionListResponse(success=True, sessions=sessions, elapsed_ms=elapsed)


@router.post("/sessions/close", response_model=BrowserSessionCloseResponse, operation_id="browser_session_close")
async def browser_session_close(req: BrowserSessionCloseRequest):
    """Close a browser session.

    - close_page=True (default): also close the browser tab, avoiding tab
      accumulation in Chrome. Use this when done with the page.
    - close_page=False: only drop the session handle; the browser tab stays
      open (use this when you want to detach but keep the page alive for reuse).

    Always call this when done with a session to free server resources, even
    though idle sessions auto-expire after session_idle_timeout_secs.
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    closed = await mgr.close_session(req.session_id, close_page=req.close_page)
    elapsed = int((_time.perf_counter() - start) * 1000)
    return BrowserSessionCloseResponse(success=True, closed=closed, elapsed_ms=elapsed)
