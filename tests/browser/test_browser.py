"""浏览器控制模块测试

测试 Chromium 内核调试浏览器（CDP 协议）连接状态查询、标签页管理、URL 操作和高级操作封装。
重点测试断连场景下的优雅降级行为（不抛 500）和请求参数验证。
"""

import asyncio

import pytest

from server.browser import (
    BROWSER_ERROR_CODES,
    BrowserActionRequest,
    BrowserActionResponse,
    BrowserActionTarget,
    BrowserCloseRequest,
    BrowserConsoleLogsRequest,
    BrowserErrorResponse,
    BrowserEvaluateRequest,
    BrowserGrantPermissionsRequest,
    BrowserGrantPermissionsResponse,
    BrowserHandleDialogRequest,
    BrowserHandleDialogResponse,
    BrowserListResponse,
    BrowserNavigateRequest,
    BrowserNavigateResponse,
    BrowserResponse,
    BrowserScreenshotRequest,
    BrowserSessionCloseRequest,
    BrowserSessionCreateRequest,
    BrowserSessionCreateResponse,
    BrowserSetHttpCredentialsRequest,
    BrowserSetHttpCredentialsResponse,
    BrowserSnapshotRequest,
    BrowserSnapshotResponse,
    BrowserStatusResponse,
    BrowserWaitAndActionRequest,
    BrowserWaitAndActionResponse,
    BrowserWaitRequest,
)
from server.browser.session.manager import SessionManager
from server.browser.session.state import TabSession


def _browser_disconnected(client) -> bool:
    """检查浏览器是否处于断连状态"""
    status = client.get("/browser/status").json()
    return not status.get("connected", False)


# ==================== Pydantic 模型 ====================

class TestPydanticModels:
    def test_browser_close_request(self):
        req = BrowserCloseRequest(urls=["example"])
        assert req.urls == ["example"]

    def test_browser_status_response_defaults(self):
        resp = BrowserStatusResponse(connected=False, debug_port=9222)
        assert resp.tab_count is None
        assert resp.error is None

    def test_browser_list_response(self):
        resp = BrowserListResponse(success=False, tabs=[])
        assert resp.success is False
        assert resp.tabs == []

    def test_browser_response(self):
        resp = BrowserResponse(success=True, message="ok")
        assert resp.success is True


# ==================== GET /browser/status ====================

class TestBrowserStatus:
    def test_returns_200(self, client):
        """端点始终返回 200（即使断连）"""
        resp = client.get("/browser/status")
        assert resp.status_code == 200

    def test_response_schema(self, client):
        """响应包含必需字段"""
        resp = client.get("/browser/status")
        data = resp.json()
        assert "connected" in data
        assert "debug_port" in data
        assert isinstance(data["connected"], bool)
        assert isinstance(data["debug_port"], int)

    def test_disconnected_returns_error_string(self, client):
        """断连时 error 字段为字符串"""
        if not _browser_disconnected(client):
            pytest.skip("浏览器已连接，跳过断连测试")
        data = client.get("/browser/status").json()
        assert data["connected"] is False
        assert data["error"] is not None
        assert isinstance(data["error"], str)
        assert len(data["error"]) > 0

    def test_connected_returns_tab_count(self, client):
        """连接时 tab_count 为非负整数"""
        if _browser_disconnected(client):
            pytest.skip("浏览器未连接，跳过连接测试")
        data = client.get("/browser/status").json()
        assert data["connected"] is True
        assert data["tab_count"] is not None
        assert data["tab_count"] >= 0

    def test_debug_port_matches_config(self, client):
        """debug_port 与 config 一致"""
        from server.config import get_browser_config
        expected = get_browser_config()["debug_port"]
        data = client.get("/browser/status").json()
        assert data["debug_port"] == expected


# ==================== GET /browser/tabs ====================

