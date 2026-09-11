"""SessionManager：浏览器会话管理器单例。

生命周期：第一次 create_session 时连接 CDP，shutdown 时关闭所有 page。
线程安全：所有方法必须在 async 上下文调用（FastAPI 路由天然 async）。

从 server/browser_session.py 迁移至 server/browser/session/ 包（Ticket 02）。
代码与原定义完全一致，仅调整 import 路径：
- TabSession：本包内 `from .state import TabSession`
- match_site_for_url / build_site_lessons_hint：`from ..site_lessons import ...`
- get_browser_config：`from ...config import get_browser_config`（包嵌套层级变化）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

from ..site_lessons import build_site_lessons_hint, match_site_for_url
from .state import TabSession

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger("localagent.browser_session")

# dialog 超时兜底秒数：5 分钟内未被 handle_dialog 处理则自动 dismiss
# （兼顾 LLM 推理时间：agent 收到 blocked_by_dialog 反馈 → 决策 → 调 handle_dialog）
_DIALOG_AUTO_DISMISS_TIMEOUT = 300.0


class SessionManager:
    """浏览器会话管理器单例。

    生命周期：第一次 create_session 时连接 CDP，shutdown 时关闭所有 page。
    线程安全：所有方法必须在 async 上下文调用（FastAPI 路由天然 async）。
    """

    def __init__(self) -> None:
        self._browser: Browser | None = None  # Playwright Browser
        self._playwright: Playwright | None = None  # Playwright 进程
        self._sessions: dict[str, TabSession] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 2026-09-03 防泄漏：是否已把 Chrome 下载落盘指到项目临时目录（browser 级，幂等）
        self._download_behavior_configured = False

    @property
    def is_connected(self) -> bool:
        return self._browser is not None

    async def _ensure_connected(self, cdp_url: str) -> None:
        """懒加载：第一次调用时连接 CDP。已连接则跳过。"""
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await asyncio.wait_for(
                pw.chromium.connect_over_cdp(cdp_url), timeout=10
            )
            self._browser = browser
            self._playwright = pw
            logger.info("BrowserSession: 已连接 CDP %s", cdp_url)

    async def _reconnect_if_needed(self, cdp_url: str) -> None:
        """检测连接是否已断开，断开则重连。"""
        if self._browser is None:
            await self._ensure_connected(cdp_url)
            return
        try:
            # 探活：browser.is_connected() 是同步方法
            if not self._browser.is_connected():
                logger.warning("BrowserSession: CDP 连接已断开，尝试重连")
                await self._cleanup_dead_browser()
                await self._ensure_connected(cdp_url)
        except Exception as e:
            logger.warning("BrowserSession: 探活失败 %s，尝试重连", e)
            await self._cleanup_dead_browser()
            await self._ensure_connected(cdp_url)

    async def _new_page_with_retry(self, context: BrowserContext, max_retries: int = 3) -> Page:
        """创建新 page，带重试机制处理 CDP "Failed to open a new tab" 竞态。

        根因：page.close() 后立即 context.new_page() 时，Chrome CDP 内部
        tab 管理可能未完成清理，导致 Target.createTarget 失败。
        重试间隔 0.5s × attempt，通常第二次即可成功。
        """
        last_err = None
        for attempt in range(max_retries):
            try:
                return await context.new_page()
            except Exception as e:
                last_err = e
                if "Failed to open a new tab" in str(e) and attempt < max_retries - 1:
                    logger.debug(
                        "BrowserSession: new_page 失败（attempt=%d），0.5s 后重试: %s",
                        attempt + 1, e,
                    )
                    await asyncio.sleep(0.5)
                    continue
                raise
        raise last_err  # type: ignore[misc]

    async def _cleanup_dead_browser(self) -> None:
        """清理已断开的 browser 句柄和所有 session。"""
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        # 所有 session 的 page 已失效，清空表
        if self._sessions:
            logger.info("BrowserSession: 清理 %d 个失效 session", len(self._sessions))
        self._sessions.clear()
        self._download_behavior_configured = False

    @staticmethod
    def _get_download_dir() -> str:
        """下载沙箱目录：<项目根>/temp/browser_<data_drive>:/Downloads（避免污染用户 <data_drive>:/Downloads）。"""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]  # server/browser/session/ -> 仓库根
        return str(repo_root / "temp" / "browser_<data_drive>:/Downloads")

    async def _configure_download_behavior(self, page: Page | None = None) -> None:
        """把 CDP 浏览器的下载落盘指到项目临时目录（每次 create_session 强制配置）。

        2026-09-03 防泄漏根因：connect_over_cdp 连真实 Chrome 后若不设置下载行为，
        Chrome 一切下载默认落盘浏览器默认下载目录（用户 <data_drive>:/Downloads）——e2e download
        测试每轮泄漏 1 个 test_download*.txt 到用户下载夹，save_as 只是另存一份、
        拦不住 Chrome 先落盘。cancel 路径虽能拦住，但 save_as 路径拦不住。

        注意：不能加"已配置就跳过"的幂等短路——实测 Playwright 的 Download.save_as
        在 CDP 模式下会自己重设 Browser.setDownloadBehavior 的 downloadPath，首次
        配置只对第一次下载生效，之后下载又回落到默认目录。故每次 create_session
        都强制重发一次（单次 CDP send 开销可忽略）。
        """
        try:
            browser = self._browser
            ctx = browser.contexts[0] if browser is not None and browser.contexts else None
            if ctx is None:
                return
            target = page if page is not None and not page.is_closed() else None
            if target is None:
                for p in ctx.pages:
                    if not p.is_closed():
                        target = p
                        break
            if target is None:
                return  # 暂无可发 CDP 的 page，留待下次 create_session
            dl_dir = self._get_download_dir()
            os.makedirs(dl_dir, exist_ok=True)
            cdp = await ctx.new_cdp_session(target)
            await cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": dl_dir,
                "eventsEnabled": True,
            })
            self._download_behavior_configured = True
            logger.debug("BrowserSession: 下载落盘已指向沙箱目录 %s", dl_dir)
        except Exception as e:
            logger.warning("BrowserSession: 配置下载行为失败（不影响使用，仅可能泄漏到默认目录）%s", e)

    async def create_session(
        self,
        url_pattern: str | None = None,
        url: str | None = None,
        cdp_url: str | None = None,
    ) -> dict:
        """创建或复用一个 TabSession。

        优先级：
        1. url_pattern：找现有 page（url 包含 pattern），命中则复用（显式接管已有 tab）
        2. url：新建 page 并 goto url
        3. 都不传：新建专用空白 tab（2026-09-03 起不复用 pages[0]，避免
           session 意外绑定用户真实 tab/扩展窗口页）

        Returns: {session_id, tab_id, url, title, created}
        """
        if cdp_url is None:
            from ...config import get_browser_config
            cfg = get_browser_config()
            cdp_url = f'http://127.0.0.1:{cfg["debug_port"]}'

        await self._reconnect_if_needed(cdp_url)
        # _reconnect_if_needed 已确保 _browser 非 None
        browser = self._browser
        assert browser is not None
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            # 极端情况：browser 已连接但还没有 context
            context = await browser.new_context()

        page = None
        # 1) url_pattern 匹配（跳过已关闭的 page）
        if url_pattern:
            for p in context.pages:
                if not p.is_closed() and url_pattern in p.url:
                    page = p
                    break
        # 2) url 新建
        if page is None and url:
            page = await self._new_page_with_retry(context)
            await page.goto(url, wait_until="domcontentloaded")
        # 3) 都不传：一律新建专用空白 tab。
        # 禁止改为复用已有 page（如 contexts[0].pages[0]）：那会抓到用户真实 tab 或
        # 扩展窗口页，配合 browser_session_close 的 close_page=True 默认值，可能把
        # 用户最后一个真实窗口关掉，导致整个调试浏览器退出、9222 消失。
        if page is None:
            page = await self._new_page_with_retry(context)

        # assert 仅为类型窄化——上面三个分支均已赋值非 None page，
        # pyright 需要这句才能放行后续的 page 调用。
        assert page is not None

        # 拿 CDP target id（page 的内部 _impl 对象上有 target_id）
        tab_id = ""
        tab_id_resolved = False
        try:
            # Playwright 内部 API：page._impl._guid 或 _target_info
            # 更稳定的做法：通过 CDP HTTP /json/list 找 url 匹配的 target id
            # T07：async 函数中同步 urllib 换成 httpx.AsyncClient，避免阻塞事件循环
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=3.0) as _client:
                _resp = await _client.get(cdp_url + "/json/list")
                targets = _resp.json()
            page_url = page.url
            for t in targets:
                if t.get("type") == "page" and t.get("url") == page_url:
                    tab_id = t.get("id", "")
                    tab_id_resolved = bool(tab_id)
                    break
        except Exception:
            # 反查失败时保持 tab_id_resolved=False，让调用方知道 tab_id 不可用于跨调用定位
            pass

        # 2026-09-03 防泄漏：把浏览器下载落盘指到项目临时目录（browser 级幂等，page 已就绪可发 CDP）
        await self._configure_download_behavior(page)

        session_id = uuid.uuid4().hex
        try:
            title = await page.title()
        except Exception:
            title = ""
        # P2-1: 按 url 域名自动匹配站点经验库
        site_lessons: list[dict] = []
        if url:
            try:
                site_lessons = match_site_for_url(url)
                if site_lessons:
                    logger.info(
                        "BrowserSession: session %s 加载 %d 条站点经验（url=%s）",
                        session_id, len(site_lessons), url,
                    )
            except Exception as e:
                logger.debug("BrowserSession: site lessons 匹配失败 %s", e)
        session = TabSession(
            session_id=session_id,
            tab_id=tab_id,
            page=page,
            url=page.url,
            title=title,
            site_lessons=site_lessons,
        )
        # 注册控制台日志收集（持久 session 才能跨调用增量返回）
        def _on_console(msg):
            try:
                session.console_logs.append({
                    "ts": time.time(),
                    "level": msg.type,
                    "text": msg.text,
                    "url": msg.location.get("url", "") if hasattr(msg, "location") else "",
                    "line": msg.location.get("lineNumber", 0) if hasattr(msg, "location") else 0,
                    "column": msg.location.get("columnNumber", 0) if hasattr(msg, "location") else 0,
                })
                # P1-2 修复：环形缓冲，溢出时丢弃最旧的并累计 dropped_count
                max_len = session.console_logs_max_len
                if len(session.console_logs) > max_len:
                    overflow = len(session.console_logs) - max_len
                    session.console_logs = session.console_logs[overflow:]
                    session.console_logs_dropped_count += overflow
            except Exception:
                pass
        page.on("console", _on_console)

        def _on_pageerror(err):
            try:
                session.console_logs.append({
                    "ts": time.time(),
                    "level": "error",
                    "text": err.message,
                    "url": "",
                    "line": 0,
                    "column": 0,
                })
                max_len = session.console_logs_max_len
                if len(session.console_logs) > max_len:
                    overflow = len(session.console_logs) - max_len
                    session.console_logs = session.console_logs[overflow:]
                    session.console_logs_dropped_count += overflow
            except Exception:
                pass
        page.on("pageerror", _on_pageerror)

        # 监听 page close，自动清理 session
        # pyright stub 将 "close" 事件 handler 标注为 (Page) -> Awaitable[None] | None，
        # 用 *args 兼容（运行时无论传不传参都不会 TypeError）。
        def _on_close(*_args: object) -> None:
            self._sessions.pop(session_id, None)
            logger.debug("BrowserSession: page 关闭，session %s 已清理", session_id)
        page.on("close", _on_close)

        # 持久 dialog/popup/filechooser 监听器：
        # expect_* context manager 跨 HTTP 请求时序不可靠（agent 顺序调用模式下
        # wait_for 返回后才会调用 click，dialog 此时还没产生），所以改为持久监听器
        # + 队列模式。wait_for 优先消费队列，再等待新事件。
        #
        # dialog 生命周期：_on_dialog 不自动 dismiss——存引用 + 启动 5 分钟超时兜底 task。
        # 在超时前 handle_dialog 可调 dlg.accept(prompt_text)/dlg.dismiss()；
        # 超时后兜底 task 自动 dismiss 并清空 last_dialog_obj，避免 dialog 永久阻塞 page。
        # 超时秒数 DIALOG_AUTO_DISMISS_TIMEOUT = 300（5 分钟，兼顾 LLM 推理时间）。
        async def _on_dialog(dlg):
            # 必须是 async handler：Playwright Python async API 下 dlg.dismiss()
            # 返回 coroutine，sync 调用不会真正 dismiss，dialog 会一直阻塞 click 等动作
            # 直到超时。
            try:
                session._append_pending("pending_dialogs", {
                    "type": dlg.type,
                    "message": dlg.message,
                    "page_url": page.url,
                    "ts": time.time(),
                    "auto_dismissed": False,
                })
                session.last_dialog_obj = dlg
                session.dialog_auto_dismissed = False
                session.dialog_event.set()
            except Exception:
                pass
            # 启动超时兜底 task：5 分钟内未被 handle_dialog 处理则自动 dismiss
            # （不放在 finally 块：finally 会立即执行，我们要的是延迟 dismiss）
            task = asyncio.ensure_future(
                _auto_dismiss_dialog_after(dlg, session, _DIALOG_AUTO_DISMISS_TIMEOUT)
            )
            session._dialog_timeout_tasks.add(task)
            task.add_done_callback(session._dialog_timeout_tasks.discard)
        page.on("dialog", _on_dialog)

        async def _auto_dismiss_dialog_after(dlg, sess: TabSession, timeout: float) -> None:
            """超时兜底：timeout 秒内未被 handle_dialog 处理则自动 dismiss。

            判断依据：sess.last_dialog_obj 仍是传入的 dlg（未被 handle_dialog 处理/清空）。
            """
            try:
                await asyncio.sleep(timeout)
                # 检查 last_dialog_obj 是否仍是这个 dlg（未被 handle_dialog 处理）
                # 用 id 比较，避免 Dialog 对象 __eq__ 行为不确定
                if sess.last_dialog_obj is dlg:
                    try:
                        await dlg.dismiss()
                    except Exception:
                        pass
                    sess.last_dialog_obj = None
                    sess.dialog_auto_dismissed = True
                    # 标记队列中对应事件为 auto_dismissed
                    for ev in reversed(sess.pending_dialogs):
                        if not ev.get("auto_dismissed"):
                            ev["auto_dismissed"] = True
                            break
                    logger.debug(
                        "BrowserSession: dialog 超时兜底自动 dismiss（%ss）", timeout
                    )
            except asyncio.CancelledError:
                # session close 时取消，正常行为
                pass
            except Exception as e:
                logger.debug("BrowserSession: _auto_dismiss_dialog_after 异常 %s", e)

        def _on_popup(new_page):
            try:
                # 2026-09-03 归属修复：监听器从 context 级（page.context.on("page")）
                # 改为 page 级——只收本 session 页面 spawn 的 popup（window.open /
                # target=_blank），不再串扰其他 session 新建页污染 pending_popups；
                # 同时持有 popup Page 引用，供 close_session 连坐关闭（孤儿 tab 修复）。
                session.popup_pages.add(new_page)
                new_page.once("close", lambda: session.popup_pages.discard(new_page))
                # 异步收集 popup 信息（new_page.title() 是 async）
                async def _collect():
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                        title = await new_page.title()
                    except Exception:
                        title = ""
                    session._append_pending("pending_popups", {
                        "url": new_page.url,
                        "title": title,
                        "ts": time.time(),
                    })
                    session.popup_event.set()
                # strong reference 防 GC：asyncio.ensure_future 创建的 task 若不持有引用，
                # 会被事件循环的弱引用机制回收（Python 文档明确警告），导致 _collect 永不执行。
                task = asyncio.ensure_future(_collect())
                session._popup_tasks.add(task)
                task.add_done_callback(session._popup_tasks.discard)
            except Exception:
                pass
        page.on("popup", _on_popup)

        def _on_filechooser(fc):
            try:
                session._append_pending("pending_filechoosers", {
                    "multiple": fc.is_multiple(),
                    "page_url": page.url,
                    "ts": time.time(),
                })
                session.last_filechooser_obj = fc
                session.filechooser_event.set()
            except Exception:
                pass
        page.on("filechooser", _on_filechooser)

        def _on_download(dl):
            try:
                session._append_pending("pending_<data_drive>:/Downloads", {
                    "url": dl.url,
                    "suggested_filename": dl.suggested_filename,
                    "page_url": page.url,
                    "ts": time.time(),
                })
                session.last_download_obj = dl
                session.download_event.set()
            except Exception as e:
                logger.warning("BrowserSession: _on_download 异常 %s", e)
        page.on("download", _on_download)

        self._sessions[session_id] = session
        # 启动清理任务（如未启动）
        self._ensure_cleanup_task()
        return {
            "session_id": session_id,
            "tab_id": tab_id,
            "tab_id_resolved": tab_id_resolved,
            "url": page.url,
            "title": title,
            "created": True,
            "site_lessons": site_lessons,
            "site_lessons_hint": build_site_lessons_hint(site_lessons),
        }

    def get_session(self, session_id: str) -> TabSession | None:
        """同步获取 session（不 touch，不重连）。"""
        return self._sessions.get(session_id)

    async def get_page(self, session_id: str) -> Page | None:
        """获取 page 句柄并 touch。session 不存在返回 None。"""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        # 探活：page 是否已关闭
        if s.page.is_closed():
            self._sessions.pop(session_id, None)
            return None
        s.touch()
        return s.page

    def list_sessions(self) -> list[dict]:
        """列出所有活跃 session。"""
        now = time.time()
        return [
            {
                "session_id": s.session_id,
                "tab_id": s.tab_id,
                "url": s.url,
                "title": s.title,
                "created_at": s.created_at,
                "last_used_at": s.last_used_at,
                "idle_secs": int(now - s.last_used_at),
                "console_logs_buffered": len(s.console_logs),
                "site_lessons_count": len(s.site_lessons),
                "site_lessons_domains": [lesson.get("domain", "") for lesson in s.site_lessons],
            }
            for s in self._sessions.values()
        ]

    def iter_sessions(self):
        """迭代所有活跃的 TabSession 对象（供 browser_status 聚合 dialog 状态用）。

        返回 snapshot 引用（不复制），调用方只读取，不修改。
        """
        return list(self._sessions.values())

    async def get_context(self) -> BrowserContext | None:
        """获取 BrowserContext（contexts[0]），用于 context 级操作
        （set_http_credentials / grant_permissions 等）。

        未连接时返回 None（不抛异常，由调用方决定如何处理）。
        """
        if self._browser is None:
            # 尝试重连一次（可能后端启动后第一次调用）
            try:
                from ...config import get_browser_config
                cfg = get_browser_config()
                cdp_url = f'http://127.0.0.1:{cfg["debug_port"]}'
                await self._reconnect_if_needed(cdp_url)
            except Exception:
                return None
        if self._browser is None:
            return None
        contexts = self._browser.contexts
        if not contexts:
            return None
        return contexts[0]

    async def close_session(self, session_id: str, close_page: bool = True) -> bool:
        """关闭 session。close_page=True（默认）时同时关闭浏览器 page。

        历史背景：曾因 close_page=True 触发 Chrome CDP "Failed to open a new tab" 异常
        而全链路改为 False，但经 2026-08-05 验证该异常已不存在（Playwright/Chrome 升级修复）。
        现恢复默认 True，确保 session 关闭时 page 也被清理，避免 Chrome tab 无限累积。
        """
        s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        # 取消未完成的 dialog 超时兜底 task，避免 asyncio task 泄漏
        for task in list(s._dialog_timeout_tasks):
            task.cancel()
        s._dialog_timeout_tasks.clear()
        # 2026-09-03 防泄漏兜底：关 page 前 cancel 未消费的 download 对象
        # （download 事件触发后若既未 save_as 也未 cancel，CDP 连真实 Chrome 会把
        # 下载落盘到浏览器默认下载目录——实测泄漏 test_download*.txt 到用户 <data_drive>:/Downloads）
        if s.last_download_obj is not None:
            try:
                await s.last_download_obj.cancel()
            except Exception:
                pass  # 下载已完成/已被保存，cancel 失败属正常
            s.last_download_obj = None
        if close_page and not s.page.is_closed():
            try:
                await s.page.close()
            except Exception as e:
                logger.debug("BrowserSession: 关闭 page 失败 %s", e)
        # 2026-09-03 popup 归属修复：连坐关闭本 session 页面 spawn 的 popup
        # （window.open 的子页若不关会成为孤儿 tab，每轮 e2e 泄漏 4-5 个）
        if close_page:
            for popup_page in list(s.popup_pages):
                try:
                    if not popup_page.is_closed():
                        await popup_page.close()
                except Exception as e:
                    logger.debug("BrowserSession: 关闭 popup page 失败 %s", e)
            s.popup_pages.clear()
        return True

    async def close_all(self) -> None:
        """关闭所有 session（shutdown 时调用），同时关闭对应的浏览器 page。"""
        for sid in list(self._sessions.keys()):
            await self.close_session(sid, close_page=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None

    def _ensure_cleanup_task(self) -> None:
        """启动后台清理任务（每 60s 扫一次）。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        except RuntimeError:
            # 无事件循环（测试环境）时跳过
            pass

    async def _cleanup_loop(self) -> None:
        """后台循环：清理 idle 超时的 session。"""
        from ...config import get_browser_config
        while True:
            try:
                await asyncio.sleep(60)
                cfg = get_browser_config()
                timeout = cfg.get("session_idle_timeout_secs", 300)
                max_count = cfg.get("session_max_count", 20)
                now = time.time()
                # 1) 清理 idle 超时
                expired = [
                    sid for sid, s in self._sessions.items()
                    if now - s.last_used_at > timeout
                ]
                for sid in expired:
                    await self.close_session(sid, close_page=True)
                    logger.info("BrowserSession: session %s idle 超时已清理（page 已关闭）", sid)
                # 2) LRU 淘汰：超过 max_count 时按 last_used_at 最早的关闭
                if len(self._sessions) > max_count:
                    sorted_sessions = sorted(
                        self._sessions.items(), key=lambda kv: kv[1].last_used_at
                    )
                    excess = len(self._sessions) - max_count
                    for sid, _ in sorted_sessions[:excess]:
                        await self.close_session(sid, close_page=True)
                        logger.info("BrowserSession: session %s LRU 淘汰（page 已关闭）", sid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BrowserSession: cleanup loop 异常 %s", e)


# 单例
_manager: SessionManager | None = None
_manager_lock = asyncio.Lock()


async def get_session_manager() -> SessionManager:
    """获取 SessionManager 单例（async 安全）。"""
    global _manager
    if _manager is not None:
        return _manager
    async with _manager_lock:
        if _manager is None:
            _manager = SessionManager()
    return _manager


async def reset_session_manager() -> None:
    """shutdown 时调用，清理单例。"""
    global _manager
    if _manager is not None:
        await _manager.close_all()
        _manager = None
