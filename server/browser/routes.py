"""浏览器基础路由：router 定义 + 共享 helper + 基础端点。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 03）。
代码与原定义完全一致，仅调整 import 路径：
- error_model / playwright_executor / session.manager 从本包子模块 import
- get_browser_config：`from .config import` → `from ..config import`（包嵌套层级变化）

迁移内容：
- router 定义（APIRouter(prefix="/browser", tags=["浏览器"])）
- 共享 helper：_cdp_endpoint（来自 playwright_executor）、_shot_dir、_new_shot_path、
  _resolve_tab_id_to_url、_get_session_page
- 基础端点：browser_status / browser_list_tabs / browser_navigate /
  browser_screenshot / browser_console_logs
- 相关 Pydantic 模型

策略说明（Ticket 03）：
本文件定义新的 router 对象并注册基础端点，但主 app 仍使用原 server/browser.py
的 router（通过 __init__.py exec 加载）。Ticket 04 会把本文件的 router 注册到
主 app，届时基础端点从此处提供；Ticket 06 删除 browser.py 后本文件成为唯一定义。
"""

import asyncio
import json
import logging
import os
import tempfile
import uuid

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema

from ..config import get_browser_config
from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse, _classify_error
from .playwright_executor import (
    _build_playwright_script,
    _cdp_endpoint,
    _execute_playwright,
)

# 注意：get_session_manager 不在顶部 import。session.manager 在顶部 import 会与
# site_lessons.py（Ticket 05 追加的端点模块）形成循环导入
# （routes → session.manager → site_lessons → routes.router，此时 router 尚未定义）。
# 改为在 _get_session_page / browser_console_logs 等函数内延迟 import，与
# action_endpoints / wait_endpoints / snapshot_endpoints 的模式一致。
logger = logging.getLogger("localagent.browser")
router = APIRouter(prefix="/browser", tags=["浏览器"])


# ========== 共享 helper ==========

def _shot_dir() -> str:
    """浏览器 VL 反馈截图临时目录（不在项目 temp/ 下，避免污染 git 工作树）。"""
    d = os.path.join(tempfile.gettempdir(), "localagent_browser_shots")
    os.makedirs(d, exist_ok=True)
    return d


def _new_shot_path() -> str:
    return os.path.join(_shot_dir(), f"shot_{uuid.uuid4().hex}.png")


def _resolve_tab_id_to_url(tab_id: str) -> str | None:
    """P1-4 修复：把 CDP target id 解析为 URL（通过 HTTP /json/list）。

    legacy endpoint 收到 tab_id 时调用此函数，把 target id 转成 URL 后
    作为精确匹配传给子进程的 _find_page(tab_id=url)，避免子串匹配歧义。

    T07：同步 urllib.request 改 httpx.Client（连接池复用）；本函数为同步 helper，
    被 sync legacy endpoint 调用，不在 async 端点直接路径上。

    Returns: URL 字符串；tab_id 不存在或浏览器未运行时返回 None。
    """
    if not tab_id:
        return None
    try:
        from lib.async_http import get_sync_client
        port = get_browser_config()["debug_port"]
        resp = get_sync_client().get(f"http://127.0.0.1:{port}/json/list", timeout=3)
        all_targets = resp.json()
        for t in all_targets:
            if t.get("type") == "page" and t.get("id") == tab_id:
                return t.get("url", "")
    except Exception as e:
        logger.warning("_resolve_tab_id_to_url 失败: %s", e)
    return None


async def _get_session_page(session_id: str):
    """获取持久 session 的 Page 句柄。无 session/已失效返回 None。"""
    if not session_id:
        return None
    from .session.manager import get_session_manager
    mgr = await get_session_manager()
    return await mgr.get_page(session_id)


# ========== 请求/响应模型 ==========

class BrowserListResponse(BaseSchema):
    """浏览器标签页列表响应。

    每个标签包含稳定标识符（评估文档 P1 修复）：
    - target_id: CDP target id（稳定，跨调用可复用，作为 tab_id 使用）
    - opener_target_id: 通过 target=_blank 打开时的源标签 target id
    - opener_url: 源标签 URL（解析 opener_target_id 得到，方便 agent 理解）
    - attached: 是否被 DevTools 附着
    """
    success: bool
    tabs: list[dict]


class BrowserStatusResponse(BaseSchema):
    """浏览器状态响应。

    标签统计口径修复（评估文档 7.4 节）：
    - target_count: CDP /json/list 返回的所有 target 数（含 page/service_worker/iframe 等）
    - page_count: 仅 type=="page" 的数量（与 /browser/tabs 返回条数一致）
    - context_count: Playwright browser.contexts 数量（需 Playwright；HTTP 模式下=None）
    - tab_count: 向后兼容字段，等于 page_count（旧调用方仍可用）

    原行为只返回 tab_count，但 status 统计的是 CDP target，tabs 只列 page 条目，
    容易让 agent 误判。新字段明确区分三种口径。

    2026-08-06 Ticket 03 扩展（spec: browser-dialog-handling 主动查询通道）：
    - has_pending_dialog: 是否有任意 session 被 dialog 阻塞（聚合所有 session）
    - pending_dialog_count: 阻塞中的 dialog 总数
    - pending_dialogs: 阻塞 dialog 列表（含 session_id/type/message/page_url/ts）
    """
    connected: bool
    debug_port: int
    tab_count: int | None = None  # 向后兼容（=page_count）
    target_count: int | None = None  # 所有 CDP target 数
    page_count: int | None = None  # 仅 type=="page"
    context_count: int | None = None  # Playwright contexts（HTTP 模式下 None）
    error: str | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞状态（主动查询通道）
    has_pending_dialog: bool = False
    pending_dialog_count: int = 0
    pending_dialogs: list[dict] = []


