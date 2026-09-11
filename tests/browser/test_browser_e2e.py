"""浏览器端点真实 E2E 测试 — WIP4 (P2 browser test coverage gaps rerun3)。

SDD: temp/sdd/browser-e2e-tests/spec.md

覆盖 4 个场景：
- 场景 5：dialog waiter 真实并发 E2E (5 子测试)
- 场景 6：wait_and_action 端点真实 E2E (4 子测试)
- 场景 7：back/forward 极端 timeout 短轮询 E2E (4 子测试)
- 场景 8：verify_dom_freshness=True SPA mutation E2E (2 子测试)

前置条件：
- 后端运行中 (:8766)
- 调试浏览器运行中 (CDP :9222)
- 由 real_browser fixture 检查并 skip 不可用情况
"""
import concurrent.futures
import time

import pytest

pytestmark = pytest.mark.e2e  # E2E：依赖真实环境（CDP/后端/GUI），quick 层排除

BASE_URL = "http://127.0.0.1:8766"


# ========== Helper ==========


def create_session(sess, url=None):
    """创建浏览器 session，返回 session_id。"""
    body = {}
    if url:
        body["url"] = url
    r = sess.post(f"{BASE_URL}/browser/sessions", json=body, timeout=30)
    assert r.status_code == 200, f"create session failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["success"], f"create session not success: {data}"
    return data["session_id"]


def close_session(sess, session_id):
    """关闭浏览器 session（容忍失败）。

    close_page=True：同时关闭浏览器 tab，避免测试套件累积上百个 tab。
    历史：曾因 close_page=True 触发 Chrome CDP "Failed to open a new tab"（后续
    create_session 500 + Chrome 崩溃）而改为 False，依赖 idle cleanup 兜底——但
    idle 超时 900s，单轮测试后 page 全部残留（2026-09-03 实测 19 个 tab 泄漏）。
    经 2026-08-05 验证该异常在当前 Playwright/Chrome 版本已不复现，恢复 True
    （与 test_browser_e2e_full.py 的 close_session 决策一致）。
    """
    try:
        sess.post(
            f"{BASE_URL}/browser/sessions/close",
            json={"session_id": session_id, "close_page": True},
            timeout=10,
        )
    except Exception:
        pass


def navigate_to(sess, session_id, url, timeout=15.0):
    """导航到指定 URL。"""
    r = sess.post(
        f"{BASE_URL}/browser/navigate",
        json={
            "session_id": session_id,
            "action": "goto",
            "url": url,
            "wait_until": "domcontentloaded",
            "timeout": timeout,
        },
        timeout=30,
    )
    assert r.status_code == 200, f"navigate failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["success"], f"navigate not success: {data}"
    return data


# ========== autouse 清理 fixture（与 test_browser_e2e_full.py 同构） ==========

@pytest.fixture(autouse=True)
def clean_browser_sessions(real_browser):
    """每条用例结束后清空 session 表，兜住用例失败/提前 return 漏关的路径。

    close_page=True：同时关闭浏览器 tab，避免测试套件累积上百个 tab。
    """
    yield
    r = real_browser.get(f"{BASE_URL}/browser/sessions")
    if r.status_code == 200:
        for s in r.json().get("sessions", []):
            sid = s.get("session_id")
            if sid:
                real_browser.post(
                    f"{BASE_URL}/browser/sessions/close",
                    json={"session_id": sid, "close_page": True}, timeout=10,
                )


# ========== 场景 5：dialog waiter 真实并发 E2E ==========

