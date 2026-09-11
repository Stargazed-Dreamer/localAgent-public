"""操作模板网关 (templates.py) 测试

覆盖：
- TEMPLATES 完整性：每个模板都有 name/category/description/prompt_hint/parameters/template
- _TEMPLATE_INDEX：name → template 映射
- _render_template：占位符替换、backend_url/cdp_url 自动注入、bool/None 处理
- GET /templates/list：列表 + category 过滤
- POST /templates/run：未知模板/缺必填/执行成功/执行失败
"""


from server import templates
from server.templates import (
    _TEMPLATE_INDEX,
    TEMPLATES,
    ListTemplatesResponse,
    TemplateExecRequest,
    TemplateExecResponse,
    _render_template,
)

# ==================== TEMPLATES 完整性 ====================

class TestTemplatesIntegrity:
    def test_templates_is_non_empty_list(self):
        assert isinstance(TEMPLATES, list)
        assert len(TEMPLATES) >= 10  # 至少 10 个模板

    def test_each_template_has_required_fields(self):
        required_fields = {"name", "category", "description", "prompt_hint", "parameters", "template"}
        for t in TEMPLATES:
            missing = required_fields - set(t.keys())
            assert not missing, f"模板 {t.get('name')} 缺字段: {missing}"

    def test_each_template_name_is_unique(self):
        names = [t["name"] for t in TEMPLATES]
        assert len(names) == len(set(names)), f"模板名重复: {names}"

    def test_each_template_has_parameters_list(self):
        for t in TEMPLATES:
            assert isinstance(t["parameters"], list), f"{t['name']} parameters 不是 list"

    def test_each_parameter_has_required_fields(self):
        required = {"name", "type", "required", "default", "description"}
        for t in TEMPLATES:
            for p in t["parameters"]:
                missing = required - set(p.keys())
                assert not missing, f"模板 {t['name']} 参数 {p.get('name')} 缺字段: {missing}"

    def test_each_template_has_non_empty_code(self):
        for t in TEMPLATES:
            assert isinstance(t["template"], str)
            assert len(t["template"]) > 10, f"{t['name']} 代码太短"

    def test_known_categories_present(self):
        """已知分类都应有模板"""
        cats = {t["category"] for t in TEMPLATES}
        expected = {"screen", "browser", "game", "file", "system"}
        assert expected.issubset(cats), f"缺少分类: {expected - cats}"

    def test_specific_templates_exist(self):
        """关键模板存在"""
        names = set(_TEMPLATE_INDEX.keys())
        must_have = {
            "screenshot_and_ocr",
            "screenshot_inline_for_llm",
            "browser_navigate_and_wait",
            "browser_extract_article",
            "game_wait_and_click",
            "game_auto_click_loop",
            "file_search_content",
            "file_list_recent",
            "system_window_focus",
        }
        missing = must_have - names
        assert not missing, f"缺少关键模板: {missing}"

    def test_template_index_matches_templates(self):
        """_TEMPLATE_INDEX 与 TEMPLATES 一一对应"""
        assert len(_TEMPLATE_INDEX) == len(TEMPLATES)
        for t in TEMPLATES:
            assert t["name"] in _TEMPLATE_INDEX
            assert _TEMPLATE_INDEX[t["name"]] is t


# ==================== Pydantic 模型 ====================

class TestPydanticModels:
    """请求/响应模型的默认值语义（实例化回读断言已清，schema 由 FastAPI 校验兜底）。"""

    def test_template_exec_request_default_params(self):
        req = TemplateExecRequest(template="foo")
        assert req.params == {}

    def test_template_exec_response_optional_fields(self):
        resp = TemplateExecResponse(
            success=True, template="foo", elapsed_ms=100, message="ok",
        )
        assert resp.exec_id is None
        assert resp.stdout is None
        assert resp.stderr is None

# ==================== _render_template ====================

