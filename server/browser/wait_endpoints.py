"""browser_wait_for / browser_wait_and_action 端点 — 状态等待 + 原子 wait+trigger API。

从 server/browser_wait_endpoints.py 迁移至 server/browser/ 包（Ticket 04）。
代码与原定义完全一致，仅调整 import 路径：
- BROWSER_ERROR_CODES / BrowserErrorResponse / _classify_error：从 server.browser.error_model import
- _build_playwright_script / _execute_playwright：从 server.browser.playwright_executor import
- router：从 server.browser.routes import
- get_session_manager：从 server.browser.session.manager import

迁移内容：
- BrowserWaitRequest / BrowserWaitResponse 模型 + browser_wait_for 端点
- BrowserWaitAndActionRequest / BrowserWaitAndActionResponse 模型 + browser_wait_and_action 端点
- _build_trigger_locator 辅助函数

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import asyncio
import json

from lib.schema import BaseSchema

# 从本包各子模块复用共享件（router / 错误模型 / playwright 子进程执行器 / 辅助函数）
from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse, _classify_error
from .playwright_executor import _build_playwright_script, _execute_playwright
from .routes import router


class BrowserWaitRequest(BaseSchema):
    """状态等待请求（评估文档 P0 第 4 项，替代固定 wait_after）。

    支持的 wait_type：
    - selector: 等待元素出现/消失（state=visible/hidden/attached/detached）
    - text: 等待文本出现/消失（text_contains=True 默认）
    - url: 等待 URL 匹配（url_pattern 子串或 regex）
    - load_state: 等待页面加载状态（load/domcontentloaded/networkidle）
    - popup: 等待新标签弹出（target=_blank），返回新标签 target_id
    - filechooser: 等待文件选择对话框触发（P2-1 新增）
    - dialog: 等待 JS 对话框触发（alert/confirm/prompt），自动 dismiss/accept（P2-1 新增）
    - download: 等待文件下载触发，可自动 save_as 到指定目录（session 模式推荐）

    - session_id: 持久 session id（推荐，热态 <250ms）。不传则走 url_pattern 子进程模型
    - url_pattern: 无 session 时匹配标签页（有 session 时忽略）
    """
    session_id: str | None = None
    url_pattern: str | None = None
    wait_type: str  # selector/text/url/load_state/popup/filechooser/dialog/download
    # 按 wait_type 使用
    selector: str | None = None
    state: str = "visible"  # selector 用：visible/hidden/attached/detached
    text: str | None = None
    text_contains: bool = True
    url_match: str | None = None  # url 用：子串或 regex
    url_regex: bool = False
    load_state: str = "networkidle"  # load_state 用：load/domcontentloaded/networkidle
    timeout: float = 30.0
    # P2-1: filechooser/dialog 用
    file_paths: list[str] | None = None  # filechooser 触发后自动 set_files（可选）
    dialog_action: str = "dismiss"  # dialog 用：accept/dismiss（默认 dismiss 避免阻塞）
    dialog_prompt_text: str | None = None  # dialog 用：prompt 类型时的输入文本
    # download 用
    download_dir: str | None = None  # download 触发后自动 save_as 到此目录（可选，不传则不主动保存）


class BrowserWaitResponse(BaseSchema):
    success: bool
    wait_type: str
    elapsed_ms: int
    matched: bool  # 是否等到目标（timeout 时为 False）
    data: dict | None = None  # popup 时返回新标签信息
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    # 仅在 wait_type != "dialog" 且 session 有 pending dialog 时设置
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


@router.post("/wait_for", response_model=BrowserWaitResponse, operation_id="browser_wait_for")
async def browser_wait_for(req: BrowserWaitRequest):
    """Wait for a state condition (selector/text/url/load_state/popup).

    Replaces the fixed wait_after sleep with real state synchronization. Returns
    matched=False on timeout (not an error) so agents can branch on the result.

    - selector: wait for element visibility/existence (state=visible/hidden/attached/detached)
    - text: wait for text to appear/disappear on the page
    - url: wait for URL to match (substring or regex)
    - load_state: wait for page load state (load/domcontentloaded/networkidle)
    - popup: wait for a new tab opened via target=_blank, returns its target_id
    - filechooser: wait for a file chooser dialog (P2-1)
    - dialog: wait for a JS dialog (alert/confirm/prompt), auto dismiss/accept (P2-1)
    - download: wait for a file download, optionally save_as to download_dir

    Example — wait for results to appear after a search:
      browser_wait_for(wait_type="selector", url_pattern="search",
                       selector=".result-item", state="visible", timeout=10)
    Example — wait for popup after clicking a link:
      browser_wait_for(wait_type="popup", url_pattern="main", timeout=5)
    Example — wait for file chooser and auto-set files:
      browser_wait_for(wait_type="filechooser", session_id="...",
                       file_paths=["C:\\\\upload.pdf"], timeout=5)
    Example — wait for JS dialog and accept:
      browser_wait_for(wait_type="dialog", session_id="...",
                       dialog_action="accept", timeout=5)
    Example — wait for download and save to directory:
      browser_wait_for(wait_type="download", session_id="...",
                       download_dir="C:\\\\<data_drive>:/Downloads", timeout=30)
    """
    valid_types = {"selector", "text", "url", "load_state", "popup", "filechooser", "dialog", "download"}
    if req.wait_type not in valid_types:
        return BrowserWaitResponse(
            success=False, wait_type=req.wait_type, elapsed_ms=0, matched=False,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="wait",
                debug_detail=f"wait_type must be one of {sorted(valid_types)}",
                elapsed_ms=0,
            ),
        )

    import re as _re
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
            return BrowserWaitResponse(
                success=False, wait_type=req.wait_type, elapsed_ms=elapsed, matched=False,
                error=BrowserErrorResponse(
                    error_code="TAB_NOT_FOUND",
                    error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                    phase="connect",
                    debug_detail=f"session_id {req.session_id} 不存在或已失效",
                    elapsed_ms=elapsed,
                ),
            )
        # 2026-08-06 Ticket 03：dialog 阻塞预检
        # 例外：wait_type="dialog" 不预检（其目的就是等 dialog，应允许等待）
        if req.wait_type != "dialog":
            from .dialog_endpoints import _check_dialog_block
            block = _check_dialog_block(sess)
            if block is not None:
                elapsed = int((_time.perf_counter() - start) * 1000)
                return BrowserWaitResponse(
                    success=False, wait_type=req.wait_type, elapsed_ms=elapsed, matched=False,
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
            matched = False
            data = None
            wt = req.wait_type
            if wt == "selector":
                try:
                    await page.wait_for_selector(req.selector or "", state=req.state, timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "text":
                try:
                    await page.get_by_text(req.text or "", exact=not req.text_contains).wait_for(timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "url":
                um = req.url_match or ""
                is_regex = req.url_regex
                pat = _re.compile(um) if is_regex else None
                # 轮询
                steps = int(timeout_ms / 200) + 1
                for _ in range(steps):
                    cur = page.url
                    if is_regex:
                        if pat and pat.search(cur):
                            matched = True
                            break
                    else:
                        if um in cur:
                            matched = True
                            break
                    await asyncio.sleep(0.2)
            elif wt == "load_state":
                try:
                    await page.wait_for_load_state(req.load_state, timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "popup":
                # P0-1 修复：优先消费 session 持久监听器队列中的 popup 事件
                # 这样 agent 顺序调用 click → wait_for(popup) 也能命中
                if sess is not None:
                    pending = sess.pop_pending_popup()
                    if pending:
                        data = {"url": pending.get("url"), "title": pending.get("title")}
                        matched = True
                    else:
                        # 等待新 popup 事件（持久监听器 set Event）
                        sess.popup_event.clear()
                        try:
                            await asyncio.wait_for(sess.popup_event.wait(), timeout=req.timeout)
                            pending = sess.pop_pending_popup()
                            if pending:
                                data = {"url": pending.get("url"), "title": pending.get("title")}
                                matched = True
                        except TimeoutError:
                            matched = False
                else:
                    try:
                        async with page.context.expect_page(timeout=timeout_ms) as new_page_info:
                            pass
                        new_page = await new_page_info.value
                        await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                        data = {"url": new_page.url, "title": await new_page.title()}
                        matched = True
                    except Exception:
                        matched = False
            elif wt == "filechooser":
                # P0-1 修复：优先消费 session 持久监听器队列中的 filechooser 事件
                # 同时尝试在 wait_for 内通过 expect_file_chooser 等待新事件
                if sess is not None:
                    pending = sess.pop_pending_filechooser()
                    if pending:
                        # 已有未消费的 filechooser，尝试 set_files（last_filechooser_obj 可能仍有效）
                        fc_obj = sess.last_filechooser_obj
                        if req.file_paths and fc_obj is not None:
                            try:
                                await fc_obj.set_files(req.file_paths)
                            except Exception:
                                pass  # dialog 已被自动 dismiss，set_files 失败可忽略
                        sess.last_filechooser_obj = None
                        data = {"multiple": pending.get("multiple"), "page_url": pending.get("page_url")}
                        matched = True
                    else:
                        # 双保险：同时挂 expect_file_chooser 和等待 Event
                        sess.filechooser_event.clear()
                        try:
                            async with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
                                # 等待 Event 触发（expect_file_chooser 也会触发 page.on('filechooser')）
                                try:
                                    await asyncio.wait_for(sess.filechooser_event.wait(), timeout=req.timeout)
                                except TimeoutError:
                                    pass
                            fc = await fc_info.value
                            if req.file_paths:
                                await fc.set_files(req.file_paths)
                            data = {"multiple": fc.is_multiple(), "page_url": page.url}
                            matched = True
                        except Exception:
                            # 兜底：检查队列是否在 expect_file_chooser 期间被填充
                            pending = sess.pop_pending_filechooser()
                            if pending:
                                data = {"multiple": pending.get("multiple"), "page_url": pending.get("page_url")}
                                matched = True
                            else:
                                matched = False
                else:
                    # P2-1: 等待文件选择对话框，可自动 set_files
                    try:
                        async with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
                            pass
                        fc = await fc_info.value
                        if req.file_paths:
                            await fc.set_files(req.file_paths)
                        data = {"multiple": fc.is_multiple(), "page_url": page.url}
                        matched = True
                    except Exception:
                        matched = False
            elif wt == "dialog":
                # P0-1 修复 + 2026-08-06 改造：优先消费 session 持久监听器队列中的 dialog 事件。
                # 2026-08-06 改造：_on_dialog 不再立即 dismiss，dialog 触发后仍存在（阻塞 page）。
                # wait_for 只返回 dialog 信息，不处理 dialog（dialog_action 仅声明意图）。
                # agent 需调 /browser/handle_dialog 主动 accept/dismiss，否则 page 阻塞直到
                # 5 分钟超时兜底自动 dismiss。
                if sess is not None:
                    pending = sess.pop_pending_dialog()
                    if pending:
                        data = {
                            "type": pending.get("type"),
                            "message": pending.get("message"),
                            "page_url": pending.get("page_url"),
                            "auto_dismissed": pending.get("auto_dismissed", False),
                            "intended_action": req.dialog_action,  # 仅声明意图，用 handle_dialog 真正处理
                        }
                        matched = True
                    else:
                        # 等待新 dialog 事件（持久监听器 set Event）
                        sess.dialog_event.clear()
                        try:
                            await asyncio.wait_for(sess.dialog_event.wait(), timeout=req.timeout)
                            pending = sess.pop_pending_dialog()
                            if pending:
                                data = {
                                    "type": pending.get("type"),
                                    "message": pending.get("message"),
                                    "page_url": pending.get("page_url"),
                                    "auto_dismissed": pending.get("auto_dismissed", False),
                                    "intended_action": req.dialog_action,
                                }
                                matched = True
                        except TimeoutError:
                            matched = False
                else:
                    # P2-1: 等待 JS 对话框（alert/confirm/prompt），自动 accept/dismiss
                    try:
                        async with page.expect_dialog(timeout=timeout_ms) as dlg_info:
                            pass
                        dlg = await dlg_info.value
                        data = {"type": dlg.type, "message": dlg.message, "page_url": page.url}
                        if req.dialog_action == "accept":
                            if dlg.type == "prompt":
                                await dlg.accept(req.dialog_prompt_text or "")
                            else:
                                await dlg.accept()
                        else:
                            await dlg.dismiss()
                        matched = True
                    except Exception:
                        matched = False
            elif wt == "download":
                # download 等待：优先消费 session 持久监听器队列中的 download 事件
                # （与 dialog/popup/filechooser 同模式：page.on('download') 持久监听器
                # 在 manager.create_session 时挂载，触发时存入 pending_<data_drive>:/Downloads 队列）
                # 若 download_dir 提供，则用 last_download_obj.save_as() 保存到指定目录
                if sess is not None:
                    pending = sess.pop_pending_download()
                    if not pending:
                        # 等待新 download 事件（持久监听器 set Event）
                        sess.download_event.clear()
                        try:
                            await asyncio.wait_for(sess.download_event.wait(), timeout=req.timeout)
                            pending = sess.pop_pending_download()
                        except TimeoutError:
                            pending = None
                    if pending:
                        data = {
                            "url": pending.get("url"),
                            "suggested_filename": pending.get("suggested_filename"),
                            "page_url": pending.get("page_url"),
                        }
                        # 尝试 save_as（last_download_obj 可能仍有效）
                        if req.download_dir:
                            dl_obj = sess.last_download_obj
                            if dl_obj is not None:
                                try:
                                    import os as _os
                                    _os.makedirs(req.download_dir, exist_ok=True)
                                    fname = pending.get("suggested_filename") or "download"
                                    save_path = _os.path.join(req.download_dir, fname)
                                    await dl_obj.save_as(save_path)
                                    data["saved_to"] = save_path
                                    data["saved"] = True
                                except Exception as e:
                                    data["saved"] = False
                                    data["save_error"] = str(e)
                            else:
                                data["saved"] = False
                                data["save_error"] = "download object no longer available (auto-cleaned by listener)"
                        sess.last_download_obj = None
                        matched = True
                    else:
                        matched = False
                else:
                    # 无 session 子进程模式：用 expect_download（仅能捕获 wait_for 期间触发的下载）
                    try:
                        async with page.expect_download(timeout=timeout_ms) as dl_info:
                            pass
                        dl = await dl_info.value
                        data = {
                            "url": dl.url,
                            "suggested_filename": dl.suggested_filename,
                            "page_url": page.url,
                        }
                        if req.download_dir:
                            import os as _os
                            _os.makedirs(req.download_dir, exist_ok=True)
                            fname = dl.suggested_filename or "download"
                            save_path = _os.path.join(req.download_dir, fname)
                            await dl.save_as(save_path)
                            data["saved_to"] = save_path
                            data["saved"] = True
                        matched = True
                    except Exception:
                        matched = False
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserWaitResponse(
                success=True, wait_type=req.wait_type, elapsed_ms=elapsed,
                matched=matched, data=data,
            )
        except Exception as e:
            elapsed = int((_time.perf_counter() - start) * 1000)
            err = _classify_error(str(e), "wait", elapsed)
            return BrowserWaitResponse(
                success=False, wait_type=req.wait_type, elapsed_ms=elapsed,
                matched=False, error=err,
            )

    # 无 session：走 exec_python 子进程模型（冷启动）
    params = {
        "url_pattern": req.url_pattern,
        "wait_type": req.wait_type,
        "selector": req.selector,
        "state": req.state,
        "text": req.text,
        "text_contains": req.text_contains,
        "url_match": req.url_match,
        "url_regex": req.url_regex,
        "load_state": req.load_state,
        "timeout": req.timeout,
        "file_paths": req.file_paths,
        "dialog_action": req.dialog_action,
        "dialog_prompt_text": req.dialog_prompt_text,
        "download_dir": req.download_dir,
    }
    body = """