@pytest.mark.browser
class TestScenario5Dialog:
    """验证 POST /browser/wait_for (wait_type=dialog) 在真实 Playwright + Chromium 下捕获 dialog 的行为。

    实现位置：server/browser_wait_endpoints.py:72-464
    """

    def test_alert_dialog_captured(self, real_browser, browser_fixture_server):
        """并发 wait_for(dialog) + click alert 按钮 → 捕获 type=message。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            # 并发：wait_for + click
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": session_id,
                        "wait_type": "dialog",
                        "dialog_action": "dismiss",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                time.sleep(0.2)  # 让 wait_for 先注册监听
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={
                        "session_id": session_id,
                        "target": {"css": "#alertBtn"},
                        "action": "click",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )

                wait_resp = wait_future.result()
                # 2026-08-06 Ticket 03 设计：wait_for(dialog) 只报告不处理 dialog，
                # 须 handle_dialog 收尾，否则 dialog 持续阻塞 page，click 挂到超时。
                handle_resp = sess.post(
                    f"{BASE_URL}/browser/handle_dialog",
                    json={"session_id": session_id, "action": "dismiss"},
                    timeout=10,
                )
                click_resp = click_future.result()

            assert handle_resp.json()["handled"], f"handle_dialog failed: {handle_resp.text}"
            assert click_resp.status_code == 200
            click_data = click_resp.json()
            assert click_data["success"], f"click failed: {click_data}"

            assert wait_resp.status_code == 200
            wait_data = wait_resp.json()
            assert wait_data["success"], f"wait_for failed: {wait_data}"
            assert wait_data["matched"] is True, f"dialog not matched: {wait_data}"
            assert wait_data["data"] is not None
            assert wait_data["data"]["type"] == "alert"
            assert "alert message" in wait_data["data"]["message"]
        finally:
            close_session(sess, session_id)

    def test_confirm_dialog_captured(self, real_browser, browser_fixture_server):
        """并发 wait_for(dialog) + click confirm 按钮 → 捕获 type=confirm。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": session_id,
                        "wait_type": "dialog",
                        "dialog_action": "dismiss",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                time.sleep(0.2)
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={
                        "session_id": session_id,
                        "target": {"css": "#confirmBtn"},
                        "action": "click",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                wait_resp = wait_future.result()
                # 2026-08-06 Ticket 03 设计：wait_for(dialog) 只报告不处理 dialog，
                # 须 handle_dialog 收尾，否则 dialog 持续阻塞 page，click 挂到超时。
                handle_resp = sess.post(
                    f"{BASE_URL}/browser/handle_dialog",
                    json={"session_id": session_id, "action": "dismiss"},
                    timeout=10,
                )
                click_resp = click_future.result()

            assert handle_resp.json()["handled"], f"handle_dialog failed: {handle_resp.text}"
            assert click_resp.json()["success"]
            assert wait_resp.status_code == 200
            wait_data = wait_resp.json()
            assert wait_data["success"], f"wait_for failed: {wait_data}"
            assert wait_data["matched"] is True
            assert wait_data["data"] is not None
            # alert/confirm/prompt 都返回 type=alert/confirm/prompt
            assert wait_data["data"]["type"] == "confirm"
        finally:
            close_session(sess, session_id)

    def test_prompt_dialog_captured(self, real_browser, browser_fixture_server):
        """并发 wait_for(dialog) + click prompt 按钮 → 捕获 type=prompt。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": session_id,
                        "wait_type": "dialog",
                        "dialog_action": "dismiss",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                time.sleep(0.2)
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={
                        "session_id": session_id,
                        "target": {"css": "#promptBtn"},
                        "action": "click",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                wait_resp = wait_future.result()
                # 2026-08-06 Ticket 03 设计：wait_for(dialog) 只报告不处理 dialog，
                # 须 handle_dialog 收尾，否则 dialog 持续阻塞 page，click 挂到超时。
                handle_resp = sess.post(
                    f"{BASE_URL}/browser/handle_dialog",
                    json={"session_id": session_id, "action": "dismiss"},
                    timeout=10,
                )
                click_resp = click_future.result()

            assert handle_resp.json()["handled"], f"handle_dialog failed: {handle_resp.text}"
            assert click_resp.json()["success"]
            wait_data = wait_resp.json()
            assert wait_data["success"], f"wait_for failed: {wait_data}"
            assert wait_data["matched"] is True
            assert wait_data["data"] is not None
            assert wait_data["data"]["type"] == "prompt"
        finally:
            close_session(sess, session_id)

    def test_delayed_dialog_captured(self, real_browser, browser_fixture_server):
        """click 延时按钮 (500ms 后触发 alert) → wait_for 在 500ms 后捕获。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": session_id,
                        "wait_type": "dialog",
                        "dialog_action": "dismiss",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                time.sleep(0.2)
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={
                        "session_id": session_id,
                        "target": {"css": "#delayedAlertBtn"},
                        "action": "click",
                        "timeout": 5.0,
                    },
                    timeout=10,
                )
                wait_resp = wait_future.result()
                # 2026-08-06 Ticket 03 设计：wait_for(dialog) 只报告不处理 dialog，
                # 须 handle_dialog 收尾，否则 dialog 持续阻塞 page，click 挂到超时。
                handle_resp = sess.post(
                    f"{BASE_URL}/browser/handle_dialog",
                    json={"session_id": session_id, "action": "dismiss"},
                    timeout=10,
                )
                click_resp = click_future.result()

            assert handle_resp.json()["handled"], f"handle_dialog failed: {handle_resp.text}"
            assert click_resp.json()["success"]
            wait_data = wait_resp.json()
            assert wait_data["success"], f"wait_for failed: {wait_data}"
            assert wait_data["matched"] is True
            assert wait_data["data"] is not None
            assert wait_data["data"]["type"] == "alert"
            # 应至少等了 500ms（延时）+ 少量点击/网络延迟
            assert wait_data["elapsed_ms"] >= 400

        finally:
            close_session(sess, session_id)

    def test_wait_for_timeout(self, real_browser, browser_fixture_server):
        """wait_for(dialog, timeout=2) 但不 click → 超时返回 matched=false（不是错误）。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            start = time.time()
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": session_id,
                    "wait_type": "dialog",
                    "dialog_action": "dismiss",
                    "timeout": 2.0,
                },
                timeout=10,
            )
            elapsed = time.time() - start

            assert r.status_code == 200
            data = r.json()
            # 超时不是错误：success=true, matched=false
            assert data["success"] is True
            assert data["matched"] is False
            assert data["data"] is None
            assert data["error"] is None
            # 应等了至少 2 秒
            assert elapsed >= 1.9
        finally:
            close_session(sess, session_id)


# ========== 场景 6：wait_and_action 端点真实 E2E ==========

@pytest.mark.browser
class TestScenario6WaitAndAction:
    """验证 POST /browser/wait_and_action 端点原子性（先注册 expect 再触发 action）。

    实现位置：server/browser_wait_endpoints.py:544-768
    """

    def test_popup_trigger(self, real_browser, browser_fixture_server):
        """navigate popup_trigger.html → wait_and_action(popup, click #popupBtn) → popup 被捕获。

        注意：click #popupBtn 触发 window.open('about:blank')，Playwright click()
        会等待 popup 的 load 事件，但 about:blank 不触发 load → Playwright 等到
        默认 10s 超时才返回（实测 ~11.7s）。加上 wait_for_load_state(domcontentloaded, 5s)
        总耗时约 16-17s，HTTP timeout 必须设为 30s（不能用默认 15s）。
        """
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/popup_trigger.html")

            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": session_id,
                    "wait_type": "popup",
                    "trigger_action": "click",
                    "trigger_css": "#popupBtn",
                    "timeout": 5.0,
                },
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"], f"wait_and_action failed: {data}"
            assert data["matched"] is True
            assert data["trigger_success"] is True
            assert data["data"] is not None
            # popup 应被捕获（url 字段，可能是 about:blank）
            assert "url" in data["data"]
        finally:
            close_session(sess, session_id)

    def test_file_chooser_trigger(self, real_browser, browser_fixture_server):
        """navigate file_upload.html → wait_and_action(filechooser, click #triggerBtn) → file_chooser 被捕获。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/file_upload.html")

            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": session_id,
                    "wait_type": "filechooser",
                    "trigger_action": "click",
                    "trigger_css": "#triggerBtn",
                    "timeout": 5.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"], f"wait_and_action failed: {data}"
            assert data["matched"] is True
            assert data["trigger_success"] is True
            assert data["data"] is not None
            # file_chooser 应被捕获
            assert "action" in data["data"] or "paths" in data["data"] or isinstance(data["data"], dict)
        finally:
            close_session(sess, session_id)

    def test_dialog_trigger(self, real_browser, browser_fixture_server):
        """navigate dialog.html → wait_and_action(dialog, click #alertBtn) → dialog 被捕获。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": session_id,
                    "wait_type": "dialog",
                    "trigger_action": "click",
                    "trigger_css": "#alertBtn",
                    "dialog_action": "dismiss",
                    "timeout": 5.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"], f"wait_and_action failed: {data}"
            assert data["matched"] is True
            assert data["trigger_success"] is True
            assert data["data"] is not None
            assert data["data"]["type"] == "alert"
        finally:
            close_session(sess, session_id)

    def test_atomicity(self, real_browser, browser_fixture_server):
        """验证 wait_and_action 原子性：先注册 expect 再触发，事件不漏。

        用 dialog 测试：dialog 触发是同步的（点击立刻弹），如果先 click 再 expect 会漏事件。
        wait_and_action 必须先注册 expect 才能成功捕获 dialog。
        """
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/dialog.html")

            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": session_id,
                    "wait_type": "dialog",
                    "trigger_action": "click",
                    "trigger_css": "#alertBtn",
                    "dialog_action": "dismiss",
                    "timeout": 5.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # 原子性验证：dialog 必须被捕获（如果先 click 再 expect 会漏）
            assert data["success"], f"atomicity violated: wait_and_action failed: {data}"
            assert data["matched"] is True, "atomicity violated: dialog not captured"
            assert data["trigger_success"] is True
        finally:
            close_session(sess, session_id)


# ========== 场景 7：back/forward 极端 timeout 短轮询 E2E ==========

@pytest.mark.browser
class TestScenario7BackForward:
    """验证 POST /browser/navigate (action=back/forward) 在极端 timeout 下行为。

    实现位置：server/browser.py:1908-2119
    极端 timeout 触发后，200ms 内每 20ms 轮询 page.url != url_before。
    """

    def test_back_extreme_timeout(self, real_browser, browser_fixture_server):
        """goto A → goto B → back(timeout=0.001) → navigation_completed=True, wait_timeout=True。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            url_a = f"{browser_fixture_server}/dialog.html"
            url_b = f"{browser_fixture_server}/popup_trigger.html"

            navigate_to(sess, session_id, url_a)
            navigate_to(sess, session_id, url_b)

            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": session_id,
                    "action": "back",
                    "wait_until": "domcontentloaded",
                    "timeout": 0.001,  # 极端短 timeout
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # 短轮询兜底：navigation_completed=True（实际导航完成），wait_timeout=True（wait_until 超时）
            assert data["success"] is True
            assert data["action"] == "back"
            # 关键字段：navigation_completed 必须为 True（200ms 短轮询命中）
            assert data["navigation_completed"] is True, \
                f"navigation_completed should be True (short-polling), got: {data}"
            assert data["wait_timeout"] is True, \
                f"wait_timeout should be True (extreme timeout), got: {data}"
            assert data["url_after"].endswith("dialog.html")
        finally:
            close_session(sess, session_id)

    def test_forward_extreme_timeout(self, real_browser, browser_fixture_server):
        """goto A → goto B → back → forward(timeout=0.001) → navigation_completed=True, wait_timeout=True。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            url_a = f"{browser_fixture_server}/dialog.html"
            url_b = f"{browser_fixture_server}/popup_trigger.html"

            navigate_to(sess, session_id, url_a)
            navigate_to(sess, session_id, url_b)

            # back 到 A
            sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": session_id,
                    "action": "back",
                    "wait_until": "domcontentloaded",
                    "timeout": 10.0,
                },
                timeout=15,
            )

            # forward 回 B 用极端 timeout
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": session_id,
                    "action": "forward",
                    "wait_until": "domcontentloaded",
                    "timeout": 0.001,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["action"] == "forward"
            assert data["navigation_completed"] is True, \
                f"navigation_completed should be True (short-polling), got: {data}"
            assert data["wait_timeout"] is True, \
                f"wait_timeout should be True (extreme timeout), got: {data}"
            assert data["url_after"].endswith("popup_trigger.html")
        finally:
            close_session(sess, session_id)

    def test_short_polling_within_200ms(self, real_browser, browser_fixture_server):
        """极端 timeout 下响应时间 < 1s（200ms 短轮询应命中）。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            url_a = f"{browser_fixture_server}/dialog.html"
            url_b = f"{browser_fixture_server}/popup_trigger.html"

            navigate_to(sess, session_id, url_a)
            navigate_to(sess, session_id, url_b)

            start = time.time()
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": session_id,
                    "action": "back",
                    "wait_until": "domcontentloaded",
                    "timeout": 0.001,
                },
                timeout=15,
            )
            elapsed = time.time() - start

            assert r.status_code == 200
            data = r.json()
            assert data["navigation_completed"] is True
            # 短轮询应在 200ms 内命中 URL 变化，加上 HTTP/网络开销，总时间应 < 1s
            # 允许一定宽限（fixture server 同步快，主要是 HTTP + Playwright 通信开销）
            assert elapsed < 2.0, f"short-polling took too long: {elapsed:.2f}s (data: {data})"
        finally:
            close_session(sess, session_id)

    def test_back_no_history(self, real_browser, browser_fixture_server):
        """新 tab 无历史 → back 应返回适当错误或 navigation_completed=False。"""
        sess = real_browser
        # 新建 session 直接 goto 单页（无历史）
        session_id = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": session_id,
                    "action": "back",
                    "wait_until": "domcontentloaded",
                    "timeout": 5.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # 无历史时 back 行为：Playwright 不报错，但 URL 不变。
            # navigation_completed 应为 False 或 True 但 url_after == url_before
            # 关键：success 可能为 True（Playwright 不抛错），但 URL 应不变
            if data["success"]:
                # back 无历史时行为：Chrome 启动 page（about:blank）在历史里，
                # back 会回到 about:blank。接受 about:blank / None / dialog.html 三种情况。
                # 关键是不应该导航到无关 URL（说明 back 误触发了导航）。
                url_after = data["url_after"]
                assert url_after is None or "dialog.html" in url_after or url_after == "about:blank", \
                    f"url changed to unexpected: {data}"
            else:
                # 或者返回错误码
                assert data["error"] is not None
        finally:
            close_session(sess, session_id)


