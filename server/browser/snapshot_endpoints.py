"""P0-1 修复 snapshot：自有 ARIA/DOM 实现 + browser_snapshot 端点。

替代已废弃的 page.accessibility.snapshot()（Playwright 1.40+ 已迁移/移除 Page.accessibility）。
注入 JS 遍历 DOM 树，按 ARIA/role 属性构造节点列表，避开 API 兼容问题。

包含：
- _ARIA_SNAPSHOT_JS：JS 字符串常量，遍历 DOM 构造 ARIA 节点列表
- compute_aria_snapshot：async 函数，调用 page.evaluate 执行 _ARIA_SNAPSHOT_JS
- _DOM_HASH_JS：JS 字符串常量，计算 DOM 版本签名
- compute_dom_hash：async 函数，调用 page.evaluate 执行 _DOM_HASH_JS
- BrowserSnapshotRequest / BrowserSnapshotResponse 模型（Ticket 04 追加）
- browser_snapshot 端点（Ticket 04 追加）

JS 常量和 compute 函数从 server/browser_session.py 迁移至 server/browser/ 包（Ticket 02）。
browser_snapshot 端点从 server/browser.py 迁移至本文件（Ticket 04）。
代码与原定义完全一致，仅调整 import 路径：
- BROWSER_ERROR_CODES / BrowserErrorResponse / _classify_error：从 server.browser.error_model import
- _build_playwright_script / _execute_playwright：从 server.browser.playwright_executor import
- router：从 server.browser.routes import
- get_session_manager：从 server.browser.session.manager import
- build_site_lessons_hint：从 server.browser.site_lessons import
- _ARIA_SNAPSHOT_JS / compute_aria_snapshot / compute_dom_hash：本文件自带，无需 import
"""

from __future__ import annotations

import json

from lib.schema import BaseSchema

from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse, _classify_error
from .playwright_executor import _build_playwright_script, _execute_playwright
from .routes import router