class TestRenderTemplate:
    def test_replaces_string_placeholder(self):
        template = 'name = "{name}"'
        rendered = _render_template(template, {"name": "hello"})
        assert rendered == 'name = "hello"'

    def test_replaces_int_placeholder(self):
        template = "count = {count}"
        rendered = _render_template(template, {"count": 42})
        assert rendered == "count = 42"

    def test_replaces_float_placeholder(self):
        template = "wait = {wait}"
        rendered = _render_template(template, {"wait": 1.5})
        assert rendered == "wait = 1.5"

    def test_replaces_bool_placeholder(self):
        template = "flag = {flag}"
        rendered = _render_template(template, {"flag": True})
        assert "true" in rendered

    def test_replaces_none_with_empty(self):
        template = "x = '{x}'"
        rendered = _render_template(template, {"x": None})
        assert rendered == "x = ''"

    def test_injects_backend_url(self, monkeypatch):
        """包含 {backend_url} 时自动注入"""
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )
        template = "API = '{backend_url}'"
        rendered = _render_template(template, {})
        assert "127.0.0.1" in rendered
        assert "8766" in rendered

    def test_injects_cdp_url(self, monkeypatch):
        """包含 {cdp_url} 时自动注入"""
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )
        template = "cdp = '{cdp_url}'"
        rendered = _render_template(template, {})
        assert "9222" in rendered

    def test_no_system_placeholders_no_injection(self, monkeypatch):
        """无 backend_url/cdp_url 时不注入"""
        called = {"server": False, "chrome": False}

        def fake_server():
            called["server"] = True
            return {"host": "127.0.0.1", "port": 8766}

        def fake_chrome():
            called["chrome"] = True
            return {"debug_port": 9222}

        monkeypatch.setattr("server.config.get_server_config", fake_server)
        monkeypatch.setattr("server.config.get_chrome_config", fake_chrome)

        _render_template("plain code without placeholders", {})
        assert called["server"] is False
        assert called["chrome"] is False

    def test_unknown_placeholder_left_untouched(self):
        """未提供的占位符保持原样"""
        template = "x = {unknown}"
        rendered = _render_template(template, {})
        assert "{unknown}" in rendered

    def test_real_template_renders_without_error(self, monkeypatch):
        """所有真实模板用默认参数都能渲染"""
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )
        for t in TEMPLATES:
            params = {p["name"]: p["default"] for p in t["parameters"]}
            rendered = _render_template(t["template"], params)
            assert isinstance(rendered, str)
            assert len(rendered) > 0, f"{t['name']} 渲染结果为空"


# ==================== GET /templates/list ====================