# ========== 基础端点 ==========

@router.get("/status", response_model=BrowserStatusResponse, operation_id="browser_status")
async def browser_status():
    """查询浏览器连接状态（轻量级，不使用playwright）

    如果 connected=false，说明调试浏览器未启动，agent 应自主启动：
    exec_python 运行 tools/browser/start_debug_browser.py
    不要问用户，不要打开用户的工作浏览器。

    标签统计口径修复（评估文档 7.4 节）：
    - target_count: CDP 所有 target 数（含 service_worker/iframe 等）
    - page_count: 仅 type=="page"（与 /browser/tabs 一致）
    - tab_count: 向后兼容，等于 page_count
    - context_count: HTTP 模式下 None（需 Playwright 才能获取 browser.contexts）

    2026-08-06 Ticket 03 扩展（主动查询通道）：
    - has_pending_dialog: 是否有任意 session 被 dialog 阻塞
    - pending_dialog_count / pending_dialogs: 阻塞 dialog 详情
    agent 在调操作端点（action/navigate/screenshot 等）前应先看 has_pending_dialog，
    若为 True 则先调 /browser/handle_dialog 处理 dialog，否则操作会被预检拦截。
    """
    cfg = get_browser_config()
    port = cfg["debug_port"]
    # 聚合所有 session 的 pending dialog 状态（即使浏览器断连也尝试读取 session 状态，
    # 让 agent 能感知残留 dialog；session 也会在 page 失效后自动清理）。
    # 直接读 _manager 全局变量（不调 async get_session_manager() 避免初始化新实例，
    # browser_status 保持轻量级、不连 CDP）。
    from .session import manager as _mgr_mod
    mgr = _mgr_mod._manager
    has_pending_dialog = False
    pending_dialog_list: list[dict] = []
    if mgr is not None:
        for s in mgr.iter_sessions():
            if s.has_pending_dialog():
                # 取第一个未 auto_dismissed 的 dialog
                for ev in s.pending_dialogs:
                    if not ev.get("auto_dismissed"):
                        pending_dialog_list.append({
                            "session_id": s.session_id,
                            "type": ev.get("type"),
                            "message": ev.get("message"),
                            "page_url": ev.get("page_url"),
                            "ts": ev.get("ts"),
                            "auto_dismissed": ev.get("auto_dismissed", False),
                        })
                        has_pending_dialog = True
                        break
    try:
        # T07：async 端点中的同步 urllib.request 换成模块级 httpx.AsyncClient 单例，
        # 避免阻塞事件循环 + 连接池复用（spec Implementation Decisions #3）
        from lib.async_http import get_async_client
        _client = get_async_client()
        await _client.get(f"http://127.0.0.1:{port}/json/version", timeout=2.0)
        # 尝试获取标签页数量
        tabs_resp = await _client.get(f"http://127.0.0.1:{port}/json/list", timeout=2.0)
        all_targets = tabs_resp.json()
        page_count = sum(1 for t in all_targets if t.get("type") == "page")
        target_count = len(all_targets)
        return BrowserStatusResponse(
            connected=True, debug_port=port,
            tab_count=page_count,  # 向后兼容
            target_count=target_count,
            page_count=page_count,
            context_count=None,  # HTTP 模式下无法获取，需 Playwright
            has_pending_dialog=has_pending_dialog,
            pending_dialog_count=len(pending_dialog_list),
            pending_dialogs=pending_dialog_list,
        )
    except Exception as e:
        # T07 回归修复：httpx.ConnectTimeout/ConnectError 的 str() 可能为空，
        # 补 type name 保证 error 字段非空（test_disconnected_returns_error_string 契约）
        err_msg = str(e) or f"{type(e).__name__}: 浏览器调试端口 {port} 不可达"
        return BrowserStatusResponse(
            connected=False, debug_port=port,
            error=err_msg,
            has_pending_dialog=has_pending_dialog,
            pending_dialog_count=len(pending_dialog_list),
            pending_dialogs=pending_dialog_list,
        )


@router.get("/tabs", response_model=BrowserListResponse, operation_id="browser_list_tabs")
async def browser_list_tabs():
    """列出当前浏览器所有标签页（通过CDP HTTP接口，不使用playwright）

    浏览器未运行时返回空列表（success=False），不抛 500，
    避免前端定时刷新刷屏日志。

    稳定标识符修复（评估文档 P1）：
    - target_id: CDP target id（稳定，跨调用可复用）
    - opener_target_id: target=_blank 时的源标签 target id
    - opener_url: 源标签 URL（自动解析）
    - attached: 是否被 DevTools 附着（近似 active）
    - type: target 类型（page/service_worker/...）；只返回 page 类型

    target_id 可用于 browser_close(tab_ids=[...]) 精确关闭，
    避免同 URL 多标签的歧义。
    """
    cfg = get_browser_config()
    port = cfg["debug_port"]
    try:
        # T07：async 端点中的同步 urllib.request 换成模块级 httpx.AsyncClient 单例，
        # 避免阻塞事件循环 + 连接池复用（spec Implementation Decisions #3）
        from lib.async_http import get_async_client
        _client = get_async_client()
        resp = await _client.get(f"http://127.0.0.1:{port}/json/list", timeout=3.0)
        tabs_data = resp.json()
        # 只保留 page 类型，但保留所有稳定字段
        page_targets = [t for t in tabs_data if t.get("type") == "page"]
        # 构建 target_id → url 映射，用于解析 opener_url
        url_by_target_id = {t.get("id", ""): t.get("url", "") for t in page_targets}
        tabs = []
        for t in page_targets:
            opener_id = t.get("openerId")
            tabs.append({
                "target_id": t.get("id", ""),
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "type": t.get("type", ""),
                "attached": bool(t.get("attached", False)),
                "opener_target_id": opener_id or "",
                "opener_url": url_by_target_id.get(opener_id, "") if opener_id else "",
            })
        return BrowserListResponse(success=True, tabs=tabs)
    except Exception:
        # 浏览器未运行或连接失败，返回空列表而非 500
        return BrowserListResponse(success=False, tabs=[])


