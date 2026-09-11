"""高级工具网关 (advanced.py) 测试

覆盖：
- _categorize：按 operation_id 前缀分类
- build_registry_from_app：从 FastAPI app.openapi() 构建注册表
- _advanced_registry：收录逻辑（只收录 excluded_ops 中的操作）
- GET /advanced/tools：列表 + category 过滤
- POST /advanced/run：路由到下游端点（path/query/body 参数分离、HTTP 错误处理）
- ADR-0023 DANGEROUS_TOOLS：危险工具走 command_guard 审批，普通工具默认放行
"""


import asyncio

import pytest

from server import advanced
from server.advanced import (
    _CATEGORY_PREFIXES,
    DANGEROUS_TOOLS,
    AdvancedToolRequest,
    AdvancedToolResponse,
    _categorize,
    _check_dangerous_tool_approval,
    _is_dangerous_tool,
    build_registry_from_app,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """每个测试前后清空注册表，避免相互影响"""
    saved = dict(advanced._advanced_registry)
    advanced._advanced_registry.clear()
    yield
    advanced._advanced_registry.clear()
    advanced._advanced_registry.update(saved)


# ==================== _categorize ====================

class TestCategorize:
    def test_ocr_prefix(self):
        assert _categorize("ocr_set_keep_models") == "ocr"

    def test_vl_prefix(self):
        assert _categorize("vl_understand") == "ocr"

    def test_vision_prefix(self):
        assert _categorize("vision_parse") == "vision"

    def test_mindforge_prefix(self):
        assert _categorize("mindforge_preload") == "mindforge"

    def test_exec_prefix(self):
        assert _categorize("exec_output") == "exec"

    def test_memory_prefix(self):
        assert _categorize("memory_set") == "memory"

    def test_browser_prefix(self):
        assert _categorize("browser_open") == "browser"

    def test_screen_prefix(self):
        assert _categorize("screen_capture") == "screen"

    def test_agent_prefix(self):
        assert _categorize("agent_chat") == "agent"

    def test_docviewer_prefix(self):
        assert _categorize("docviewer_read") == "docviewer"

    def test_system_prefix(self):
        assert _categorize("system_status") == "system"

    def test_keep_awake_prefix(self):
        assert _categorize("keep_awake_on") == "system"

    def test_set_keep_awake_specific(self):
        """set_keep_awake 是单独列出的条目，匹配 system"""
        assert _categorize("set_keep_awake") == "system"

    def test_shutdown_prefix(self):
        assert _categorize("shutdown_now") == "system"

    def test_mcp_stats_specific(self):
        """mcp_stats 是单独条目"""
        assert _categorize("mcp_stats") == "system"

    def test_todos_prefix(self):
        assert _categorize("todos_list") == "todos"

    def test_wip_prefix(self):
        assert _categorize("wip_list") == "todos"

    def test_unknown_returns_other(self):
        assert _categorize("unknown_operation") == "other"

    def test_empty_string_returns_other(self):
        assert _categorize("") == "other"

    def test_prefix_matching_is_case_sensitive(self):
        """前缀匹配区分大小写"""
        assert _categorize("OCR_upper") == "other"

    def test_all_categories_have_prefix_entries(self):
        """检查 _CATEGORY_PREFIXES 中每一项都能正确分类一个示例 operation_id"""
        for prefix, cat in _CATEGORY_PREFIXES:
            sample = f"{prefix}sample"
            assert _categorize(sample) == cat, f"prefix={prefix} 应分类为 {cat}"


# ==================== build_registry_from_app ====================

class TestBuildRegistry:
    def test_empty_app_returns_zero(self):
        """空 app（无路由）返回 0"""
        class FakeApp:
            def openapi(self):
                return {"paths": {}}

        count = build_registry_from_app(FakeApp(), excluded_ops=[])
        assert count == 0
        assert advanced._advanced_registry == {}

    def test_only_collects_excluded_ops(self):
        """只收录 excluded_ops 列出的 operation_id"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/foo": {
                            "get": {
                                "operationId": "foo_get",
                                "summary": "Get foo",
                            },
                            "post": {
                                "operationId": "foo_create",
                                "summary": "Create foo",
                            },
                        },
                        "/bar": {
                            "get": {
                                "operationId": "bar_get",
                                "summary": "Get bar",
                            },
                        },
                    }
                }

        # 只收录 foo_get 和 bar_get
        count = build_registry_from_app(
            FakeApp(), excluded_ops=["foo_get", "bar_get"]
        )
        assert count == 2
        assert "foo_get" in advanced._advanced_registry
        assert "bar_get" in advanced._advanced_registry
        # foo_create 不在 excluded_ops 中，不应被收录
        assert "foo_create" not in advanced._advanced_registry

    def test_skips_mcp_internal_endpoints(self):
        """mcp_connection / mcp_messages 即使在 excluded_ops 中也跳过"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/mcp": {
                            "get": {"operationId": "mcp_connection"},
                            "post": {"operationId": "mcp_messages"},
                        },
                        "/other": {
                            "get": {"operationId": "other_get"},
                        },
                    }
                }

        count = build_registry_from_app(
            FakeApp(),
            excluded_ops=["mcp_connection", "mcp_messages", "other_get"],
        )
        assert count == 1
        assert "mcp_connection" not in advanced._advanced_registry
        assert "mcp_messages" not in advanced._advanced_registry
        assert "other_get" in advanced._advanced_registry

    def test_records_path_method_summary(self):
        """记录 path/method/summary 字段"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/users/{uid}": {
                            "delete": {
                                "operationId": "users_delete",
                                "summary": "Delete a user",
                                "description": "Deletes user by id",
                            },
                        },
                    }
                }

        build_registry_from_app(FakeApp(), excluded_ops=["users_delete"])
        info = advanced._advanced_registry["users_delete"]
        assert info["path"] == "/users/{uid}"
        assert info["method"] == "DELETE"
        assert info["summary"] == "Delete a user"
        assert info["description"] == "Deletes user by id"

    def test_summary_falls_back_to_description_first_line(self):
        """无 summary 时用 description 第一行"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/x": {
                            "post": {
                                "operationId": "x_post",
                                "description": "First line.\nSecond line.",
                            },
                        },
                    }
                }

        build_registry_from_app(FakeApp(), excluded_ops=["x_post"])
        assert advanced._advanced_registry["x_post"]["summary"] == "First line."

    def test_records_parameters(self):
        """记录 path/query 参数"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/items/{id}": {
                            "get": {
                                "operationId": "items_get",
                                "parameters": [
                                    {
                                        "name": "id",
                                        "in": "path",
                                        "required": True,
                                        "description": "Item ID",
                                    },
                                    {
                                        "name": "verbose",
                                        "in": "query",
                                        "required": False,
                                        "description": "Verbose output",
                                    },
                                ],
                            },
                        },
                    }
                }

        build_registry_from_app(FakeApp(), excluded_ops=["items_get"])
        params = advanced._advanced_registry["items_get"]["parameters"]
        assert len(params) == 2
        assert params[0]["name"] == "id"
        assert params[0]["in"] == "path"
        assert params[0]["required"] is True
        assert params[1]["name"] == "verbose"
        assert params[1]["in"] == "query"
        assert params[1]["required"] is False

    def test_has_body_flag(self):
        """有 requestBody 时 has_body=True"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/create": {
                            "post": {
                                "operationId": "create_item",
                                "requestBody": {"content": {}},
                            },
                        },
                        "/list": {
                            "get": {
                                "operationId": "list_items",
                            },
                        },
                    }
                }

        build_registry_from_app(
            FakeApp(), excluded_ops=["create_item", "list_items"]
        )
        assert advanced._advanced_registry["create_item"]["has_body"] is True
        assert advanced._advanced_registry["list_items"]["has_body"] is False

    def test_category_assigned(self):
        """根据 operation_id 前缀分配 category"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/ocr/keep": {
                            "post": {"operationId": "ocr_set_keep_models"},
                        },
                        "/foo": {
                            "get": {"operationId": "unknown_thing"},
                        },
                    }
                }

        build_registry_from_app(
            FakeApp(),
            excluded_ops=["ocr_set_keep_models", "unknown_thing"],
        )
        assert advanced._advanced_registry["ocr_set_keep_models"]["category"] == "ocr"
        assert advanced._advanced_registry["unknown_thing"]["category"] == "other"

    def test_openapi_exception_returns_zero(self):
        """openapi() 抛异常时返回 0，不崩溃"""
        class FakeApp:
            def openapi(self):
                raise RuntimeError("schema error")

        count = build_registry_from_app(FakeApp(), excluded_ops=["foo"])
        assert count == 0
        assert advanced._advanced_registry == {}

    def test_skips_non_http_methods(self):
        """跳过非 HTTP 方法（如 options/trace）"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/x": {
                            "get": {"operationId": "x_get"},
                            "options": {"operationId": "x_options"},
                            "trace": {"operationId": "x_trace"},
                            "head": {"operationId": "x_head"},
                        },
                    }
                }

        # 把所有都放进 excluded_ops，但 options/trace 不会被收录（head 也应跳过）
        build_registry_from_app(
            FakeApp(),
            excluded_ops=["x_get", "x_options", "x_trace", "x_head"],
        )
        # x_get 一定收录；options/trace 不在白名单方法中
        assert "x_get" in advanced._advanced_registry
        # options 和 trace 不应被收录
        assert "x_options" not in advanced._advanced_registry
        assert "x_trace" not in advanced._advanced_registry

    def test_operation_without_id_skipped(self):
        """无 operationId 的端点跳过"""
        class FakeApp:
            def openapi(self):
                return {
                    "paths": {
                        "/x": {
                            "get": {"summary": "no op id"},
                        },
                    }
                }

        count = build_registry_from_app(FakeApp(), excluded_ops=[])
        assert count == 0

    def test_overwrites_previous_registry(self):
        """重复调用会清空旧注册表"""
        class FakeApp1:
            def openapi(self):
                return {
                    "paths": {
                        "/a": {"get": {"operationId": "a_get"}},
                    }
                }

        class FakeApp2:
            def openapi(self):
                return {
                    "paths": {
                        "/b": {"get": {"operationId": "b_get"}},
                    }
                }

        build_registry_from_app(FakeApp1(), excluded_ops=["a_get"])
        assert "a_get" in advanced._advanced_registry
        build_registry_from_app(FakeApp2(), excluded_ops=["b_get"])
        assert "a_get" not in advanced._advanced_registry
        assert "b_get" in advanced._advanced_registry


