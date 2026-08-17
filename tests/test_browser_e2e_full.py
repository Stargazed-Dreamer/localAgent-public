"""Browser Use 端到端测试完整套件 — 覆盖 temp/sdd/browser-use-e2e-tests/checklist.md 的 L1/L2/L3 用例。

测试分层：
- L1 集成层：真实后端 + 调试浏览器 + 本地 fixture 服务器（不触公网）
- L2 真实公网站点 E2E（只读为主，标记 @pytest.mark.network）
- L3 失败注入与边界用例（混沌测试，标记 @pytest.mark.chaos）

前置条件：
- 后端运行中 (:8766) — `uv run python -m server.main`
- 调试浏览器运行中 (CDP :9222) — `uv run python tools/browser/start_debug_browser.py`
  并加 `--disable-extensions` 避免扩展拦截点击（如 __hcfy__ 翻译扩展）
- 由 real_browser fixture 检查并 skip 不可用情况

注意：
- 每条用例前后由 autouse `clean_browser_sessions` fixture 清空 session 表
- 写操作只在 fixture 服务器上做，真实站点只做读操作
- temp 截图/上传文件用完即删
"""
import concurrent.futures
import os
import tempfile
import time

import pytest

BASE_URL = "http://127.0.0.1:8766"


# ========== Helpers ==========

def create_session(sess, url=None, url_pattern=None):
    """创建浏览器 session，返回 session_id。"""
    body = {}
    if url:
        body["url"] = url
    if url_pattern:
        body["url_pattern"] = url_pattern
    r = sess.post(f"{BASE_URL}/browser/sessions", json=body, timeout=30)
    assert r.status_code == 200, f"create session failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["success"], f"create session not success: {data}"
    return data["session_id"]


def close_session(sess, session_id):
    """关闭浏览器 session（容忍失败）。

    close_page=True（默认）：同时关闭浏览器 tab，避免测试套件累积上百个 tab。
    历史：曾因 close_page=True 触发 Chrome CDP "Failed to open a new tab" 而改为 False，
    但经 2026-08-05 验证该异常在当前 Playwright/Chrome 版本已不复现，恢复 True 根治 tab 累积。
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


def wait_for_sessions_empty(sess, timeout=5):
    """等待 session 表清空（容忍超时）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = sess.get(f"{BASE_URL}/browser/sessions", timeout=5)
        if r.status_code == 200:
            if not r.json().get("sessions"):
                return True
        time.sleep(0.2)
    return False


# ========== autouse 清理 fixture ==========

@pytest.fixture(autouse=True)
def clean_browser_sessions(real_browser):
    """每条用例前后清空 session 表，避免 session 泄漏干扰。

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


# ========== L1-A 状态查询端点（只读） ==========

@pytest.mark.browser
class TestL1AStatus:
    """L1-A 状态查询端点（只读，无副作用）。"""

    def test_L1_A01_status_browser_connected(self, real_browser):
        """GET /browser/status 浏览器已连接。"""
        r = real_browser.get(f"{BASE_URL}/browser/status", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["connected"] is True, f"expected connected=True, got: {data}"
        assert data["debug_port"] == 9222
        assert data["tab_count"] >= 0
        assert data["page_count"] >= 0
        assert data["target_count"] >= data["page_count"]
        assert data["context_count"] is None  # HTTP 模式
        assert data["error"] is None

    def test_L1_A03_list_tabs(self, real_browser):
        """GET /browser/tabs 列出标签页。"""
        r = real_browser.get(f"{BASE_URL}/browser/tabs", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert isinstance(data["tabs"], list)
        assert len(data["tabs"]) >= 1
        tab = data["tabs"][0]
        assert tab["target_id"]
        assert tab["title"] is not None
        assert tab["url"] is not None
        assert tab["type"] == "page"

    def test_L1_A05_health_browser_fields(self, real_browser):
        """GET /health 含 browser 字段。"""
        r = real_browser.get(f"{BASE_URL}/health", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "browser" in data
        b = data["browser"]
        assert "connected" in b
        assert "page_count" in b
        assert "debug_port" in b
        # 实现字段名为 sessions_count（复数），checklist 文档写错为 session_count
        assert "sessions_count" in b
        assert "session_idle_timeout_secs" in b


# ========== L1-B 标签页管理 ==========

@pytest.mark.browser
class TestL1BTabManagement:
    """L1-B 标签页管理（close/list）。"""

    def test_L1_B01_close_by_url_all_matches_false(self, real_browser, browser_fixture_server):
        """POST /browser/close 按 URL 模糊匹配（all_matches=false）。"""
        sess = real_browser
        # 创建两个相同 fixture URL 的 tab
        sid1 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        sid2 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/close",
                json={"urls": ["dialog.html"], "all_matches": False},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "已关闭 1 个标签页" in data["message"]
        finally:
            close_session(sess, sid1)
            close_session(sess, sid2)

    def test_L1_B02_close_all_matches_true(self, real_browser, browser_fixture_server):
        """POST /browser/close all_matches=true 批量关闭。"""
        sess = real_browser
        sid1 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        sid2 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/close",
                json={"urls": ["dialog.html"], "all_matches": True},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            # 至少关闭 1 个（可能 2 个，看 Chrome 状态）
            assert "已关闭" in data["message"]
        finally:
            close_session(sess, sid1)
            close_session(sess, sid2)

    def test_L1_B03_close_by_tab_ids(self, real_browser, browser_fixture_server):
        """POST /browser/close 按 tab_ids 精确关闭。"""
        sess = real_browser
        sid1 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # 拿 tab_id
            tabs_r = sess.get(f"{BASE_URL}/browser/tabs", timeout=10)
            tabs = tabs_r.json()["tabs"]
            dialog_tabs = [t for t in tabs if "dialog.html" in (t.get("url") or "")]
            assert dialog_tabs, "no dialog.html tab found"
            target_id = dialog_tabs[0]["target_id"]
            r = sess.post(
                f"{BASE_URL}/browser/close",
                json={"tab_ids": [target_id]},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid1)

    def test_L1_B05_close_no_match(self, real_browser):
        """POST /browser/close 无匹配。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/close",
            json={"urls": ["__no_such_url__"]},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "已关闭 0 个标签页" in data["message"]

    def test_L1_B06_close_missing_params(self, real_browser):
        """POST /browser/close 缺参数 → 400。"""
        r = real_browser.post(f"{BASE_URL}/browser/close", json={}, timeout=10)
        assert r.status_code == 400


# ========== L1-C session 生命周期 ==========