# ---- close ----

class BrowserCloseRequest(BaseSchema):
    """关闭标签页请求。

    支持两种匹配方式（可同时使用，取并集）：
    - urls: URL 子串模糊匹配（向后兼容）
    - tab_ids: 精确 target id 列表（来自 browser_list_tabs 的 target_id）

    同 URL 多标签处理（仅 urls 匹配时生效）：
    - all_matches=False（默认）：每个 URL 模式只关闭第一个匹配标签，
      避免误关同域多个标签（评估文档 4.2/5 节"同 URL 多标签无法稳定区分"修复）
    - all_matches=True：批量关闭所有匹配标签（原行为）
    """
    urls: list[str] = []  # 模糊匹配（向后兼容）
    tab_ids: list[str] = []  # 精确 target id 列表
    all_matches: bool = False  # False=每模式只关第一个；True=关所有匹配


class BrowserResponse(BaseSchema):
    """用于表示浏览器操作响应的数据模型。

    功能：封装浏览器请求的执行结果，包含是否成功及提示信息。
    参数：
        success (bool): 指示操作是否成功。
        message (str): 与操作相关的详细消息或错误描述。
    返回值：此类的实例将作为浏览器响应的标准化结构。
    """
    success: bool  # 操作成功状态标志
    message: str  # 包含响应详情或错误信息


@router.post("/close", response_model=BrowserResponse, operation_id="browser_close")
async def browser_close(req: BrowserCloseRequest):
    """关闭匹配标签页（通过/exec/python执行playwright）

    支持两种匹配方式（可同时使用，取并集）：
    - urls: URL 子串模糊匹配（向后兼容）
    - tab_ids: 精确 target id 列表（推荐，避免歧义）

    同 URL 多标签处理（评估文档 P1 修复）：
    - all_matches=False（默认）：每个 URL 模式只关闭第一个匹配标签
    - all_matches=True：批量关闭所有匹配标签（原行为）

    返回逐标签关闭结果（data.closing_details），包含 target_id/url/status/error。
    """
    # 兼容旧调用：urls 为空且 tab_ids 也为空时返回明确错误
    if not req.urls and not req.tab_ids:
        raise HTTPException(status_code=400, detail="urls 和 tab_ids 至少提供一个")

    # 用户输入通过 JSON 序列化注入（json.dumps 二次转义确保安全），避免 f-string 代码注入
    params_literal = json.dumps(json.dumps(
        {"patterns": req.urls, "tab_ids": req.tab_ids, "all_matches": req.all_matches},
        ensure_ascii=False,
    ))
    cdp_url = _cdp_endpoint()
    code = f"""
import asyncio, json
from playwright.async_api import async_playwright

_PARAMS = json.loads({params_literal})
_CDP_URL = '{cdp_url}'

async def main():
    async with async_playwright() as p:
        browser = await asyncio.wait_for(
            p.chromium.connect_over_cdp(_CDP_URL), timeout=10
        )
        context = browser.contexts[0]
        details = []
        closed = 0
        # 1) 精确 tab_id 匹配（CDP target id == page 的 _impl_object_id 不直接等价，
        #    所以用 page.url + page title 等做二次确认；这里通过遍历 context.pages
        #    并用 page 的 target_id 属性匹配——Playwright Page 对象有 .target_id? 没有。
        #    退而求其次：tab_ids 通过 CDP HTTP /json/list 解析为 URL，再按 URL 精确匹配）
        # 实际方案：用 CDP HTTP /json/list 把 tab_id → url 解析出来，再按 URL 精确关闭。
        import urllib.request as _urllib_request
        try:
            _resp = _urllib_request.urlopen(_CDP_URL + '/json/list', timeout=3)
            _targets = json.loads(_resp.read().decode())
        except Exception:
            _targets = []
        _id_to_url = {{t.get('id', ''): t.get('url', '') for t in _targets if t.get('type') == 'page'}}
        _urls_to_close_exact = [_id_to_url.get(tid, '') for tid in _PARAMS['tab_ids']]
        _urls_to_close_exact = [u for u in _urls_to_close_exact if u]

        # 2) 收集所有匹配的 page（先按精确 URL，再按 patterns 模糊）
        matched_pages = []
        seen_ids = set()
        # 精确 URL 匹配（tab_ids 解析得到的 URL）
        for page in context.pages:
            if page.url in _urls_to_close_exact and id(page) not in seen_ids:
                matched_pages.append(page)
                seen_ids.add(id(page))
        # patterns 模糊匹配
        for pattern in _PARAMS['patterns']:
            if _PARAMS['all_matches']:
                # 批量：所有匹配
                for page in context.pages:
                    if pattern in page.url and id(page) not in seen_ids:
                        matched_pages.append(page)
                        seen_ids.add(id(page))
            else:
                # 仅第一个匹配
                for page in context.pages:
                    if pattern in page.url and id(page) not in seen_ids:
                        matched_pages.append(page)
                        seen_ids.add(id(page))
                        break
        # 逐个关闭并记录结果
        for page in matched_pages:
            info = {{"url": page.url, "status": "pending", "error": ""}}
            try:
                await page.close()
                info["status"] = "closed"
                closed += 1
            except Exception as e:
                info["status"] = "error"
                info["error"] = str(e)
            details.append(info)
        print(f'OK:closed={{closed}} details={{json.dumps(details, ensure_ascii=False)}}')

asyncio.run(main())
"""
    from ..exec import ExecRequest, exec_python
    result = await exec_python(ExecRequest(code=code))
    if result.success:
        # 解析关闭详情，附带在 message 中
        out = (result.stdout or "").strip()
        closed_count = 0
        if out.startswith("OK:"):
            payload = out[3:]
            # 形如 "closed=2 details=[...]"
            try:
                if "details=" in payload:
                    parts = payload.split("details=", 1)
                    closed_count = int(parts[0].replace("closed=", "").strip())
                    # details 已解析但不返回（保留关闭详情字段在 message 中较为冗长，
                    # 当前实现只返回 closed 数；如需 details 可扩展 BrowserResponse.data）
                else:
                    closed_count = int(payload.replace("closed=", "").strip())
            except (ValueError, json.JSONDecodeError):
                pass
        msg = f"已关闭 {closed_count} 个标签页"
        return BrowserResponse(success=True, message=msg)
    else:
        raise HTTPException(status_code=500, detail=f"浏览器操作失败: {result.error}")