# ==================== Pydantic 模型 ====================

class TestPydanticModels:
    """请求/响应模型的默认值语义（实例化回读断言已清，schema 由 FastAPI 校验兜底）。"""

    def test_advanced_tool_request_default_params(self):
        req = AdvancedToolRequest(tool="foo")
        assert req.params == {}

    def test_advanced_tool_response_optional_fields(self):
        resp = AdvancedToolResponse(
            success=True, tool="foo", status_code=200,
        )
        assert resp.result is None
        assert resp.error is None

# ==================== GET /advanced/tools ====================

class TestListAdvancedToolsEndpoint:
    def test_returns_all_when_no_filter(self, client):
        """无 category 过滤时返回全部"""
        # 先注入几个测试工具
        advanced._advanced_registry["ocr_test1"] = {
            "path": "/ocr/test1", "method": "POST",
            "summary": "Test 1", "description": "",
            "category": "ocr", "parameters": [], "has_body": False,
        }
        advanced._advanced_registry["vision_test2"] = {
            "path": "/vision/test2", "method": "GET",
            "summary": "Test 2", "description": "",
            "category": "vision", "parameters": [], "has_body": False,
        }

        resp = client.get("/advanced/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert "ocr" in data["categories"]
        assert "vision" in data["categories"]

    def test_filter_by_category(self, client):
        """category 过滤只返回匹配分类"""
        advanced._advanced_registry["ocr_test1"] = {
            "path": "/ocr/test1", "method": "POST",
            "summary": "T1", "description": "",
            "category": "ocr", "parameters": [], "has_body": False,
        }
        advanced._advanced_registry["vision_test2"] = {
            "path": "/vision/test2", "method": "GET",
            "summary": "T2", "description": "",
            "category": "vision", "parameters": [], "has_body": False,
        }

        resp = client.get("/advanced/tools", params={"category": "ocr"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert "ocr" in data["categories"]
        assert "vision" not in data["categories"]

    def test_filter_nonexistent_category_returns_empty(self, client):
        """category 不存在时返回空"""
        advanced._advanced_registry["ocr_test1"] = {
            "path": "/ocr/test1", "method": "POST",
            "summary": "T1", "description": "",
            "category": "ocr", "parameters": [], "has_body": False,
        }
        resp = client.get("/advanced/tools", params={"category": "nonexistent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["categories"] == {}

    def test_empty_registry_returns_zero(self, client):
        """空注册表返回 total=0"""
        resp = client.get("/advanced/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["categories"] == {}

    def test_parameters_serialized(self, client):
        """parameters 正确序列化"""
        advanced._advanced_registry["users_get"] = {
            "path": "/users/{uid}", "method": "GET",
            "summary": "Get user", "description": "",
            "category": "other",
            "parameters": [
                {"name": "uid", "in": "path", "required": True, "description": "User ID"},
            ],
            "has_body": False,
        }
        resp = client.get("/advanced/tools?verbose=true")
        data = resp.json()
        params = data["categories"]["other"][0]["parameters"]
        assert params[0]["name"] == "uid"
        assert params[0]["location"] == "path"
        assert params[0]["required"] is True


# ==================== POST /advanced/run ====================

class TestAdvancedRunEndpoint:
    def test_unknown_tool_returns_404(self, client):
        """未知工具返回 404"""
        resp = client.post("/advanced/run", json={"tool": "nonexistent_tool"})
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "nonexistent_tool" in detail
        assert "localagent_list_tools" in detail

    def test_run_get_with_query_params(self, client, monkeypatch):
        """GET 请求 + query 参数路由"""
        advanced._advanced_registry["status_get"] = {
            "path": "/status", "method": "GET",
            "summary": "Status", "description": "",
            "category": "system",
            "parameters": [
                {"name": "verbose", "in": "query", "required": False, "description": ""},
            ],
            "has_body": False,
        }

        captured = {}

        class FakeResp:
            status_code = 200
            text = '{"ok": true}'

            def json(self):
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                captured["url"] = url
                captured["params"] = params
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "status_get", "params": {"verbose": True}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200
        assert data["result"] == {"ok": True}
        assert data["error"] is None
        # 检查路由参数
        assert "/status" in captured["url"]
        assert captured["params"] == {"verbose": True}

    def test_run_post_with_body(self, client, monkeypatch):
        """POST 请求 + body 参数路由"""
        advanced._advanced_registry["items_create"] = {
            "path": "/items", "method": "POST",
            "summary": "Create", "description": "",
            "category": "other",
            "parameters": [],
            "has_body": True,
        }

        captured = {}

        class FakeResp:
            status_code = 201
            text = '{"id": 1}'

            def json(self):
                return {"id": 1}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, params=None, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "items_create", "params": {"name": "foo"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 201
        assert data["result"] == {"id": 1}
        assert captured["json"] == {"name": "foo"}

    def test_run_with_path_params(self, client, monkeypatch):
        """路径参数被正确替换"""
        advanced._advanced_registry["user_delete"] = {
            "path": "/users/{uid}", "method": "DELETE",
            "summary": "Delete", "description": "",
            "category": "other",
            "parameters": [
                {"name": "uid", "in": "path", "required": True, "description": ""},
            ],
            "has_body": False,
        }

        captured = {}

        class FakeResp:
            status_code = 204
            text = ""

            def json(self):
                raise ValueError("no json")

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def delete(self, url, params=None, json=None):
                captured["url"] = url
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "user_delete", "params": {"uid": 42}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 204
        # 路径参数被替换
        assert "/users/42" in captured["url"]

    def test_run_returns_error_for_4xx(self, client, monkeypatch):
        """下游返回 4xx 时 success=False"""
        advanced._advanced_registry["bad_get"] = {
            "path": "/bad", "method": "GET",
            "summary": "Bad", "description": "",
            "category": "other", "parameters": [], "has_body": False,
        }

        class FakeResp:
            status_code = 404
            text = '{"detail": "not found"}'

            def json(self):
                return {"detail": "not found"}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "bad_get"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status_code"] == 404
        assert data["result"] is None
        assert "404" in data["error"]

    def test_run_returns_502_on_request_error(self, client, monkeypatch):
        """httpx.RequestError 时返回 502"""
        advanced._advanced_registry["fail_get"] = {
            "path": "/fail", "method": "GET",
            "summary": "Fail", "description": "",
            "category": "other", "parameters": [], "has_body": False,
        }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                raise advanced.httpx.RequestError("connection refused")

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "fail_get"})
        assert resp.status_code == 502
        assert "内部调用失败" in resp.json()["detail"]

    def test_run_with_put_method(self, client, monkeypatch):
        """PUT 请求路由"""
        advanced._advanced_registry["user_update"] = {
            "path": "/users/{uid}", "method": "PUT",
            "summary": "Update", "description": "",
            "category": "other",
            "parameters": [
                {"name": "uid", "in": "path", "required": True, "description": ""},
            ],
            "has_body": True,
        }

        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True}

            @property
            def text(self):
                return '{"ok": true}'

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def put(self, url, params=None, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "user_update", "params": {"uid": 5, "name": "bob"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "/users/5" in captured["url"]
        assert captured["json"] == {"name": "bob"}

    def test_run_with_patch_method(self, client, monkeypatch):
        """PATCH 请求路由"""
        advanced._advanced_registry["user_patch"] = {
            "path": "/users/{uid}", "method": "PATCH",
            "summary": "Patch", "description": "",
            "category": "other",
            "parameters": [
                {"name": "uid", "in": "path", "required": True, "description": ""},
            ],
            "has_body": True,
        }

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True}

            @property
            def text(self):
                return '{"ok": true}'

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def patch(self, url, params=None, json=None):
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "user_patch", "params": {"uid": 5}},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_run_no_body_remaining_params_go_to_query(self, client, monkeypatch):
        """无 body 的端点：剩余参数放入 query"""
        advanced._advanced_registry["list_items"] = {
            "path": "/items", "method": "GET",
            "summary": "List", "description": "",
            "category": "other", "parameters": [], "has_body": False,
        }

        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"items": []}

            @property
            def text(self):
                return '{"items": []}'

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                captured["params"] = params
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "list_items", "params": {"limit": 10, "offset": 5}},
        )
        assert resp.status_code == 200
        assert captured["params"] == {"limit": 10, "offset": 5}

    def test_run_text_response_when_json_fails(self, client, monkeypatch):
        """下游返回非 JSON 时 result 为 {"text": ...}"""
        advanced._advanced_registry["text_get"] = {
            "path": "/text", "method": "GET",
            "summary": "Text", "description": "",
            "category": "other", "parameters": [], "has_body": False,
        }

        class FakeResp:
            status_code = 200
            text = "plain text response"

            def json(self):
                raise ValueError("not json")

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "text_get"})
        data = resp.json()
        assert data["success"] is True
        assert data["result"] == {"text": "plain text response"}

    def test_run_no_params_field(self, client, monkeypatch):
        """请求不带 params 字段也能工作"""
        advanced._advanced_registry["simple_get"] = {
            "path": "/simple", "method": "GET",
            "summary": "Simple", "description": "",
            "category": "other", "parameters": [], "has_body": False,
        }

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True}

            @property
            def text(self):
                return '{"ok": true}'

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "simple_get"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ==================== ADR-0023 DANGEROUS_TOOLS 权限检查 ====================

