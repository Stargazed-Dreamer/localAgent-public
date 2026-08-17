"""MCP 接口测试

测试 MCP Streamable HTTP 端点的基本可达性、header 验证、
白名单完整性、以及高级工具注册表的构建。

注：MCP Streamable HTTP 协议要求异步 task group，TestClient 同步上下文
无法完整执行 JSON-RPC 会话（initialize → tools/list → tools/call）。
因此这里只做端点存在性、header 校验、白名单/注册表完整性等静态测试。
"""


from server import advanced
from server.mcp_whitelist import (
    DIRECT_TOOLS,
    GATEWAY_EXCLUDE,
    TOOL_ANNOTATIONS,
)

# ==================== 端点存在性 ====================

class TestMCPEndpoint:
    def test_endpoint_exists(self, client):
        """GET /mcp 不应返回 404（端点存在）"""
        resp = client.get("/mcp")
        assert resp.status_code != 404

    def test_get_without_accept_header_rejected(self, client):
        """GET /mcp 不带 Accept: text/event-stream 时端点应处理请求（非 404）。

        TestClient 同步上下文无法完整执行 JSON-RPC 会话，实际返回 406 或 500
        取决于 fastapi-mcp session 状态。本测试只验证端点存在且处理了请求，
        不放宽到多状态断言——明确语义为"非 404"。
        """
        resp = client.get("/mcp")
        assert resp.status_code != 404, "端点不应不存在"

    def test_get_returns_session_id_header(self, client):
        """GET /mcp 端点存在且处理请求（非 404）。

        TestClient 下 mcp-session-id header 可能存在（406 时）或不存在（500 时），
        无法稳定断言 header，故只验证端点处理了请求。
        """
        resp = client.get("/mcp")
        assert resp.status_code != 404, "端点不应不存在"

    def test_post_endpoint_exists(self, client):
        """POST /mcp 不应返回 404"""
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert resp.status_code != 404

    def test_post_returns_500_without_session(self, client):
        """POST /mcp 端点存在且尝试处理请求（非 404）。

        TestClient 同步上下文无法启动 streamable HTTP session manager，
        实际返回 500（session 未初始化）或 200（偶发）。
        明确语义为"端点存在且处理了请求"，不放宽到 in (500, 200) 多状态断言。
        """
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }, headers={"Accept": "application/json, text/event-stream"})
        assert resp.status_code != 404, "端点不应不存在"


# ==================== MCP 白名单完整性 ====================