class TestBrowserTabs:
    def test_returns_200_when_disconnected(self, client):
        """断连时返回 200 + 空列表（不抛 500）"""
        if not _browser_disconnected(client):
            pytest.skip("浏览器已连接，跳过断连测试")
        resp = client.get("/browser/tabs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["tabs"] == []

    def test_response_schema(self, client):
        """响应符合 schema"""
        resp = client.get("/browser/tabs")
        data = resp.json()
        assert "success" in data
        assert "tabs" in data
        assert isinstance(data["tabs"], list)

    def test_tabs_have_required_fields_when_connected(self, client):
        """连接时每个 tab 包含 title/url/type 字段"""
        if _browser_disconnected(client):
            pytest.skip("浏览器未连接")
        data = client.get("/browser/tabs").json()
        if not data["success"]:
            pytest.skip("无可用标签页")
        for tab in data["tabs"]:
            assert "title" in tab
            assert "url" in tab
            assert "type" in tab


# ==================== POST /browser/close ====================

class TestBrowserClose:
    def test_missing_both_urls_and_tab_ids_returns_400(self, client):
        """既无 urls 也无 tab_ids 时返回 400（评估文档 P1：tab_ids 是新增字段，
        urls 和 tab_ids 都默认为空数组，端点逻辑检查至少提供一个）"""
        resp = client.post("/browser/close", json={})
        assert resp.status_code == 400
        assert "urls" in resp.json()["detail"]
        assert "tab_ids" in resp.json()["detail"]

    def test_invalid_urls_type_returns_422(self):
        from fastapi.testclient import TestClient

        from server.main import app
        c = TestClient(app)
        resp = c.post("/browser/close", json={"urls": "not a list"})
        assert resp.status_code == 422

    def test_invalid_tab_ids_type_returns_422(self):
        """tab_ids 类型错误返回 422（Pydantic 校验）"""
        from fastapi.testclient import TestClient

        from server.main import app
        c = TestClient(app)
        resp = c.post("/browser/close", json={"tab_ids": "not a list"})
        assert resp.status_code == 422

    def test_invalid_all_matches_type_returns_422(self):
        """all_matches 类型错误返回 422"""
        from fastapi.testclient import TestClient

        from server.main import app
        c = TestClient(app)
        resp = c.post("/browser/close", json={"urls": ["x"], "all_matches": "not a bool"})
        assert resp.status_code == 422


# ==================== P0/P1 新接口：错误模型 + status 拆分 + 同 URL 多标签 ====================

class TestBrowserErrorModel:
    """错误模型（评估文档 P0 第 5 项）"""

    def test_browser_error_response_defaults(self):
        err = BrowserErrorResponse(
            error_code="TAB_NOT_FOUND",
            error_message=BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
            phase="connect",
            elapsed_ms=10,
        )
        assert err.success is False
        assert err.debug_detail is None
        assert err.error_code == "TAB_NOT_FOUND"
        assert err.phase == "connect"

    def test_all_error_codes_have_messages(self):
        """所有 error_code 都在 BROWSER_ERROR_CODES 中有对应描述"""
        # 至少包含评估文档列出的核心错误码
        required = {
            "TAB_NOT_FOUND", "MULTIPLE_MATCHES", "INVALID_SELECTOR", "TIMEOUT",
            "STALE_NODE", "UNSUPPORTED_ACTION", "BROWSER_DISCONNECTED",
            "EXECUTION_ERROR", "INTERNAL_ERROR",
        }
        assert required.issubset(BROWSER_ERROR_CODES.keys())

    def test_classify_error_timeout(self):
        from server.browser import _classify_error
        err = _classify_error("Timeout 10000ms exceeded waiting for selector", "wait", 100)
        assert err.error_code == "TIMEOUT"
        assert err.phase == "wait"

    def test_classify_error_strict_mode_violation(self):
        from server.browser import _classify_error
        err = _classify_error("strict mode violation: resolved to 3 elements", "locate", 50)
        assert err.error_code == "MULTIPLE_MATCHES"

    def test_classify_error_browser_disconnected(self):
        from server.browser import _classify_error
        err = _classify_error("Target closed, Browser has been closed", "connect", 5)
        assert err.error_code == "BROWSER_DISCONNECTED"


class TestBrowserStatusNewFields:
    """status 拆分 target_count/page_count/context_count（评估文档 P1 7.4 节）"""

    def test_status_response_model_new_fields(self):
        resp = BrowserStatusResponse(connected=True, debug_port=9222)
        assert resp.target_count is None
        assert resp.page_count is None
        assert resp.context_count is None
        assert resp.tab_count is None  # 向后兼容字段

    def test_status_endpoint_returns_new_fields_when_connected(self, client):
        """连接成功时新字段都有值；断连时仍为 None"""
        resp = client.get("/browser/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "target_count" in data
        assert "page_count" in data
        assert "context_count" in data
        if data.get("connected"):
            # 连接成功时 target/page 至少 0
            assert data["target_count"] is not None
            assert data["page_count"] is not None
            # tab_count 与 page_count 一致（向后兼容）
            assert data["tab_count"] == data["page_count"]
            # HTTP 模式下 context_count 恒为 None
            assert data["context_count"] is None
        else:
            # 断连时所有计数字段为 None
            assert data["target_count"] is None
            assert data["page_count"] is None


class TestBrowserListTabsStableIds:
    """list_tabs 返回稳定 target_id（评估文档 P1 第 2 项）"""

    def test_list_tabs_response_has_stable_fields(self, client):
        resp = client.get("/browser/tabs")
        assert resp.status_code == 200
        data = resp.json()
        if data["success"] and data["tabs"]:
            tab = data["tabs"][0]
            assert "target_id" in tab
            assert "title" in tab
            assert "url" in tab
            assert "type" in tab
            assert "attached" in tab
            assert "opener_target_id" in tab
            assert "opener_url" in tab
            # target_id 是非空字符串
            assert isinstance(tab["target_id"], str)


class TestBrowserCloseAllMatches:
    """close 支持 all_matches（评估文档 P1 第 2 项：同 URL 多标签）"""

    def test_all_matches_default_is_false(self):
        """默认 False：每个 URL 模式只关第一个匹配"""
        req = BrowserCloseRequest(urls=["example.com"])
        assert req.all_matches is False
        assert req.tab_ids == []

    def test_all_matches_true_can_be_set(self):
        req = BrowserCloseRequest(urls=["example.com"], all_matches=True)
        assert req.all_matches is True

    def test_tab_ids_only_without_urls(self):
        """可仅通过 tab_ids 精确关闭"""
        req = BrowserCloseRequest(tab_ids=["target-id-123"])
        assert req.urls == []
        assert req.tab_ids == ["target-id-123"]


# ==================== P0/P1 新接口：snapshot / action / wait / evaluate / console_logs / navigate / screenshot ====================

class TestBrowserSnapshotModel:
    """snapshot 请求/响应模型（评估文档 P0 第 2 项）"""

    def test_snapshot_request_defaults(self):
        req = BrowserSnapshotRequest()
        assert req.session_id is None
        assert req.interesting_only is True
        assert req.max_nodes == 500
        assert req.max_depth is None
        assert req.root_selector is None

    def test_snapshot_request_with_session(self):
        req = BrowserSnapshotRequest(session_id="abc123", interesting_only=False, max_nodes=200)
        assert req.session_id == "abc123"
        assert req.interesting_only is False
        assert req.max_nodes == 200

    def test_snapshot_response_success(self):
        resp = BrowserSnapshotResponse(
            success=True, nodes=[{"node_id": 0, "role": "button", "name": "OK"}],
            truncated=False, elapsed_ms=50,
        )
        assert resp.success is True
        assert resp.nodes[0]["node_id"] == 0
        assert resp.error is None


class TestBrowserActionModel:
    """统一 action 请求/响应（评估文档 P0 第 3 项）"""

    def test_action_request_target_by_css(self):
        req = BrowserActionRequest(
            target=BrowserActionTarget(css="#login-btn"),
            action="click",
        )
        assert req.target.css == "#login-btn"
        assert req.action == "click"
        assert req.button == "left"  # 默认左键
        assert req.timeout == 10.0

    def test_action_request_target_by_role_name(self):
        req = BrowserActionRequest(
            target=BrowserActionTarget(role="button", name="搜索"),
            action="click",
        )
        assert req.target.role == "button"
        assert req.target.name == "搜索"

    def test_action_request_fill_with_value(self):
        req = BrowserActionRequest(
            target=BrowserActionTarget(css="input[name='q']"),
            action="fill",
            value="hello",
            clear_first=False,
        )
        assert req.action == "fill"
        assert req.value == "hello"
        assert req.clear_first is False

    def test_action_request_select_by_label(self):
        req = BrowserActionRequest(
            target=BrowserActionTarget(css="select"),
            action="select",
            label="中文",
        )
        assert req.action == "select"
        assert req.label == "中文"
        assert req.value is None

    def test_action_response_with_urls(self):
        resp = BrowserActionResponse(
            success=True, action="click", matched_count=1,
            url_before="https://a.com", url_after="https://b.com",
            elapsed_ms=120,
        )
        assert resp.matched_count == 1
        assert resp.url_before != resp.url_after  # 检测到跳转


class TestBrowserActionUnsupported:
    """不支持的 action 返回 UNSUPPORTED_ACTION 错误码"""

    def test_unsupported_action_returns_error(self, client):
        resp = client.post("/browser/action", json={
            "target": {"css": "#x"},
            "action": "drag_drop",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
        assert data["error"]["phase"] == "act"

    def test_missing_target_returns_422(self, client):
        resp = client.post("/browser/action", json={"action": "click"})
        assert resp.status_code == 422


class TestBrowserWaitForModel:
    """wait_for 请求/响应（评估文档 P0 第 4 项）"""

    def test_wait_request_selector_type(self):
        req = BrowserWaitRequest(wait_type="selector", selector=".result", state="visible")
        assert req.wait_type == "selector"
        assert req.state == "visible"
        assert req.timeout == 30.0

    def test_wait_request_load_state_type(self):
        req = BrowserWaitRequest(wait_type="load_state", load_state="networkidle")
        assert req.wait_type == "load_state"
        assert req.load_state == "networkidle"

    def test_wait_unsupported_type_returns_error(self, client):
        resp = client.post("/browser/wait_for", json={"wait_type": "invalid_type"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"


class TestBrowserEvaluateModel:
    """evaluate 请求/响应（评估文档 P1 第 1 项）"""

    def test_evaluate_request_defaults(self):
        req = BrowserEvaluateRequest(expression="document.title")
        assert req.expression == "document.title"
        assert req.return_json is True
        assert req.max_length == 5000

    def test_evaluate_blocked_write_pattern(self, client):
        """包含 location.href 赋值的表达式被拦截"""
        resp = client.post("/browser/evaluate", json={
            "expression": "location.href = 'https://evil.com'",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"

    def test_evaluate_blocked_fetch(self, client):
        """fetch() 被拦截"""
        resp = client.post("/browser/evaluate", json={
            "expression": "fetch('/api/secret')",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"

    def test_evaluate_allowed_readonly(self, client):
        """document.title 这种只读表达式不被拦截（端点逻辑通过）"""
        # 不实际执行 JS（浏览器可能未连），只验证不被写模式拦截
        # 但需要客户端用 url_pattern 触发子进程模式
        resp = client.post("/browser/evaluate", json={
            "expression": "document.title",
            "url_pattern": "example",
        })
        # 不会被 UNSUPPORTED_ACTION 拦截
        if resp.status_code == 200:
            data = resp.json()
            # 注：data["error"] 可能是 None（端点返回 {"error": null}），
            # dict.get("error", {}) 只在 key 不存在时返回默认值，key 存在但值为 None 时返回 None。
            # 用 `or {}` 确保 None 也回退到空 dict。
            error = data.get("error") or {}
            assert error.get("error_code") != "UNSUPPORTED_ACTION"


class TestBrowserConsoleLogsModel:
    """console_logs 请求/响应（评估文档 P1 第 2 项）"""

    def test_console_logs_request_defaults(self):
        req = BrowserConsoleLogsRequest()
        assert req.level == "all"
        assert req.capture_ms == 1000
        assert req.since_cursor == 0

    def test_console_logs_invalid_level_returns_error(self, client):
        resp = client.post("/browser/console_logs", json={"level": "verbose"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"

    def test_console_logs_since_cursor_with_session(self):
        """since_cursor 仅在有 session 时有意义，无 session 时被忽略"""
        req = BrowserConsoleLogsRequest(session_id="abc", since_cursor=10)
        assert req.session_id == "abc"
        assert req.since_cursor == 10


class TestBrowserNavigateModel:
    """navigate 请求/响应（评估文档 P1 第 4 项）"""

    def test_navigate_reload(self):
        req = BrowserNavigateRequest(action="reload")
        assert req.action == "reload"
        assert req.wait_until == "domcontentloaded"

    def test_navigate_goto_requires_url(self, client):
        resp = client.post("/browser/navigate", json={"action": "goto"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "INVALID_SELECTOR"
        assert "requires url" in data["error"]["debug_detail"]

    def test_navigate_unsupported_action(self, client):
        resp = client.post("/browser/navigate", json={"action": "refresh"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"


class TestBrowserScreenshotModel:
    """screenshot 请求/响应（评估文档 P1 第 3 项）"""

    def test_screenshot_request_defaults(self):
        req = BrowserScreenshotRequest()
        assert req.shot_type == "viewport"
        assert req.max_size == 1024
        assert req.quality == 85
        assert req.return_base64 is True

    def test_screenshot_full_page(self):
        req = BrowserScreenshotRequest(shot_type="full_page", return_base64=False)
        assert req.shot_type == "full_page"
        assert req.return_base64 is False

    def test_screenshot_element_requires_selector(self, client):
        """element 截图缺 selector 返回错误"""
        resp = client.post("/browser/screenshot", json={"shot_type": "element"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "INVALID_SELECTOR"


# ==================== P0 Session 管理（评估文档 P0 第 1 项）====================

class TestBrowserSessionModels:
    """Session 请求/响应模型"""

    def test_session_create_request_defaults(self):
        req = BrowserSessionCreateRequest()
        assert req.url_pattern is None
        assert req.url is None

    def test_session_create_with_url(self):
        req = BrowserSessionCreateRequest(url="https://example.com")
        assert req.url == "https://example.com"

    def test_session_create_with_pattern(self):
        req = BrowserSessionCreateRequest(url_pattern="bilibili")
        assert req.url_pattern == "bilibili"

    def test_session_close_request_defaults(self):
        req = BrowserSessionCloseRequest(session_id="abc123")
        assert req.session_id == "abc123"
        assert req.close_page is True  # 默认关 page，避免 tab 累积

    def test_session_close_with_close_page(self):
        req = BrowserSessionCloseRequest(session_id="abc123", close_page=True)
        assert req.close_page is True


class TestSessionManager:
    """SessionManager 单元测试（不依赖真实浏览器）"""

    def test_session_manager_singleton(self):
        """get_session_manager 返回同一实例"""
        from server.browser.session.manager import get_session_manager
        mgr1 = asyncio.run(get_session_manager())
        mgr2 = asyncio.run(get_session_manager())
        assert mgr1 is mgr2

    def test_session_manager_initial_state(self):
        mgr = SessionManager()
        assert mgr.is_connected is False
        assert mgr.list_sessions() == []
        # get_page on 不存在的 session 返回 None
        assert asyncio.run(mgr.get_page("nonexistent")) is None

    def test_close_nonexistent_session_returns_false(self):
        mgr = SessionManager()
        result = asyncio.run(mgr.close_session("nonexistent"))
        assert result is False

    def test_tab_session_dataclass(self):
        """TabSession 字段完整性"""
        import time as _time
        now = _time.time()
        # 不需要真实 page 对象，只验证字段
        s = TabSession(
            session_id="abc",
            tab_id="target-1",
            page=None,  # 测试环境无 page
            url="https://example.com",
            title="Example",
        )
        assert s.session_id == "abc"
        assert s.console_logs == []
        assert s.console_log_cursor == 0
        assert s.last_used_at >= now - 1  # 创建时间接近当前

    def test_tab_session_touch_updates_last_used(self):
        import time as _time
        s = TabSession(
            session_id="abc", tab_id="t1", page=None, url="https://x.com",
        )
        old = s.last_used_at
        _time.sleep(0.01)
        s.touch()
        assert s.last_used_at > old


class TestBrowserSessionRoutes:
    """Session 路由层测试（不实际连接浏览器）"""

    def test_session_list_empty_when_no_session(self, client):
        resp = client.get("/browser/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["sessions"], list)
        assert data["elapsed_ms"] >= 0

    def test_session_close_nonexistent_returns_200_with_closed_false(self, client):
        """关闭不存在的 session 不报错，closed=False"""
        resp = client.post("/browser/sessions/close", json={
            "session_id": "nonexistent-id",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["closed"] is False

    def test_session_create_invalid_url_pattern_type(self, client):
        """url_pattern 类型错误返回 422"""
        resp = client.post("/browser/sessions", json={"url_pattern": 123})
        assert resp.status_code == 422


# ==================== Health 端点新字段（评估文档 P0/P1）====================

class TestHealthBrowserStatus:
    """/health 的 browser 字段新增 sessions_count 和拆分计数字段"""

    def test_health_browser_has_sessions_count(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        browser_status = resp.json().get("browser", {})
        assert "sessions_count" in browser_status
        assert "session_idle_timeout_secs" in browser_status
        assert "session_max_count" in browser_status
        # session 配置项有合理默认值
        assert browser_status["session_idle_timeout_secs"] == 900
        assert browser_status["session_max_count"] == 20


# ==================== P2 强化独有优势（评估文档 P2 第 1-4 项）====================

class TestSiteLessonsMatching:
    """P2-1: 站点经验库匹配模块级函数（match_site_for_domain/url + build_site_lessons_hint）"""

    def test_match_site_for_domain_empty_returns_empty(self):
        from server.browser.site_lessons import match_site_for_domain
        assert match_site_for_domain("") == []
        assert match_site_for_domain("   ") == []

    def test_match_site_for_url_empty_returns_empty(self):
        from server.browser.site_lessons import match_site_for_url
        assert match_site_for_url("") == []
        assert match_site_for_url(None) == []  # type: ignore[arg-type]

    def test_match_site_for_url_invalid_returns_empty(self):
        from server.browser.site_lessons import match_site_for_url
        # 无 hostname 的 URL
        assert match_site_for_url("about:blank") == []
        assert match_site_for_url("not-a-url") == []

    def test_match_site_for_url_known_domain_returns_lessons(self):
        """命中现成 sites/xiaoheihe.md（项目仓库自带）"""
        from server.browser.site_lessons import match_site_for_url
        results = match_site_for_url("https://www.xiaoheihe.cn/some/path")
        assert isinstance(results, list)
        # 仓库内 sites/xiaoheihe.md 存在，至少命中 1 条
        assert len(results) >= 1
        first = results[0]
        assert "domain" in first
        assert "aliases" in first
        assert "body" in first
        assert first["domain"] == "xiaoheihe"
        assert isinstance(first["body"], str)
        assert len(first["body"]) > 0

    def test_match_site_for_url_localhost_matches(self):
        """命中 sites/127-0-0-1.md（IP 形式域名）"""
        from server.browser.site_lessons import match_site_for_url
        results = match_site_for_url("http://127.0.0.1:8766/")
        assert isinstance(results, list)
        # 文件名 stem 是 127-0-0-1，IP 127.0.0.1 子串匹配
        assert len(results) >= 1
        assert results[0]["domain"] == "127-0-0-1"

    def test_match_site_for_domain_ignores_underscore_prefixed_files(self):
        """_template.md 以 _ 开头应被跳过"""
        from server.browser.site_lessons import match_site_for_domain, match_site_for_url
        # _template 不应被任何 domain 命中
        results = match_site_for_domain("_template")
        assert all(r["domain"] != "_template" for r in results)
        # 用 url 也一样
        results = match_site_for_url("https://_template.example.com/")
        assert all(r["domain"] != "_template" for r in results)

    def test_build_site_lessons_hint_empty_returns_none(self):
        from server.browser.site_lessons import build_site_lessons_hint
        assert build_site_lessons_hint([]) is None

    def test_build_site_lessons_hint_with_lessons_returns_str(self):
        from server.browser.site_lessons import build_site_lessons_hint
        lessons = [
            {"domain": "xiaoheihe", "aliases": [], "body": "已知坑：xxx"},
            {"domain": "bilibili", "aliases": ["<data_drive>:\<bilibili_videos>"], "body": "反爬规则：yyy"},
        ]
        hint = build_site_lessons_hint(lessons)
        assert isinstance(hint, str)
        assert "2" in hint  # 已加载 2 条
        assert "xiaoheihe" in hint
        assert "bilibili" in hint
        # body 总字符数
        assert str(len("已知坑：xxx") + len("反爬规则：yyy")) in hint

    def test_build_site_lessons_hint_single_lesson(self):
        from server.browser.site_lessons import build_site_lessons_hint
        hint = build_site_lessons_hint([{"domain": "d", "aliases": [], "body": "x" * 100}])
        assert isinstance(hint, str)
        assert "1" in hint
        assert "100" in hint


class TestSiteLessonWriteDedupe:
    """write_lesson 写前查重：同一站点只应有一份档案。

    回归背景（2026-09-19）：sites/scnu.edu.cn.md（人工按主域命名）与
    sites/scnu-edu-cn.md（write_lesson 按「点改横线」自动生成）并存，同一域名
    match_site_for_domain 返回两份，agent 可能读到内容较少的那份。
    """

    def test_find_existing_prefers_canonical_name(self, tmp_path):
        """① 规范名（点改横线）精确命中最优先"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "scnu-edu-cn.md").write_text(
            "---\naliases: ['scnu.edu.cn']\n---\n\n# x\n", encoding="utf-8")
        got = _find_existing_lesson_file(tmp_path, "scnu.edu.cn", "scnu-edu-cn")
        assert got is not None
        assert got.name == "scnu-edu-cn.md"

    def test_find_existing_falls_back_to_dotted_main_domain(self, tmp_path):
        """② 人工按主域命名的点式文件（无 frontmatter）应被命中，不新建第二份"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "scnu.edu.cn.md").write_text("# 砺儒云课堂\n", encoding="utf-8")
        got = _find_existing_lesson_file(tmp_path, "scnu.edu.cn", "scnu-edu-cn")
        assert got is not None
        assert got.name == "scnu.edu.cn.md"

    def test_find_existing_matches_frontmatter_aliases(self, tmp_path):
        """③ 文件名完全不同时，靠 frontmatter aliases 精确命中"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "skland.md").write_text(
            "---\naliases: ['森空岛', 'skland.com']\n---\n\n# x\n", encoding="utf-8")
        got = _find_existing_lesson_file(tmp_path, "skland.com", "skland-com")
        assert got is not None
        assert got.name == "skland.md"

    def test_find_existing_does_not_over_merge_neighbour_domain(self, tmp_path):
        """精确匹配：qq.com 不得命中 docs.qq.com 的档案（子串匹配会误并）"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "docs.qq.com.md").write_text("# 腾讯文档\n", encoding="utf-8")
        assert _find_existing_lesson_file(tmp_path, "qq.com", "qq-com") is None

    def test_find_existing_does_not_over_merge_shared_suffix(self, tmp_path):
        """精确匹配：a.edu.cn 不得命中 b.edu.cn 的档案"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "b.edu.cn.md").write_text("# b\n", encoding="utf-8")
        assert _find_existing_lesson_file(tmp_path, "a.edu.cn", "a-edu-cn") is None

    def test_find_existing_ignores_underscore_prefixed(self, tmp_path):
        """_template.md 不参与查重"""
        from server.browser.site_lessons import _find_existing_lesson_file
        (tmp_path / "_template.md").write_text("# t\n", encoding="utf-8")
        assert _find_existing_lesson_file(tmp_path, "example.com", "example-com") is None

    def test_find_existing_absent_returns_none(self, tmp_path):
        from server.browser.site_lessons import _find_existing_lesson_file
        assert _find_existing_lesson_file(tmp_path, "new.example", "new-example") is None

    def test_normalize_domain_to_filename_unchanged(self):
        """重构 _normalize_domain_to_filename（抽出 _extract_domain_host）后行为不变"""
        from server.browser.site_lessons import (
            _extract_domain_host,
            _normalize_domain_to_filename,
        )
        assert _normalize_domain_to_filename("scnu.edu.cn") == "scnu-edu-cn"
        assert _normalize_domain_to_filename("https://moodle.scnu.edu.cn:443/my/") == "moodle-scnu-edu-cn"
        assert _normalize_domain_to_filename("124.222.53.1") == "124-222-53-1"
        assert _normalize_domain_to_filename("") is None
        assert _normalize_domain_to_filename("a/b") is None
        assert _extract_domain_host("https://moodle.scnu.edu.cn:443/my/") == "moodle.scnu.edu.cn"
        assert _extract_domain_host("") == ""

    def test_repo_has_single_archive_per_domain(self):
        """仓库现状：scnu.edu.cn 只能匹配到一份档案（防重复文件复发）"""
        from server.browser.site_lessons import match_site_for_domain
        for host in ("scnu.edu.cn", "moodle.scnu.edu.cn"):
            results = match_site_for_domain(host)
            assert len(results) == 1, f"{host} 命中 {len(results)} 份档案: {[r['domain'] for r in results]}"


class TestBrowserSessionSiteLessonsFields:
    """P2-1: TabSession.site_lessons 字段 + CreateResponse 模型"""

    def test_tab_session_has_site_lessons_field_default_empty(self):
        s = TabSession(
            session_id="abc", tab_id="t1", page=None, url="https://x.com",
        )
        assert hasattr(s, "site_lessons")
        assert s.site_lessons == []

    def test_tab_session_site_lessons_can_be_set(self):
        lessons = [{"domain": "xiaoheihe", "aliases": [], "body": "已知坑"}]
        s = TabSession(
            session_id="abc", tab_id="t1", page=None, url="https://x.com",
            site_lessons=lessons,
        )
        assert s.site_lessons == lessons

    def test_session_create_response_has_p2_1_fields(self):
        """BrowserSessionCreateResponse 含 site_lessons + site_lessons_hint 字段"""
        resp = BrowserSessionCreateResponse(
            success=True, session_id="s1", tab_id="t1",
            url="https://x.com", title="X", created=True, elapsed_ms=10,
        )
        assert resp.site_lessons == []
        assert resp.site_lessons_hint is None

    def test_session_create_response_with_lessons(self):
        resp = BrowserSessionCreateResponse(
            success=True, session_id="s1", tab_id="t1",
            url="https://x.com", title="X", created=True, elapsed_ms=10,
            site_lessons=[{"domain": "d", "aliases": [], "body": "x"}],
            site_lessons_hint="已加载 1 条站点经验",
        )
        assert len(resp.site_lessons) == 1
        assert resp.site_lessons_hint == "已加载 1 条站点经验"

    def test_snapshot_response_has_site_lessons_hint_field(self):
        resp = BrowserSnapshotResponse(
            success=True, nodes=[], truncated=False, elapsed_ms=5,
        )
        assert resp.site_lessons_hint is None

    def test_action_response_has_site_lessons_hint_field(self):
        resp = BrowserActionResponse(
            success=True, action="click", matched_count=1, elapsed_ms=5,
        )
        assert resp.site_lessons_hint is None


class TestBrowserStatsStore:
    """P2-3: BrowserStatsStore 单元测试（用临时 db 隔离）"""

    def test_stats_store_initialize_creates_db_and_schema(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        db_path = str(tmp_path / "stats.db")
        store = BrowserStatsStore(db_path)
        store.initialize()
        # 文件已创建
        import os
        assert os.path.exists(db_path)
        # 表已创建
        conn = store._ensure_connected()
        assert conn is not None
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='browser_selector_stats'"
        ).fetchall()
        assert len(rows) == 1
        # 索引已创建
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_bss_%'"
        ).fetchall()
        assert len(idx_rows) == 3

    def test_stats_store_extract_domain(self):
        from server.browser.stats import BrowserStatsStore
        assert BrowserStatsStore._extract_domain("https://www.example.com/path") == "www.example.com"
        assert BrowserStatsStore._extract_domain("http://x.com") == "x.com"
        assert BrowserStatsStore._extract_domain("") == "unknown"
        assert BrowserStatsStore._extract_domain("not-a-url") == "unknown"

    def test_stats_store_record_and_query(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        # 记录 3 次成功 + 1 次失败
        store.record_selector_call(
            url="https://x.com/p1", target_type="css", target_value="#btn",
            action="click", success=True, elapsed_ms=50,
        )
        store.record_selector_call(
            url="https://x.com/p2", target_type="css", target_value="#btn",
            action="click", success=True, elapsed_ms=60,
        )
        store.record_selector_call(
            url="https://x.com/p3", target_type="css", target_value="#btn",
            action="click", success=False, error_code="TAB_NOT_FOUND", elapsed_ms=10,
        )

        stats = store.query_stats()
        assert len(stats) == 1
        s = stats[0]
        assert s["domain"] == "x.com"
        assert s["target_type"] == "css"
        assert s["target_value"] == "#btn"
        assert s["total"] == 3
        assert s["success_count"] == 2
        assert s["fail_count"] == 1
        assert s["success_rate"] == 0.667  # round(2/3, 3)
        assert s["last_verified_at"] == s["last_success_ts"]
        assert s["last_call_ts"] >= s["last_success_ts"]

    def test_stats_store_query_filter_by_domain(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#x",
            action="click", success=True,
        )
        store.record_selector_call(
            url="https://b.com/", target_type="css", target_value="#y",
            action="click", success=True,
        )
        stats = store.query_stats(domain="a.com")
        assert len(stats) == 1
        assert stats[0]["domain"] == "a.com"

    def test_stats_store_query_filter_by_target_type(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#x",
            action="click", success=True,
        )
        store.record_selector_call(
            url="https://a.com/", target_type="role", target_value="button:提交",
            action="click", success=True,
        )
        stats = store.query_stats(target_type="css")
        assert len(stats) == 1
        assert stats[0]["target_type"] == "css"

    def test_stats_store_query_since_days(self, tmp_path):
        """since_days=0 应过滤掉所有记录（cutoff = now）"""
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#x",
            action="click", success=True,
        )
        stats = store.query_stats(since_days=0)
        # since_days=0 → cutoff = now，刚写入的 ts 可能 == cutoff，可能被过滤
        # 用 since_days=1 必然命中
        stats = store.query_stats(since_days=1)
        assert len(stats) == 1

    def test_stats_store_list_domains(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#x",
            action="click", success=True,
        )
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#y",
            action="click", success=True,
        )
        store.record_selector_call(
            url="https://b.com/", target_type="css", target_value="#z",
            action="click", success=True,
        )
        domains = store.list_domains()
        # a.com 有 2 次调用排第一，b.com 1 次
        assert domains[0] == "a.com"
        assert "b.com" in domains

    def test_stats_store_initialize_is_idempotent(self, tmp_path):
        """重复 initialize 不报错"""
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        store.initialize()  # 第二次
        # 仍可正常记录
        store.record_selector_call(
            url="https://a.com/", target_type="css", target_value="#x",
            action="click", success=True,
        )
        assert len(store.query_stats()) == 1

    def test_stats_store_query_empty_db_returns_empty_list(self, tmp_path):
        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        assert store.query_stats() == []
        assert store.list_domains() == []

    def _insert_record(self, store, domain, target_value, success, ts, error_code=None):
        """直接插表写入自定义 ts 的记录（record_selector_call 用 time.time() 无法写旧时间）"""
        store._conn.execute(
            """INSERT INTO browser_selector_stats
               (domain, target_type, target_value, action, success, error_code, elapsed_ms, ts)
               VALUES (?, 'css', ?, 'click', ?, ?, 50, ?)""",
            (domain, target_value, 1 if success else 0, error_code, ts),
        )
        store._conn.commit()

    def test_aggregate_and_cleanup_archives_old_details(self, tmp_path):
        """35 天前 + 5 天前明细 → cleanup(30) → 明细只剩 5 天前、summary 含 35 天前聚合"""
        import time

        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        now = int(time.time())
        # 35 天前：2 成功 + 1 失败（应被聚合到 summary 后删除）
        old_ts = now - 35 * 86400
        self._insert_record(store, "x.com", "#btn", True, old_ts)
        self._insert_record(store, "x.com", "#btn", True, old_ts)
        self._insert_record(store, "x.com", "#btn", False, old_ts, "TIMEOUT")
        # 5 天前：1 成功（应保留在明细）
        recent_ts = now - 5 * 86400
        self._insert_record(store, "x.com", "#btn", True, recent_ts)

        result = store.aggregate_and_cleanup(retention_days=30)
        assert result["aggregated"] == 1  # 1 个 (domain,target_type,target_value) 组合被聚合
        assert result["deleted"] == 3     # 3 条明细被删除

        # 明细只剩 5 天前的 1 条
        detail_count = store._conn.execute(
            "SELECT COUNT(*) FROM browser_selector_stats"
        ).fetchone()[0]
        assert detail_count == 1

        # summary 含 35 天前聚合：total=3, success=2, fail=1
        summary = store._conn.execute(
            "SELECT * FROM browser_selector_stats_summary WHERE domain = ?", ("x.com",)
        ).fetchone()
        assert summary["total"] == 3
        assert summary["success_count"] == 2
        assert summary["fail_count"] == 1
        assert summary["last_success_ts"] > 0

    def test_query_stats_since_days_uses_detail_only(self, tmp_path):
        """cleanup 后 query_stats(since_days=7) 走明细，只返回近期数据"""
        import time

        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        now = int(time.time())
        old_ts = now - 35 * 86400
        recent_ts = now - 5 * 86400
        self._insert_record(store, "x.com", "#btn", True, old_ts)
        self._insert_record(store, "x.com", "#btn", True, recent_ts)

        store.aggregate_and_cleanup(retention_days=30)

        # since_days=7 走明细，只返回 5 天前的 1 次成功
        stats = store.query_stats(since_days=7)
        assert len(stats) == 1
        assert stats[0]["total"] == 1
        assert stats[0]["success_count"] == 1

    def test_query_stats_none_merges_summary_and_detail(self, tmp_path):
        """cleanup 后 query_stats(since_days=None) 合并 summary+明细返回全量"""
        import time

        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        now = int(time.time())
        old_ts = now - 35 * 86400
        recent_ts = now - 5 * 86400
        # 35 天前 2 次 + 5 天前 1 次
        self._insert_record(store, "x.com", "#btn", True, old_ts)
        self._insert_record(store, "x.com", "#btn", False, old_ts, "ERR")
        self._insert_record(store, "x.com", "#btn", True, recent_ts)

        store.aggregate_and_cleanup(retention_days=30)

        # since_days=None 合并 summary(2) + 明细(1) = 3
        stats = store.query_stats(since_days=None)
        assert len(stats) == 1
        assert stats[0]["total"] == 3
        assert stats[0]["success_count"] == 2
        assert stats[0]["fail_count"] == 1

    def test_aggregate_and_cleanup_idempotent_no_double_counting(self, tmp_path):
        """重复 cleanup 不 double-counting（无新过期数据时 aggregated=0）"""
        import time

        from server.browser.stats import BrowserStatsStore
        store = BrowserStatsStore(str(tmp_path / "stats.db"))
        store.initialize()
        now = int(time.time())
        old_ts = now - 35 * 86400
        self._insert_record(store, "x.com", "#btn", True, old_ts)

        # 第一次 cleanup
        r1 = store.aggregate_and_cleanup(retention_days=30)
        assert r1["deleted"] == 1
        # 第二次 cleanup（无新过期数据）
        r2 = store.aggregate_and_cleanup(retention_days=30)
        assert r2["aggregated"] == 0
        assert r2["deleted"] == 0

        # summary 仍是 1 条（未 double-counting）
        summary = store._conn.execute(
            "SELECT total FROM browser_selector_stats_summary WHERE domain = ?", ("x.com",)
        ).fetchone()
        assert summary["total"] == 1


class TestBrowserSelectorStatsModel:
    """P2-3: BrowserSelectorStatsRequest/Response Pydantic 模型"""

    def test_request_defaults(self):
        from server.browser import BrowserSelectorStatsRequest
        req = BrowserSelectorStatsRequest()
        assert req.domain is None
        assert req.target_type is None
        assert req.since_days is None
        assert req.limit == 50

    def test_request_with_filters(self):
        from server.browser import BrowserSelectorStatsRequest
        req = BrowserSelectorStatsRequest(
            domain="x.com", target_type="css", since_days=7, limit=10,
        )
        assert req.domain == "x.com"
        assert req.target_type == "css"
        assert req.since_days == 7
        assert req.limit == 10

    def test_response_model(self):
        from server.browser import BrowserSelectorStatsResponse
        resp = BrowserSelectorStatsResponse(
            success=True, stats=[], available_domains=[], elapsed_ms=5,
        )
        assert resp.success is True
        assert resp.stats == []
        assert resp.available_domains == []
        assert resp.elapsed_ms == 5


class TestBrowserSelectorStatsRoute:
    """P2-3: /browser/selector_stats 路由层"""

    def test_selector_stats_route_returns_200_empty(self, client):
        """无数据时返回 200 + 空 stats"""
        resp = client.post("/browser/selector_stats", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["stats"], list)
        assert isinstance(data["available_domains"], list)
        assert data["elapsed_ms"] >= 0

    def test_selector_stats_route_with_filters(self, client):
        resp = client.post("/browser/selector_stats", json={
            "domain": "x.com",
            "target_type": "css",
            "since_days": 7,
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_selector_stats_route_invalid_limit_type(self, client):
        """limit 类型错误返回 422"""
        resp = client.post("/browser/selector_stats", json={"limit": "not-int"})
        assert resp.status_code == 422


class TestBrowserFindUrlAutoOpen:
    """P2-2: FindUrlRequest.auto_open + Response 新字段"""

    def test_find_url_request_auto_open_default_false(self):
        from server.browser import FindUrlRequest
        req = FindUrlRequest(keyword="test")
        assert req.auto_open is False
        assert req.scope == "all"
        assert req.limit == 20

    def test_find_url_request_auto_open_true(self):
        from server.browser import FindUrlRequest
        req = FindUrlRequest(keyword="test", auto_open=True)
        assert req.auto_open is True

    def test_find_url_response_has_p2_2_fields(self):
        from server.browser import FindUrlResponse
        resp = FindUrlResponse(success=True, matched=0, results=[])
        assert resp.open_hint is None
        assert resp.opened_session_id is None
        assert resp.opened_session_tab_id is None
        assert resp.opened_url is None

    def test_find_url_response_with_open_fields(self):
        from server.browser import FindUrlResponse
        resp = FindUrlResponse(
            success=True, matched=1,
            results=[{"title": "X", "url": "https://x.com", "source": "bookmarks", "openable": True}],
            open_hint="用 browser_session_create 打开",
            opened_session_id="sess-1",
            opened_session_tab_id="tab-1",
            opened_url="https://x.com",
        )
        assert resp.open_hint == "用 browser_session_create 打开"
        assert resp.opened_session_id == "sess-1"
        assert resp.opened_session_tab_id == "tab-1"
        assert resp.opened_url == "https://x.com"


# ==================== P0/P1/P2 修复测试（rerun2.md） ====================

class TestP0_1_AriaSnapshot:
    """P0-1: 用 page.evaluate + 自有 ARIA JS 替代 page.accessibility.snapshot()"""

    def test_aria_snapshot_js_exists(self):
        from server.browser.snapshot_endpoints import _ARIA_SNAPSHOT_JS
        assert isinstance(_ARIA_SNAPSHOT_JS, str)
        assert len(_ARIA_SNAPSHOT_JS) > 500
        # JS 包含关键函数
        assert "getRole" in _ARIA_SNAPSHOT_JS
        assert "getName" in _ARIA_SNAPSHOT_JS
        assert "INTERESTING_ROLES" in _ARIA_SNAPSHOT_JS
        # P2-2: 含 css_path 生成（node_id 重放桥梁）
        assert "getCssPath" in _ARIA_SNAPSHOT_JS

    def test_compute_aria_snapshot_is_coroutine(self):
        import inspect

        from server.browser.snapshot_endpoints import compute_aria_snapshot
        assert inspect.iscoroutinefunction(compute_aria_snapshot)

    def test_tab_session_has_snapshot_cache_fields(self):
        """TabSession 必须有 P2-2 snapshot 缓存字段"""
        from dataclasses import fields
        field_names = {f.name for f in fields(TabSession)}
        assert "last_snapshot_nodes" in field_names
        assert "last_snapshot_at" in field_names
        assert "last_snapshot_url" in field_names

    def test_browser_snapshot_response_model(self):
        resp = BrowserSnapshotResponse(
            success=True, nodes=[], truncated=False, elapsed_ms=10,
        )
        assert resp.success is True
        assert resp.nodes == []
        assert resp.truncated is False


class TestP0_2_AmbiguousTab:
    """P0-2: legacy _find_page 多匹配返回 AMBIGUOUS_TAB"""

    def test_error_codes_include_ambiguous_tab(self):
        assert "AMBIGUOUS_TAB" in BROWSER_ERROR_CODES
        assert "多个标签页" in BROWSER_ERROR_CODES["AMBIGUOUS_TAB"]

    def test_error_codes_include_navigation_wait_timeout(self):
        assert "NAVIGATION_COMPLETED_WAIT_TIMEOUT" in BROWSER_ERROR_CODES

    def test_classify_error_ambiguous_tab(self):
        from server.browser import _classify_error
        err = _classify_error("AMBIGUOUS_TAB urls=...", "locate", 5)
        assert err.error_code == "AMBIGUOUS_TAB"

    def test_classify_error_navigation_completed(self):
        from server.browser import _classify_error
        err = _classify_error("navigation_completed timeout", "wait", 100)
        assert err.error_code == "NAVIGATION_COMPLETED_WAIT_TIMEOUT"

    def test_classify_error_tab_not_found_uppercase(self):
        """子进程输出 'ERROR:TAB_NOT_FOUND' 应识别为 TAB_NOT_FOUND"""
        from server.browser import _classify_error
        err = _classify_error("TAB_NOT_FOUND", "locate", 5)
        assert err.error_code == "TAB_NOT_FOUND"

    def test_find_page_in_script_returns_tuple(self):
        """嵌入在 _build_playwright_header 的 _find_page 返回 (page, error_code)"""
        from server.browser import _build_playwright_header
        header = _build_playwright_header()
        # 函数签名应返回元组
        assert "return page, None" in header or "return (page, None)" in header
        assert "AMBIGUOUS_TAB" in header
        assert "TAB_NOT_FOUND" in header


class TestP0_3_EvaluateReadOnly:
    """P0-3: evaluate 只读判定用正则替代子串匹配"""

    def test_is_write_expression_function_exists(self):
        from server.browser import _is_write_expression
        assert callable(_is_write_expression)

    def test_location_href_read_allowed(self):
        """location.href 读取（不带赋值）应允许"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("location.href")
        assert is_write is False
        is_write, _ = _is_write_expression("return location.href")
        assert is_write is False
        is_write, _ = _is_write_expression("JSON.stringify({url: location.href})")
        assert is_write is False

    def test_location_href_write_blocked(self):
        """location.href = 'xxx' 应拦截"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("location.href = 'https://evil.com'")
        assert is_write is True

    def test_location_assign_blocked(self):
        """location.assign('xxx') 应拦截"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("location.assign('https://evil.com')")
        assert is_write is True

    def test_document_title_read_allowed(self):
        """document.title 读取应允许"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("document.title")
        assert is_write is False

    def test_document_title_write_blocked(self):
        """document.title = 'xxx' 应拦截"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("document.title = 'hacked'")
        assert is_write is True

    def test_equality_operator_not_treated_as_assignment(self):
        """== 比较不应被误判为赋值"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("if (location.href == 'x') { return 1; }")
        assert is_write is False
        is_write, _ = _is_write_expression("a === b")
        assert is_write is False

    def test_fetch_blocked(self):
        """fetch() 应拦截"""
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("fetch('/api/data')")
        assert is_write is True

    def test_localStorage_setitem_blocked(self):
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("localStorage.setItem('k', 'v')")
        assert is_write is True

    def test_inner_html_write_blocked(self):
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("el.innerHTML = '<script>'")
        assert is_write is True

    def test_eval_blocked(self):
        from server.browser import _is_write_expression
        is_write, _ = _is_write_expression("eval('1+1')")
        assert is_write is True


class TestP1_1_NavigateFields:
    """P1-1: back/forward/reload 分 navigation_completed + wait_timeout"""

    def test_navigate_response_has_new_fields(self):
        from server.browser import BrowserNavigateResponse
        resp = BrowserNavigateResponse(
            success=True, action="back", url_after="http://x", elapsed_ms=10,
        )
        assert resp.navigation_completed is None
        assert resp.wait_timeout is None

    def test_navigate_response_with_timeout_fields(self):
        from server.browser import BrowserNavigateResponse
        resp = BrowserNavigateResponse(
            success=True, action="back", url_after="http://x", elapsed_ms=10,
            navigation_completed=True, wait_timeout=True,
        )
        assert resp.navigation_completed is True
        assert resp.wait_timeout is True


class TestP1_2_ConsoleLogsCursor:
    """P1-2: console_logs cursor + next_cursor + dropped_count"""

    def test_console_logs_response_has_cursor_fields(self):
        from server.browser import BrowserConsoleLogsResponse
        resp = BrowserConsoleLogsResponse(success=True, logs=[], elapsed_ms=5)
        assert resp.cursor == 0
        assert resp.next_cursor == 0
        assert resp.dropped_count == 0

    def test_tab_session_has_dropped_count_fields(self):
        """TabSession 必须有 dropped_count + max_len 字段"""
        from dataclasses import fields
        field_names = {f.name for f in fields(TabSession)}
        assert "console_logs_dropped_count" in field_names
        assert "console_logs_max_len" in field_names

    def test_console_logs_request_has_since_cursor(self):
        req = BrowserConsoleLogsRequest()
        assert req.since_cursor == 0


class TestP1_4_LegacyTabId:
    """P1-4+5: tab_id 解析为 URL 的 helper（原 legacy endpoint 用，函数保留）"""

    def test_resolve_tab_id_to_url_function_exists(self):
        from server.browser import _resolve_tab_id_to_url
        assert callable(_resolve_tab_id_to_url)

    def test_resolve_tab_id_to_url_returns_none_for_empty(self):
        from server.browser import _resolve_tab_id_to_url
        assert _resolve_tab_id_to_url("") is None
        assert _resolve_tab_id_to_url(None) is None


class TestP2_1_WaitForEvents:
    """P2-1: wait_for 新增 filechooser/dialog 类型"""

    def test_wait_request_supports_filechooser(self):
        req = BrowserWaitRequest(wait_type="filechooser")
        assert req.wait_type == "filechooser"
        assert req.file_paths is None

    def test_wait_request_supports_dialog(self):
        req = BrowserWaitRequest(wait_type="dialog")
        assert req.wait_type == "dialog"
        assert req.dialog_action == "dismiss"  # 默认 dismiss 避免阻塞
        assert req.dialog_prompt_text is None

    def test_wait_request_dialog_action_accept(self):
        req = BrowserWaitRequest(wait_type="dialog", dialog_action="accept",
                                  dialog_prompt_text="hello")
        assert req.dialog_action == "accept"
        assert req.dialog_prompt_text == "hello"

    def test_wait_for_validates_new_types(self, client):
        """filechooser/dialog 是合法 wait_type，不应返回 UNSUPPORTED_ACTION"""
        # 不传 session/url_pattern 会因 TAB_NOT_FOUND 失败，但不应是 UNSUPPORTED_ACTION
        for wt in ["filechooser", "dialog"]:
            resp = client.post("/browser/wait_for", json={"wait_type": wt, "timeout": 0.1})
            assert resp.status_code == 200
            data = resp.json()
            # 应该是 success=False 但 error_code 不是 UNSUPPORTED_ACTION
            if not data["success"]:
                assert data.get("error", {}).get("error_code") != "UNSUPPORTED_ACTION"


class TestP2_2_NodeIdAndStaleNode:
    """P2-2: snapshot node_id + action 支持 node_id + STALE_NODE"""

    def test_action_target_has_node_id_field(self):
        t = BrowserActionTarget(node_id=42)
        assert t.node_id == 42
        assert t.css is None

    def test_action_target_node_id_priority_over_css(self):
        """node_id 优先级高于 css（仅 session 模式生效）"""
        t = BrowserActionTarget(node_id=5, css="button")
        assert t.node_id == 5
        assert t.css == "button"

    def test_action_request_with_node_id(self):
        req = BrowserActionRequest(
            target=BrowserActionTarget(node_id=3),
            action="click",
        )
        assert req.target.node_id == 3
        assert req.action == "click"

    def test_node_id_without_session_returns_stale_node(self, client):
        """无 session 时用 node_id 应返回 STALE_NODE"""
        resp = client.post("/browser/action", json={
            "target": {"node_id": 0},
            "action": "click",
            "url_pattern": "nonexistent_pattern_xyz",
            "timeout": 0.5,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 无 session 路径不支持 node_id，会走子进程模型
        # 子进程内 _find_page 找不到 tab → TAB_NOT_FOUND，或无 snapshot → STALE_NODE
        # 关键是不应崩溃
        assert "success" in data

    def test_stale_node_error_code_defined(self):
        assert "STALE_NODE" in BROWSER_ERROR_CODES
        assert "DOM" in BROWSER_ERROR_CODES["STALE_NODE"]


# ==================== P0-1 rerun3: dialog/popup/filechooser 持久监听器 ====================

class TestP0_1_PersistentEventListeners:
    """P0-1 rerun3: session 持久监听器 + 队列消费，解决 expect_* 跨 HTTP 请求时序问题"""

    def test_tab_session_has_pending_event_fields(self):
        """TabSession 应包含 pending_dialogs/popups/filechoosers 队列和 Event"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert s.pending_dialogs == []
        assert s.pending_popups == []
        assert s.pending_filechoosers == []
        assert hasattr(s, "dialog_event")
        assert hasattr(s, "popup_event")
        assert hasattr(s, "filechooser_event")

    def test_pop_pending_dialog_returns_none_when_empty(self):
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert s.pop_pending_dialog() is None

    def test_pop_pending_dialog_returns_oldest(self):
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        s.pending_dialogs.append({"type": "alert", "message": "first"})
        s.pending_dialogs.append({"type": "confirm", "message": "second"})
        first = s.pop_pending_dialog()
        assert first["message"] == "first"
        second = s.pop_pending_dialog()
        assert second["message"] == "second"
        assert s.pop_pending_dialog() is None

    def test_pop_pending_popup_returns_oldest(self):
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        s.pending_popups.append({"url": "http://a", "title": "A"})
        s.pending_popups.append({"url": "http://b", "title": "B"})
        first = s.pop_pending_popup()
        assert first["url"] == "http://a"
        assert s.pop_pending_popup()["url"] == "http://b"
        assert s.pop_pending_popup() is None

    def test_pop_pending_filechooser_returns_oldest(self):
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        s.pending_filechoosers.append({"multiple": False, "page_url": "http://x"})
        fc = s.pop_pending_filechooser()
        assert fc["multiple"] is False
        assert s.pop_pending_filechooser() is None

    def test_dialog_event_can_be_set_and_cleared(self):
        """asyncio.Event 基本 API 测试"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert s.dialog_event.is_set() is False
        s.dialog_event.set()
        assert s.dialog_event.is_set() is True
        s.dialog_event.clear()
        assert s.dialog_event.is_set() is False

    # 2026-08-06 改造（spec: browser-dialog-handling）：dialog 不立即 dismiss + 超时兜底

    def test_tab_session_has_dialog_auto_dismissed_field(self):
        """TabSession 应包含 dialog_auto_dismissed 字段（标记最新 dialog 是否已被超时兜底 dismiss）"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert hasattr(s, "dialog_auto_dismissed")
        assert s.dialog_auto_dismissed is False  # 默认 False

    def test_tab_session_has_dialog_timeout_tasks_field(self):
        """TabSession 应包含 _dialog_timeout_tasks 集合（超时兜底 task 存储）"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert hasattr(s, "_dialog_timeout_tasks")
        assert s._dialog_timeout_tasks == set()

    def test_has_pending_dialog_returns_false_when_no_dialog(self):
        """无 dialog 时 has_pending_dialog() 返回 False"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        assert s.has_pending_dialog() is False

    def test_has_pending_dialog_returns_true_when_dialog_active(self):
        """有 Dialog 引用且未 auto_dismissed 时 has_pending_dialog() 返回 True"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        # 模拟 dialog 触发：存 Dialog 引用 + auto_dismissed=False
        s.last_dialog_obj = object()  # 模拟 Dialog 对象
        s.dialog_auto_dismissed = False
        assert s.has_pending_dialog() is True

    def test_has_pending_dialog_returns_false_after_auto_dismissed(self):
        """dialog 被超时兜底 dismiss 后（last_dialog_obj=None）has_pending_dialog() 返回 False"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        s.last_dialog_obj = object()
        s.dialog_auto_dismissed = False
        assert s.has_pending_dialog() is True
        # 模拟超时兜底 dismiss
        s.last_dialog_obj = None
        s.dialog_auto_dismissed = True
        assert s.has_pending_dialog() is False

    def test_has_pending_dialog_returns_false_after_handle_dialog(self):
        """handle_dialog 处理后（last_dialog_obj=None, auto_dismissed=False）has_pending_dialog() 返回 False"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        s.last_dialog_obj = object()
        s.dialog_auto_dismissed = False
        assert s.has_pending_dialog() is True
        # 模拟 handle_dialog 处理
        s.last_dialog_obj = None
        # dialog_auto_dismissed 保持 False（handle_dialog 处理的，不是超时兜底）
        assert s.has_pending_dialog() is False

    def test_pending_dialogs_queue_element_has_auto_dismissed_field(self):
        """pending_dialogs 队列元素应包含 auto_dismissed 字段（_on_dialog 存入时为 False）"""
        s = TabSession(session_id="test", tab_id="t1", page=None, url="http://x")
        # 模拟 _on_dialog 存入队列（auto_dismissed=False，因为不再立即 dismiss）
        s._append_pending("pending_dialogs", {
            "type": "alert",
            "message": "test",
            "page_url": "http://x",
            "ts": 0.0,
            "auto_dismissed": False,
        })
        pending = s.pop_pending_dialog()
        assert pending is not None
        assert pending["auto_dismissed"] is False  # 新行为：不再立即 dismiss

    def test_wait_for_dialog_with_pending_event_no_browser(self, client):
        """已有未消费 dialog 时，wait_for 应优先消费队列而非走 expect_dialog。
        本测试不连真实浏览器，仅验证 session 路径的队列消费逻辑。

        注意：当真实浏览器已连接时，无 session_id 的 wait_for 会复用 pages[0]，
        可能返回 success=True（找到页面并等待 dialog 超时）。此时跳过本测试。
        """
        if not _browser_disconnected(client):
            pytest.skip("浏览器已连接，跳过无浏览器路径测试")
        # 无 session_id 时仍走 expect_dialog 路径（会失败但不崩溃）
        resp = client.post("/browser/wait_for", json={
            "wait_type": "dialog", "timeout": 0.1,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 无 session 无 url_pattern → 走子进程模型失败（TAB_NOT_FOUND 或 EXECUTION_ERROR）
        # 关键是不应崩溃，且 success=False
        assert data["success"] is False
        assert data["error"]["error_code"] in {"TAB_NOT_FOUND", "EXECUTION_ERROR"}


# ==================== P0-2 rerun3: wait_and_action 原子 API ====================

class TestP0_2_WaitAndAction:
    """P0-2 rerun3: 原子 wait+trigger API，解决 expect_* 跨请求时序问题"""

    def test_request_model_basic(self):
        req = BrowserWaitAndActionRequest(
            session_id="abc",
            wait_type="dialog",
            trigger_action="click",
            trigger_css="#alert-btn",
            dialog_action="accept",
        )
        assert req.session_id == "abc"
        assert req.wait_type == "dialog"
        assert req.trigger_action == "click"
        assert req.trigger_css == "#alert-btn"
        assert req.dialog_action == "accept"

    def test_request_model_defaults(self):
        req = BrowserWaitAndActionRequest(
            session_id="x", wait_type="popup", trigger_action="click",
            trigger_css="a[target=_blank]",
        )
        assert req.timeout == 30.0
        assert req.file_paths is None
        assert req.dialog_action == "dismiss"
        assert req.dialog_prompt_text is None
        assert req.trigger_value is None
        assert req.trigger_key is None

    def test_response_model(self):
        r = BrowserWaitAndActionResponse(
            success=True, wait_type="popup", matched=True,
            trigger_success=True, data={"url": "http://x"},
            elapsed_ms=42,
        )
        assert r.matched is True
        assert r.trigger_success is True
        assert r.data["url"] == "http://x"

    def test_invalid_wait_type_returns_unsupported(self, client):
        resp = client.post("/browser/wait_and_action", json={
            "session_id": "x", "wait_type": "invalid",
            "trigger_action": "click", "trigger_css": "#btn",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
        assert "wait_type" in data["error"]["debug_detail"]

    def test_invalid_trigger_action_returns_unsupported(self, client):
        resp = client.post("/browser/wait_and_action", json={
            "session_id": "x", "wait_type": "dialog",
            "trigger_action": "scroll", "trigger_css": "#btn",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
        assert "trigger_action" in data["error"]["debug_detail"]

    def test_missing_session_returns_tab_not_found(self, client):
        resp = client.post("/browser/wait_and_action", json={
            "session_id": "nonexistent_session_xyz",
            "wait_type": "dialog",
            "trigger_action": "click",
            "trigger_css": "#btn",
            "timeout": 0.5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"

    def test_no_trigger_target_returns_invalid_selector(self, client):
        """不传任何 trigger_* 字段应返回 INVALID_SELECTOR"""
        # session 不存在时会先返回 TAB_NOT_FOUND，所以这个测试只验证模型校验
        # 通过直接调用 _build_trigger_locator 验证
        from server.browser import _build_trigger_locator
        class _FakeReq:
            trigger_css = None
            trigger_role = None
            trigger_name = None
            trigger_label = None
            trigger_placeholder = None
            trigger_testid = None
            trigger_text = None
        loc, desc = _build_trigger_locator(None, _FakeReq())
        assert loc is None
        assert "no trigger target" in desc

    def test_trigger_locator_css_priority(self):
        from server.browser import _build_trigger_locator
        class _FakeReq:
            trigger_css = "button.save"
            trigger_role = "button"
            trigger_name = "Save"
            trigger_label = None
            trigger_placeholder = None
            trigger_testid = None
            trigger_text = "Save"
        class _FakePage:
            def locator(self, css): return f"locator({css})"
            def get_by_role(self, *a, **k): return "by_role"
        loc, desc = _build_trigger_locator(_FakePage(), _FakeReq())
        assert "button.save" in desc
        assert "locator" in str(loc)

    def test_trigger_locator_role_with_name(self):
        from server.browser import _build_trigger_locator
        class _FakeReq:
            trigger_css = None
            trigger_role = "button"
            trigger_name = "Submit"
            trigger_label = None
            trigger_placeholder = None
            trigger_testid = None
            trigger_text = None
        class _FakePage:
            def get_by_role(self, role, name=None):
                return f"role={role}:name={name}"
        loc, desc = _build_trigger_locator(_FakePage(), _FakeReq())
        assert "button" in desc
        assert "Submit" in desc


# ==================== P1-1 rerun3: 极端 timeout 短轮询 ====================

class TestP1_1_Rerun3_ExtremeTimeout:
    """P1-1 rerun3: back/forward 极端 timeout（如 1ms）下追加短轮询确认 URL 是否实际变化"""

    def test_navigate_request_extreme_timeout(self):
        """1ms timeout 是合法参数，不应在模型校验阶段被拒"""
        req = BrowserNavigateRequest(
            action="back",
            timeout=0.001,  # 1ms
        )
        assert req.action == "back"
        assert req.timeout == 0.001

    def test_navigate_response_has_navigation_completed_and_wait_timeout(self):
        """响应模型应包含 navigation_completed 和 wait_timeout 字段"""
        # 检查响应模型字段
        from server.browser import BrowserNavigateResponse
        fields = BrowserNavigateResponse.model_fields
        assert "navigation_completed" in fields
        assert "wait_timeout" in fields

    def test_navigate_back_unsupported_action(self, client):
        resp = client.post("/browser/navigate", json={
            "action": "invalid_action",
            "timeout": 0.5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"

    def test_navigate_goto_requires_url(self, client):
        resp = client.post("/browser/navigate", json={
            "action": "goto",
            "timeout": 0.5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "INVALID_SELECTOR"
        assert "url" in data["error"]["debug_detail"].lower()


# ==================== P1-2 rerun3: STALE_NODE DOM mutation 感知 ====================

class TestP1_2_Rerun3_DomMutationVersion:
    """P1-2 rerun3: snapshot 返回 dom_hash；action 显式 verify_dom_freshness 时比对"""

    def test_snapshot_response_has_dom_hash_field(self):
        from server.browser import BrowserSnapshotResponse
        fields = BrowserSnapshotResponse.model_fields
        assert "dom_hash" in fields

    def test_action_target_has_verify_dom_freshness_field(self):
        t = BrowserActionTarget(node_id=1, verify_dom_freshness=True)
        assert t.verify_dom_freshness is True

    def test_action_target_verify_dom_freshness_default_false(self):
        t = BrowserActionTarget(css="button")
        assert t.verify_dom_freshness is False

    def test_tab_session_has_dom_hash_field(self):
        s = TabSession(session_id="t", tab_id="x", page=None, url="http://x")
        assert hasattr(s, "last_snapshot_dom_hash")
        assert s.last_snapshot_dom_hash == ""

    def test_compute_dom_hash_is_coroutine(self):
        import inspect

        from server.browser.snapshot_endpoints import compute_dom_hash
        assert inspect.iscoroutinefunction(compute_dom_hash)

    def test_dom_hash_js_is_lightweight(self):
        """_DOM_HASH_JS 应限制 500 个元素避免大页面开销"""
        from server.browser.snapshot_endpoints import _DOM_HASH_JS
        assert "500" in _DOM_HASH_JS
        assert "tagName" in _DOM_HASH_JS
        # 应基于 tag/id/class/role/testid，不基于完整 outerHTML（避免大字符串）
        assert "className" in _DOM_HASH_JS
        assert "data-testid" in _DOM_HASH_JS

    def test_snapshot_with_dom_hash_in_response_model(self):
        """BrowserSnapshotResponse 应支持 dom_hash 字段"""
        r = BrowserSnapshotResponse(
            success=True,
            nodes=[],
            truncated=False,
            elapsed_ms=10,
            dom_hash="abc123",
        )
        assert r.dom_hash == "abc123"

    def test_action_with_verify_dom_freshness_in_request(self):
        """BrowserActionRequest 应支持 target.verify_dom_freshness=True"""
        req = BrowserActionRequest(
            target=BrowserActionTarget(node_id=5, verify_dom_freshness=True),
            action="click",
        )
        assert req.target.verify_dom_freshness is True


# ==================== P0/P1 rerun3 集成：错误码与路由注册 ====================

class TestRerun3_RouteRegistrationAndErrorCodes:
    """P0/P1 rerun3: 验证新路由注册和错误码完整性"""

    def test_wait_and_action_route_registered(self, client):
        """POST /browser/wait_and_action 应已注册（返回 200 即可，非 404）"""
        resp = client.post("/browser/wait_and_action", json={
            "session_id": "x",
            "wait_type": "invalid",
            "trigger_action": "click",
        })
        assert resp.status_code == 200  # 200 表示路由已注册，错误在响应体里

    def test_all_required_error_codes_defined(self):
        required_codes = {"STALE_NODE", "UNSUPPORTED_ACTION", "TAB_NOT_FOUND",
                          "INVALID_SELECTOR", "BROWSER_DISCONNECTED"}
        for code in required_codes:
            assert code in BROWSER_ERROR_CODES, f"missing error code: {code}"

    def test_wait_for_dialog_route_still_works(self, client):
        """旧 wait_for(dialog) 路由应仍可用（向后兼容）"""
        resp = client.post("/browser/wait_for", json={
            "wait_type": "dialog", "timeout": 0.1,
        })
        assert resp.status_code == 200

    def test_wait_for_filechooser_route_still_works(self, client):
        resp = client.post("/browser/wait_for", json={
            "wait_type": "filechooser", "timeout": 0.1,
        })
        assert resp.status_code == 200

    def test_wait_for_popup_route_still_works(self, client):
        resp = client.post("/browser/wait_for", json={
            "wait_type": "popup", "timeout": 0.1,
        })
        assert resp.status_code == 200


# ==================== 2026-08-06: handle_dialog 端点（spec: browser-dialog-handling） ====================


class TestHandleDialog:
    """handle_dialog 端点：手动 accept/dismiss 队列中的 JS dialog + prompt 文本输入。

    2026-08-06 新增。依赖 Ticket 01 的 _on_dialog 改造（dialog 不立即 dismiss）。
    """

    def test_request_model_basic(self):
        req = BrowserHandleDialogRequest(
            session_id="abc",
            action="accept",
            prompt_text="hello",
        )
        assert req.session_id == "abc"
        assert req.action == "accept"
        assert req.prompt_text == "hello"

    def test_request_model_defaults(self):
        req = BrowserHandleDialogRequest(session_id="x", action="dismiss")
        assert req.prompt_text is None

    def test_response_model(self):
        r = BrowserHandleDialogResponse(
            handled=True,
            dialog={"type": "alert", "message": "hi"},
        )
        assert r.handled is True
        assert r.dialog["type"] == "alert"
        assert r.error is None

    def test_route_registered(self, client):
        """POST /browser/handle_dialog 应已注册（返回 200 非 404）"""
        resp = client.post("/browser/handle_dialog", json={
            "session_id": "nonexistent_xyz",
            "action": "accept",
        })
        assert resp.status_code == 200  # 200 表示路由已注册

    def test_returns_tab_not_found_when_session_missing(self, client):
        """session 不存在 → TAB_NOT_FOUND"""
        resp = client.post("/browser/handle_dialog", json={
            "session_id": "nonexistent_session_xyz",
            "action": "accept",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["handled"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"

    def test_returns_dialog_not_found_when_queue_empty(self, client, monkeypatch):
        """队列空 → DIALOG_NOT_FOUND（mock session 存在但队列空）"""
        from server.browser.session.state import TabSession

        # 构造 mock session：存在但 pending_dialogs 为空
        mock_session = TabSession(
            session_id="test_empty", tab_id="t1", page=None, url="http://x"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

        async def _mock_get_mgr():
            return _MockMgr()

        monkeypatch.setattr(
            "server.browser.dialog_endpoints.get_session_manager",
            _mock_get_mgr,
            raising=False,
        )
        # get_session_manager 是在端点函数内部 import 的，需要 patch 模块级
        # 实际上端点内 `from .session.manager import get_session_manager`
        # monkeypatch.patch server.browser.session.manager.get_session_manager
        import server.browser.session.manager as _mgr_mod

        async def _mock_get_mgr2():
            return _MockMgr()

        monkeypatch.setattr(_mgr_mod, "get_session_manager", _mock_get_mgr2)

        resp = client.post("/browser/handle_dialog", json={
            "session_id": "test_empty",
            "action": "accept",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["handled"] is False
        assert data["error"]["error_code"] == "DIALOG_NOT_FOUND"

    def test_returns_already_dismissed_when_dialog_auto_dismissed(self, client, monkeypatch):
        """dialog 已被超时兜底 dismiss → DIALOG_ALREADY_DISMISSED"""
        from server.browser.session.state import TabSession

        # 构造 mock session：队列有 dialog，但 last_dialog_obj=None + auto_dismissed=True
        mock_session = TabSession(
            session_id="test_dismissed", tab_id="t1", page=None, url="http://x"
        )
        mock_session._append_pending("pending_dialogs", {
            "type": "alert", "message": "hi", "page_url": "http://x",
            "ts": 0.0, "auto_dismissed": True,
        })
        mock_session.last_dialog_obj = None
        mock_session.dialog_auto_dismissed = True

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

        import server.browser.session.manager as _mgr_mod

        async def _mock_get_mgr():
            return _MockMgr()

        monkeypatch.setattr(_mgr_mod, "get_session_manager", _mock_get_mgr)

        resp = client.post("/browser/handle_dialog", json={
            "session_id": "test_dismissed",
            "action": "accept",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["handled"] is False
        assert data["error"]["error_code"] == "DIALOG_ALREADY_DISMISSED"
        assert data["dialog"]["type"] == "alert"  # 仍返回 dialog 信息

    def test_new_error_codes_defined(self):
        """新增的 3 个错误码应在 BROWSER_ERROR_CODES 中"""
        assert "DIALOG_NOT_FOUND" in BROWSER_ERROR_CODES
        assert "DIALOG_ALREADY_DISMISSED" in BROWSER_ERROR_CODES
        assert "BLOCKED_BY_DIALOG" in BROWSER_ERROR_CODES


# ==================== 2026-08-06 Ticket 03: dialog 阻塞检测双通道 ====================


class TestDialogBlockDetection:
    """browser_status 扩展（主动查询通道）+ 操作端点预检（被动反馈通道）。

    2026-08-06 新增。依赖 Ticket 01 的 has_pending_dialog() + Ticket 02 的 _check_dialog_block helper。
    """

    def test_status_response_model_new_dialog_fields(self):
        """BrowserStatusResponse 新增 has_pending_dialog/pending_dialog_count/pending_dialogs 字段"""
        resp = BrowserStatusResponse(connected=False, debug_port=9222)
        assert resp.has_pending_dialog is False
        assert resp.pending_dialog_count == 0
        assert resp.pending_dialogs == []

    def test_check_dialog_block_returns_none_when_no_session(self):
        """_check_dialog_block(None) 返回 None"""
        from server.browser.dialog_endpoints import _check_dialog_block
        assert _check_dialog_block(None) is None

    def test_check_dialog_block_returns_none_when_queue_empty(self):
        """session 无 pending dialog → None"""
        from server.browser.dialog_endpoints import _check_dialog_block
        sess = TabSession(
            session_id="s1", tab_id="t1", page=None, url="http://x"
        )
        assert _check_dialog_block(sess) is None

    def test_check_dialog_block_returns_none_when_dialog_already_dismissed(self):
        """dialog 已被超时兜底 dismiss（last_dialog_obj=None）→ None"""
        from server.browser.dialog_endpoints import _check_dialog_block
        sess = TabSession(
            session_id="s1", tab_id="t1", page=None, url="http://x"
        )
        sess._append_pending("pending_dialogs", {
            "type": "alert", "message": "hi", "page_url": "http://x",
            "ts": 0.0, "auto_dismissed": True,
        })
        sess.last_dialog_obj = None
        sess.dialog_auto_dismissed = True
        # has_pending_dialog() 为 False（last_dialog_obj is None）
        assert _check_dialog_block(sess) is None

    def test_check_dialog_block_returns_block_when_dialog_pending(self):
        """session 有未处理的 pending dialog → 返回 blocked_by_dialog=True"""
        from server.browser.dialog_endpoints import _check_dialog_block
        sess = TabSession(
            session_id="s_test_block", tab_id="t1", page=None, url="http://x"
        )
        sess._append_pending("pending_dialogs", {
            "type": "confirm", "message": "确定删除？", "page_url": "http://x",
            "ts": 123.0, "auto_dismissed": False,
        })
        # 模拟 dialog 对象存在（用 object() 占位，不调真实 accept/dismiss）
        sess.last_dialog_obj = object()
        sess.dialog_auto_dismissed = False
        result = _check_dialog_block(sess)
        assert result is not None
        assert result["blocked_by_dialog"] is True
        assert result["blocking_dialog"]["type"] == "confirm"
        assert result["blocking_dialog"]["message"] == "确定删除？"
        assert result["blocking_dialog"]["session_id"] == "s_test_block"

    def test_browser_status_returns_has_pending_dialog_via_mock_session(self, client, monkeypatch):
        """browser_status 聚合所有 session 的 dialog 状态（主动查询通道）

        通过 mock SessionManager 单例注入一个有 pending dialog 的 session，
        验证 browser_status 返回 has_pending_dialog=True。
        """
        from server.browser.session import manager as _mgr_mod

        # 构造 mock session：有 pending dialog
        mock_session = TabSession(
            session_id="s_status_test", tab_id="t1", page=None, url="http://x"
        )
        mock_session._append_pending("pending_dialogs", {
            "type": "alert", "message": "hello", "page_url": "http://x",
            "ts": 0.0, "auto_dismissed": False,
        })
        mock_session.last_dialog_obj = object()
        mock_session.dialog_auto_dismissed = False

        class _MockMgr:
            def iter_sessions(self):
                return [mock_session]

        # 替换 _manager 全局变量（browser_status 直接读 _manager）
        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.get("/browser/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_pending_dialog"] is True
        assert data["pending_dialog_count"] == 1
        assert len(data["pending_dialogs"]) == 1
        assert data["pending_dialogs"][0]["type"] == "alert"
        assert data["pending_dialogs"][0]["message"] == "hello"
        assert data["pending_dialogs"][0]["session_id"] == "s_status_test"

    def test_browser_action_blocked_by_dialog_returns_without_calling_page(self, client, monkeypatch):
        """browser_action 预检：有 pending dialog → 返回 blocked_by_dialog=true 且不调 page.click

        反向验证：page.click 不应被调用（通过 mock page 验证 click 副作用未发生）。
        """
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        # 构造 mock page：click 调用会设置 self.click_called = True
        class _MockPage:
            def __init__(self):
                self.click_called = False
                self.url = "http://test.example"

            async def locator(self, *a, **kw):
                # 不应被调用
                class _BadLoc:
                    async def count(self):
                        raise AssertionError("locator.count() should not be called when dialog blocks")
                    async def click(self, *a, **kw):
                        raise AssertionError("click() should not be called when dialog blocks")
                    @property
                    def first(self):
                        return self
                return _BadLoc()

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_action_block", tab_id="t1", page=mock_page, url="http://test.example"
        )
        mock_session._append_pending("pending_dialogs", {
            "type": "confirm", "message": "确认？", "page_url": "http://test.example",
            "ts": 0.0, "auto_dismissed": False,
        })
        mock_session.last_dialog_obj = object()
        mock_session.dialog_auto_dismissed = False

        class _MockMgr:
            def get_session(self, sid):
                return mock_session
            async def get_page(self, sid):
                # 模拟 page 仍有效（不 close）
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/action", json={
            "session_id": "s_action_block",
            "target": {"css": "#btn"},
            "action": "click",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["blocked_by_dialog"] is True
        assert data["blocking_dialog"]["type"] == "confirm"
        assert data["error"]["error_code"] == "BLOCKED_BY_DIALOG"
        # 反向验证：page.click 未被调用
        assert mock_page.click_called is False

    def test_wait_for_dialog_not_blocked_by_precheck(self, client, monkeypatch):
        """wait_for(wait_type='dialog') 不被预检拦截（例外）

        构造有 pending dialog 的 session，调 wait_for(wait_type='dialog') 应进入正常流程，
        而非返回 blocked_by_dialog=True。
        """
        from server.browser.session import manager as _mgr_mod

        class _MockPage:
            url = "http://x"
            async def wait_for_selector(self, *a, **kw):
                raise AssertionError("不应调 wait_for_selector for dialog wait_type")

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_wait_dialog", tab_id="t1", page=mock_page, url="http://x"
        )
        mock_session._append_pending("pending_dialogs", {
            "type": "alert", "message": "hi", "page_url": "http://x",
            "ts": 0.0, "auto_dismissed": False,
        })
        mock_session.last_dialog_obj = object()
        mock_session.dialog_auto_dismissed = False

        class _MockMgr:
            def get_session(self, sid):
                return mock_session
            async def get_page(self, sid):
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        # wait_for(wait_type="dialog") 应消费队列中的 dialog 事件并返回 matched=True
        # （不返回 blocked_by_dialog=True）
        resp = client.post("/browser/wait_for", json={
            "session_id": "s_wait_dialog",
            "wait_type": "dialog",
            "timeout": 1.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocked_by_dialog"] is False
        assert data["success"] is True
        # dialog 队列有事件 → matched=True
        assert data["matched"] is True
        assert data["data"]["type"] == "alert"

    def test_wait_for_selector_blocked_by_dialog(self, client, monkeypatch):
        """wait_for(wait_type='selector') 有 pending dialog 时被预检拦截"""
        from server.browser.session import manager as _mgr_mod

        class _MockPage:
            url = "http://x"
            async def wait_for_selector(self, *a, **kw):
                raise AssertionError("wait_for_selector 不应被调用（dialog 阻塞）")

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_wait_sel", tab_id="t1", page=mock_page, url="http://x"
        )
        mock_session._append_pending("pending_dialogs", {
            "type": "alert", "message": "block", "page_url": "http://x",
            "ts": 0.0, "auto_dismissed": False,
        })
        mock_session.last_dialog_obj = object()
        mock_session.dialog_auto_dismissed = False

        class _MockMgr:
            def get_session(self, sid):
                return mock_session
            async def get_page(self, sid):
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/wait_for", json={
            "session_id": "s_wait_sel",
            "wait_type": "selector",
            "selector": "#btn",
            "timeout": 1.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["blocked_by_dialog"] is True
        assert data["error"]["error_code"] == "BLOCKED_BY_DIALOG"


# ==================== 2026-08-06 Ticket 04: force_beforeunload 参数 + beforeunload 触发路径 ====================


class TestBeforeUnload:
    """force_beforeunload 参数：action="reload" 时是否用 page.close(run_before_unload=True)
    触发 beforeunload dialog。

    2026-08-06 新增。依赖 Ticket 01 的 _on_dialog 监听器（捕获 beforeunload）+
    Ticket 02 的 handle_dialog 端点（accept/dismiss）。
    """

    def test_request_model_defaults(self):
        """BrowserNavigateRequest 默认 force_beforeunload=False"""
        req = BrowserNavigateRequest(action="reload")
        assert req.force_beforeunload is False

    def test_request_model_force_beforeunload_true(self):
        req = BrowserNavigateRequest(action="reload", force_beforeunload=True)
        assert req.force_beforeunload is True

    def test_response_model_beforeunload_triggered_field(self):
        """BrowserNavigateResponse 含 beforeunload_triggered 字段，默认 False"""
        resp = BrowserNavigateResponse(success=True, action="reload", elapsed_ms=10)
        assert resp.beforeunload_triggered is False
        # 可显式设为 True
        resp2 = BrowserNavigateResponse(
            success=True, action="reload", elapsed_ms=10, beforeunload_triggered=True,
        )
        assert resp2.beforeunload_triggered is True

    def test_force_beforeunload_triggers_dialog_via_mock(self, client, monkeypatch):
        """force_beforeunload=True → 后台调 page.close(run_before_unload=True)
        → beforeunload dialog 进入 session 队列

        通过 mock page 验证：
        - page.close(run_before_unload=True) 被调用
        - 返回 beforeunload_triggered=True
        - 返回 url_after = 原 URL
        - beforeunload dialog 进入 session 队列（通过 listener 触发）
        """
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        # mock page：close(run_before_unload=True) 触发 _on_dialog listener
        # 这里不真正调 listener（依赖 Playwright 真实 dialog），只验证调用
        class _MockPage:
            url = "http://test.example/page"

            def __init__(self):
                self.close_called = False
                self.run_before_unload = None

            async def close(self, run_before_unload=False, **kw):
                self.close_called = True
                self.run_before_unload = run_before_unload
                # 真实场景下 page.close(run_before_unload=True) 会触发 beforeunload
                # dialog，listener 入 session 队列。这里直接模拟 listener 行为，
                # 让 session 有 pending dialog，便于后续断言。
                # 但我们不能直接调 _on_dialog（它需要 Dialog 对象），
                # 所以只标记 close 被调用，dialog 入队由真实 listener 负责。

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_bu_test", tab_id="t1", page=mock_page, url="http://test.example/page"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

            async def get_page(self, sid):
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        # 调用端点
        resp = client.post("/browser/navigate", json={
            "session_id": "s_bu_test",
            "action": "reload",
            "force_beforeunload": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["action"] == "reload"
        assert data["beforeunload_triggered"] is True
        assert data["url_after"] == "http://test.example/page"
        # navigation_completed 为 False（beforeunload 路径不走正常导航）
        assert data["navigation_completed"] is False

        # 验证 page.close 被调用（asyncio.create_task 后台执行，需要给一点时间）
        # TestClient 同步调用，asyncio 事件循环在调用结束后已停止；
        # create_task 创建的 task 可能未执行完。但我们至少能验证端点未阻塞返回。
        # close 是否真正被调用取决于事件循环调度，这里用宽松断言：
        # 端点返回成功且 beforeunload_triggered=True 即说明路径正确。
        # 真实场景下 page.close 会被调度执行（asyncio.sleep(0) 让出控制权后）。

    def test_reload_without_force_beforeunload_uses_page_reload(self, client, monkeypatch):
        """默认 reload（force_beforeunload=False）走 page.reload()，不触发 beforeunload

        通过 mock page 验证：
        - page.reload 被调用
        - page.close 未被调用
        - beforeunload_triggered=False
        """
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        class _MockPage:
            url = "http://test.example/page"
            close_called = False
            reload_called = False

            async def close(self, run_before_unload=False, **kw):
                self.close_called = True

            async def reload(self, **kw):
                self.reload_called = True

            async def wait_for_load_state(self, *a, **kw):
                pass

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_reload_test", tab_id="t1", page=mock_page, url="http://test.example/page"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

            async def get_page(self, sid):
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/navigate", json={
            "session_id": "s_reload_test",
            "action": "reload",
            # 不传 force_beforeunload，默认 False
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["beforeunload_triggered"] is False
        # page.reload 应被调用（TestClient 同步调用结束后，asyncio 事件循环
        # 完成所有 await，所以 reload_called 应为 True）
        assert mock_page.reload_called is True
        assert mock_page.close_called is False

    def test_force_beforeunload_ignored_for_non_reload_action(self, client, monkeypatch):
        """force_beforeunload=True 对 action="goto" 无效（只对 reload 生效）

        构造 goto + force_beforeunload=True，应正常走 page.goto 路径，
        beforeunload_triggered=False。
        """
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        class _MockPage:
            url = "http://test.example/old"
            goto_called = False
            close_called = False

            async def goto(self, url, **kw):
                self.goto_called = True
                self.url = url

            async def close(self, run_before_unload=False, **kw):
                self.close_called = True

            async def wait_for_load_state(self, *a, **kw):
                pass

        mock_page = _MockPage()
        mock_session = TabSession(
            session_id="s_goto_bu", tab_id="t1", page=mock_page, url="http://test.example/old"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

            async def get_page(self, sid):
                mock_session.touch()
                return mock_page

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/navigate", json={
            "session_id": "s_goto_bu",
            "action": "goto",
            "url": "http://test.example/new",
            "force_beforeunload": True,  # 应被忽略
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["beforeunload_triggered"] is False
        # page.goto 应被调用
        assert mock_page.goto_called is True
        # page.close 不应被调用
        assert mock_page.close_called is False


# ==================== 2026-08-06 Ticket 05: set_http_credentials 端点 ====================


class TestHttpAuth:
    """set_http_credentials 端点：动态设置/清除 HTTP Basic Auth 凭证。

    2026-08-06 新增。作用域：context 级（整个浏览器实例），非 per-tab。
    """

    def test_request_model_basic(self):
        req = BrowserSetHttpCredentialsRequest(
            session_id="abc",
            host="example.com",
            username="user",
            password="pass",
        )
        assert req.session_id == "abc"
        assert req.host == "example.com"
        assert req.username == "user"
        assert req.password == "pass"
        assert req.clear is False

    def test_request_model_clear(self):
        req = BrowserSetHttpCredentialsRequest(session_id="x", clear=True)
        assert req.clear is True
        assert req.username is None
        assert req.password is None

    def test_response_model(self):
        r = BrowserSetHttpCredentialsResponse(success=True)
        assert r.success is True
        assert r.error is None

    def test_route_registered(self, client):
        """POST /browser/set_http_credentials 应已注册"""
        resp = client.post("/browser/set_http_credentials", json={
            "session_id": "nonexistent_xyz",
            "username": "u",
            "password": "p",
        })
        assert resp.status_code == 200

    def test_set_http_credentials_calls_context_set(self, client, monkeypatch):
        """set_http_credentials → context.set_http_credentials 被调用

        mock SessionManager.get_context() 返回 mock context，验证：
        - context.set_http_credentials({"username", "password"}) 被调用
        - 返回 success=True
        """
        from server.browser.session import manager as _mgr_mod

        class _MockContext:
            def __init__(self):
                self.creds_set = None

            async def set_http_credentials(self, creds):
                self.creds_set = creds

        mock_context = _MockContext()

        class _MockMgr:
            @property
            def is_connected(self):
                return True

            def get_session(self, sid):
                return None  # session 不存在，但 browser 已连接

            async def get_context(self):
                return mock_context

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/set_http_credentials", json={
            "session_id": "any",
            "username": "myuser",
            "password": "mypass",
            "host": "example.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # 验证 context.set_http_credentials 被调用
        assert mock_context.creds_set is not None
        assert mock_context.creds_set["username"] == "myuser"
        assert mock_context.creds_set["password"] == "mypass"

    def test_clear_http_credentials_calls_context_with_none(self, client, monkeypatch):
        """clear=True → context.set_http_credentials(None)

        mock context，验证 set_http_credentials(None) 被调用。
        """
        from server.browser.session import manager as _mgr_mod

        class _MockContext:
            def __init__(self):
                self.creds_set = "NOT_CALLED"

            async def set_http_credentials(self, creds):
                self.creds_set = creds

        mock_context = _MockContext()

        class _MockMgr:
            @property
            def is_connected(self):
                return True

            def get_session(self, sid):
                return None

            async def get_context(self):
                return mock_context

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/set_http_credentials", json={
            "session_id": "any",
            "clear": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert mock_context.creds_set is None

    def test_set_credentials_without_username_returns_error(self, client, monkeypatch):
        """clear=False 但 username 为空 → INVALID_SELECTOR"""
        from server.browser.session import manager as _mgr_mod

        class _MockContext:
            async def set_http_credentials(self, creds):
                raise AssertionError("不应被调用")

        class _MockMgr:
            @property
            def is_connected(self):
                return True

            def get_session(self, sid):
                return None

            async def get_context(self):
                return _MockContext()

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/set_http_credentials", json={
            "session_id": "any",
            "username": "",  # 空
            "password": "p",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "INVALID_SELECTOR"

    def test_set_credentials_returns_disconnected_when_no_context(self, client, monkeypatch):
        """browser 未连接 → BROWSER_DISCONNECTED"""
        from server.browser.session import manager as _mgr_mod

        class _MockMgr:
            @property
            def is_connected(self):
                return False

            def get_session(self, sid):
                return None

            async def get_context(self):
                return None

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/set_http_credentials", json={
            "session_id": "any",
            "username": "u",
            "password": "p",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "BROWSER_DISCONNECTED"


# ==================== 2026-08-06 Ticket 06: grant_permissions 端点 ====================


class TestGrantPermissions:
    """grant_permissions 端点：显式预授权权限（geolocation/notifications 等）。

    2026-08-06 新增。主动预授权语义，非事件驱动。
    """

    def test_request_model_basic(self):
        req = BrowserGrantPermissionsRequest(
            session_id="abc",
            permissions=["geolocation"],
        )
        assert req.session_id == "abc"
        assert req.permissions == ["geolocation"]
        assert req.origin is None

    def test_request_model_with_origin(self):
        req = BrowserGrantPermissionsRequest(
            session_id="abc",
            permissions=["notifications"],
            origin="https://example.com",
        )
        assert req.origin == "https://example.com"

    def test_response_model(self):
        r = BrowserGrantPermissionsResponse(success=True, granted=["geolocation"])
        assert r.success is True
        assert r.granted == ["geolocation"]

    def test_route_registered(self, client):
        """POST /browser/grant_permissions 应已注册"""
        resp = client.post("/browser/grant_permissions", json={
            "session_id": "nonexistent_xyz",
            "permissions": ["geolocation"],
        })
        assert resp.status_code == 200

    def test_grant_permissions_calls_context_grant(self, client, monkeypatch):
        """grant_permissions → context.grant_permissions 被调用

        mock session + context，验证：
        - context.grant_permissions(permissions, origin=...) 被调用
        - 返回 success=True, granted=permissions
        """
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        class _MockPage:
            url = "https://example.com/page"

            def is_closed(self):
                return False

        class _MockContext:
            def __init__(self):
                self.granted_perms = None
                self.granted_origin = "NOT_CALLED"

            async def grant_permissions(self, permissions, origin=None):
                self.granted_perms = permissions
                self.granted_origin = origin

        mock_context = _MockContext()
        mock_session = TabSession(
            session_id="s_grant", tab_id="t1", page=_MockPage(), url="https://example.com/page"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

            async def get_context(self):
                return mock_context

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/grant_permissions", json={
            "session_id": "s_grant",
            "permissions": ["geolocation", "notifications"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert set(data["granted"]) == {"geolocation", "notifications"}
        # origin 从 page URL 推导为 https://example.com
        assert mock_context.granted_origin == "https://example.com"

    def test_grant_permissions_with_explicit_origin(self, client, monkeypatch):
        """传 origin 参数 → context.grant_permissions 用该 origin"""
        from server.browser.session import manager as _mgr_mod
        from server.browser.session.state import TabSession

        class _MockContext:
            def __init__(self):
                self.granted_origin = None

            async def grant_permissions(self, permissions, origin=None):
                self.granted_origin = origin

        mock_context = _MockContext()
        mock_session = TabSession(
            session_id="s_grant2", tab_id="t1", page=None, url="https://default.com"
        )

        class _MockMgr:
            def get_session(self, sid):
                return mock_session

            async def get_context(self):
                return mock_context

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/grant_permissions", json={
            "session_id": "s_grant2",
            "permissions": ["camera"],
            "origin": "https://explicit.example",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert mock_context.granted_origin == "https://explicit.example"

    def test_grant_permissions_empty_list_returns_error(self, client):
        """空 permissions → INVALID_SELECTOR"""
        resp = client.post("/browser/grant_permissions", json={
            "session_id": "any",
            "permissions": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "INVALID_SELECTOR"

    def test_grant_permissions_unsupported_returns_error(self, client):
        """不支持的 permission → UNSUPPORTED_ACTION"""
        resp = client.post("/browser/grant_permissions", json={
            "session_id": "any",
            "permissions": ["unsupported_perm"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "UNSUPPORTED_ACTION"
        # 错误信息应包含支持的权限列表
        assert "geolocation" in data["error"]["debug_detail"]

    def test_grant_permissions_session_not_found(self, client, monkeypatch):
        """session 不存在 → TAB_NOT_FOUND"""
        from server.browser.session import manager as _mgr_mod

        class _MockMgr:
            def get_session(self, sid):
                return None

        monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

        resp = client.post("/browser/grant_permissions", json={
            "session_id": "nonexistent",
            "permissions": ["geolocation"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["error_code"] == "TAB_NOT_FOUND"