@pytest.mark.browser
class TestL1CSessionLifecycle:
    """L1-C session 生命周期。"""

    def test_L1_C01_create_session_url_mode(self, real_browser, browser_fixture_server):
        """POST /browser/sessions 创建 session（url 模式）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # 验证 session 已创建
            r = sess.get(f"{BASE_URL}/browser/sessions", timeout=10)
            sessions = r.json()["sessions"]
            assert any(s["session_id"] == sid for s in sessions)
            target = next(s for s in sessions if s["session_id"] == sid)
            assert "dialog.html" in target["url"]
            assert target["title"]  # 非空
        finally:
            close_session(sess, sid)

    def test_L1_C03_create_session_no_url_no_pattern(self, real_browser):
        """POST /browser/sessions 无 url 和 url_pattern → attach 到现有 page。"""
        sess = real_browser
        r = sess.post(f"{BASE_URL}/browser/sessions", json={}, timeout=30)
        assert r.status_code == 200
        data = r.json()
        # 可能成功 attach，也可能失败（看是否有 page）
        if data["success"]:
            close_session(sess, data["session_id"])
        else:
            # 失败也是合法行为（无 page 时）
            assert data.get("error") is not None

    def test_L1_C04_create_session_url_pattern_multi_match_silent_first(self, real_browser, browser_fixture_server):
        """POST /browser/sessions url_pattern 多匹配 → 静默选第一个（不抛 AMBIGUOUS_TAB）。"""
        sess = real_browser
        sid1 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        sid2 = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/sessions",
                json={"url_pattern": "dialog.html"},
                timeout=30,
            )
            # 应成功（静默选第一个），不抛 AMBIGUOUS_TAB
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            close_session(sess, data["session_id"])
        finally:
            close_session(sess, sid1)
            close_session(sess, sid2)

    def test_L1_C05_create_session_url_pattern_no_match(self, real_browser):
        """POST /browser/sessions url_pattern 无匹配 → 回退到 pages[0]。

        实现真源（session/manager.py:117-130）：url_pattern 无匹配时，
        page 仍为 None，代码进入第 3 步「复用第一个 page 或新建空白」。
        因此 url_pattern 无匹配不会失败，而是 attach 到现有 page。
        checklist 附录 C 偏差修正：原期望 HTTP 500，实际回退到 pages[0]。
        """
        sess = real_browser
        r = sess.post(
            f"{BASE_URL}/browser/sessions",
            json={"url_pattern": "__no_such_pattern__"},
            timeout=30,
        )
        # 实现回退到 pages[0]，返回 200 + success=True
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        # 清理
        close_session(sess, data["session_id"])

    def test_L1_C06_list_sessions(self, real_browser, browser_fixture_server):
        """GET /browser/sessions 列出活跃 session。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.get(f"{BASE_URL}/browser/sessions", timeout=10)
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert isinstance(data["sessions"], list)
            assert any(s["session_id"] == sid for s in data["sessions"])
            target = next(s for s in data["sessions"] if s["session_id"] == sid)
            # 关键字段
            assert "tab_id" in target
            assert "url" in target
            assert "title" in target
            assert "created_at" in target
            assert "last_used_at" in target
            assert "idle_secs" in target
            assert "console_logs_buffered" in target
            assert "site_lessons_count" in target
            assert "site_lessons_domains" in target
        finally:
            close_session(sess, sid)

    def test_L1_C07_close_session_close_page_false(self, real_browser, browser_fixture_server):
        """POST /browser/sessions/close close_page=false（默认）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        r = sess.post(
            f"{BASE_URL}/browser/sessions/close",
            json={"session_id": sid, "close_page": False},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["closed"] is True
        # 后验：session 不再存在
        r = sess.get(f"{BASE_URL}/browser/sessions", timeout=10)
        sessions = r.json()["sessions"]
        assert not any(s["session_id"] == sid for s in sessions)

    def test_L1_C09_close_nonexistent_session(self, real_browser):
        """POST /browser/sessions/close 不存在的 session_id → 幂等。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/sessions/close",
            json={"session_id": "__nonexistent__"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["closed"] is False


# ========== L1-D navigate 端点 ==========

@pytest.mark.browser
class TestL1DNavigate:
    """L1-D navigate 端点（back/forward/reload/goto）。"""

    def test_L1_D01_navigate_goto(self, real_browser, browser_fixture_server):
        """POST /browser/navigate goto。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": sid,
                    "action": "goto",
                    "url": f"{browser_fixture_server}/popup_trigger.html",
                    "wait_until": "domcontentloaded",
                    "timeout": 10,
                },
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["action"] == "goto"
            assert "popup_trigger.html" in data["url_after"]
            assert data["navigation_completed"] is True
            assert data["wait_timeout"] is False
        finally:
            close_session(sess, sid)

    def test_L1_D02_navigate_reload(self, real_browser, browser_fixture_server):
        """POST /browser/navigate reload。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "reload", "wait_until": "domcontentloaded"},
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "dialog.html" in data["url_after"]
            assert data["navigation_completed"] is True
        finally:
            close_session(sess, sid)

    def test_L1_D03_navigate_back(self, real_browser, browser_fixture_server):
        """POST /browser/navigate back。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            navigate_to(sess, sid, f"{browser_fixture_server}/dialog.html")
            navigate_to(sess, sid, f"{browser_fixture_server}/popup_trigger.html")
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "back", "wait_until": "domcontentloaded", "timeout": 15},
                timeout=60,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "dialog.html" in data["url_after"]
        finally:
            close_session(sess, sid)

    def test_L1_D04_navigate_forward(self, real_browser, browser_fixture_server):
        """POST /browser/navigate forward。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            navigate_to(sess, sid, f"{browser_fixture_server}/dialog.html")
            navigate_to(sess, sid, f"{browser_fixture_server}/popup_trigger.html")
            # back
            sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "back", "wait_until": "domcontentloaded", "timeout": 15},
                timeout=60,
            )
            # forward
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "forward", "wait_until": "domcontentloaded", "timeout": 15},
                timeout=60,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "popup_trigger.html" in data["url_after"]
        finally:
            close_session(sess, sid)

    def test_L1_D05_navigate_goto_missing_url(self, real_browser):
        """POST /browser/navigate goto 缺 url → INVALID_SELECTOR。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "goto"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "INVALID_SELECTOR"
            assert data["error"]["phase"] == "act"
            assert "goto action requires url" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_D06_navigate_unknown_action(self, real_browser):
        """POST /browser/navigate 未知 action → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "__unknown__"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "act"
            assert "action must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_D08_navigate_nonexistent_session(self, real_browser):
        """POST /browser/navigate 不存在的 session_id → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/navigate",
            json={"session_id": "__nonexistent__", "action": "reload"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"
        assert data["error"]["phase"] == "connect"


# ========== L1-E snapshot 端点 ==========

@pytest.mark.browser
class TestL1ESnapshot:
    """L1-E snapshot 端点（ARIA DOM 树）。"""

    def test_L1_E01_snapshot_session_interesting_only(self, real_browser, browser_fixture_server):
        """POST /browser/snapshot session 模式 + interesting_only=true。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True, "max_nodes": 500},
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert isinstance(data["nodes"], list)
            assert len(data["nodes"]) > 0
            # 至少有一个 button 角色
            buttons = [n for n in data["nodes"] if n.get("role") == "button"]
            assert any("Trigger Alert" in (n.get("name") or "") for n in buttons)
            assert data["dom_hash"]
            assert len(data["dom_hash"]) >= 30
        finally:
            close_session(sess, sid)

    def test_L1_E03_snapshot_max_nodes_truncate(self, real_browser, browser_fixture_server):
        """POST /browser/snapshot max_nodes 截断。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "max_nodes": 3, "interesting_only": False},
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert len(data["nodes"]) <= 3
            assert data["truncated"] is True
        finally:
            close_session(sess, sid)

    def test_L1_E06_snapshot_nonexistent_session(self, real_browser):
        """POST /browser/snapshot 不存在的 session → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/snapshot",
            json={"session_id": "__nope__"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"
        assert data["error"]["phase"] == "connect"

    def test_L1_E09_snapshot_dom_hash_stability(self, real_browser, browser_fixture_server):
        """POST /browser/snapshot dom_hash 稳定性。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r1 = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            )
            r2 = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            )
            assert r1.status_code == 200 and r2.status_code == 200
            h1 = r1.json()["dom_hash"]
            h2 = r2.json()["dom_hash"]
            assert h1 == h2, f"dom_hash should be stable: {h1} vs {h2}"
        finally:
            close_session(sess, sid)


# ========== L1-F browser_action 端点 ==========

@pytest.mark.browser
class TestL1FAction:
    """L1-F browser_action 端点（统一动作 API）。"""

    def test_L1_F01_click_by_css(self, real_browser, browser_fixture_server):
        """POST /browser/action click by css。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#alertBtn"}, "action": "click"},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["action"] == "click"
            assert data["matched_count"] == 1
            assert data["url_before"] == data["url_after"]
        finally:
            close_session(sess, sid)

    def test_L1_F02_click_by_role_name(self, real_browser, browser_fixture_server):
        """POST /browser/action click by role+name。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"role": "button", "name": "Trigger Alert"},
                    "action": "click",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched_count"] == 1
        finally:
            close_session(sess, sid)

    def test_L1_F03_click_by_text(self, real_browser, browser_fixture_server):
        """POST /browser/action click by text。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"text": "Trigger Alert"},
                    "action": "click",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F04_click_by_testid_scroll_into_view(self, real_browser, browser_fixture_server):
        """POST /browser/action scroll_into_view by testid。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/spa_mutation.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"testid": "initial"},
                    "action": "scroll_into_view",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched_count"] == 1
        finally:
            close_session(sess, sid)

    def test_L1_F05_fill_by_label(self, real_browser, browser_fixture_server):
        """POST /browser/action fill by label。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"label": "Name"},
                    "action": "fill",
                    "value": "Alice",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F06_fill_by_placeholder(self, real_browser, browser_fixture_server):
        """POST /browser/action fill by placeholder。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"placeholder": "enter your email"},
                    "action": "fill",
                    "value": "test@example.com",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F08_type_chars(self, real_browser, browser_fixture_server):
        """POST /browser/action type 逐字符输入。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"css": "#nameInput"},
                    "action": "type",
                    "value": "abc",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            # 验证值
            r2 = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "document.querySelector('#nameInput').value"},
                timeout=10,
            )
            assert "abc" in r2.json()["value"]
        finally:
            close_session(sess, sid)

    def test_L1_F09_press_key(self, real_browser, browser_fixture_server):
        """POST /browser/action press 按键。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            # 先 fill
            sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#nameInput"}, "action": "fill", "value": "x"},
                timeout=15,
            )
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#nameInput"}, "action": "press", "key": "End"},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F10_select_by_value(self, real_browser, browser_fixture_server):
        """POST /browser/action select by value。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"css": "#sel"},
                    "action": "select",
                    "value": "opt2",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F11_select_by_label(self, real_browser, browser_fixture_server):
        """POST /browser/action select by label。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"css": "#sel"},
                    "action": "select",
                    "label": "选项三",
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F12_check_uncheck(self, real_browser, browser_fixture_server):
        """POST /browser/action check / uncheck。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r1 = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#chk"}, "action": "check"},
                timeout=15,
            )
            assert r1.json()["success"] is True
            r2 = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#chk"}, "action": "uncheck"},
                timeout=15,
            )
            assert r2.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F13_hover(self, real_browser, browser_fixture_server):
        """POST /browser/action hover。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#nameInput"}, "action": "hover"},
                timeout=15,
            )
            assert r.status_code == 200
            assert r.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F14_scroll_into_view(self, real_browser, browser_fixture_server):
        """POST /browser/action scroll_into_view。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#navLink"}, "action": "scroll_into_view"},
                timeout=15,
            )
            assert r.status_code == 200
            assert r.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F15_double_click(self, real_browser, browser_fixture_server):
        """POST /browser/action double_click。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"css": ".multi-btn", "nth": 0},
                    "action": "double_click",
                },
                timeout=15,
            )
            assert r.status_code == 200
            assert r.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F16_unknown_action(self, real_browser):
        """POST /browser/action 未知 action → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#btn"}, "action": "__unknown__"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "act"
            assert "action must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_F17_target_missing_all_fields(self, real_browser):
        """POST /browser/action target 缺所有字段 → INVALID_SELECTOR。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {}, "action": "click"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "INVALID_SELECTOR"
            assert "no target field provided" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_F18_element_not_found(self, real_browser):
        """POST /browser/action 元素未找到 → ELEMENT_NOT_FOUND。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#__nope__"}, "action": "click"},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["matched_count"] == 0
            assert data["error"]["error_code"] == "ELEMENT_NOT_FOUND"
            assert data["error"]["phase"] == "locate"
        finally:
            close_session(sess, sid)

    def test_L1_F19_multiple_matches_with_nth(self, real_browser, browser_fixture_server):
        """POST /browser/action 多匹配 + nth。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/form.html")
        try:
            # 无 nth → MULTIPLE_MATCHES
            r1 = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": ".multi-btn"}, "action": "click"},
                timeout=15,
            )
            d1 = r1.json()
            assert d1["success"] is False
            assert d1["matched_count"] > 1
            assert d1["error"]["error_code"] == "MULTIPLE_MATCHES"
            # 加 nth=0 → 成功
            r2 = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"css": ".multi-btn", "nth": 0},
                    "action": "click",
                },
                timeout=15,
            )
            d2 = r2.json()
            assert d2["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_F21_action_nonexistent_session(self, real_browser):
        """POST /browser/action 不存在的 session → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/action",
            json={"session_id": "__nope__", "target": {"css": "#btn"}, "action": "click"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"
        assert data["error"]["phase"] == "connect"


# ========== L1-G node_id + STALE_NODE 机制 ==========

@pytest.mark.browser
class TestL1GNodeIdStaleNode:
    """L1-G node_id + STALE_NODE 机制（仅 session 模式）。"""

    def _find_node_by_role(self, snapshot_data, role):
        for n in snapshot_data["nodes"]:
            if n.get("role") == role:
                return n
        return None

    def test_L1_G01_action_by_node_id(self, real_browser, browser_fixture_server):
        """POST /browser/action by node_id（snapshot 后重放）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            snap = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            ).json()
            btn = self._find_node_by_role(snap, "button")
            assert btn is not None, "no button in snapshot"
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"node_id": btn["node_id"]},
                    "action": "click",
                },
                timeout=15,
            )
            assert r.status_code == 200
            assert r.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L1_G02_node_id_url_changed_stale(self, real_browser, browser_fixture_server):
        """node_id + URL 变化 → STALE_NODE。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            snap = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            ).json()
            btn = self._find_node_by_role(snap, "button")
            # navigate 到新页
            navigate_to(sess, sid, f"{browser_fixture_server}/popup_trigger.html")
            # 用旧 node_id → STALE_NODE
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"node_id": btn["node_id"]},
                    "action": "click",
                },
                timeout=15,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "STALE_NODE"
            assert "url_changed=True" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_G04_node_id_no_snapshot_stale(self, real_browser, browser_fixture_server):
        """node_id + 无 snapshot → STALE_NODE。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"node_id": 1}, "action": "click"},
                timeout=15,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "STALE_NODE"
            assert "请先调用 browser_snapshot" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_G05_node_id_not_in_snapshot(self, real_browser, browser_fixture_server):
        """node_id 不在 snapshot 中 → STALE_NODE。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            )
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"node_id": 99999}, "action": "click"},
                timeout=15,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "STALE_NODE"
            assert "不在最近 snapshot" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)