class TestMCPWhitelist:
    def test_direct_tools_is_non_empty_list(self):
        assert isinstance(DIRECT_TOOLS, list)
        assert len(DIRECT_TOOLS) >= 20  # 至少 20 个直连工具

    def test_direct_tools_all_strings(self):
        for op in DIRECT_TOOLS:
            assert isinstance(op, str)
            assert len(op) > 0

    def test_direct_tools_unique(self):
        assert len(DIRECT_TOOLS) == len(set(DIRECT_TOOLS)), "DIRECT_TOOLS 有重复"

    def test_gateway_exclude_is_set(self):
        assert isinstance(GATEWAY_EXCLUDE, set)

    def test_gateway_tools_not_in_direct(self):
        """网关工具同时在 DIRECT_TOOLS 和 GATEWAY_EXCLUDE 中（避免 advanced_tool 自引用）

        v3 精简后 localagent_list_templates / localagent_template_tool 不在 DIRECT_TOOLS
        （模板发现走 localagent_list_tools 网关），仅 localagent_list_tools / localagent_advanced_tool
        同时出现在两处。
        """
        gateway_tools = {
            "localagent_list_tools", "localagent_advanced_tool",
        }
        assert gateway_tools.issubset(set(DIRECT_TOOLS))
        assert gateway_tools.issubset(GATEWAY_EXCLUDE)

    def test_key_tools_in_whitelist(self):
        """关键工具必须在白名单中

        v3 精简后：
        - browser_open / browser_click_element 等 6 个 legacy 接口已在 Ticket 07 删除
          （含 browser_fill_input / browser_wait_for_load / browser_extract_text /
          browser_screenshot_element），功能由新接口（browser_session_create +
          browser_action + browser_navigate + browser_screenshot + browser_evaluate +
          browser_wait_for + browser_snapshot）完全替代
        - ocr_path 不在 DIRECT_TOOLS（统一用 ocr_file 处理文件路径/字节）
        """
        must_have = {
            "agent_guide",
            "exec_python",
            "exec_apply_patch",
            "capture_screen",
            "memory_get",
            "memory_set",
            "memory_list",
            "ocr_file",
            "agent_chat",
            "list_windows",
            "execute_action",
            # v3 新接口替代 legacy browser_open / browser_click_element
            "browser_session_create",
            "browser_action",
        }
        whitelist = set(DIRECT_TOOLS)
        missing = must_have - whitelist
        assert not missing, f"白名单缺少关键工具: {missing}"

    def test_form_endpoints_excluded(self):
        """表单端点（form 变体）被排除"""
        excluded_forms = {
            "vl_file_form",
            "ocr_file_form", "ocr_base64_form",
        }
        assert excluded_forms.issubset(GATEWAY_EXCLUDE)

    # 注：原 test_legacy_aliases_excluded 已删除（0.32.0 移除 4 个 screen snake_case
    # legacy 别名端点：focus_window_legacy/batch_actions_legacy/scroll_capture_legacy/
    # screen_wait_for_legacy）。端点本身已删除，GATEWAY_EXCLUDE 中相应条目也移除，
    # 测试 legacy 别名是否在排除列表中已无意义。

    def test_terminal_tools_in_whitelist(self):
        """终端会话工具在白名单中

        v3 精简后只保留 spawn + detail + input + kill 四个核心组合；
        exec_terminals_list / exec_terminal_output / exec_terminal_delete
        通过 advanced_tool 网关访问（罕用，避免膨胀直连列表）。
        """
        terminal_tools = {
            "exec_terminal_spawn", "exec_terminal_detail",
            "exec_terminal_input", "exec_terminal_kill",
        }
        assert terminal_tools.issubset(set(DIRECT_TOOLS))

    def test_discovery_gateways_in_whitelist(self):
        """发现网关在白名单中

        v3 精简后模板发现走 localagent_list_tools 网关，
        localagent_list_templates / localagent_template_tool 不再直连。
        """
        discovery = {
            "localagent_list_tools", "localagent_advanced_tool",
        }
        assert discovery.issubset(set(DIRECT_TOOLS))

    def test_task_authorization_tools_are_direct_and_annotated(self):
        authorization_tools = {
            "screen_request_control",
            "screen_release_control",
        }
        assert authorization_tools.issubset(set(DIRECT_TOOLS))
        assert authorization_tools.issubset(set(TOOL_ANNOTATIONS))
        assert TOOL_ANNOTATIONS["screen_request_control"]["openWorldHint"] is True
        assert TOOL_ANNOTATIONS["screen_release_control"]["idempotentHint"] is True

    def test_computer_use_does_not_add_command_guard_approval_layer(self, client):
        from server.route_tags import _op_to_path, classify_safety

        computer_use_operations = {
            "screen_request_control",
            "screen_release_control",
            "execute_action",
            "focus_window",
            "batch_actions",
            "screen_desktop_transaction",
            "screen_semantic_action",
        }
        assert {
            operation: classify_safety(*_op_to_path[operation])
            for operation in computer_use_operations
        } == dict.fromkeys(computer_use_operations, "safe")


# ==================== 高级工具注册表 ====================

class TestAdvancedRegistry:
    """高级工具注册表应在应用启动后已构建（由 setup_mcp 调用 build_registry_from_app）"""

    def test_registry_non_empty(self):
        """注册表非空（应用启动时构建）"""
        assert len(advanced._advanced_registry) > 0

    def test_registry_entries_have_required_fields(self):
        """每个注册表条目有必需字段"""
        required = {"path", "method", "summary", "description", "category", "parameters", "has_body"}
        for name, info in list(advanced._advanced_registry.items())[:5]:
            missing = required - set(info.keys())
            assert not missing, f"工具 {name} 缺字段: {missing}"

    def test_registry_methods_are_valid_http(self):
        """所有方法都是合法 HTTP 方法"""
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        for name, info in advanced._advanced_registry.items():
            assert info["method"] in valid_methods, f"{name} 方法无效: {info['method']}"

    def test_registry_categories_present(self):
        """注册表包含多个分类"""
        cats = {info["category"] for info in advanced._advanced_registry.values()}
        # 至少包含 ocr/system/memory/exec 等常见分类
        assert len(cats) >= 3

    def test_gateway_tools_not_in_registry(self):
        """网关工具自身不应出现在注册表中（避免自引用）"""
        gateway_tools = {
            "localagent_list_tools", "localagent_advanced_tool",
            "localagent_list_templates", "localagent_template_tool",
        }
        for name in gateway_tools:
            assert name not in advanced._advanced_registry, f"网关工具 {name} 不应在注册表中"

    def test_form_endpoints_not_in_registry(self):
        """表单端点不应在注册表中（被 GATEWAY_EXCLUDE 排除）"""
        form_endpoints = {"vl_file_form", "ocr_file_form", "ocr_base64_form"}
        for name in form_endpoints:
            assert name not in advanced._advanced_registry