import json, re
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
            wt = _PARAMS["wait_type"]
            timeout_ms = int(_PARAMS["timeout"] * 1000)
            matched = False
            data = None
            if wt == "selector":
                state = _PARAMS["state"]
                try:
                    await page.wait_for_selector(_PARAMS["selector"], state=state, timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "text":
                # 等待文本出现（contains/exact）
                try:
                    await page.get_by_text(_PARAMS["text"], exact=not _PARAMS["text_contains"]).wait_for(timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "url":
                # 轮询 URL 匹配
                um = _PARAMS["url_match"]
                is_regex = _PARAMS["url_regex"]
                if is_regex:
                    pat = re.compile(um)
                async def _check_url():
                    for _ in range(int(timeout_ms / 200) + 1):
                        cur = page.url
                        if is_regex:
                            if pat.search(cur):
                                return True
                        else:
                            if um in cur:
                                return True
                        await asyncio.sleep(0.2)
                    return False
                matched = await _check_url()
            elif wt == "load_state":
                try:
                    await page.wait_for_load_state(_PARAMS["load_state"], timeout=timeout_ms)
                    matched = True
                except Exception:
                    matched = False
            elif wt == "popup":
                # popup 等待：监听 page 事件
                try:
                    async with page.context.expect_page(timeout=timeout_ms) as new_page_info:
                        pass  # 等待已存在；如果用户已点 click 触发 popup，这里能捕获
                    new_page = await new_page_info.value
                    await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                    data = {"url": new_page.url, "title": await new_page.title()}
                    matched = True
                except Exception:
                    matched = False
            elif wt == "filechooser":
                # P2-1: 等待文件选择对话框，可自动 set_files
                try:
                    async with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
                        pass
                    fc = await fc_info.value
                    fps = _PARAMS.get("file_paths")
                    if fps:
                        await fc.set_files(fps)
                    data = {"multiple": fc.is_multiple(), "page_url": page.url}
                    matched = True
                except Exception:
                    matched = False
            elif wt == "dialog":
                # P2-1: 等待 JS 对话框（alert/confirm/prompt），自动 accept/dismiss
                try:
                    async with page.expect_dialog(timeout=timeout_ms) as dlg_info:
                        pass
                    dlg = await dlg_info.value
                    data = {"type": dlg.type, "message": dlg.message, "page_url": page.url}
                    action = _PARAMS.get("dialog_action", "dismiss")
                    if action == "accept":
                        if dlg.type == "prompt":
                            await dlg.accept(_PARAMS.get("dialog_prompt_text") or "")
                        else:
                            await dlg.accept()
                    else:
                        await dlg.dismiss()
                    matched = True
                except Exception:
                    matched = False
            elif wt == "download":
                # P2-1: 等待文件下载，可自动 save_as 到 download_dir
                try:
                    async with page.expect_download(timeout=timeout_ms) as dl_info:
                        pass
                    dl = await dl_info.value
                    data = {
                        "url": dl.url,
                        "suggested_filename": dl.suggested_filename,
                        "page_url": page.url,
                    }
                    ddir = _PARAMS.get("download_dir")
                    if ddir:
                        import os as _os
                        _os.makedirs(ddir, exist_ok=True)
                        fname = dl.suggested_filename or "download"
                        save_path = _os.path.join(ddir, fname)
                        await dl.save_as(save_path)
                        data["saved_to"] = save_path
                        data["saved"] = True
                    matched = True
                except Exception:
                    matched = False
            print('OK:' + json.dumps({"matched": matched, "data": data}))
        except Exception as e:
            print(f'ERROR:{e}')

asyncio.run(main())
"""
    code = _build_playwright_script(params, body)
    success, message, elapsed = await _execute_playwright(code, int(req.timeout + 10))
    if success and message.startswith("{"):
        try:
            payload = json.loads(message)
            return BrowserWaitResponse(
                success=True,
                wait_type=req.wait_type,
                elapsed_ms=elapsed,
                matched=payload.get("matched", False),
                data=payload.get("data"),
            )
        except (json.JSONDecodeError, KeyError):
            pass
    if success:
        err = _classify_error(message, "verify", elapsed)
        return BrowserWaitResponse(
            success=False, wait_type=req.wait_type, elapsed_ms=elapsed,
            matched=False, error=err,
        )
    err = _classify_error(message, "wait", elapsed)
    return BrowserWaitResponse(
        success=False, wait_type=req.wait_type, elapsed_ms=elapsed,
        matched=False, error=err,
    )


# ---- wait_and_action (P0-2: 原子 wait+trigger API) ----

class BrowserWaitAndActionRequest(BaseSchema):
    """原子 wait+trigger 请求（评估文档 rerun3 P0-2）。

    解决 popup/filechooser/dialog 在 agent 顺序调用模式下时序不可靠的问题：
    - 旧模式：agent 调 wait_for → wait_for 返回 → agent 调 action 触发事件
      → 但 wait_for 已经返回，无法捕获 action 触发的事件
    - 新模式：单次调用内 enter expect_* context → 执行 trigger action → 退出 context
      → 同一协程内完成 wait+trigger，符合 Playwright expect_* 设计

    支持的 wait_type：
    - popup: 等待新标签弹出（target=_blank），返回新标签 url/title
    - filechooser: 等待文件选择对话框，可自动 set_files
    - dialog: 等待 JS 对话框（alert/confirm/prompt），按 dialog_action 处理

    trigger 是一个简化的 action 描述（click/fill/press/select），用于在 expect_*
    context 内触发事件。trigger 与 wait_for 在同一协程内执行，确保事件能被捕获。

    trigger 目标支持 trigger_node_id（来自 snapshot 的 node_id，反查 css_path，
    与 BrowserActionTarget.node_id 同语义，含 STALE_NODE 检查）、trigger_css、
    trigger_role+name、trigger_label、trigger_placeholder、trigger_testid、
    trigger_text。优先级：node_id > css > role > label > placeholder > testid > text。

    仅 session 模式可用（无 session 时使用子进程模型无法跨调用保持 expect_* context）。
    """
    session_id: str
    wait_type: str  # popup/filechooser/dialog
    # trigger action（在 expect_* context 内执行）
    trigger_action: str  # click/fill/press/select
    trigger_node_id: int | None = None  # 来自 snapshot 的 node_id（反查 css_path，仅 session 模式）
    trigger_css: str | None = None  # CSS 选择器（推荐）
    trigger_role: str | None = None
    trigger_name: str | None = None
    trigger_text: str | None = None
    trigger_testid: str | None = None
    trigger_label: str | None = None
    trigger_placeholder: str | None = None
    trigger_value: str | None = None  # fill 用
    trigger_key: str | None = None  # press 用
    # wait 参数
    timeout: float = 30.0
    # filechooser 用
    file_paths: list[str] | None = None
    # dialog 用
    dialog_action: str = "dismiss"  # accept/dismiss
    dialog_prompt_text: str | None = None


class BrowserWaitAndActionResponse(BaseSchema):
    success: bool
    wait_type: str
    matched: bool  # 是否等到事件
    trigger_success: bool  # trigger action 是否成功
    data: dict | None = None  # 事件信息（popup url/title、dialog type/message 等）
    elapsed_ms: int
    error: BrowserErrorResponse | None = None
    # 2026-08-06 Ticket 03：dialog 阻塞预检反馈（被动通道）
    # 仅在 wait_type != "dialog" 且 session 有 pending dialog 时设置
    blocked_by_dialog: bool = False
    blocking_dialog: dict | None = None


def _build_trigger_locator(page, req: BrowserWaitAndActionRequest):
    """从 wait_and_action 请求构造 Playwright locator（与 BrowserActionTarget 类似的优先级）。

    Returns: (locator, description) 或 (None, error_msg)
    """
    if req.trigger_css:
        return page.locator(req.trigger_css), f"css={req.trigger_css}"
    if req.trigger_role:
        if req.trigger_name:
            return page.get_by_role(req.trigger_role, name=req.trigger_name), \
                   f"role={req.trigger_role}:name={req.trigger_name}"
        return page.get_by_role(req.trigger_role), f"role={req.trigger_role}"
    if req.trigger_label:
        return page.get_by_label(req.trigger_label), f"label={req.trigger_label}"
    if req.trigger_placeholder:
        return page.get_by_placeholder(req.trigger_placeholder), \
               f"placeholder={req.trigger_placeholder}"
    if req.trigger_testid:
        return page.get_by_test_id(req.trigger_testid), f"testid={req.trigger_testid}"
    if req.trigger_text:
        return page.get_by_text(req.trigger_text), f"text={req.trigger_text}"
    return None, "no trigger target field provided"


@router.post("/wait_and_action", response_model=BrowserWaitAndActionResponse,
             operation_id="browser_wait_and_action")
async def browser_wait_and_action(req: BrowserWaitAndActionRequest):
    """Atomic wait+trigger: enter expect_* context, execute trigger action, exit context.

    Solves the timing issue in agent sequential call mode where wait_for returns
    before action triggers the event. This endpoint does expect_* + action in the
    same coroutine, which is how Playwright expect_* is designed to be used.

    Only session mode is supported (cross-request expect_* context is unreliable).

    Example — click a button that opens a popup:
      browser_wait_and_action(
        session_id="...", wait_type="popup",
        trigger_action="click", trigger_css="a[target=_blank]")
    Example — click a button that triggers a file chooser:
      browser_wait_and_action(
        session_id="...", wait_type="filechooser",
        trigger_action="click", trigger_css="#upload-btn",
        file_paths=["C:\\\\upload.pdf"])
    Example — click a button that triggers a JS alert:
      browser_wait_and_action(
        session_id="...", wait_type="dialog",
        trigger_action="click", trigger_css="#alert-btn",
        dialog_action="accept")
    Example — click by node_id from a previous snapshot:
      browser_wait_and_action(
        session_id="...", wait_type="dialog",
        trigger_action="click", trigger_node_id=42,
        dialog_action="accept")
    """
    valid_waits = {"popup", "filechooser", "dialog"}
    if req.wait_type not in valid_waits:
        return BrowserWaitAndActionResponse(
            success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
            elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="wait",
                debug_detail=f"wait_type must be one of {sorted(valid_waits)}",
                elapsed_ms=0,
            ),
        )
    valid_triggers = {"click", "fill", "press", "select"}
    if req.trigger_action not in valid_triggers:
        return BrowserWaitAndActionResponse(
            success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
            elapsed_ms=0,
            error=BrowserErrorResponse(
                error_code="UNSUPPORTED_ACTION",
                error_message=BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                phase="act",
                debug_detail=f"trigger_action must be one of {sorted(valid_triggers)}",
                elapsed_ms=0,
            ),
        )

    import time as _time
    start = _time.perf_counter()

    from .session.manager import get_session_manager
    mgr = await get_session_manager()
    sess = mgr.get_session(req.session_id)
    page = await mgr.get_page(req.session_id) if sess else None
    if page is None:
        elapsed = int((_time.perf_counter() - start) * 1000)
        return BrowserWaitAndActionResponse(
            success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
            elapsed_ms=elapsed,
            error=BrowserErrorResponse(
                error_code="TAB_NOT_FOUND",
                error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                phase="connect",
                debug_detail=f"session_id {req.session_id} 不存在或已失效",
                elapsed_ms=elapsed,
            ),
        )

    # 2026-08-06 Ticket 03：dialog 阻塞预检
    # 例外：wait_type="dialog" 不预检（其目的就是等 dialog，应允许触发）
    if req.wait_type != "dialog":
        from .dialog_endpoints import _check_dialog_block
        block = _check_dialog_block(sess)
        if block is not None:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserWaitAndActionResponse(
                success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
                elapsed_ms=elapsed,
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

    # P2-2: trigger_node_id 优先（反查 css_path，与 browser_action.node_id 同语义）
    # 含 STALE_NODE 检查：snapshot URL 变化或 >60s 过期则拒绝
    if req.trigger_node_id is not None:
        if sess is None or not sess.last_snapshot_nodes:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserWaitAndActionResponse(
                success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
                elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="STALE_NODE",
                    error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                    phase="locate",
                    debug_detail="trigger_node_id 需要 session 且已 snapshot；请先调用 browser_snapshot",
                    elapsed_ms=elapsed,
                ),
            )
        _now = _time.time()
        if sess.last_snapshot_url != page.url or (_now - sess.last_snapshot_at) > 60.0:
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserWaitAndActionResponse(
                success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
                elapsed_ms=elapsed,
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
        _matched_node = None
        for n in sess.last_snapshot_nodes:
            if n.get("node_id") == req.trigger_node_id:
                _matched_node = n
                break
        if not _matched_node or not _matched_node.get("css_path"):
            elapsed = int((_time.perf_counter() - start) * 1000)
            return BrowserWaitAndActionResponse(
                success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
                elapsed_ms=elapsed,
                error=BrowserErrorResponse(
                    error_code="STALE_NODE",
                    error_message=BROWSER_ERROR_CODES["STALE_NODE"],
                    phase="locate",
                    debug_detail=f"trigger_node_id={req.trigger_node_id} 不在最近 snapshot 中或无 css_path",
                    elapsed_ms=elapsed,
                ),
            )
        # 反查成功：把 css_path 注入 trigger_css，清空 trigger_node_id
        req = req.model_copy(update={
            "trigger_css": _matched_node["css_path"],
            "trigger_node_id": None,
        })

    # 构造 trigger locator
    loc, loc_desc = _build_trigger_locator(page, req)
    if loc is None:
        elapsed = int((_time.perf_counter() - start) * 1000)
        return BrowserWaitAndActionResponse(
            success=False, wait_type=req.wait_type, matched=False, trigger_success=False,
            elapsed_ms=elapsed,
            error=BrowserErrorResponse(
                error_code="INVALID_SELECTOR",
                error_message=BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                phase="locate",
                debug_detail=loc_desc,
                elapsed_ms=elapsed,
            ),
        )

    timeout_ms = int(req.timeout * 1000)
    matched = False
    trigger_success = False
    data = None
    try:
        # 清空 session 持久监听器队列中的旧事件，避免与本次 expect_* 混淆
        if sess is not None:
            if req.wait_type == "popup":
                sess.pending_popups.clear()
                sess.popup_event.clear()
            elif req.wait_type == "filechooser":
                sess.pending_filechoosers.clear()
                sess.filechooser_event.clear()
            elif req.wait_type == "dialog":
                sess.pending_dialogs.clear()
                sess.dialog_event.clear()

        if req.wait_type == "popup":
            try:
                async with page.context.expect_page(timeout=timeout_ms) as new_page_info:
                    # 在 context 内执行 trigger action
                    loc_first = loc.first
                    if req.trigger_action == "click":
                        await loc_first.click(timeout=timeout_ms)
                    elif req.trigger_action == "fill":
                        await loc_first.fill(req.trigger_value or "", timeout=timeout_ms)
                    elif req.trigger_action == "press":
                        await loc_first.press(req.trigger_key or "Enter", timeout=timeout_ms)
                    elif req.trigger_action == "select":
                        await loc_first.select_option(req.trigger_value, timeout=timeout_ms)
                    trigger_success = True
                new_page = await new_page_info.value
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                    title = await new_page.title()
                except Exception:
                    title = ""
                data = {"url": new_page.url, "title": title}
                matched = True
            except Exception as e:
                # 兜底：检查持久监听器队列
                if sess is not None:
                    pending = sess.pop_pending_popup()
                    if pending:
                        data = {"url": pending.get("url"), "title": pending.get("title")}
                        matched = True
                if not matched:
                    raise e

        elif req.wait_type == "filechooser":
            try:
                async with page.expect_file_chooser(timeout=timeout_ms) as fc_info:
                    loc_first = loc.first
                    if req.trigger_action == "click":
                        await loc_first.click(timeout=timeout_ms)
                    elif req.trigger_action == "fill":
                        await loc_first.fill(req.trigger_value or "", timeout=timeout_ms)
                    elif req.trigger_action == "press":
                        await loc_first.press(req.trigger_key or "Enter", timeout=timeout_ms)
                    elif req.trigger_action == "select":
                        await loc_first.select_option(req.trigger_value, timeout=timeout_ms)
                    trigger_success = True
                fc = await fc_info.value
                if req.file_paths:
                    await fc.set_files(req.file_paths)
                data = {"multiple": fc.is_multiple(), "page_url": page.url}
                matched = True
            except Exception as e:
                if sess is not None:
                    pending = sess.pop_pending_filechooser()
                    if pending:
                        fc_obj = sess.last_filechooser_obj
                        if req.file_paths and fc_obj is not None:
                            try:
                                await fc_obj.set_files(req.file_paths)
                            except Exception:
                                pass
                        sess.last_filechooser_obj = None
                        data = {"multiple": pending.get("multiple"), "page_url": pending.get("page_url")}
                        matched = True
                if not matched:
                    raise e

        elif req.wait_type == "dialog":
            # P0-2 修复 + 2026-08-06 改造：page.expect_dialog() 已移除，
            # 改用 persistent listener + dialog_event 模式。
            # 2026-08-06 改造：_on_dialog 不再立即 dismiss，dialog 触发后 page 操作会阻塞，
            # 所以 click 必须异步触发（asyncio.create_task），dialog_event set 后用
            # last_dialog_obj 主动 accept/dismiss，dialog 处理后 click task 才能完成。
            # dialog_action 现在真正生效（accept/prompt 输入），不再是意图声明。
            try:
                if sess is not None:
                    # session 模式：用持久监听器 + 异步触发 + 主动处理 dialog
                    sess.dialog_event.clear()
                    loc_first = loc.first

                    async def _do_trigger():
                        if req.trigger_action == "click":
                            await loc_first.click(timeout=timeout_ms)
                        elif req.trigger_action == "fill":
                            await loc_first.fill(req.trigger_value or "", timeout=timeout_ms)
                        elif req.trigger_action == "press":
                            await loc_first.press(req.trigger_key or "Enter", timeout=timeout_ms)
                        elif req.trigger_action == "select":
                            await loc_first.select_option(req.trigger_value, timeout=timeout_ms)

                    # 异步触发 action（click 可能阻塞，因为 dialog 触发后未被处理）
                    trigger_task = asyncio.create_task(_do_trigger())
                    # 等 dialog_event（dialog 触发后 handler set event + 存 last_dialog_obj）
                    try:
                        await asyncio.wait_for(sess.dialog_event.wait(), timeout=req.timeout)
                    except TimeoutError:
                        pass
                    pending = sess.pop_pending_dialog()
                    if pending:
                        # 主动处理 dialog（accept/dismiss），dialog 处理后 trigger_task 能完成
                        dlg = sess.last_dialog_obj
                        if dlg is not None:
                            try:
                                if req.dialog_action == "accept":
                                    if pending.get("type") == "prompt":
                                        await dlg.accept(req.dialog_prompt_text or "")
                                    else:
                                        await dlg.accept()
                                else:
                                    await dlg.dismiss()
                                sess.last_dialog_obj = None
                            except Exception:
                                pass
                        data = {
                            "type": pending.get("type"),
                            "message": pending.get("message"),
                            "page_url": pending.get("page_url"),
                            "auto_dismissed": pending.get("auto_dismissed", False),
                            "intended_action": req.dialog_action,
                        }
                        matched = True
                    # 等 trigger_task 完成（dialog 处理后 click 能完成）
                    try:
                        await asyncio.wait_for(trigger_task, timeout=5)
                        trigger_success = True
                    except TimeoutError:
                        trigger_task.cancel()
                        trigger_success = matched
                else:
                    # 无 session 子进程模式：无 persistent listener，dialog 会被 Playwright 自动 dismiss
                    # 注：此模式无法捕获 dialog 信息（无 listener），仅触发 action
                    loc_first = loc.first
                    if req.trigger_action == "click":
                        await loc_first.click(timeout=timeout_ms)
                    elif req.trigger_action == "fill":
                        await loc_first.fill(req.trigger_value or "", timeout=timeout_ms)
                    elif req.trigger_action == "press":
                        await loc_first.press(req.trigger_key or "Enter", timeout=timeout_ms)
                    elif req.trigger_action == "select":
                        await loc_first.select_option(req.trigger_value, timeout=timeout_ms)
                    trigger_success = True
                    matched = False  # 无 listener 无法捕获 dialog 信息
            except Exception as e:
                # 兜底：检查持久监听器队列 + 处理未 dismiss 的 dialog
                if sess is not None:
                    pending = sess.pop_pending_dialog()
                    if pending:
                        dlg = sess.last_dialog_obj
                        if dlg is not None:
                            try:
                                if req.dialog_action == "accept":
                                    if pending.get("type") == "prompt":
                                        await dlg.accept(req.dialog_prompt_text or "")
                                    else:
                                        await dlg.accept()
                                else:
                                    await dlg.dismiss()
                                sess.last_dialog_obj = None
                            except Exception:
                                pass
                        data = {
                            "type": pending.get("type"),
                            "message": pending.get("message"),
                            "page_url": pending.get("page_url"),
                            "auto_dismissed": pending.get("auto_dismissed", False),
                            "intended_action": req.dialog_action,
                        }
                        matched = True
                if not matched:
                    raise e

        elapsed = int((_time.perf_counter() - start) * 1000)
        return BrowserWaitAndActionResponse(
            success=True, wait_type=req.wait_type, matched=matched,
            trigger_success=trigger_success, data=data, elapsed_ms=elapsed,
        )
    except Exception as e:
        elapsed = int((_time.perf_counter() - start) * 1000)
        err = _classify_error(str(e), "wait", elapsed)
        return BrowserWaitAndActionResponse(
            success=False, wait_type=req.wait_type, matched=matched,
            trigger_success=trigger_success, elapsed_ms=elapsed, error=err,
        )