# ========== L1-H wait_for 端点 ==========

@pytest.mark.browser
class TestL1HWaitFor:
    """L1-H wait_for 端点（状态等待）。"""

    def test_L1_H01_wait_for_selector_visible(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for selector visible。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "selector",
                    "selector": "#alertBtn",
                    "state": "visible",
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched"] is True
        finally:
            close_session(sess, sid)

    def test_L1_H03_wait_for_selector_timeout(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for selector timeout。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            start = time.time()
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "selector",
                    "selector": "#__nope__",
                    "timeout": 2,
                },
                timeout=15,
            )
            elapsed = time.time() - start
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True  # 超时不报错
            assert data["matched"] is False
            assert elapsed >= 1.9
        finally:
            close_session(sess, sid)

    def test_L1_H04_wait_for_text_contains(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for text contains。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "text",
                    "text": "Trigger Alert",
                    "text_contains": True,
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched"] is True
        finally:
            close_session(sess, sid)

    def test_L1_H07_wait_for_url_substring(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for url substring。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "url",
                    "url_match": "dialog",
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is True
        finally:
            close_session(sess, sid)

    def test_L1_H09_wait_for_load_state(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for load_state。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "load_state",
                    "load_state": "networkidle",
                    "timeout": 10,
                },
                timeout=20,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is True
        finally:
            close_session(sess, sid)

    def test_L1_H10_wait_for_unknown_wait_type(self, real_browser):
        """POST /browser/wait_for 未知 wait_type → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={"session_id": sid, "wait_type": "__unknown__", "timeout": 1},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "wait"
            assert "wait_type must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_H11_wait_for_nonexistent_session(self, real_browser):
        """POST /browser/wait_for 不存在的 session → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/wait_for",
            json={"session_id": "__nope__", "wait_type": "selector", "selector": "#x", "timeout": 1},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"
        assert data["error"]["phase"] == "connect"

    def test_L1_H12_wait_for_download_supported(self, real_browser, browser_fixture_server, tmp_path):
        """POST /browser/wait_for wait_type=download 已支持：click + wait_for 捕获下载事件。

        实现真源（wait_endpoints.py download 分支）：session 持久监听器 page.on('download')
        在 manager.create_session 时挂载，触发时存入 pending_<data_drive>:/Downloads 队列。
        wait_for 优先消费队列，再等待 download_event；download_dir 提供时自动 save_as。

        download.html 的 #downloadBtn 用 Blob + a.click() 触发下载，
        suggested_filename=test_download.txt（由 a.download 属性决定）。
        """
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/download.html")
        try:
            # 并发：先 click 触发下载，再 wait_for(download) 消费队列
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={"session_id": sid, "target": {"css": "#downloadBtn"}, "action": "click"},
                    timeout=15,
                )
                time.sleep(0.3)
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": sid,
                        "wait_type": "download",
                        "download_dir": str(tmp_path),
                        "timeout": 5,
                    },
                    timeout=15,
                )
                click_result = click_future.result(timeout=20)
                wait_result = wait_future.result(timeout=20)
            assert click_result.json()["success"] is True
            data = wait_result.json()
            assert data["success"] is True
            assert data["matched"] is True
            assert data["data"]["suggested_filename"] == "test_download.txt"
            # save_as 应成功
            assert data["data"].get("saved") is True
            saved_to = data["data"].get("saved_to", "")
            assert saved_to and "test_download.txt" in saved_to
        finally:
            close_session(sess, sid)

    def test_L1_H12b_wait_for_download_timeout(self, real_browser, browser_fixture_server):
        """POST /browser/wait_for wait_type=download 超时 → matched=False（非 UNSUPPORTED_ACTION）。

        实现真源：download 已支持，超时返回 success=True, matched=False（与 selector/text
        超时同语义），不返回 UNSUPPORTED_ACTION。
        """
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/download.html")
        try:
            start = time.time()
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={"session_id": sid, "wait_type": "download", "timeout": 2},
                timeout=10,
            )
            elapsed = time.time() - start
            assert r.status_code == 200
            data = r.json()
            # download 已支持，不会返回 UNSUPPORTED_ACTION
            assert data["success"] is True
            assert data["matched"] is False
            assert elapsed >= 1.9
        finally:
            close_session(sess, sid)