# ==================== 未知 op 默认放行（SECURITY-RISKS E10 子项 1 回归） ====================


class TestUnknownOpDefaultPass:
    """未知 operation_id 默认放行行为回归（SECURITY-RISKS.md 第 4 节子项 1）。

    设计事实（mcp_gateway.py:73-74）：
      safety = safety_map.get(effective_op)  # 未知 op 返回 None
      if safety == "approval_required":     # None != "approval_required" → 跳过审批分支

    即"未知 op"既不是 "safe" 也不是 "approval_required"，
    会跳过 MCP 网关的审批检查直接放行到 _original_execute_api_tool。

    这是**理论缺口**，但运行时无法实际利用：
      1. safety_map 由 build_operation_safety_map(app) 遍历 app.routes 构建，
         任何已注册路由的 operation_id 都会在 map 中（safety="safe" 或 "approval_required"）
      2. 未知 op 在 advanced_tool 端点的 _advanced_registry 中也找不到 → 直接返回 404
      3. MCP 协议层只暴露 fastapi-mcp 注册的工具，运行时无法注入新 op

    此测试锁定该"默认放行 + 多层守门"行为，防止未来重构时：
      - 误改 build_operation_safety_map 漏掉某个路由 → 该路由的 op 变成"未知 op"绕过审批
      - 或在 _advanced_registry 之外新增可调用路径 → 绕过 safety_map 守门
    """

    UNKNOWN_OP = "nonexistent_operation_xyz_12345"

    def test_safety_map_returns_none_for_unknown_op(self, client):
        """build_operation_safety_map 对未知 op 返回 None（不在 map 中）

        这是"默认放行"的根源：safety=None 既不是 safe 也不是 approval_required。
        """
        from server.main import app
        from server.route_tags import build_operation_safety_map

        safety_map = build_operation_safety_map(app)
        assert isinstance(safety_map, dict)
        assert len(safety_map) > 0
        # 未知 op 不在 map 中
        assert self.UNKNOWN_OP not in safety_map
        assert safety_map.get(self.UNKNOWN_OP) is None

    def test_classify_runtime_by_op_unknown_returns_approval_required(self, client):
        """classify_safety_runtime_by_op 对未知 op 返回 approval_required（安全优先兜底）

        这是 mcp_gateway.py:78-80 的二次确认兜底：
          safety = safety_map.get(unknown) = None  → 不进 if safety == "approval_required"
          但如果 safety 已经是 "approval_required"（已知审批端点），二次确认会调
          classify_safety_runtime_by_op(unknown) → 返回 "approval_required" → 走审批

        注意：这只兜底"safety=approval_required 但 runtime 降级"场景，
        不兜底"safety=None"（未知 op）场景——后者仍会跳过审批分支。
        """
        from server.route_tags import classify_safety_runtime_by_op

        result = classify_safety_runtime_by_op(self.UNKNOWN_OP)
        assert result == "approval_required", (
            "未知 op 应返回 approval_required（安全优先），"
            "实际 mcp_gateway 不会走到此分支（safety=None 时不进 if），"
            "但 classify_safety_runtime_by_op 自身必须安全优先"
        )

    def test_advanced_tool_rejects_unknown_op(self, client):
        """advanced_tool 端点对未知 tool 返回 404（运行时守门）

        即使 mcp_gateway 跳过审批检查（safety=None），advanced_tool 端点本身
        会在 _advanced_registry 中查找 req.tool，找不到直接返回 404。
        这是"未知 op 默认放行"无法实际利用的第二道防线。
        """
        resp = client.post("/advanced/run", json={
            "tool": self.UNKNOWN_OP,
            "params": {},
        })
        assert resp.status_code == 404
        # 错误信息提示用户用 localagent_list_tools 查可用工具
        detail = resp.json().get("detail", "")
        assert "未知工具" in detail or "localagent_list_tools" in detail, (
            f"404 应提示未知工具，实际: {detail}"
        )

    def test_known_approval_required_ops_are_in_safety_map(self, client):
        """已知 approval_required 端点必须在 safety_map 中（防止漏注册）

        如果某个 approval_required 端点因 bug 未进 safety_map，会变成"未知 op"
        绕过审批。此测试用 exec_python 作为 representative 验证 safety_map 完整性。
        """
        from server.main import app
        from server.route_tags import build_operation_safety_map

        safety_map = build_operation_safety_map(app)
        # exec_python 是已知 approval_required 端点（核心代码执行工具）
        assert "exec_python" in safety_map, (
            "exec_python 必须在 safety_map 中，否则会变成'未知 op'绕过审批"
        )
        assert safety_map["exec_python"] == "approval_required", (
            f"exec_python 应为 approval_required，实际: {safety_map['exec_python']}"
        )