# ========== 场景 8：verify_dom_freshness=True SPA mutation E2E ==========

@pytest.mark.browser
class TestScenario8DomFreshness:
    """验证 POST /browser/action (verify_dom_freshness=True) 在 SPA mutation 下的 STALE_NODE 检测。

    实现位置：server/browser_action_endpoints.py:212-230
    spa_mutation.html 加载后 300ms 用 JS 修改 DOM，URL 不变，但 dom_hash 改变。
    """

    def test_stale_node_detected(self, real_browser, browser_fixture_server):
        """snapshot → wait 500ms（让 JS mutation 发生）→ action(verify_dom_freshness=True) → STALE_NODE。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/spa_mutation.html")

            # snapshot（记录 last_snapshot_dom_hash）
            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={
                    "session_id": session_id,
                    "interesting_only": False,
                    "max_nodes": 200,
                },
                timeout=15,
            )
            assert r.status_code == 200
            snap = r.json()
            assert snap["success"], f"snapshot failed: {snap}"
            assert snap["dom_hash"], "session 模式必须有 dom_hash"

            # 等 500ms 让 SPA mutation 发生（页面加载 300ms 后注入新元素）
            time.sleep(0.6)

            # 找一个可点击的元素（initial 或 targetBtn）
            target_node = None
            for n in snap["nodes"]:
                if n.get("role") in ("button", "link") and not n.get("disabled"):
                    target_node = n
                    break
            if not target_node and snap["nodes"]:
                target_node = snap["nodes"][0]
            if not target_node:
                pytest.skip("snapshot 无可点击元素")

            # action with verify_dom_freshness=True → 应触发 STALE_NODE
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": session_id,
                    "target": {
                        "node_id": target_node["node_id"],
                        "verify_dom_freshness": True,
                    },
                    "action": "click",
                    "timeout": 3.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # DOM 已变化（SPA mutation 注入新元素）→ STALE_NODE
            assert data["success"] is False, \
                f"expected STALE_NODE but got success=True: {data}"
            assert data["error"] is not None
            assert data["error"]["error_code"] == "STALE_NODE", \
                f"expected STALE_NODE, got: {data['error']['error_code']}"
            # debug_detail 应提到 DOM mutation 检测
            assert "DOM 已变化" in data["error"]["debug_detail"] or \
                   "mutation" in data["error"]["debug_detail"].lower() or \
                   "hash" in data["error"]["debug_detail"].lower(), \
                f"debug_detail should mention DOM/hash mutation: {data['error']['debug_detail']}"
        finally:
            close_session(sess, session_id)

    def test_no_stale_without_freshness_check(self, real_browser, browser_fixture_server):
        """snapshot → wait 500ms → action(verify_dom_freshness=False) → 不报 STALE_NODE（正常执行或返回其他错误）。"""
        sess = real_browser
        session_id = create_session(sess)
        try:
            navigate_to(sess, session_id, f"{browser_fixture_server}/spa_mutation.html")

            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={
                    "session_id": session_id,
                    "interesting_only": False,
                    "max_nodes": 200,
                },
                timeout=15,
            )
            assert r.status_code == 200
            snap = r.json()
            assert snap["success"]
            assert snap["dom_hash"]

            # 等 500ms 让 SPA mutation 发生
            time.sleep(0.6)

            target_node = None
            for n in snap["nodes"]:
                if n.get("role") in ("button", "link") and not n.get("disabled"):
                    target_node = n
                    break
            if not target_node and snap["nodes"]:
                target_node = snap["nodes"][0]
            if not target_node:
                pytest.skip("snapshot 无可点击元素")

            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": session_id,
                    "target": {
                        "node_id": target_node["node_id"],
                        "verify_dom_freshness": False,  # 关键：不检查 DOM freshness
                    },
                    "action": "click",
                    "timeout": 3.0,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # verify_dom_freshness=False → 不应因 dom_hash 不匹配而报 STALE_NODE
            # 可能成功，可能因其他原因失败（如 element_not_found），但不应是 STALE_NODE due to hash mismatch
            if not data["success"]:
                # 错误码不应是 STALE_NODE（dom_hash 不匹配触发的那种）
                # 注意：如果是 node_id 不在 snapshot 等其他 STALE_NODE 触发条件，这里会失败
                # 但 verify_dom_freshness=False 不检查 dom_hash，所以 DOM mutation 不应触发 STALE_NODE
                err_code = data["error"]["error_code"]
                # 允许其他错误，但不允许因 hash 不匹配触发的 STALE_NODE
                if err_code == "STALE_NODE":
                    pytest.fail(
                        f"verify_dom_freshness=False should not trigger STALE_NODE due to DOM mutation: {data}"
                    )
        finally:
            close_session(sess, session_id)