_ARIA_SNAPSHOT_JS = r"""
(args) => {
  const rootSelector = args.rootSelector;
  const interestingOnly = args.interestingOnly;
  const maxDepth = args.maxDepth;
  const maxNodes = args.maxNodes;

  const root = rootSelector ? document.querySelector(rootSelector) : document.body;
  if (!root) return {nodes: [], truncated: false};

  // tag → implicit role 映射（HTML5 ARIA 标准）
  const TAG_ROLE = {
    a: 'link', button: 'button', input: 'textbox', select: 'combobox',
    textarea: 'textbox', option: 'option', checkbox: 'checkbox',
    radio: 'radio', slider: 'slider', progress: 'progressbar',
    h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading',
    h5: 'heading', h6: 'heading', img: 'image', nav: 'navigation',
    main: 'main', header: 'banner', footer: 'contentinfo',
    aside: 'complementary', section: 'region', article: 'article',
    form: 'form', search: 'search', ul: 'list', ol: 'list', li: 'listitem',
    table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
    dialog: 'dialog', menu: 'menu', menuitem: 'menuitem',
    tab: 'tab', tabpanel: 'tabpanel', details: 'group', summary: 'button',
  };

  // interesting_only=True 时保留的 role（可交互 + 语义关键元素）
  const INTERESTING_ROLES = new Set([
    'button', 'link', 'textbox', 'combobox', 'checkbox', 'radio',
    'slider', 'progressbar', 'heading', 'image', 'navigation',
    'main', 'banner', 'contentinfo', 'complementary', 'region',
    'article', 'form', 'search', 'list', 'listitem', 'table', 'row',
    'cell', 'columnheader', 'dialog', 'menu', 'menuitem', 'tab', 'tabpanel',
    'option', 'group', 'switch',
  ]);

  // 计算元素的 name（按 ARIA 优先级）
  function getName(el) {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.getAttribute('aria-labelledby')) {
      const lab = document.getElementById(el.getAttribute('aria-labelledby'));
      if (lab) return (lab.innerText || lab.textContent || '').trim();
    }
    // label[for] 关联
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.innerText || lab.textContent || '').trim();
    }
    // 包裹式 label
    const wrap = el.closest('label');
    if (wrap && wrap !== el) {
      const txt = (wrap.innerText || wrap.textContent || '').trim();
      if (txt) return txt;
    }
    // placeholder / title
    if (el.placeholder) return el.placeholder;
    if (el.title) return el.title;
    // 自身文本（按钮、链接）
    const own = (el.innerText || el.textContent || '').trim();
    if (own && own.length < 200) return own;
    // alt 属性（图片）
    if (el.alt) return el.alt;
    return '';
  }

  // 计算 role
  function getRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (TAG_ROLE[tag]) {
      // input 的 type 影响 role
      if (tag === 'input') {
        const t = (el.type || 'text').toLowerCase();
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'range') return 'slider';
        if (t === 'button' || t === 'submit' || t === 'reset') return 'button';
        if (t === 'search') return 'searchbox';
        return 'textbox';
      }
      return TAG_ROLE[tag];
    }
    return null;
  }

  const nodes = [];
  let truncated = false;
  let nextId = 0;

  function walk(el, depth, parentId) {
    if (truncated) return;
    if (maxDepth !== null && depth > maxDepth) return;
    if (nodes.length >= maxNodes) { truncated = true; return; }

    const role = getRole(el);
    const isInteresting = role && INTERESTING_ROLES.has(role);
    let nodeId = null;
    // interesting_only=True: 仅包含 INTERESTING_ROLES 元素（按钮/链接/输入框等）
    // interesting_only=False: 包含所有 DOM 元素（div/span/p 等），无 ARIA role 时用 tag 名作 fallback
    const includeNode = interestingOnly ? (role && isInteresting) : true;
    if (includeNode) {
      nodeId = nextId++;
      nodes.push({
        node_id: nodeId,
        role: role || el.tagName.toLowerCase(),
        name: getName(el),
        value: el.value !== undefined ? String(el.value) : null,
        checked: (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio'))
          ? !!el.checked : null,
        selected: el.tagName === 'OPTION' ? !!el.selected : null,
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        href: el.tagName === 'A' && el.href ? el.href : null,
        depth: depth,
        parent_id: parentId,
        // P2-2: 路径签名用于 node_id 稳定重放（CSS 路径）
        css_path: getCssPath(el),
      });
    }
    // 递归子元素
    for (const child of el.children) {
      walk(child, depth + 1, nodeId);
      if (truncated) return;
    }
  }

  function getCssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body) {
      let part = cur.tagName.toLowerCase();
      if (cur.dataset && cur.dataset.testid) {
        part += '[data-testid="' + cur.dataset.testid + '"]';
      }
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(s => s.tagName === cur.tagName);
        if (sibs.length > 1) {
          const idx = sibs.indexOf(cur) + 1;
          part += ':nth-of-type(' + idx + ')';
        }
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.length ? parts.join(' > ') : '';
  }

  walk(root, 0, null);
  return {nodes: nodes, truncated: truncated};
}
"""


async def compute_aria_snapshot(page, root_selector: str | None, interesting_only: bool,
                                max_depth: int | None, max_nodes: int) -> dict:
    """P0-1 修复：用 page.evaluate + 自有 ARIA JS 替代 page.accessibility.snapshot()。

    返回 {nodes: list[dict], truncated: bool}，节点字段与 BrowserSnapshotResponse 一致。
    """
    result = await page.evaluate(_ARIA_SNAPSHOT_JS, {
        "rootSelector": root_selector,
        "interestingOnly": interesting_only,
        "maxDepth": max_depth,
        "maxNodes": max_nodes,
    })
    return result


# P1-2 rerun3：DOM 版本签名计算（轻量 JS，用于 STALE_NODE 精细化判定）
# 与 ARIA snapshot 独立，用于 action 时校验 DOM 是否在同 URL 下发生 mutation。
# 签名基于：元素总数 + 前 N 个元素的 tag/id/class/role/value 拼接，避免大页面开销。
_DOM_HASH_JS = r"""
() => {
  const els = document.querySelectorAll('body *');
  let sig = els.length + ':';
  let i = 0;
  for (const el of els) {
    if (i++ >= 500) { sig += '...'; break; }
    sig += el.tagName + '|'
         + (el.id || '') + '|'
         + (typeof el.className === 'string' ? el.className : '') + '|'
         + (el.getAttribute('role') || '') + '|'
         + (el.getAttribute('data-testid') || '') + ';';
  }
  return sig;
}
"""