# ---- console_logs ----

class BrowserConsoleLogsRequest(BaseSchema):
    """控制台日志请求（评估文档 P1 第 2 项）。

    收集页面 console.log/info/warn/error/debug 消息。

    有 session_id 时：直接读取该 session 的持久日志缓冲（跨调用累积），
    支持 since_cursor 增量返回。无 session_id 时：脚本生命周期内挂监听器，
    一次性抓取 capture_ms 毫秒的日志。

    - session_id: 持久 session id（推荐，支持增量返回）。不传则走一次性抓取模式
    - url_pattern: 无 session 时匹配标签页
    - level: 过滤级别（log/info/warning/error/debug/all）
    - filter: 关键字过滤（大小写不敏感）
    - capture_ms: 无 session 时抓取窗口（毫秒），默认 1000
    - since_cursor: 有 session 时从第 N 条开始返回（增量），默认 0=全部
    """
    session_id: str | None = None
    url_pattern: str | None = None
    tab_id: str | None = None  # 无会话模式：CDP target id 精确消歧
    level: str = "all"  # all/log/info/warning/error/debug
    filter: str | None = None
    capture_ms: int = 1000
    since_cursor: int = 0


class BrowserConsoleLogsResponse(BaseSchema):
    success: bool
    logs: list[dict]  # [{level, text, url, line, column, ts?}]
    # P1-2 修复：cursor 增量返回，避免 agent 重复读已读日志
    # - cursor: 本次返回的日志在缓冲区中的起始 index（= 请求的 since_cursor 或 0）
    # - next_cursor: 下次请求应传的 since_cursor（= 当前已读到的位置）
    # - dropped_count: 因缓冲区溢出累计被丢弃的日志数（仅 session 模式有意义）
    cursor: int = 0
    next_cursor: int = 0
    dropped_count: int = 0
    elapsed_ms: int
    error: BrowserErrorResponse | None = None


