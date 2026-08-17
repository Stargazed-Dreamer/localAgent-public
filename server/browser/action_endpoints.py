"""browser_action 端点 — 统一浏览器动作 API（click/fill/select/check/press/hover 等）。

从 server/browser_action_endpoints.py 迁移至 server/browser/ 包（Ticket 04）。
代码与原定义完全一致，仅调整 import 路径：
- BROWSER_ERROR_CODES / BrowserErrorResponse / _classify_error：从 server.browser.error_model import
- _build_playwright_script / _execute_playwright：从 server.browser.playwright_executor import
- router：从 server.browser.routes import
- build_site_lessons_hint：从 server.browser.site_lessons import
- get_session_manager：从 server.browser.session.manager import
- compute_dom_hash：从 server.browser.snapshot_endpoints import
- record_selector_call：从 server.browser.stats import

迁移内容：
- BrowserActionTarget / BrowserActionRequest / BrowserActionResponse 模型
- browser_action 端点

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import asyncio
import json

from lib.schema import BaseSchema

# 从本包各子模块复用共享件（router / 错误模型 / playwright 子进程执行器 / 辅助函数）
from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse, _classify_error
from .playwright_executor import _build_playwright_script, _execute_playwright
from .routes import router


class BrowserActionTarget(BaseSchema):
    """元素定位目标（评估文档 P0 第 3 项）。

    支持的定位方式（按优先级）：
    - node_id: 来自 snapshot 的 node_id（P2-2 新增，仅 session 模式可用，
      自动从 sess.last_snapshot_nodes 反查 css_path）
    - css: CSS 选择器（如 "button.submit", "#login-btn"）
    - role + name: 角色 + 名称（如 role="button", name="搜索"）
    - label: 关联 label 的输入框（getByLabel）
    - placeholder: 占位符文本（getByPlaceholder）
    - testid: data-testid 属性值
    - text: 可见文本（exact=False 默认包含匹配）

    至少提供一个。同时提供多个时，按 node_id > css > role+name > label >
    placeholder > testid > text 的优先级使用第一个非空字段。

    P2-2 STALE_NODE 语义：
    - 使用 node_id 时，服务端会检查 sess.last_snapshot_url 与当前 page.url 是否一致
    - URL 变化或 snapshot 过期（>60s）时返回 STALE_NODE，agent 应重新 snapshot
    - P1-2 rerun3 新增：verify_dom_freshness=True 时，即使 URL 没变且未过期，
      也会重算 dom_hash 比对，感知同 URL 下的 DOM mutation（如 SPA 路由/局部刷新）
    """
    node_id: int | None = None  # P2-2: 来自 snapshot 的 node_id（仅 session 模式）
    css: str | None = None
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    testid: str | None = None
    text: str | None = None
    text_exact: bool = False  # text 是否精确匹配
    nth: int | None = None  # 多个匹配时取第几个（0-based）；None=严格模式（多个匹配时报错）
    # P1-2 rerun3: 同 URL 下 DOM mutation 感知（默认 False 保持热路径性能）
    verify_dom_freshness: bool = False


class BrowserActionRequest(BaseSchema):
    """统一浏览器动作请求（评估文档 P0 第 3 项）。

    支持的 action：
    - click: 单击（button: left/right/middle）
    - double_click: 双击
    - fill: 设置输入框值（替换原值；clear_first=True 先清空）
    - type: 逐字符输入（模拟键盘，触发更多事件）
    - press: 按键（如 "Enter", "Escape", "ArrowDown"）
    - select: 原生 select 选项（按 value 或 label）
    - check / uncheck: 复选框勾选/取消
    - hover: 鼠标悬停
    - scroll_into_view: 滚动到元素可见

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则走 url_pattern 子进程模型
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）

    返回 action 前后 URL、匹配数量、动作耗时和错误码。
    """
    session_id: str | None = None
    url_pattern: str | None = None
    target: BrowserActionTarget
    action: str  # click/double_click/fill/type/press/select/check/uncheck/hover/scroll_into_view
    # action 参数（按 action 类型使用）
    value: str | None = None  # fill/type 的值；select 的 value
    label: str | None = None  # select 按 label 选择时的标签文本
    key: str | None = None  # press 的按键
    button: str = "left"  # click 的按钮
    clear_first: bool = True  # fill 时先清空
    timeout: float = 10.0
    wait_after: float = 0.5  # 动作后等待（保留兼容；推荐改用 browser_wait_for）


class BrowserActionResponse(BaseSchema):
    """统一浏览器动作响应。

    - matched_count: target 匹配到的元素数（0=未找到；>1=潜在歧义，nth 或严格模式会报错）
    - url_before / url_after: 动作前后页面 URL（检测跳转）
    - site_lessons_hint: P2-1 站点经验提示（仅 session 有 lessons 时；None=无）
    - elapsed_ms: 总耗时
    - blocked_by_dialog: 2026-08-06 Ticket 03：dialog 阻塞预检反馈
    - blocking_dialog: 阻塞 dialog 详情（含 session_id/type/message/page_url/ts）
    """
    success: bool
    action: str
    matched_count: int
    url_before: str | None = None
    url_after: str | None = None
    elapsed_ms: int
    site_lessons_hint: str | None = None
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


@router.post("/action", response_model=BrowserActionResponse, operation_id="browser_action")
async def browser_action(req: BrowserActionRequest):
    """Unified browser action endpoint (covers click/fill/select/check/press/hover/etc).

    Replaces the narrow click_element/fill_input routes for new code. Target can be
    CSS, role+name, label, placeholder, testid, or text — agents pick whichever is
    most stable for the page.

    Supported actions: click, double_click, fill, type, press, select, check,
    uncheck, hover, scroll_into_view.

    Returns matched_count + url_before/url_after so agents can detect navigation
    and disambiguate multiple matches without a separate status call.

    Tip — 操作前若已知该网站有 site_lessons（见响应 site_lessons_hint），请参考已知 DOM 坑/
    反爬规则；首次访问新网站建议先调 browser_match_site(domain=...) 查询经验。新踩坑可用
    browser_write_lesson 写入供下次访问使用。

    Example — select "beta" in a dropdown:
      browser_action(action="select", target={"css":"select#lang"}, value="beta")
    Example — click the "搜索" button by role+name:
      browser_action(action="click", target={"role":"button","name":"搜索"})
    Example — press Enter in a search box:
      browser_action(action="press", target={"css":"input[name='q']"}, key="Enter")
    """
    valid_actions = {
        "click", "double_click", "fill", "type", "press",
        "select", "check", "uncheck", "hover", "scroll_into_view",
    }
    if req.action not in valid_actions:
        return BrowserActionResponse(
            success=False, action=req.action, matched_count=0, elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="act",
                debug_detail=f"action must be one of {sorted(valid_actions)}",
                elapsed_ms=0,
            ),
        )

    import time as _time
    start = _time.perf_counter()

    # 优先用持久 session（热态 <250ms）
    if req.session_id:
        from .session.manager import get_session_manager
        from .site_lessons import build_site_lessons_hint
        mgr = await get_session_manager()
        sess = mgr.get_session(req.session_id)
        page = await mgr.get_page(req.session_id) if sess else None
        if page is None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserActionResponse(
                success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
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
            return BrowserActionResponse(
                success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
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
        # P2-1: 附带站点经验提示
        lessons_hint = build_site_lessons_hint(sess.site_lessons) if sess else None
        # P2-3: 确定 target_type/value 用于记录统计
        from .stats import record_selector_call
        t = req.target
        # P2-2: node_id 优先（仅 session 模式，需 snapshot 缓存）
        if t.node_id is not None:
            if sess is None or not sess.last_snapshot_nodes:
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="STALE_NODE",
                        error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                        phase="locate",
                        debug_detail="node_id 需要 session 且已 snapshot；请先调用 browser_snapshot",
                        elapsed_ms=elapsed,
                    ),
                )
            # 检查 snapshot 是否过期（URL 变化或 >60s）
            _now = _time.time()
            if sess.last_snapshot_url != page.url or (_now - sess.last_snapshot_at) > 60.0:
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="STALE_NODE",
                        error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                        phase="locate",
                        debug_detail=(
                            f"snapshot 已过期（url_changed={sess.last_snapshot_url != page.url}, "
                            f"age={int(_now - sess.last_snapshot_at)}s）；请重新 snapshot"
                        ),
                        elapsed_ms=elapsed,
                    ),
                )
            # P1-2 rerun3：可选 DOM mutation 感知
            # agent 显式 verify_dom_freshness=True 时，重算 dom_hash 比对，
            # 感知同 URL 下的 DOM mutation（SPA 路由/局部刷新等场景）。
            # 默认 False 保持热路径性能（仅 URL+60s 校验）。
            if t.verify_dom_freshness and sess.last_snapshot_dom_hash:
                from .snapshot_endpoints import compute_dom_hash
                current_dom_hash = await compute_dom_hash(page)
                if current_dom_hash and current_dom_hash != sess.last_snapshot_dom_hash:
                    elapsed = int((_time.perf_counter() - start) * 1000)
                    return BrowserActionResponse(
                        success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
                        error=BrowserErrorResponse(
                            error_code="STALE_NODE",
                            error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                            phase="locate",
                            debug_detail=(
                                "DOM 已变化（同 URL 下 mutation 检测命中）；"
                                f"snapshot_hash={sess.last_snapshot_dom_hash[:32]}..., "
                                f"current_hash={current_dom_hash[:32]}...；请重新 snapshot"
                            ),
                            elapsed_ms=elapsed,
                        ),
                    )
            # 反查 node_id → css_path
            _matched_node = None
            for n in sess.last_snapshot_nodes:
                if n.get("node_id") == t.node_id:
                    _matched_node = n
                    break
            if not _matched_node or not _matched_node.get("css_path"):
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="STALE_NODE",
                        error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                        phase="locate",
                        debug_detail=f"node_id={t.node_id} 不在最近 snapshot 中或无 css_path",
                        elapsed_ms=elapsed,
                    ),
                )
            # 把 css_path 作为 CSS selector
            _t_type, _t_val = "node_id", f"#{t.node_id}:{_matched_node['css_path']}"
            t = t.model_copy(update={"css": _matched_node["css_path"], "node_id": None})
        elif t.css:
            _t_type, _t_val = "css", t.css
        elif t.role:
            _t_type, _t_val = "role", f"{t.role}:{t.name or ''}"
        elif t.label:
            _t_type, _t_val = "label", t.label
        elif t.placeholder:
            _t_type, _t_val = "placeholder", t.placeholder
        elif t.testid:
            _t_type, _t_val = "testid", t.testid
        elif t.text:
            _t_type, _t_val = "text", t.text
        else:
            _t_type, _t_val = "none", ""
        try:
            loc = None
            if t.css:
                loc = page.locator(t.css)
            elif t.role:
                loc = page.get_by_role(t.role, name=t.name) if t.name else page.get_by_role(t.role)
            elif t.label:
                loc = page.get_by_label(t.label)
            elif t.placeholder:
                loc = page.get_by_placeholder(t.placeholder)
            elif t.testid:
                loc = page.get_by_test_id(t.testid)
            elif t.text:
                loc = page.get_by_text(t.text, exact=t.text_exact)
            else:
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=0, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="INVALID_SELECTOR",
                        error_message=BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                        phase="locate",
                        debug_detail="no target field provided",
                        elapsed_ms=elapsed,
                    ),
                )
            matched = await loc.count()
            if matched == 0:
                elapsed = int((_time.perf_counter() - start) * 1000)
                record_selector_call(
                    url=page.url, target_type=_t_type, target_value=_t_val,
                    action=req.action, success=False,
                    error_code="ELEMENT_NOT_FOUND", elapsed_ms=elapsed,
                )
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=0,
                    url_before=page.url, url_after=page.url, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="ELEMENT_NOT_FOUND",
                        error_message=BROWSER_ERROR_CODES["ELEMENT_NOT_FOUND"],
                        phase="locate",
                        debug_detail="count() == 0",
                        elapsed_ms=elapsed,
                    ),
                )
            if t.nth is not None:
                loc = loc.nth(t.nth)
            elif matched > 1:
                elapsed = int((_time.perf_counter() - start) * 1000)
                record_selector_call(
                    url=page.url, target_type=_t_type, target_value=_t_val,
                    action=req.action, success=False,
                    error_code="MULTIPLE_MATCHES", elapsed_ms=elapsed,
                )
                return BrowserActionResponse(
                    success=False, action=req.action, matched_count=matched,
                    url_before=page.url, url_after=page.url, elapsed_ms=elapsed,
                    error=BrowserErrorResponse(
                        error_code="MULTIPLE_MATCHES",
                        error_message=BROWSER_ERROR_CODES["MULTIPLE_MATCHES"],
                        phase="locate",
                        debug_detail=f"resolved to {matched} elements",
                        elapsed_ms=elapsed,
                    ),
                )
            url_before = page.url
            loc_first = loc.first
            timeout_ms = int(req.timeout * 1000)
            if req.action == "click":
                await loc_first.click(timeout=timeout_ms, button=req.button)
            elif req.action == "double_click":
                await loc_first.dblclick(timeout=timeout_ms, button=req.button)
            elif req.action == "fill":
                if req.clear_first:
                    await loc_first.fill('')
                await loc_first.fill(req.value or '', timeout=timeout_ms)
            elif req.action == "type":
                await loc_first.press_sequentially(req.value or '', delay=50, timeout=timeout_ms)
            elif req.action == "press":
                await loc_first.press(req.key or 'Enter', timeout=timeout_ms)
            elif req.action == "select":
                if req.label:
                    await loc_first.select_option(label=req.label, timeout=timeout_ms)
                else:
                    await loc_first.select_option(req.value, timeout=timeout_ms)
            elif req.action == "check":
                await loc_first.check(timeout=timeout_ms)
            elif req.action == "uncheck":
                await loc_first.uncheck(timeout=timeout_ms)
            elif req.action == "hover":
                await loc_first.hover(timeout=timeout_ms)
            elif req.action == "scroll_into_view":
                await loc_first.scroll_into_view_if_needed(timeout=timeout_ms)
            if req.wait_after > 0:
                await asyncio.sleep(req.wait_after)
            url_after = page.url
            elapsed = int((_time.perf_counter() - start) * 1000)
            record_selector_call(
                url=page.url, target_type=_t_type, target_value=_t_val,
                action=req.action, success=True, elapsed_ms=elapsed,
            )
            return BrowserActionResponse(
                success=True, action=req.action, matched_count=matched,
                url_before=url_before, url_after=url_after,
                elapsed_ms=elapsed, site_lessons_hint=lessons_hint,
            )
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "act", elapsed)
            record_selector_call(
                url=page.url, target_type=_t_type, target_value=_t_val,
                action=req.action, success=False,
                error_code=err.error_code, elapsed_ms=elapsed,
            )
            return BrowserActionResponse(
                success=False, action=req.action, matched_count=0,
                elapsed_ms=elapsed, error=err,
            )

    # 无 session：走 exec_python 子进程模型（冷启动）
    params = {
        "url_pattern": req.url_pattern,
        "target": req.target.model_dump(),
        "action": req.action,
        "value": req.value,
        "label": req.label,
        "key": req.key,
        "button": req.button,
        "clear_first": req.clear_first,
        "timeout": req.timeout,
        "wait_after": req.wait_after,
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
        await Stealth().apply_stealth_async(page)
        url_before = page.url
        try:
            t = _PARAMS["target"]
            # 构造 locator（按优先级）
            loc = None
            if t.get("css"):
                loc = page.locator(t["css"])
            elif t.get("role"):
                if t.get("name"):
                    loc = page.get_by_role(t["role"], name=t["name"])
                else:
                    loc = page.get_by_role(t["role"])
            elif t.get("label"):
                loc = page.get_by_label(t["label"])
            elif t.get("placeholder"):
                loc = page.get_by_placeholder(t["placeholder"])
            elif t.get("testid"):
                loc = page.get_by_test_id(t["testid"])
            elif t.get("text"):
                loc = page.get_by_text(t["text"], exact=t.get("text_exact", False))
            else:
                print('ERROR:no target field provided')
                return
            # 限定到 nth 或严格模式
            nth = t.get("nth")
            if nth is not None:
                loc = loc.nth(nth)
            else:
                # 严格模式：count() > 1 时报错
                cnt = await loc.count()
                if cnt > 1:
                    print(f'ERROR:strict mode violation: resolved to {cnt} elements')
                    return
            matched = await loc.count()
            if matched == 0:
                print('ERROR:element not found')
                return
            # 单点定位到 first（count==1 或 nth 已选）
            loc_first = loc.first
            action = _PARAMS["action"]
            timeout_ms = int(_PARAMS["timeout"] * 1000)
            if action == "click":
                await loc_first.click(timeout=timeout_ms, button=_PARAMS["button"])
            elif action == "double_click":
                await loc_first.dblclick(timeout=timeout_ms, button=_PARAMS["button"])
            elif action == "fill":
                if _PARAMS["clear_first"]:
                    await loc_first.fill('')
                await loc_first.fill(_PARAMS["value"] or '', timeout=timeout_ms)
            elif action == "type":
                await loc_first.press_sequentially(_PARAMS["value"] or '', delay=50, timeout=timeout_ms)
            elif action == "press":
                await loc_first.press(_PARAMS["key"] or 'Enter', timeout=timeout_ms)
            elif action == "select":
                if _PARAMS["label"]:
                    await loc_first.select_option(label=_PARAMS["label"], timeout=timeout_ms)
                else:
                    await loc_first.select_option(_PARAMS["value"], timeout=timeout_ms)
            elif action == "check":
                await loc_first.check(timeout=timeout_ms)
            elif action == "uncheck":
                await loc_first.uncheck(timeout=timeout_ms)
            elif action == "hover":
                await loc_first.hover(timeout=timeout_ms)
            elif action == "scroll_into_view":
                await loc_first.scroll_into_view_if_needed(timeout=timeout_ms)
            await asyncio.sleep(_PARAMS["wait_after"])
            url_after = page.url
            print('OK:' + json.dumps({
                "matched_count": matched,
                "url_before": url_before,
                "url_after": url_after,
            }))
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, int(req.timeout + 10))
    if success and message.startswith("{"):
        try:
            payload = json.loads(message)
            return BrowserActionResponse(
                success=True,
                action=req.action,
                matched_count=payload["matched_count"],
                url_before=payload["url_before"],
                url_after=payload["url_after"],
                elapsed_ms=elapsed,
            )
        except (json.JSONDecodeError, KeyError):
            pass
    if success:
        # 形如 "OK:..." 但非 JSON（不应发生）
        err = _classify_error(message, "verify", elapsed)
        return BrowserActionResponse(
            success=False, action=req.action, matched_count=0,
            elapsed_ms=elapsed, error=err,
        )
    err = _classify_error(message, "act", elapsed)
    return BrowserActionResponse(
        success=False, action=req.action, matched_count=0,
        elapsed_ms=elapsed, error=err,
    )