class TestListTemplatesEndpoint:
    def test_returns_all_when_no_filter(self, client):
        resp = client.get("/templates/list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == len(TEMPLATES)
        cats = data["categories"]
        # 至少 5 个分类
        assert len(cats) >= 5

    def test_filter_by_category(self, client):
        """category 过滤"""
        # 先确定一个存在的分类
        target_cat = TEMPLATES[0]["category"]
        resp = client.get("/templates/list", params={"category": target_cat})
        assert resp.status_code == 200
        data = resp.json()
        # 只返回 target_cat 分类的模板
        for entry in data["categories"].get(target_cat, []):
            assert entry["category"] == target_cat

    def test_filter_nonexistent_category_returns_empty(self, client):
        resp = client.get("/templates/list", params={"category": "nonexistent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["categories"] == {}

    def test_parameters_have_defaults(self, client):
        """参数的 default 字段被序列化"""
        resp = client.get("/templates/list")
        data = resp.json()
        # 找一个有参数的模板
        found = False
        for _cat, tmpls in data["categories"].items():
            for t in tmpls:
                if t["parameters"]:
                    for p in t["parameters"]:
                        assert "default" in p
                        assert "required" in p
                        assert "type" in p
                    found = True
        assert found, "没有任何模板有参数"

    def test_response_schema_matches_pydantic(self, client):
        """响应符合 ListTemplatesResponse schema"""
        resp = client.get("/templates/list")
        data = resp.json()
        # 序列化测试
        parsed = ListTemplatesResponse(**data)
        assert parsed.total == data["total"]


# ==================== POST /templates/run ====================

class TestTemplateRunEndpoint:
    def test_unknown_template_returns_404(self, client):
        """未知模板返回 404"""
        resp = client.post(
            "/templates/run",
            json={"template": "nonexistent_template"},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "nonexistent_template" in detail
        assert "localagent_list_templates" in detail

    def test_missing_required_param_returns_422(self, client):
        """缺必填参数返回 422"""
        # game_wait_and_click 需要 wait_text/click_x/click_y
        resp = client.post(
            "/templates/run",
            json={"template": "game_wait_and_click", "params": {}},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "必填参数" in detail or "缺少" in detail

    def test_empty_required_value_returns_422(self, client):
        """必填参数为空字符串也算缺失"""
        resp = client.post(
            "/templates/run",
            json={
                "template": "game_wait_and_click",
                "params": {"wait_text": "", "click_x": 0, "click_y": 0},
            },
        )
        assert resp.status_code == 422

    def test_run_executes_code(self, client, monkeypatch):
        """成功渲染并调用 exec_python"""
        # Mock exec_python
        captured = {}

        class FakeResult:
            success = True
            stdout = "execution output"
            stderr = ""
            stdout_truncated = False
            exec_id = "exec_test123"

        async def fake_exec_python(req):
            captured["code"] = req.code
            captured["timeout"] = req.timeout
            return FakeResult()

        # Mock ExecRequest
        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        monkeypatch.setattr(templates, "exec_python", fake_exec_python, raising=False)
        # 直接 patch server.exec 模块的 exec_python 和 ExecRequest
        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)

        # Mock config 以渲染 backend_url
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        resp = client.post(
            "/templates/run",
            json={
                "template": "screenshot_and_ocr",
                "params": {"mode": "fullscreen"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["template"] == "screenshot_and_ocr"
        assert data["stdout"] == "execution output"
        assert data["exec_id"] == "exec_test123"
        assert data["elapsed_ms"] >= 0
        assert "代码" not in data["message"]  # 不应泄露代码
        # 验证渲染后的代码被传入
        assert "8766" in captured["code"]

    def test_run_handles_exec_failure(self, client, monkeypatch):
        """exec_python 返回失败时正确包装"""
        class FakeResult:
            success = False
            stdout = ""
            stderr = "syntax error"
            stdout_truncated = False
            exec_id = None

        async def fake_exec_python(req):
            return FakeResult()

        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        resp = client.post(
            "/templates/run",
            json={"template": "screenshot_and_ocr"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["stderr"] == "syntax error"

    def test_run_handles_exception(self, client, monkeypatch):
        """exec_python 抛异常时返回失败响应"""
        async def fake_exec_python(req):
            raise RuntimeError("subprocess crashed")

        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        resp = client.post(
            "/templates/run",
            json={"template": "screenshot_and_ocr"},
        )
        # 异常被捕获，返回 200 + success=False
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "执行异常" in data["message"]

    def test_run_fills_defaults_for_optional_params(self, client, monkeypatch):
        """可选参数未提供时使用默认值"""
        captured = {}

        class FakeResult:
            success = True
            stdout = ""
            stderr = ""
            stdout_truncated = False
            exec_id = None

        async def fake_exec_python(req):
            captured["code"] = req.code
            return FakeResult()

        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        # screenshot_and_ocr 有 3 个参数都是可选，全部不传
        resp = client.post(
            "/templates/run",
            json={"template": "screenshot_and_ocr"},
        )
        assert resp.status_code == 200
        # 默认 mode=fullscreen 应该被渲染到代码里
        assert "fullscreen" in captured["code"]

    def test_run_with_user_provided_params_overrides_defaults(self, client, monkeypatch):
        """用户提供的参数覆盖默认值"""
        captured = {}

        class FakeResult:
            success = True
            stdout = ""
            stderr = ""
            stdout_truncated = False
            exec_id = None

        async def fake_exec_python(req):
            captured["code"] = req.code
            return FakeResult()

        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        resp = client.post(
            "/templates/run",
            json={
                "template": "screenshot_and_ocr",
                "params": {"mode": "window", "window_title": "TestWin"},
            },
        )
        assert resp.status_code == 200
        assert "window" in captured["code"]
        assert "TestWin" in captured["code"]

    def test_run_with_extra_params_not_in_template(self, client, monkeypatch):
        """多余参数被忽略（不影响渲染）"""
        class FakeResult:
            success = True
            stdout = ""
            stderr = ""
            stdout_truncated = False
            exec_id = None

        async def fake_exec_python(req):
            return FakeResult()

        class FakeExecRequest:
            def __init__(self, code=None, timeout=None):
                self.code = code
                self.timeout = timeout

        import server.exec as exec_module
        monkeypatch.setattr(exec_module, "exec_python", fake_exec_python)
        monkeypatch.setattr(exec_module, "ExecRequest", FakeExecRequest)
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766},
        )
        monkeypatch.setattr(
            "server.config.get_chrome_config",
            lambda: {"debug_port": 9222},
        )

        resp = client.post(
            "/templates/run",
            json={
                "template": "screenshot_and_ocr",
                "params": {"unknown_param": "value"},
            },
        )
        # 多余参数被忽略，渲染正常
        assert resp.status_code == 200
        assert resp.json()["success"] is True