async def compute_dom_hash(page) -> str:
    """P1-2 rerun3：计算当前页面的 DOM 版本签名。

    用于 STALE_NODE 精细化判定：snapshot 时存一份，action 用 node_id 时
    （agent 显式 verify_dom_freshness=True）重算一次比对，不一致则视为 DOM 已变化。

    签名基于 body 下前 500 个元素的 tag/id/class/role/data-testid 拼接，
    能感知大部分结构性 mutation（增删元素、改 id/class/role），但不感知
    纯文本/value 变化（这些通过 ARIA snapshot 本身的 nodes 内容感知）。
    """
    try:
        return await page.evaluate(_DOM_HASH_JS)
    except Exception:
        return ""


# ========== browser_snapshot 端点（Ticket 04 从 server/browser.py 迁移） ==========

class BrowserSnapshotRequest(BaseSchema):
    """可访问性 DOM 快照请求（评估文档 P0 第 2 项）。

    输出 Playwright accessibility snapshot（role/name/value/checked/selected/disabled/href），
    平铺节点列表（不嵌套，便于 agent 解析）。agent 应先 snapshot 再 action，
    避免猜测 CSS 选择器。

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则按 url_pattern
      走 exec_python 子进程模型（0.5–1.4 秒固定开销）
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）
    - root_selector: 限定快照根节点（CSS），不填则从页面根开始
    - interesting_only: True=只返回可交互元素（按钮/链接/输入框等），False=返回完整树
    - max_depth: 树最大深度（避免超大页面爆 token），不填则不限
    - max_nodes: 节点数上限（默认 500），超出截断并标记 truncated=true
    """
    session_id: str | None = None
    url_pattern: str | None = None
    root_selector: str | None = None
    interesting_only: bool = True
    max_depth: int | None = None
    max_nodes: int = 500


class BrowserSnapshotResponse(BaseSchema):
    """可访问性 DOM 快照响应。

    - nodes: 平铺的节点列表（不嵌套，便于 agent 解析），每项含：
      - node_id: 服务端分配的整数 id（仅在本次快照内有效；DOM 变化后旧 id 失效）
      - role: button / link / textbox / combobox / checkbox / heading / text / ...
      - name: 可见名称（按钮文字/链接文本/输入框 label 等）
      - value: 当前值（输入框文本/选择框值）
      - checked: 复选框状态（None=非复选框）
      - selected: 选项是否选中（None=非选项）
      - disabled: 是否禁用
      - href: 链接 URL（None=非链接）
      - depth: 在树中的深度
      - parent_id: 父节点 id（None=根）
    - truncated: 节点数超过 max_nodes 时为 True
    - dom_hash: P1-2 rerun3 新增：DOM 版本签名（基于 nodes 内容的轻量 hash）
      agent 可通过比较两次 snapshot 的 dom_hash 判断 DOM 是否变化；服务端在
      action 用 node_id 时（且 agent 显式 verify_dom_freshness=True）也会比对。
    - site_lessons_hint: P2-1 站点经验提示（仅 session 有 lessons 时；None=无）
    - elapsed_ms: 耗时
    - blocked_by_dialog: 2026-08-06 Ticket 03：dialog 阻塞预检反馈
    - blocking_dialog: 阻塞 dialog 详情
    """
    success: bool
    nodes: list[dict]
    truncated: bool
    elapsed_ms: int
    dom_hash: str | None = None
    site_lessons_hint: str | None = None
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