# ========== L1-I dialog/popup/filechooser wait_for ==========

@pytest.mark.browser
class TestL1IDialogPopupFilechooser:
    """L1-I dialog/popup/filechooser wait_for（事件等待）。"""

    def test_L1_I04_dialog_timeout(self, real_browser, browser_fixture_server):
        """wait_for dialog timeout。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            start = time.time()
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={
                    "session_id": sid,
                    "wait_type": "dialog",
                    "dialog_action": "dismiss",
                    "timeout": 2,
                },
                timeout=10,
            )
            elapsed = time.time() - start
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched"] is False
            assert elapsed >= 1.9
        finally:
            close_session(sess, sid)

    def test_L1_I06_wait_for_popup_pending_queue(self, real_browser, browser_fixture_server):
        """wait_for popup + 消费 pending 队列。

        注意：click #popupBtn 触发 window.open('about:blank')，Playwright click()
        会等待 popup 的 load 事件，但 about:blank 不触发 load → Playwright 等到
        默认 10s 超时才返回（实测 ~11.7s）。因此 click 的 HTTP timeout 必须设为
        30s（不能用默认 10s），否则 click_future.result() 会 ReadTimeout。
        wait_for popup 立即命中 pending 队列（0ms），无需长 timeout。
        """
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/popup_trigger.html")
        try:
            # 并发：先 click 弹出，再 wait_for popup
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={"session_id": sid, "target": {"css": "#popupBtn"}, "action": "click"},
                    timeout=30,
                )
                time.sleep(0.3)
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={"session_id": sid, "wait_type": "popup", "timeout": 5},
                    timeout=15,
                )
                click_resp = click_future.result()
                wait_resp = wait_future.result()
            assert click_resp.json()["success"] is True
            data = wait_resp.json()
            assert data["success"] is True
            assert data["matched"] is True
            assert data["data"] is not None
        finally:
            close_session(sess, sid)

    def test_L1_I07_wait_for_popup_timeout(self, real_browser, browser_fixture_server):
        """wait_for popup timeout。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/popup_trigger.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={"session_id": sid, "wait_type": "popup", "timeout": 2},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is False
        finally:
            close_session(sess, sid)

    def test_L1_I08_wait_for_filechooser_set_files(self, real_browser, browser_fixture_server):
        """wait_for filechooser + set_files。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/file_upload.html")
        # 创建 temp 测试文件
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")  # noqa: SIM115
        tmp.write("test upload content")
        tmp.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/wait_for",
                    json={
                        "session_id": sid,
                        "wait_type": "filechooser",
                        "file_paths": [tmp.name],
                        "timeout": 5,
                    },
                    timeout=10,
                )
                time.sleep(0.3)
                click_future = pool.submit(
                    sess.post,
                    f"{BASE_URL}/browser/action",
                    json={"session_id": sid, "target": {"css": "#triggerBtn"}, "action": "click"},
                    timeout=10,
                )
                wait_resp = wait_future.result()
                click_resp = click_future.result()
            assert click_resp.json()["success"] is True
            data = wait_resp.json()
            assert data["success"] is True
            assert data["matched"] is True
            assert data["data"] is not None
            assert "file_upload.html" in (data["data"].get("page_url") or "")
        finally:
            close_session(sess, sid)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_L1_I09_wait_for_filechooser_timeout(self, real_browser, browser_fixture_server):
        """wait_for filechooser timeout。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/file_upload.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_for",
                json={"session_id": sid, "wait_type": "filechooser", "timeout": 2},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is False
        finally:
            close_session(sess, sid)