class TestDangerousToolsSet:
    """DANGEROUS_TOOLS 集合本身的内容与 _is_dangerous_tool 判定。"""

    def test_set_not_empty(self):
        """集合非空（Anti-Cheat：至少 3 个端点）"""
        assert len(DANGEROUS_TOOLS) >= 3

    def test_set_contains_spec_required_tools(self):
        """spec 明确要求的端点必须在集合中"""
        # spec Code-2 段明确点名：apikey_set_key/memory_delete/auto_shutdown_trigger
        # 注：apikey_set_key 在源码中实际为 apikey_create_key/update_key/delete_key 三个
        assert "memory_delete" in DANGEROUS_TOOLS
        assert "auto_shutdown_trigger" in DANGEROUS_TOOLS
        # apikey 系列（create/update/delete 任一即可，spec 用 set_key 泛指写操作）
        assert any(op.startswith("apikey_") for op in DANGEROUS_TOOLS)

    def test_is_dangerous_tool_true_for_members(self):
        """集合内成员返回 True"""
        for op in DANGEROUS_TOOLS:
            assert _is_dangerous_tool(op) is True, f"{op} 应为危险工具"

    def test_is_dangerous_tool_false_for_non_members(self):
        """集合外成员返回 False"""
        assert _is_dangerous_tool("ocr_status") is False
        assert _is_dangerous_tool("memory_get") is False
        assert _is_dangerous_tool("memory_set") is False  # memory_set 已移出 DANGEROUS_TOOLS
        assert _is_dangerous_tool("nonexistent_tool") is False
        assert _is_dangerous_tool("") is False

    def test_set_members_are_strings(self):
        """集合内所有成员都是 str 类型（防止误写入其他类型）"""
        for op in DANGEROUS_TOOLS:
            assert isinstance(op, str)