@router.post("/snapshot", response_model=BrowserSnapshotResponse, operation_id="browser_snapshot")
async def browser_snapshot(req: BrowserSnapshotRequest):
    """Get an accessibility-tree snapshot of the current page.

    Returns a flat list of nodes (role/name/value/checked/selected/disabled/href),
    so agents can locate elements like Browser Use without guessing CSS selectors.

    - session_id: pass from browser_session_create for hot-path reuse (~250ms vs
      0.5–1.4s cold start). Falls back to url_pattern subprocess mode if absent.
    - interesting_only (default True): only return interactive elements (button/link/
      textbox/combobox/checkbox/heading), drop decorative div/span. Set False for full tree.
    - max_depth: cap tree depth to avoid huge outputs.
    - max_nodes (default 500): truncate node list and set truncated=True.

    Use this BEFORE browser_action when you don't know the CSS selector — read the
    snapshot, find the target's role+name, then call browser_action with target=
    {"role": "button", "name": "搜索"}.

    Tip — 若 session 已加载 site_lessons，响应中的 site_lessons_hint 会提示已知坑；
    首次访问新网站建议先调 browser_match_site(domain=...) 查询是否有已记录经验。

    Example:
      browser_snapshot(session_id="...", interesting_only=True, max_nodes=200)
    Example (no session):
      browser_snapshot(url_pattern="bilibili", interesting_only=True, max_nodes=200)
    """
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
            return BrowserSnapshotResponse(
                success=False, nodes=[], truncated=False, elapsed_ms=elapsed,
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
            return BrowserSnapshotResponse(
                success=False, nodes=[], truncated=False, elapsed_ms=elapsed,
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
        # P2-1: 附带站点经验提示（仅当 session 有 lessons 时）
        lessons_hint = build_site_lessons_hint(sess.site_lessons) if sess else None
        try:
            # P0-1 修复：用自有 ARIA JS 替代已废弃的 page.accessibility.snapshot()
            # 同时为 P2-2 在 session 内缓存最近一次 snapshot，供 node_id 重放
            result = await compute_aria_snapshot(
                page, req.root_selector, req.interesting_only,
                req.max_depth, req.max_nodes,
            )
            nodes = result.get("nodes", [])
            truncated = result.get("truncated", False)
            # P1-2 rerun3：计算 dom_hash 用于 STALE_NODE 精细化判定
            # 轻量 page.evaluate（约 3-5ms），与 ARIA 遍历独立
            dom_hash = await compute_dom_hash(page)
            # P2-2: 缓存 snapshot 到 session，支持 node_id 重放 + STALE_NODE 检测
            if sess is not None:
                sess.last_snapshot_nodes = nodes
                sess.last_snapshot_at = _time.time()
                sess.last_snapshot_url = page.url
                sess.last_snapshot_dom_hash = dom_hash
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserSnapshotResponse(
                success=True, nodes=nodes, truncated=truncated,
                elapsed_ms=elapsed, dom_hash=dom_hash,
                site_lessons_hint=lessons_hint,
            )
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "locate", elapsed)
            return BrowserSnapshotResponse(
                success=False, nodes=[], truncated=False, elapsed_ms=elapsed, error=err,
            )

    # 无 session：走 exec_python 子进程模型（冷启动）
    # P0-1 修复：从本模块导入 ARIA JS，在子进程内通过 page.evaluate 执行
    params = {
        "url_pattern": req.url_pattern,
        "root_selector": req.root_selector,
        "interesting_only": req.interesting_only,
        "max_depth": req.max_depth,
        "max_nodes": req.max_nodes,
        "aria_js": _ARIA_SNAPSHOT_JS,
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
        try:
            # P0-1 修复：用 page.evaluate + ARIA JS 替代 page.accessibility.snapshot()
            result = await page.evaluate(_PARAMS["aria_js"], {
                "rootSelector": _PARAMS["root_selector"],
                "interestingOnly": _PARAMS["interesting_only"],
                "maxDepth": _PARAMS["max_depth"],
                "maxNodes": _PARAMS["max_nodes"],
            })
            nodes = result.get("nodes", [])
            truncated = result.get("truncated", False)
            print('OK:' + json.dumps({"nodes": nodes, "truncated": truncated}, ensure_ascii=False))
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, 30)
    if success and message.startswith("{"):
        try:
            payload = json.loads(message)
            return BrowserSnapshotResponse(
                success=True,
                nodes=payload["nodes"],
                truncated=payload["truncated"],
                elapsed_ms=elapsed,
            )
        except (json.JSONDecodeError, KeyError) as e:
            err = _classify_error(f"snapshot parse failed: {e}", "verify", elapsed)
            return BrowserSnapshotResponse(
                success=False, nodes=[], truncated=False, elapsed_ms=elapsed, error=err,
            )
    if success:
        # message 是 "OK:..." 但不是 JSON
        err = _classify_error(message, "verify", elapsed)
        return BrowserSnapshotResponse(
            success=False, nodes=[], truncated=False, elapsed_ms=elapsed, error=err,
        )
    err = _classify_error(message, "locate", elapsed)
    return BrowserSnapshotResponse(
        success=False, nodes=[], truncated=False, elapsed_ms=elapsed, error=err,
    )