@router.post("/console_logs", response_model=BrowserConsoleLogsResponse, operation_id="browser_console_logs")
async def browser_console_logs(req: BrowserConsoleLogsRequest):
    """Capture page console logs for a capture window.

    Listens to console events for `capture_ms` milliseconds (default 1000) and
    returns matching entries. Use level/filter to narrow down — e.g. only errors,
    or messages containing a specific string.

    With session_id: reads the session's persistent log buffer (accumulated across
    calls since session creation) and supports since_cursor for incremental reads.
    Without session_id: one-shot capture for capture_ms milliseconds.

    Example — incremental read of all new logs since last cursor:
      browser_console_logs(session_id="abc123", since_cursor=42)
    Example — one-shot capture errors for 2 seconds (no session):
      browser_console_logs(url_pattern="app", level="error", capture_ms=2000)
    """
    valid_levels = {"all", "log", "info", "warning", "error", "debug"}
    if req.level not in valid_levels:
        return BrowserConsoleLogsResponse(
            success=False, logs=[], elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="act",
                debug_detail=f"level must be one of {sorted(valid_levels)}",
                elapsed_ms=0,
            ),
        )

    import time as _time
    start = _time.perf_counter()

    # 有 session：直接读取持久日志缓冲（增量）
    if req.session_id:
        from .session.manager import get_session_manager
        mgr = await get_session_manager()
        s = mgr.get_session(req.session_id)
        if s is None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserConsoleLogsResponse(
                success=False, logs=[], elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="TAB_NOT_FOUND",
                    error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                    phase="connect",
                    debug_detail=f"session_id {req.session_id} 不存在或已失效",
                    elapsed_ms=elapsed,
                ),
            )
        # 从 since_cursor 开始读取（P1-2: 增量返回 cursor/next_cursor/dropped_count）
        all_logs = s.console_logs
        cursor = max(0, req.since_cursor)
        # cursor 超过当前缓冲区长度时（缓冲已丢弃旧日志），从最新位置开始
        if cursor > len(all_logs):
            cursor = len(all_logs)
        new_logs = all_logs[cursor:]
        # 过滤 level + filter
        result_logs = []
        for log in new_logs:
            if req.level != "all" and log.get("level") != req.level:
                continue
            if req.filter and req.filter.lower() not in log.get("text", "").lower():
                continue
            result_logs.append(log)
        elapsed = int((_time.perf_counter() - start) * 1000)
        next_cursor = len(all_logs)
        s.console_log_cursor = next_cursor
        return BrowserConsoleLogsResponse(
            success=True, logs=result_logs, elapsed_ms=elapsed,
            cursor=cursor, next_cursor=next_cursor,
            dropped_count=s.console_logs_dropped_count,
        )

    # 无 session：一次性抓取模式
    # 修复幽灵参数：无会话模式支持 tab_id 精确消歧。
    _tab_id_url = None
    if req.tab_id:
        _tab_id_url = await asyncio.to_thread(_resolve_tab_id_to_url, req.tab_id)
        if _tab_id_url is None:
            _elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserConsoleLogsResponse(
                success=False, logs=[], elapsed_ms=_elapsed, error=BrowserErrorResponse(
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
        "level": req.level,
        "filter": req.filter,
        "capture_ms": req.capture_ms,
    }
    body = """
import json
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
        logs = []
        def on_console(msg):
            level = msg.type  # 'log', 'info', 'warning', 'error', 'debug'
            text = msg.text
            filt = _PARAMS.get("filter")
            if filt and filt.lower() not in text.lower():
                return
            want = _PARAMS["level"]
            if want != "all" and level != want:
                return
            logs.append({
                "level": level,
                "text": text,
                "url": msg.location.get("url", "") if hasattr(msg, "location") else "",
                "line": msg.location.get("lineNumber", 0) if hasattr(msg, "location") else 0,
                "column": msg.location.get("columnNumber", 0) if hasattr(msg, "location") else 0,
            })
        page.on("console", on_console)
        # 也捕获页面错误事件（uncaught exception）
        def on_pageerror(err):
            text = err.message
            filt = _PARAMS.get("filter")
            if filt and filt.lower() not in text.lower():
                return
            want = _PARAMS["level"]
            if want not in ("all", "error"):
                return
            logs.append({"level": "error", "text": text, "url": "", "line": 0, "column": 0})
        page.on("pageerror", on_pageerror)
        # 等待 capture_ms
        await asyncio.sleep(_PARAMS["capture_ms"] / 1000.0)
        print('OK:' + json.dumps(logs, ensure_ascii=False))

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, int((req.capture_ms / 1000) + 15))
    if success and message.startswith("["):
        try:
            logs = json.loads(message)
            return BrowserConsoleLogsResponse(success=True, logs=logs, elapsed_ms=elapsed)
        except json.JSONDecodeError:
            pass
    if success:
        # OK: 但不是 JSON 数组（可能空输出）
        if message == "OK:" or not message.startswith("["):
            return BrowserConsoleLogsResponse(success=True, logs=[], elapsed_ms=elapsed)
    err = _classify_error(message, "verify", elapsed)
    return BrowserConsoleLogsResponse(success=False, logs=[], elapsed_ms=elapsed, error=err)


# ---- navigate ----

class BrowserNavigateRequest(BaseSchema):
    """标签页导航请求（评估文档 P1 第 4 项）。

    支持的 action：
    - back: 后退
    - forward: 前进
    - reload: 重新加载
    - goto: 打开新 URL（在当前标签页内导航，不开新标签）

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则走 url_pattern 子进程模型
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）
    - force_beforeunload: 2026-08-06 Ticket 04 新增。action="reload" 时生效，
      True 则用 page.close(run_before_unload=True) 触发 beforeunload dialog
      （agent 之后调 handle_dialog 处理，accept 后页面关闭，agent 再调
      browser_navigate(action="goto", url=<原 url>) 重开同 URL）。
      False（默认）则走 page.reload()，不触发 beforeunload。
    """
    session_id: str | None = None
    url_pattern: str | None = None
    tab_id: str | None = None  # 无会话模式：CDP target id 精确消歧
    action: str  # back/forward/reload/goto
    url: str | None = None  # goto 用
    wait_until: str = "domcontentloaded"  # goto 用
    timeout: float = 30.0
    force_beforeunload: bool = False  # 2026-08-06 Ticket 04


class BrowserNavigateResponse(BaseSchema):
    success: bool
    action: str
    url_after: str | None = None
    # P1-1 修复：分离"导航已完成"与"后续等待是否超时"两个语义
    # - navigation_completed=True：浏览器已发起导航（URL 已变化或 reload 已触发）
    # - wait_timeout=True：导航已完成但后续 wait_until 事件超时（bfcache/SPA 场景常见）
    # agent 应优先看 navigation_completed 判断成功，wait_timeout 仅作辅助诊断
    navigation_completed: bool | None = None
    wait_timeout: bool | None = None
    elapsed_ms: int
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None
    # 2026-08-06 Ticket 04：beforeunload 触发标记
    # True 表示已发起 page.close(run_before_unload=True)，beforeunload dialog
    # 应在 session 队列中。agent 调 handle_dialog 处理后，accept 会使页面关闭
    # （需重新 browser_navigate(action="goto", url=url_after) 重开），
    # dismiss 则页面保留在 url_after。
    beforeunload_triggered: bool = False


@router.post("/navigate", response_model=BrowserNavigateResponse, operation_id="browser_navigate")
async def browser_navigate(req: BrowserNavigateRequest):
    """Navigate the active tab: back / forward / reload / goto.

    Replaces implicit reload-via-open. Use this for in-tab navigation that
    shouldn't open a new tab.

    Tip — 访问新网站前建议先调 browser_match_site(domain=...) 查询是否有已记录经验
    （已知 DOM 坑/反爬规则/选择器回退等），避免重复踩坑。session 模式下
    browser_session_create 会自动注入 site_lessons，无需手动查询。

    Example — reload current tab:
      browser_navigate(action="reload", url_pattern="bilibili")
    Example — go to a new URL in current tab:
      browser_navigate(action="goto", url="https://example.com", url_pattern="bilibili")
    """
    valid = {"back", "forward", "reload", "goto"}
    if req.action not in valid:
        return BrowserNavigateResponse(
            success=False, action=req.action, elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="act",
                debug_detail=f"action must be one of {sorted(valid)}",
                elapsed_ms=0,
            ),
        )
    if req.action == "goto" and not req.url:
        return BrowserNavigateResponse(
            success=False, action=req.action, elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="INVALID_SELECTOR",
                error_message=BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                phase="act",
                debug_detail="goto action requires url",
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
            return BrowserNavigateResponse(
                success=False, action=req.action, elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="TAB_NOT_FOUND",
                    error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                    phase="connect",
                    debug_detail=f"session_id {req.session_id} 不存在或已失效",
                    elapsed_ms=elapsed,
                ),
            )
        # 2026-08-06 Ticket 03：dialog 阻塞预检（被动反馈通道）
        # 有 pending dialog 时直接返回 blocked_by_dialog=True，不调 page 操作
        from .dialog_endpoints import _check_dialog_block
        block = _check_dialog_block(sess)
        if block is not None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserNavigateResponse(
                success=False, action=req.action, elapsed_ms=elapsed,
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
            timeout_ms = int(req.timeout * 1000)
            # 2026-08-06 Ticket 04：force_beforeunload 触发路径
            # action="reload" + force_beforeunload=True 时，跳过 page.reload()，
            # 改走 page.close(run_before_unload=True) 触发 beforeunload dialog。
            # 该调用会阻塞至 dialog 被 handle_dialog 或超时兜底处理，故用 asyncio
            # 后台任务执行；端点立即返回，agent 之后调 handle_dialog 决定 accept/dismiss。
            # - accept → 页面关闭，session 由 page.on("close") 监听器自动清理。
            #   agent 之后调 browser_navigate(action="goto", url=url_before) 重开同 URL。
            # - dismiss → 页面保留，session 仍有效；agent 可继续操作或自行 reload。
            if req.action == "reload" and req.force_beforeunload:
                url_before = page.url
                # 后台任务：触发 beforeunload 并阻塞等待处理
                async def _trigger_beforeunload():
                    try:
                        await page.close(run_before_unload=True)
                    except Exception as _bu_err:
                        logger.debug("force_beforeunload: page.close 异常 %s", _bu_err)

                from lib.async_utils import spawn_background_task
                spawn_background_task(_trigger_beforeunload(), name="browser_beforeunload")
                # 让事件循环有机会调度后台任务，使 dialog 能尽快入 session 队列
                await asyncio.sleep(0)
                # 再让一次，确保 listener 的 _on_dialog 异步 handler 有机会执行
                await asyncio.sleep(0)
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserNavigateResponse(
                    success=True, action=req.action, url_after=url_before,
                    elapsed_ms=elapsed,
                    navigation_completed=False,
                    wait_timeout=False,
                    beforeunload_triggered=True,
                )
            # P1-1 修复：分离 navigation_completed 与 wait_timeout
            # - back/forward/reload/goto 用 wait_until="commit" 触发导航（commit 不依赖 onload）
            # - 后续用 wait_for_load_state 等待 req.wait_until 事件，超时不视为失败
            url_before = page.url
            navigation_completed = False
            wait_timeout = False
            try:
                if req.action == "back":
                    await page.go_back(timeout=timeout_ms, wait_until="commit")
                elif req.action == "forward":
                    await page.go_forward(timeout=timeout_ms, wait_until="commit")
                elif req.action == "reload":
                    await page.reload(timeout=timeout_ms, wait_until="commit")
                elif req.action == "goto":
                    await page.goto(req.url or "", timeout=timeout_ms, wait_until="commit")
                navigation_completed = True
            except Exception as nav_err:
                # commit 超时但 URL 已变化，说明导航已完成（bfcache 场景）
                err_lower = str(nav_err).lower()
                if "timeout" in err_lower:
                    # P1-1 rerun3 修复：极端 timeout（如 1ms）下 URL 可能还没来得及变化，
                    # 但 back/forward 实际已被浏览器接收，会在几毫秒后完成。
                    # 追加短轮询（200ms 内每 20ms 检查一次 URL），如果 URL 变化则视为导航已完成。
                    if page.url != url_before:
                        navigation_completed = True
                        wait_timeout = True
                    else:
                        # 短轮询：200ms 内检查 URL 是否变化
                        for _ in range(10):
                            await asyncio.sleep(0.02)
                            if page.url != url_before:
                                navigation_completed = True
                                wait_timeout = True
                                break
                        if not navigation_completed:
                            # URL 确实没变化，是真失败
                            raise
                else:
                    raise
            # 后续等待 req.wait_until 事件（如 domcontentloaded/networkidle）
            # 超时不视为失败（agent 可用 navigation_completed 判断）
            if navigation_completed and req.wait_until != "commit":
                try:
                    await page.wait_for_load_state(req.wait_until, timeout=timeout_ms)  # type: ignore[arg-type]
                except Exception:
                    wait_timeout = True
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserNavigateResponse(
                success=True, action=req.action, url_after=page.url, elapsed_ms=elapsed,
                navigation_completed=navigation_completed, wait_timeout=wait_timeout,
            )
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "act", elapsed)
            return BrowserNavigateResponse(
                success=False, action=req.action, elapsed_ms=elapsed, error=err,
            )

    # 无 session：走 exec_python 子进程模型（冷启动）
    # 修复幽灵参数：无会话模式支持 tab_id 精确消歧。
    _tab_id_url = None
    if req.tab_id:
        _tab_id_url = await asyncio.to_thread(_resolve_tab_id_to_url, req.tab_id)
        if _tab_id_url is None:
            _elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserNavigateResponse(
                success=False, action=req.action, elapsed_ms=_elapsed, error=BrowserErrorResponse(
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
        "action": req.action,
        "url": req.url,
        "wait_until": req.wait_until,
        "timeout": req.timeout,
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
            action = _PARAMS["action"]
            timeout_ms = int(_PARAMS["timeout"] * 1000)
            # P1-1 修复：分离 navigation_completed 与 wait_timeout
            url_before = page.url
            navigation_completed = False
            wait_timeout = False
            try:
                if action == "back":
                    await page.go_back(timeout=timeout_ms, wait_until="commit")
                elif action == "forward":
                    await page.go_forward(timeout=timeout_ms, wait_until="commit")
                elif action == "reload":
                    await page.reload(timeout=timeout_ms, wait_until="commit")
                elif action == "goto":
                    await page.goto(_PARAMS["url"], timeout=timeout_ms, wait_until="commit")
                navigation_completed = True
            except Exception as nav_err:
                err_lower = str(nav_err).lower()
                if "timeout" in err_lower:
                    # P1-1 rerun3 修复：极端 timeout 下追加短轮询确认 URL 是否实际变化
                    if page.url != url_before:
                        navigation_completed = True
                        wait_timeout = True
                    else:
                        for _ in range(10):
                            await asyncio.sleep(0.02)
                            if page.url != url_before:
                                navigation_completed = True
                                wait_timeout = True
                                break
                        if not navigation_completed:
                            raise
                else:
                    raise
            if navigation_completed and _PARAMS["wait_until"] != "commit":
                try:
                    await page.wait_for_load_state(_PARAMS["wait_until"], timeout=timeout_ms)
                except Exception:
                    wait_timeout = True
            import json as _json
            print('OK:' + _json.dumps({
                "url": page.url,
                "navigation_completed": navigation_completed,
                "wait_timeout": wait_timeout,
            }, ensure_ascii=False))
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, int(req.timeout + 10))
    if success:
        # P1-1: 解析 JSON 形式的 OK 响应
        url_after = message
        nav_completed = True
        wait_timeout = False
        try:
            import json as _json
            if message.startswith("{"):
                data = _json.loads(message)
                url_after = data.get("url", message)
                nav_completed = data.get("navigation_completed", True)
                wait_timeout = data.get("wait_timeout", False)
        except Exception:
            pass
        return BrowserNavigateResponse(
            success=True, action=req.action, url_after=url_after, elapsed_ms=elapsed,
            navigation_completed=nav_completed, wait_timeout=wait_timeout,
        )
    err = _classify_error(message, "act", elapsed)
    return BrowserNavigateResponse(
        success=False, action=req.action, elapsed_ms=elapsed, error=err,
    )


# ---- screenshot (视口/全页/区域) ----

class BrowserScreenshotRequest(BaseSchema):
    """页面截图请求（评估文档 P1 第 3 项）。

    支持：
    - viewport: 仅视口
    - full_page: 整页（含滚动区域）
    - element: 指定 CSS 选择器的元素截图

    返回路径 + base64（默认 mcp_image_block=True 供 MCP 转 ImageContent）。
    REST 调用方传 return_base64=False 可只返回路径，避免大字符串进 LLM 上下文。

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则走 url_pattern 子进程模型
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）
    - tab_id: 无 session 时精确消歧用。传 browser_list_tabs 返回的 target_id，
      会经 CDP /json/list 解析为 URL 做精确匹配，避免多同域 tab 触发 AMBIGUOUS_TAB
      （修复问题②）。与 url_pattern 同时传时，tab_id 精确匹配优先。
    """
    session_id: str | None = None
    url_pattern: str | None = None
    tab_id: str | None = None  # 无 session 时精确消歧（browser_list_tabs 的 target_id）
    shot_type: str = "viewport"  # viewport/full_page/element
    selector: str | None = None  # element 用
    max_size: int = 1024  # 长边像素上限（压缩）
    quality: int = 85
    return_base64: bool = True  # False=只返回路径，避免大字符串进上下文


class BrowserScreenshotResponse(BaseSchema):
    success: bool
    path: str | None = None
    data: dict | None = None  # {image, mime_type, width, height, mcp_image_block}
    elapsed_ms: int
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


@router.post("/screenshot", response_model=BrowserScreenshotResponse, operation_id="browser_screenshot")
async def browser_screenshot(req: BrowserScreenshotRequest):
    """Take a screenshot: viewport, full_page, or specific element.

    Use full_page to capture long scrolling content (eval reports Browser Use
    had this and LocalAgent didn't). Use element for a single card/button.

    - return_base64=False returns only the file path (avoids LLM context bloat).
    - max_size caps the longest edge (default 1024) and JPEG-quality compresses.

    Example — full page screenshot:
      browser_screenshot(shot_type="full_page", url_pattern="bilibili", return_base64=False)
    """
    if req.shot_type == "element" and not req.selector:
        return BrowserScreenshotResponse(
            success=False, elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="INVALID_SELECTOR",
                error_message=BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                phase="act",
                debug_detail="element shot_type requires selector",
                elapsed_ms=0,
            ),
        )
    shot_path = _new_shot_path()

    # 优先用持久 session（热态 <250ms）
    if req.session_id:
        import time as _time
        start = _time.perf_counter()
        from .session.manager import get_session_manager
        mgr = await get_session_manager()
        sess = mgr.get_session(req.session_id)
        page = await mgr.get_page(req.session_id) if sess else None
        if page is None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserScreenshotResponse(
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
            return BrowserScreenshotResponse(
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
            if req.shot_type == "viewport":
                png = await page.screenshot(full_page=False)
            elif req.shot_type == "full_page":
                png = await page.screenshot(full_page=True)
            elif req.shot_type == "element":
                el = await page.wait_for_selector(req.selector or "", timeout=10000)
                assert el is not None  # 契约：wait_for_selector 成功返回非 None（超时抛异常走下方 except）
                png = await el.screenshot()
            with open(shot_path, 'wb') as f:
                f.write(png)
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "act", elapsed)
            return BrowserScreenshotResponse(success=False, elapsed_ms=elapsed, error=err)
        # 走与子进程模式相同的压缩 + base64 流程
        success, message, elapsed = True, shot_path, int((_time.perf_counter() - start) * 1000)
    else:
        import time as _time
        start = _time.perf_counter()
        # 修复问题②：无会话模式支持 tab_id 精确消歧。先把 CDP target id 解析为 URL，
        # 再作为精确匹配传给子进程 _find_page(tab_id=url)。
        _tab_id_url = None
        if req.tab_id:
            _tab_id_url = await asyncio.to_thread(_resolve_tab_id_to_url, req.tab_id)
            if _tab_id_url is None:
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserScreenshotResponse(
                    success=False, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="TAB_NOT_FOUND",
                        error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                        phase="locate",
                        debug_detail=f"tab_id {req.tab_id} 无法解析为 URL（可能已关闭或调试浏览器未运行）",
                        elapsed_ms=elapsed,
                    ),
                )
        params = {
            "url_pattern": req.url_pattern,
            "shot_type": req.shot_type,
            "selector": req.selector,
            "shot_path": shot_path,
            "tab_id": _tab_id_url,
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
            st = _PARAMS["shot_type"]
            if st == "viewport":
                png = await page.screenshot(full_page=False)
            elif st == "full_page":
                png = await page.screenshot(full_page=True)
            elif st == "element":
                el = await page.wait_for_selector(_PARAMS["selector"], timeout=10000)
                png = await el.screenshot()
            else:
                print(f'ERROR:unknown shot_type {st}')
                return
            with open(_PARAMS["shot_path"], 'wb') as f:
                f.write(png)
            print('OK:' + _PARAMS["shot_path"])
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
        code = _build_playwright_script(params, body)
        success, message, elapsed = await _execute_playwright(code, 30)
        if not success:
            err = _classify_error(message, "act", elapsed)
            return BrowserScreenshotResponse(success=False, elapsed_ms=elapsed, error=err)
    # 压缩 + 可选 base64
    try:
        import io as _io
        from pathlib import Path

        from PIL import Image
        image_path = Path(message)
        if not image_path.is_file():
            raise FileNotFoundError(f"截图文件不存在: {message}")
        img = Image.open(image_path)
        orig_w, orig_h = img.size
        scale = min(1.0, req.max_size / max(orig_w, orig_h))
        if scale < 1.0:
            img = img.resize((max(1, int(orig_w*scale)), max(1, int(orig_h*scale))), Image.Resampling.LANCZOS)
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        buf = _io.BytesIO()
        if has_alpha:
            img.convert("RGBA").save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            img.convert("RGB").save(buf, format="JPEG", quality=req.quality, optimize=True)
            mime = "image/jpeg"
        data = None
        if req.return_base64:
            import base64 as _b64
            data = {
                "image": _b64.b64encode(buf.getvalue()).decode("ascii"),
                "mime_type": mime,
                "width": img.size[0],
                "height": img.size[1],
                "mcp_image_block": True,
            }
        return BrowserScreenshotResponse(
            success=True, path=str(image_path), data=data, elapsed_ms=elapsed,
        )
    except Exception as e:
        return BrowserScreenshotResponse(
            success=False, elapsed_ms=elapsed,
            error=BrowserErrorResponse(
                error_code="EXECUTION_ERROR",
                error_message=BROWSER_ERROR_CODES["EXECUTION_ERROR"],
                phase="verify",
                debug_detail=f"图片处理失败: {e}",
                elapsed_ms=elapsed,
            ),
        )
    finally:
        try:
            from pathlib import Path
            Path(message).unlink(missing_ok=True)
        except Exception:
            pass


# ========== Ticket 04：触发新端点模块的 @router.post 注册 ==========
# 这些模块从本文件 import router 后用 @router.post(...) 注册端点。此处 import 触发
# 模块级装饰器执行，把端点注册到本 router。模块本身定义的端点函数仍为"复制"状态
# （原 server/browser.py / browser_action_endpoints.py / browser_wait_endpoints.py
# 中的端点仍由 __init__.py exec 加载的旧 router 提供），本 router 暂不被 main.py
# 使用；Ticket 06 删除原文件并切换 main.py import 后本 router 成为唯一定义。
#
# 循环导入安全性：本文件已在第 43 行定义 router，下面 import 时本文件已部分执行
# （router 已存在），新模块 `from .routes import router` 可正确拿到 router 引用。
from . import action_endpoints as _action_endpoints  # noqa: E402,F401

# 2026-08-06 新增（spec: browser-dialog-handling）：handle_dialog 端点
# 首次 import 触发 @router.post("/handle_dialog") 注册到本 router。
from . import dialog_endpoints as _dialog_endpoints  # noqa: E402,F401
from . import evaluate_endpoints as _evaluate_endpoints  # noqa: E402,F401

# Ticket 04 补迁：session_endpoints 和 stats 顶部 `from .routes import router` 触发
# @router.post/@router.get 注册（browser_session_create/list/close、browser_selector_stats）。
from . import session_endpoints as _session_endpoints  # noqa: E402,F401
from . import site_lessons as _site_lessons_endpoints  # noqa: E402,F401

# snapshot_endpoints 含 ARIA JS + compute 函数（Ticket 02）+ browser_snapshot 端点
# （Ticket 04 追加），首次 import 触发 @router.post("/snapshot") 注册到本 router。
from . import snapshot_endpoints as _snapshot_endpoints  # noqa: E402,F401
from . import stats as _stats_endpoints  # noqa: E402,F401

# Ticket 05：VL 反馈闭环 helper + 站点经验匹配/书签历史检索端点。
# vl_feedback 仅提供 _maybe_vl_feedback helper（不注册端点）；
# site_lessons 顶部 `from .routes import router` 触发 @router.post("/match_site")
# 和 @router.post("/find_url") 注册到本 router。此处 router 已定义，无循环导入。
from . import vl_feedback as _vl_feedback  # noqa: E402,F401
from . import wait_endpoints as _wait_endpoints  # noqa: E402,F401