class TestCheckDangerousToolApproval:
    """_check_dangerous_tool_approval 单元测试：mock run_gui_dialog 验证三种 decision。

    用 asyncio.run() 调 async 函数（项目无 pytest-asyncio 依赖，与 test_a1/a4 等保持一致）。
    """

    @pytest.fixture
    def mock_run_gui_dialog(self, monkeypatch):
        """mock server.command_guard.run_gui_dialog，返回预设结果。

        用法：先调 set_result({"decision": "approve"}) 设定返回值，再调被测函数。
        """
        calls = []
        state = {"result": {"decision": "deny", "feedback": ""}, "raise": None}

        async def fake_run_gui_dialog(payload, approval_id=""):
            calls.append({"payload": payload, "approval_id": approval_id})
            if state["raise"] is not None:
                raise state["raise"]
            return state["result"]

        # _check_dangerous_tool_approval 内部 from server.command_guard import run_gui_dialog
        # 必须 patch 源模块的属性，import 后才能生效
        import server.command_guard as cg
        monkeypatch.setattr(cg, "run_gui_dialog", fake_run_gui_dialog)
        return {"calls": calls, "state": state}

    def test_user_approves_returns_true(self, mock_run_gui_dialog):
        """用户批准 → (True, "")"""
        mock_run_gui_dialog["state"]["result"] = {"decision": "approve", "feedback": "ok"}
        approved, reason = asyncio.run(_check_dangerous_tool_approval("memory_delete", {"key": "k1"}))
        assert approved is True
        assert reason == ""

    def test_user_denies_returns_false(self, mock_run_gui_dialog):
        """用户拒绝 → (False, 含拒绝原因)"""
        mock_run_gui_dialog["state"]["result"] = {"decision": "deny", "feedback": "no"}
        approved, reason = asyncio.run(_check_dangerous_tool_approval("auto_shutdown_trigger", {}))
        assert approved is False
        assert "deny" in reason

    def test_user_timeout_returns_false(self, mock_run_gui_dialog):
        """GUI 超时（decision=timeout）→ (False, 含 timeout)"""
        mock_run_gui_dialog["state"]["result"] = {"decision": "timeout", "feedback": ""}
        approved, reason = asyncio.run(_check_dangerous_tool_approval("apikey_delete_key", {"key_id": "x"}))
        assert approved is False
        assert "timeout" in reason

    def test_gui_timeout_exception_returns_false(self, mock_run_gui_dialog):
        """GUI 子进程超时抛 TimeoutError → (False, 含审批流程失败)"""
        mock_run_gui_dialog["state"]["raise"] = TimeoutError("server 200s 未响应")
        approved, reason = asyncio.run(_check_dangerous_tool_approval("memory_delete", {"key": "k"}))
        assert approved is False
        assert "审批流程失败" in reason

    def test_gui_runtime_error_returns_false(self, mock_run_gui_dialog):
        """GUI 子进程崩溃抛 RuntimeError → (False, 含审批流程失败)"""
        mock_run_gui_dialog["state"]["raise"] = RuntimeError("子进程退出码 1")
        approved, reason = asyncio.run(_check_dangerous_tool_approval("memory_delete", {"key": "k"}))
        assert approved is False
        assert "审批流程失败" in reason

    def test_payload_contains_operation_id_and_reason(self, mock_run_gui_dialog):
        """审批 payload 含 operation_id 和 guard_reason，便于 GUI 展示"""
        mock_run_gui_dialog["state"]["result"] = {"decision": "approve"}
        asyncio.run(_check_dangerous_tool_approval("memory_delete", {"key": "secret"}))
        call = mock_run_gui_dialog["calls"][0]
        assert call["payload"]["type"] == "advanced_tool"
        assert call["payload"]["operation_id"] == "memory_delete"
        assert "memory_delete" in call["payload"]["guard_reason"]
        assert "ADR-0023" in call["payload"]["guard_reason"]
        # approval_id 前缀符合约定（用于面板路由）
        assert call["approval_id"].startswith("advanced_approval_")

    def test_long_params_truncated_in_payload(self, mock_run_gui_dialog):
        """大 params 截断到 500 字符 + (truncated)，避免撑爆弹窗"""
        mock_run_gui_dialog["state"]["result"] = {"decision": "approve"}
        big_params = {"data": "x" * 2000}
        asyncio.run(_check_dangerous_tool_approval("memory_delete", big_params))
        preview = mock_run_gui_dialog["calls"][0]["payload"]["params_preview"]
        assert "(truncated)" in preview
        assert len(preview) <= 600  # 500 + "(truncated)" 后缀