# ========== L1-J wait_and_action 端点 ==========

@pytest.mark.browser
class TestL1JWaitAndAction:
    """L1-J wait_and_action 端点（原子 wait+trigger）。"""

    def test_L1_J01_wait_and_action_popup(self, real_browser, browser_fixture_server):
        """POST /browser/wait_and_action popup。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/popup_trigger.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "popup",
                    "trigger_action": "click",
                    "trigger_css": "#popupBtn",
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["matched"] is True
            assert data["trigger_success"] is True
            assert data["data"] is not None
        finally:
            close_session(sess, sid)

    def test_L1_J02_wait_and_action_filechooser(self, real_browser, browser_fixture_server):
        """POST /browser/wait_and_action filechooser + set_files。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/file_upload.html")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")  # noqa: SIM115
        tmp.write("test")
        tmp.close()
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "filechooser",
                    "trigger_action": "click",
                    "trigger_css": "#triggerBtn",
                    "file_paths": [tmp.name],
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is True
            assert data["trigger_success"] is True
        finally:
            close_session(sess, sid)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_L1_J03_wait_and_action_dialog_accept(self, real_browser, browser_fixture_server):
        """POST /browser/wait_and_action dialog alert + accept。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "dialog",
                    "trigger_action": "click",
                    "trigger_css": "#alertBtn",
                    "dialog_action": "accept",
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is True
            assert data["trigger_success"] is True
            assert data["data"]["type"] == "alert"
        finally:
            close_session(sess, sid)

    def test_L1_J05_no_session_tab_not_found(self, real_browser):
        """POST /browser/wait_and_action 无 session_id → 422 Pydantic 验证错误。

        实现真源（wait_endpoints.py:489）：BrowserWaitAndActionRequest.session_id
        是必填字段（无默认值），缺失时 FastAPI 返回 422 验证错误（不会进入
        端点函数体）。checklist 附录 C 偏差修正：原期望 TAB_NOT_FOUND，实际
        在请求模型层就被拦截。

        响应格式（自定义异常处理器）：
          {"detail": "请求参数验证失败", "errors": [{"field":"body -> session_id", ...}]}
        """
        r = real_browser.post(
            f"{BASE_URL}/browser/wait_and_action",
            json={
                "wait_type": "popup",
                "trigger_action": "click",
                "trigger_css": "#x",
                "timeout": 1,
            },
            timeout=10,
        )
        # session_id 是 Pydantic 必填字段，缺失返回 422
        assert r.status_code == 422
        data = r.json()
        # 响应格式：detail 是字符串，errors 是列表
        assert "detail" in data
        assert "errors" in data
        # 验证 errors 指向 session_id
        fields = [e.get("field", "") for e in data["errors"]]
        assert any("session_id" in f for f in fields), f"session_id not in fields: {fields}"

    def test_L1_J06_unknown_wait_type(self, real_browser):
        """POST /browser/wait_and_action 未知 wait_type → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "__unknown__",
                    "trigger_action": "click",
                    "trigger_css": "#x",
                    "timeout": 1,
                },
                timeout=10,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "wait"
            assert "wait_type must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_J07_unknown_trigger_action(self, real_browser):
        """POST /browser/wait_and_action 未知 trigger_action → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "popup",
                    "trigger_action": "hover",  # 不在 click/fill/press/select 白名单
                    "trigger_css": "#x",
                    "timeout": 1,
                },
                timeout=10,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "act"
            assert "trigger_action must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_J08_trigger_element_not_found(self, real_browser, browser_fixture_server):
        """POST /browser/wait_and_action trigger 元素未找到。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/popup_trigger.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "popup",
                    "trigger_action": "click",
                    "trigger_css": "#__nope__",
                    "timeout": 2,
                },
                timeout=15,
            )
            data = r.json()
            assert data["trigger_success"] is False
            assert data["matched"] is False
        finally:
            close_session(sess, sid)

    def test_L1_J10_trigger_field_all_missing(self, real_browser):
        """POST /browser/wait_and_action trigger 字段全缺 → INVALID_SELECTOR。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "popup",
                    "trigger_action": "click",
                    "timeout": 1,
                },
                timeout=10,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "INVALID_SELECTOR"
            assert data["error"]["phase"] == "locate"
            assert "no trigger target field provided" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)


# ========== L1-K evaluate / console_logs / screenshot ==========

@pytest.mark.browser
class TestL1KEvaluateConsoleScreenshot:
    """L1-K evaluate / console_logs / screenshot。"""

    def test_L1_K01_evaluate_readonly(self, real_browser, browser_fixture_server):
        """POST /browser/evaluate 只读表达式。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "document.title"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "Dialog Test Fixture" in data["value"]
        finally:
            close_session(sess, sid)

    def test_L1_K02_evaluate_number(self, real_browser):
        """POST /browser/evaluate 数字结果。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "1+2"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "3" in data["value"]
        finally:
            close_session(sess, sid)

    def test_L1_K04_evaluate_write_blocked(self, real_browser):
        """POST /browser/evaluate 危险写操作 → 被拦截。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={
                    "session_id": sid,
                    "expression": "document.body.innerHTML='<h1>hacked</h1>'",
                },
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert data["error"]["phase"] == "act"
            assert "blocked write pattern" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_K05_evaluate_textcontent(self, real_browser, browser_fixture_server):
        """POST /browser/evaluate 取 textContent（替代 extract_text）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={
                    "session_id": sid,
                    "expression": "document.querySelector('#alertBtn').textContent",
                },
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "Trigger Alert" in data["value"]
        finally:
            close_session(sess, sid)

    @pytest.mark.parametrize("expr,expected_substring", [
        ("location.href='https://evil.com'", "blocked write pattern"),
        ("document.cookie='x=1'", "blocked write pattern"),
        ("fetch('/api')", "blocked write pattern"),
        ("eval('1+1')", "blocked write pattern"),
        ("setTimeout(()=>{},0)", "blocked write pattern"),
        ("new Function('return 1')", "blocked write pattern"),
        ("window.open('https://x.com')", "blocked write pattern"),
        ("localStorage.setItem('k','v')", "blocked write pattern"),
        ("document.write('x')", "blocked write pattern"),
        ("new WebSocket('wss://x')", "blocked write pattern"),
    ])
    def test_L1_K06_evaluate_write_patterns(self, real_browser, expr, expected_substring):
        """POST /browser/evaluate 多种写操作拦截。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": expr},
                timeout=10,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert expected_substring in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_K07_evaluate_bypass_protection(self, real_browser):
        """POST /browser/evaluate 注释/转义绕过防护。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            # 块注释绕过
            r1 = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "location./* */href='https://evil.com'"},
                timeout=10,
            )
            assert r1.json()["success"] is False
            # 方括号属性访问绕过
            r3 = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "location['href']='x'"},
                timeout=10,
            )
            assert r3.json()["success"] is False
        finally:
            close_session(sess, sid)

    def test_L1_K08_console_logs_session_mode(self, real_browser, browser_fixture_server):
        """POST /browser/console_logs session 模式。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # 触发 console.log
            sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "console.log('test_log_1')"},
                timeout=10,
            )
            sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "console.log('test_log_2')"},
                timeout=10,
            )
            time.sleep(0.3)  # 等监听器写入缓冲
            r = sess.post(
                f"{BASE_URL}/browser/console_logs",
                json={"session_id": sid, "level": "all"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert isinstance(data["logs"], list)
            # 应至少有 2 条 log
            log_texts = [entry.get("text", "") for entry in data["logs"]]
            assert any("test_log_1" in t for t in log_texts)
            assert any("test_log_2" in t for t in log_texts)
        finally:
            close_session(sess, sid)

    def test_L1_K10_console_logs_unknown_level(self, real_browser):
        """POST /browser/console_logs 未知 level → UNSUPPORTED_ACTION。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/console_logs",
                json={"session_id": sid, "level": "__unknown__"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
            assert "level must be one of" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_K11_console_logs_nonexistent_session(self, real_browser):
        """POST /browser/console_logs 不存在的 session → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/console_logs",
            json={"session_id": "__nope__"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"

    def test_L1_K13_screenshot_viewport_base64(self, real_browser, browser_fixture_server):
        """POST /browser/screenshot viewport 截图（base64）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={
                    "session_id": sid,
                    "shot_type": "viewport",
                    "return_base64": True,
                    "max_size": 1024,
                    "quality": 85,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["path"]
            assert data["data"]["image"]
            assert "image" in data["data"]["mime_type"]
            assert data["data"]["width"] <= 1024
            assert data["data"]["height"] <= 1024
        finally:
            close_session(sess, sid)

    def test_L1_K14_screenshot_full_page(self, real_browser, browser_fixture_server):
        """POST /browser/screenshot full_page 截图。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={"session_id": sid, "shot_type": "full_page", "return_base64": True},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["data"]["width"] > 0
            assert data["data"]["height"] > 0
        finally:
            close_session(sess, sid)

    def test_L1_K15_screenshot_element(self, real_browser, browser_fixture_server):
        """POST /browser/screenshot element 截图。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={
                    "session_id": sid,
                    "shot_type": "element",
                    "selector": "#alertBtn",
                    "return_base64": True,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["data"]["image"]
        finally:
            close_session(sess, sid)

    def test_L1_K16_screenshot_element_missing_selector(self, real_browser):
        """POST /browser/screenshot element 缺 selector → INVALID_SELECTOR。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={"session_id": sid, "shot_type": "element"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "INVALID_SELECTOR"
            assert "element shot_type requires selector" in data["error"]["debug_detail"]
        finally:
            close_session(sess, sid)

    def test_L1_K17_screenshot_return_path_only(self, real_browser, browser_fixture_server):
        """POST /browser/screenshot return_base64=false（仅路径）。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={"session_id": sid, "shot_type": "viewport", "return_base64": False},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["path"]
            assert data["data"] is None
            # 清理文件
            try:
                os.unlink(data["path"])
            except OSError:
                pass
        finally:
            close_session(sess, sid)

    def test_L1_K18_screenshot_nonexistent_session(self, real_browser):
        """POST /browser/screenshot 不存在的 session → TAB_NOT_FOUND。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/screenshot",
            json={"session_id": "__nope__", "shot_type": "viewport"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"


# ========== L1-L 辅助端点 ==========

@pytest.mark.browser
class TestL1LAuxiliary:
    """L1-L 辅助端点（match_site/find_url/selector_stats）。"""

    def test_L1_L01_match_site_hit(self, real_browser):
        """POST /browser/match_site 命中（按 domain）。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/match_site",
            json={"domain": "bilibili.com"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        # 若有 bilibili.md 站点经验则 matched>=1，否则 0
        assert isinstance(data["results"], list)
        assert isinstance(data["available"], list)

    def test_L1_L02_match_site_no_hit(self, real_browser):
        """POST /browser/match_site 未命中。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/match_site",
            json={"domain": "__nope__.com"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["matched"] == 0
        assert data["results"] == []
        assert isinstance(data["available"], list)

    def test_L1_L07_selector_stats_query(self, real_browser, browser_fixture_server):
        """POST /browser/selector_stats 查询统计。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # 触发几次 action 让 stats 有数据
            sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"css": "#alertBtn"}, "action": "click"},
                timeout=15,
            )
            r = sess.post(
                f"{BASE_URL}/browser/selector_stats",
                json={"domain": "127.0.0.1"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert isinstance(data["stats"], list)
            assert isinstance(data["available_domains"], list)
        finally:
            close_session(sess, sid)

    def test_L1_L08_selector_stats_empty(self, real_browser):
        """POST /browser/selector_stats 空统计。"""
        r = real_browser.post(
            f"{BASE_URL}/browser/selector_stats",
            json={"domain": "__nope__.invalid"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["stats"] == []


# ========== L2-A example.com（最简真实站点） ==========

@pytest.mark.browser
@pytest.mark.network
class TestL2AExampleCom:
    """L2-A example.com（最简真实站点，只读）。"""

    def test_L2_A01_navigate_example_com(self, real_browser):
        """browser_navigate goto example.com。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": sid,
                    "action": "goto",
                    "url": "https://example.com",
                    "wait_until": "domcontentloaded",
                    "timeout": 15,
                },
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "example.com" in data["url_after"]
            assert data["navigation_completed"] is True
        finally:
            close_session(sess, sid)

    def test_L2_A02_snapshot_example_com(self, real_browser):
        """browser_snapshot example.com。"""
        sess = real_browser
        sid = create_session(sess, url="https://example.com")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True, "max_nodes": 200},
                timeout=30,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            # 应含 link "More information..." 和 heading "Example Domain"
            has_link = any(n.get("role") == "link" for n in data["nodes"])
            has_heading = any(n.get("role") == "heading" for n in data["nodes"])
            assert has_link or has_heading, f"expected link/heading, nodes: {data['nodes'][:5]}"
        finally:
            close_session(sess, sid)

    def test_L2_A03_evaluate_document_title(self, real_browser):
        """browser_evaluate document.title。"""
        sess = real_browser
        sid = create_session(sess, url="https://example.com")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "document.title"},
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert "Example Domain" in data["value"]
        finally:
            close_session(sess, sid)

    def test_L2_A05_screenshot_example_com(self, real_browser):
        """browser_screenshot example.com。"""
        sess = real_browser
        sid = create_session(sess, url="https://example.com")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/screenshot",
                json={"session_id": sid, "shot_type": "viewport", "return_base64": True},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["data"]["width"] > 0
            assert data["data"]["height"] > 0
        finally:
            close_session(sess, sid)


# ========== L2-B bilibili.com（复杂真实站点 + site_lessons） ==========

@pytest.mark.browser
@pytest.mark.network
class TestL2BBilibili:
    """L2-B bilibili.com（复杂真实站点 + site_lessons）。"""

    def test_L2_B01_session_create_bilibili(self, real_browser):
        """browser_session_create bilibili（site_lessons 自动注入）。"""
        sess = real_browser
        r = sess.post(
            f"{BASE_URL}/browser/sessions",
            json={"url": "https://www.bilibili.com"},
            timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        if data["success"]:
            try:
                # site_lessons_hint 应非空（若 sites/bilibili.md 存在）
                assert data.get("site_lessons_hint") is not None or data.get("site_lessons") == []
            finally:
                close_session(sess, data["session_id"])
        else:
            # 网络问题或反爬，允许跳过
            pytest.skip(f"bilibili session create failed (network/anti-bot?): {data}")

    def test_L2_B02_snapshot_bilibili_truncate(self, real_browser):
        """browser_snapshot bilibili（大量节点 + 截断）。

        实现真源：interesting_only=False 返回完整 DOM 树（含装饰性 div/span），
        bilibili 首页完整树 >500 节点，max_nodes=500 自然触发截断。
        （interesting_only=True 仅返回可交互元素 ~244 节点，不足以验证 500 截断。）
        """
        sess = real_browser
        r = sess.post(
            f"{BASE_URL}/browser/sessions",
            json={"url": "https://www.bilibili.com"},
            timeout=30,
        )
        if not r.json().get("success"):
            pytest.skip("bilibili not reachable")
        sid = r.json()["session_id"]
        try:
            r = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": False, "max_nodes": 500},
                timeout=60,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert len(data["nodes"]) <= 500
            # bilibili 完整树（interesting_only=False）>500 节点，应截断
            assert data["truncated"] is True
            assert data["dom_hash"]
        finally:
            close_session(sess, sid)


# ========== L2-C 完整工作流串联 ==========

@pytest.mark.browser
class TestL2CWorkflow:
    """L2-C 完整工作流串联（综合 E2E）。"""

    def test_L2_C01_session_snapshot_nodeid_action_verify(self, real_browser, browser_fixture_server):
        """完整流程：session → snapshot → action by node_id → wait_and_action by trigger_node_id。

        实现真源（wait_endpoints.py trigger_node_id 分支）：wait_and_action 支持
        trigger_node_id 字段，从 sess.last_snapshot_nodes 反查 css_path，含 STALE_NODE
        检查（URL 变化或 >60s 过期）。与 browser_action.node_id 同语义。
        """
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # 1. snapshot
            snap = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            ).json()
            # 找 #alertBtn 的 node_id
            btn = None
            for n in snap["nodes"]:
                if n.get("role") == "button" and "Trigger Alert" in (n.get("name") or ""):
                    btn = n
                    break
            assert btn is not None, "no #alertBtn in snapshot"
            node_id = btn["node_id"]

            # 2. 用 node_id 调 browser_action（验证 node_id 重放可用）
            r_action = sess.post(
                f"{BASE_URL}/browser/action",
                json={
                    "session_id": sid,
                    "target": {"node_id": node_id},
                    "action": "click",
                },
                timeout=15,
            )
            assert r_action.json()["success"] is True

            # 3. wait_and_action 用 trigger_node_id（反查 css_path，原子 wait+trigger）
            r = sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "dialog",
                    "trigger_action": "click",
                    "trigger_node_id": node_id,
                    "dialog_action": "accept",
                    "timeout": 5,
                },
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["matched"] is True
            assert data["data"]["type"] == "alert"
        finally:
            close_session(sess, sid)

    def test_L2_C03_stale_node_recovery(self, real_browser, browser_fixture_server):
        """完整流程：STALE_NODE 恢复。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            # snapshot A
            snap_a = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            ).json()
            btn_a = next((n for n in snap_a["nodes"] if n.get("role") == "button"), None)
            assert btn_a is not None
            # navigate 到新页
            navigate_to(sess, sid, f"{browser_fixture_server}/popup_trigger.html")
            # 用 A 的 node_id → STALE_NODE
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"node_id": btn_a["node_id"]}, "action": "click"},
                timeout=15,
            )
            assert r.json()["error"]["error_code"] == "STALE_NODE"
            # snapshot B
            snap_b = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid, "interesting_only": True},
                timeout=30,
            ).json()
            btn_b = next((n for n in snap_b["nodes"] if n.get("role") == "button"), None)
            assert btn_b is not None
            # 用 B 的 node_id → 成功
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid, "target": {"node_id": btn_b["node_id"]}, "action": "click"},
                timeout=15,
            )
            assert r.json()["success"] is True
        finally:
            close_session(sess, sid)

    def test_L2_C05_filechooser_upload(self, real_browser, browser_fixture_server):
        """完整流程：filechooser 上传。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/file_upload.html")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")  # noqa: SIM115
        tmp.write("test upload")
        tmp.close()
        try:
            sess.post(
                f"{BASE_URL}/browser/wait_and_action",
                json={
                    "session_id": sid,
                    "wait_type": "filechooser",
                    "trigger_action": "click",
                    "trigger_css": "#triggerBtn",
                    "file_paths": [tmp.name],
                    "timeout": 5,
                },
                timeout=15,
            )
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={
                    "session_id": sid,
                    "expression": "document.getElementById('fileInput').files[0].name",
                },
                timeout=10,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert os.path.basename(tmp.name) in data["value"]
        finally:
            close_session(sess, sid)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_L2_C07_console_logs_capture(self, real_browser, browser_fixture_server):
        """完整流程：console_logs 捕获。"""
        sess = real_browser
        sid = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "console.log('test_workflow_log')"},
                timeout=10,
            )
            time.sleep(0.3)
            r = sess.post(
                f"{BASE_URL}/browser/console_logs",
                json={"session_id": sid, "level": "all"},
                timeout=10,
            )
            data = r.json()
            log_texts = [entry.get("text", "") for entry in data["logs"]]
            assert any("test_workflow_log" in t for t in log_texts)
        finally:
            close_session(sess, sid)

    def test_L2_C10_back_forward_history(self, real_browser, browser_fixture_server):
        """完整流程：back/forward history 导航。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            navigate_to(sess, sid, f"{browser_fixture_server}/dialog.html")
            navigate_to(sess, sid, f"{browser_fixture_server}/popup_trigger.html")
            # back
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "back", "wait_until": "domcontentloaded", "timeout": 15},
                timeout=60,
            )
            assert "dialog.html" in r.json()["url_after"]
            # forward
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "forward", "wait_until": "domcontentloaded", "timeout": 15},
                timeout=60,
            )
            assert "popup_trigger.html" in r.json()["url_after"]
        finally:
            close_session(sess, sid)


# ========== L3 失败注入与边界用例 ==========

@pytest.mark.browser
@pytest.mark.chaos
class TestL3Chaos:
    """L3 失败注入与边界用例（混沌测试）。"""

    def test_L3_06_evaluate_long_string_truncated(self, real_browser):
        """evaluate 内存泄漏（长字符串返回）。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/evaluate",
                json={"session_id": sid, "expression": "('x'.repeat(1000000))"},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # 应被 max_length 截断（默认 5000）
            if data["success"]:
                assert len(data["value"]) <= 5100  # 留点余量
        finally:
            close_session(sess, sid)

    def test_L3_07_navigate_invalid_url(self, real_browser):
        """navigate 到无效 URL。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={
                    "session_id": sid,
                    "action": "goto",
                    "url": "https://__nope__.invalid",
                    "timeout": 10,
                },
                timeout=20,
            )
            assert r.status_code == 200
            data = r.json()
            # 应失败，错误码含 EXECUTION_ERROR 或 TIMEOUT
            if not data["success"]:
                assert data["error"]["error_code"] in {"EXECUTION_ERROR", "TIMEOUT"}
        finally:
            close_session(sess, sid)

    def test_L3_08_navigate_about_blank(self, real_browser):
        """navigate 到 about:blank。"""
        sess = real_browser
        sid = create_session(sess)
        try:
            r = sess.post(
                f"{BASE_URL}/browser/navigate",
                json={"session_id": sid, "action": "goto", "url": "about:blank"},
                timeout=15,
            )
            assert r.status_code == 200
            data = r.json()
            # about:blank 应成功
            if data["success"]:
                assert "about:blank" in (data["url_after"] or "")
        finally:
            close_session(sess, sid)

    def test_L3_12_node_id_cross_session(self, real_browser, browser_fixture_server):
        """node_id 跨 session 复用 → STALE_NODE。"""
        sess = real_browser
        sid_a = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            snap = sess.post(
                f"{BASE_URL}/browser/snapshot",
                json={"session_id": sid_a, "interesting_only": True},
                timeout=30,
            ).json()
            btn = next((n for n in snap["nodes"] if n.get("role") == "button"), None)
            assert btn is not None
            node_id_a = btn["node_id"]
        finally:
            close_session(sess, sid_a)
        # 新 session
        sid_b = create_session(sess, url=f"{browser_fixture_server}/dialog.html")
        try:
            r = sess.post(
                f"{BASE_URL}/browser/action",
                json={"session_id": sid_b, "target": {"node_id": node_id_a}, "action": "click"},
                timeout=15,
            )
            data = r.json()
            assert data["success"] is False
            assert data["error"]["error_code"] == "STALE_NODE"
        finally:
            close_session(sess, sid_b)