class TestAdvancedRunDangerousToolsEndpoint:
    """POST /advanced/run 端点对 DANGEROUS_TOOLS 的拦截/放行行为。"""

    def _register_tool(self, op_id, method="POST", path="/dangerous", has_body=True):
        """在 _advanced_registry 中注入测试工具"""
        advanced._advanced_registry[op_id] = {
            "path": path, "method": method,
            "summary": f"Test {op_id}", "description": "",
            "category": "other", "parameters": [], "has_body": has_body,
        }

    def test_dangerous_tool_user_approves_then_executes(self, client, monkeypatch):
        """危险工具 + 用户批准 → 继续执行下游端点"""
        # 声明 key 为 path 参数，确保 advanced_tool 正确替换 {key}
        advanced._advanced_registry["memory_delete"] = {
            "path": "/memory/{key}", "method": "DELETE",
            "summary": "Delete memory", "description": "",
            "category": "memory",
            "parameters": [{"name": "key", "in": "path", "required": True, "description": ""}],
            "has_body": False,
        }

        # mock 审批返回批准
        async def fake_check(op, params):
            return True, ""
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", fake_check)

        # mock HTTP 调用
        captured = {}

        class FakeResp:
            status_code = 200
            text = '{"ok": true}'
            def json(self):
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def delete(self, url, params=None):
                captured["url"] = url
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "memory_delete", "params": {"key": "k1"}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200
        assert data["result"] == {"ok": True}
        # 验证确实调用了下游端点（path 参数被替换）
        assert "/memory/k1" in captured["url"]

    def test_dangerous_tool_user_denies_returns_403(self, client, monkeypatch):
        """危险工具 + 用户拒绝 → 返回 403，不下发到下游端点"""
        self._register_tool("memory_delete", method="DELETE", path="/memory/{key}", has_body=False)

        async def fake_check(op, params):
            return False, "用户拒绝审批 (decision=deny)"
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", fake_check)

        # mock HTTP 客户端，若被调用则测试失败
        class ShouldNotBeCalled:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def delete(self, url, params=None):
                raise AssertionError("危险工具被拒绝后不应下发到下游端点")
        monkeypatch.setattr(advanced.httpx, "AsyncClient", ShouldNotBeCalled)

        resp = client.post("/advanced/run", json={"tool": "memory_delete", "params": {"key": "k1"}})
        assert resp.status_code == 200  # 网关本身 200，但 success=False
        data = resp.json()
        assert data["success"] is False
        assert data["status_code"] == 403
        assert data["result"] is None
        assert "拒绝" in data["error"]
        assert data["tool"] == "memory_delete"

    def test_dangerous_tool_approval_failure_returns_403(self, client, monkeypatch):
        """危险工具 + 审批流程异常（如 GUI 超时）→ 返回 403"""
        self._register_tool("auto_shutdown_trigger", method="POST", path="/auto-shutdown/trigger")

        async def fake_check(op, params):
            return False, "审批流程失败: GUI 子进程超时"
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", fake_check)

        class ShouldNotBeCalled:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, params=None, json=None):
                raise AssertionError("审批失败时不应下发到下游端点")
        monkeypatch.setattr(advanced.httpx, "AsyncClient", ShouldNotBeCalled)

        resp = client.post("/advanced/run", json={"tool": "auto_shutdown_trigger", "params": {"dry_run": True}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status_code"] == 403
        assert "审批流程失败" in data["error"]

    def test_non_dangerous_tool_bypasses_approval(self, client, monkeypatch):
        """普通工具（非 DANGEROUS_TOOLS）→ 直接放行，不调审批"""
        self._register_tool("status_get", method="GET", path="/status", has_body=False)

        # 若审批函数被调用则测试失败
        async def should_not_be_called(op, params):
            raise AssertionError(f"普通工具不应触发审批，但调用了 _check_dangerous_tool_approval({op!r})")
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", should_not_be_called)

        class FakeResp:
            status_code = 200
            text = '{"ok": true}'
            def json(self):
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, params=None):
                return FakeResp()
        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post("/advanced/run", json={"tool": "status_get"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_memory_set_bypasses_approval(self, client, monkeypatch):
        """memory_set 已移出 DANGEROUS_TOOLS → 直接放行，不调审批。

        决策理由：写记忆是 agent 日常操作（记忆系统设计就是让 agent 读写），
        每次写都弹窗审批会打断工作流；覆盖敏感数据的风险靠 key 命名空间
        + 审计日志防，不靠网关审批。
        """
        # 注册 memory_set 工具（path 参数 key + JSON body）
        advanced._advanced_registry["memory_set"] = {
            "path": "/memory/{key}", "method": "POST",
            "summary": "Set memory", "description": "",
            "category": "memory",
            "parameters": [{"name": "key", "in": "path", "required": True, "description": ""}],
            "has_body": True,
        }

        # 若审批函数被调用则测试失败
        async def should_not_be_called(op, params):
            raise AssertionError(
                f"memory_set 不应触发审批，但调用了 _check_dangerous_tool_approval({op!r})"
            )
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", should_not_be_called)

        # mock HTTP 调用，记录下游 URL 和 body
        captured = {}

        class FakeResp:
            status_code = 200
            text = '{"ok": true}'

            def json(self):
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, params=None, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "memory_set", "params": {"key": "k1", "value": "v1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200
        assert data["result"] == {"ok": True}
        # 验证确实调用了下游端点（path 参数被替换，body 被传递）
        assert "/memory/k1" in captured["url"]
        assert captured["json"] == {"value": "v1"}

    def test_unknown_tool_still_returns_404(self, client, monkeypatch):
        """未知工具（不在注册表）→ 404，不触发审批检查"""
        async def should_not_be_called(op, params):
            raise AssertionError("未知工具不应触发审批")
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", should_not_be_called)

        resp = client.post("/advanced/run", json={"tool": "nonexistent_dangerous_tool"})
        assert resp.status_code == 404
        assert "nonexistent_dangerous_tool" in resp.json()["detail"]

    def test_dangerous_tool_passes_params_to_approval(self, client, monkeypatch):
        """危险工具调用时，把完整 params 传给审批函数（用于 GUI 展示）"""
        self._register_tool("apikey_delete_key", method="DELETE", path="/apikey/keys/{key_id}", has_body=False)

        captured_approval = {}

        async def fake_check(op, params):
            captured_approval["op"] = op
            captured_approval["params"] = params
            return True, ""
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", fake_check)

        class FakeResp:
            status_code = 200
            text = '{"ok": true}'
            def json(self):
                return {"ok": True}

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def delete(self, url, params=None):
                return FakeResp()
        monkeypatch.setattr(advanced.httpx, "AsyncClient", FakeAsyncClient)

        resp = client.post(
            "/advanced/run",
            json={"tool": "apikey_delete_key", "params": {"key_id": "abc"}},
        )
        assert resp.status_code == 200
        # 验证审批函数收到了 params
        assert captured_approval["op"] == "apikey_delete_key"
        assert captured_approval["params"] == {"key_id": "abc"}

    def test_all_dangerous_tools_trigger_approval(self, client, monkeypatch):
        """遍历 DANGEROUS_TOOLS 集合，每个都触发审批（无遗漏）"""
        # 为每个危险工具注册一个 fake 端点
        for op in DANGEROUS_TOOLS:
            advanced._advanced_registry[op] = {
                "path": f"/fake/{op}", "method": "POST",
                "summary": f"Fake {op}", "description": "",
                "category": "other", "parameters": [], "has_body": True,
            }

        called_ops = []

        async def fake_check(op, params):
            called_ops.append(op)
            return False, "用户拒绝审批 (decision=deny)"
        monkeypatch.setattr(advanced, "_check_dangerous_tool_approval", fake_check)

        # 不需要 mock HTTP 客户端，因为审批拒绝后不会调下游
        for op in DANGEROUS_TOOLS:
            resp = client.post("/advanced/run", json={"tool": op})
            assert resp.status_code == 200, f"{op} 应返回 200"
            data = resp.json()
            assert data["success"] is False, f"{op} 应被审批拒绝"
            assert data["status_code"] == 403, f"{op} 应返回 403"

        # 验证每个危险工具都触发了审批
        assert set(called_ops) == DANGEROUS_TOOLS

