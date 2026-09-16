"""MCP 网关：挂载 FastApiMCP + ImageContent 补丁 + 高级工具注册表"""

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("localagent.mcp_gateway")

# 响应大小保护：单个 TextContent 超阈值时截断 + 强制上报用户
MAX_MCP_RESPONSE_CHARS = 50000   # 单个 TextContent 硬上限
MCP_TRUNCATE_TAIL_CHARS = 2000   # 截断时保留尾部字符数


def _approval_body(args) -> bytes:
    """计算审批指纹用的 body：剥离 _approval_token 后紧凑 JSON 序列化。

    签发（user_review_for_llm_deny 路径）与验证（token 重试 check_approval）
    必须用同一 body，否则指纹不匹配 → 人审批准后 token 无效（审批死循环）。
    旧 bug：验证固定用 b""，签发用完整 arguments 序列化（2026-08-25 修复对齐）。
    模块级函数以便测试直接导入（tests/test_mcp_gateway_approval_fingerprint.py）。
    """
    if isinstance(args, dict):
        filtered = {k: v for k, v in args.items() if k != "_approval_token"}
        return json.dumps(filtered, ensure_ascii=False).encode("utf-8")
    return b""


def _content_text(content: object) -> str:
    """读取 MCP 内容块的 text 字段。

    mcp 类型 stub 不全：ImageContent / EmbeddedResource 存在但缺 text 属性声明，
    pyright 会把字面量名 getattr 当成员访问校验。参数收窄为 object 即不再误报。
    运行时这些对象总是 fastapi-mcp 的 TextContent（带 text）。
    """
    text = getattr(content, "text", "")
    return text if isinstance(text, str) else ""


def setup_mcp(app):
    """挂载 MCP 工具接口，应用 ImageContent 补丁，构建高级工具注册表"""
    try:
        from fastapi_mcp import FastApiMCP

        from server.advanced import build_registry_from_app as _build_advanced_registry
        from server.mcp_whitelist import DIRECT_TOOLS as _mcp_include
        from server.mcp_whitelist import GATEWAY_EXCLUDE as _mcp_gateway_exclude

        _all_operation_ids = {
            info.get("operationId")
            for methods in app.openapi().get("paths", {}).values()
            for method, info in methods.items()
            if method in ("get", "post", "put", "delete", "patch") and info.get("operationId")
        }
        _mcp_gateway_ops = sorted(
            op for op in _all_operation_ids
            if op not in set(_mcp_include) and op not in _mcp_gateway_exclude
        )

        mcp = FastApiMCP(
            app,
            name="LocalAgent MCP",
            description="LocalAgent Computer Use 工具集 - 截图/窗口/键鼠/视觉AI/记忆",
            include_operations=_mcp_include,
        )

        # ===== fastapi-mcp 补丁：让内联截图工具返回 MCP ImageContent =====
        # fastapi-mcp 0.4.0 的 _execute_api_tool 永远把响应包成 TextContent，
        # 多模态 LLM 无法直接"看"到图片。这里包装一层：检测响应 JSON 中的
        # mcp_image_block=True 标记，把 image 字段转为 ImageContent 返回。
        # 多模态模型（Claude/GPT-4o/Gemini）能直接看到截图，
        # 纯文本模型仍能从 TextContent 摘要中获取元信息。
        # 注：原 call_tool 闭包通过 self._execute_api_tool 动态查找，实例属性替换后自动生效。
        from fastapi_mcp.server import types as _mcp_types

        from server.user_message import get_supplement_text

        _original_execute_api_tool = mcp._execute_api_tool

        async def _patched_execute_api_tool(client, tool_name, arguments, operation_map, http_request_info=None):
            """包装 fastapi-mcp 工具执行：审批检查 + ImageContent 转换 + 用户补充指令。"""
            # MCP 工具调用是内部函数调用，不经过 HTTP 中间件，需在此检查 safety
            try:
                from server.http_guard import check_approval
                from server.route_tags import build_operation_safety_map
                # FastApiMCP 未声明 _operation_safety_map，动态属性用 Any 承载（getattr/setattr 字面量名仍会被校验）
                _mcp_dynamic: Any = mcp
                if getattr(_mcp_dynamic, "_operation_safety_map", None) is None:
                    _mcp_dynamic._operation_safety_map = build_operation_safety_map(app)
                safety_map = _mcp_dynamic._operation_safety_map

                # 确定"有效 operation_id"用于审批检查：
                # - 直接调用 MCP 工具：用 tool_name 本身
                # - 通过 localagent_advanced_tool 网关调用：用 arguments.tool（目标 operation_id），
                #   审批绑定到目标 operation 而非网关本身，避免"一次网关审批解锁所有端点"
                effective_op = tool_name
                if tool_name == "localagent_advanced_tool" and isinstance(arguments, dict):
                    target_op = arguments.get("tool", "")
                    if target_op:
                        effective_op = target_op

                safety = safety_map.get(effective_op)
                if safety == "approval_required":
                    # 二次确认：按当前 approval_level 过滤（用户在 GUI 改 level 后立即生效，
                    # 无需重建 _operation_safety_map）。safety_map 始终按 strict 完整清单构建，
                    # 若当前 level 为 moderate/loose/none，对应端点在此降级为 safe 放行。
                    from server.route_tags import classify_safety_runtime_by_op
                    runtime_safety = classify_safety_runtime_by_op(effective_op)
                    if runtime_safety == "approval_required":
                        # token 指纹统一用"剥离 _approval_token 后序列化的 arguments"：
                        # 签发（user_review_for_llm_deny）与验证（check_approval）必须用同一
                        # body，否则指纹必然不匹配 → 人审批准后 token 永远无效（审批死循环）。
                        token = arguments.get("_approval_token", "") if isinstance(arguments, dict) else ""

                        approval_body = _approval_body(arguments)
                        virtual_path = f"/mcp/tool/{effective_op}"
                        if token:
                            # 带 token 重试：验证 token，不跑 LLM 审查
                            approval_id = check_approval("POST", virtual_path, approval_body, token)
                            from server.approval_review import log_approval_detailed
                            log_approval_detailed({
                                "event": "mcp_gateway_token_retry",
                                "operation_id": effective_op, "method": "POST",
                                "path": virtual_path,
                                "token_present": True,
                                "token_valid": approval_id is None,
                                "body_sha16": __import__("hashlib").sha256(approval_body).hexdigest()[:16],
                                "note": "check_approval 用与签发一致的 arguments 指纹验证 token（2026-08-25 修复）",
                            })
                            if approval_id is not None:
                                return [
                                    _mcp_types.TextContent(
                                        type="text",
                                        text=json.dumps({
                                            "error": "approval_required",
                                            "approval_id": approval_id,
                                            "message": f"工具 {effective_op} 的 approval_token 无效或已过期，请重新获取。",
                                            "tool_name": effective_op,
                                        }, ensure_ascii=False),
                                    )
                                ]
                            if isinstance(arguments, dict):
                                arguments = {k: v for k, v in arguments.items() if k != "_approval_token"}
                        else:
                            # 首次请求：三层递进审批（静态规则 → LLM → 人审）
                            from server.approval_review import log_approval_detailed as _log_detailed
                            from server.approval_review import try_auto_approve, user_review_for_llm_deny
                            _log_detailed({
                                "event": "mcp_gateway_first_request",
                                "operation_id": effective_op, "method": "POST",
                                "path": virtual_path,
                                "arguments_preview": json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments),
                                "token_present": False,
                            })
                            review = await asyncio.to_thread(
                                try_auto_approve,
                                effective_op,
                                arguments if isinstance(arguments, dict) else {},
                                "POST", virtual_path,
                            )
                            proceed = review["auto_approved"]
                            # 静态 block 或 LLM 未放行 → 直接弹人审，用户批准则继续执行工具
                            user_reviewed = False
                            user_feedback = ""
                            token_from_user_review = ""
                            if not proceed and review.get("layer1_static") in ("block", "pass"):
                                user_reviewed = True
                                block_decision = "static_block" if review.get("layer1_static") == "block" else review.get("layer2_llm", "deny")
                                # 与 token 重试验证用同一 body（_approval_body），
                                # 保证签发/验证指纹一致
                                args_body = _approval_body(arguments)
                                user_result = await user_review_for_llm_deny(
                                    effective_op,
                                    arguments if isinstance(arguments, dict) else {},
                                    "POST", virtual_path, args_body, review.get("reason", ""),
                                    block_decision,
                                )
                                proceed = user_result["approved"]
                                user_feedback = user_result.get("feedback", "")
                                token_from_user_review = user_result.get("approval_token", "")
                                # 事实记录：user_review_for_llm_deny 在用户批准时会签发一次性 approval_token，
                                # 但 mcp_gateway 此处未把 token 返回给 agent（token 被丢弃）。
                                # 治本缓解：approval_review._approved_cache 已在用户批准时缓存 code 指纹
                                # （key=operation_id:code_sha256_16，TTL=token_ttl_seconds），下次相同代码
                                # 调 try_auto_approve 时命中缓存直接 auto_approved=True，不进此分支，agent
                                # 无感知复用审批结果，不会重复弹窗。token 不返回仅影响"不同代码"场景，
                                # 但不同代码本就应重新审（指纹不同），故丢弃 token 无实际危害。
                                _log_detailed({
                                    "event": "mcp_gateway_user_review_result",
                                    "operation_id": effective_op, "path": virtual_path,
                                    "user_approved": proceed,
                                    "token_issued_by_user_review": bool(token_from_user_review),
                                    "token_consumed_by_caller": False,  # token 未返回给 agent（事实）
                                    "token_discarded": True,           # token 被丢弃（事实）
                                    "mitigated_by_cache": True,         # 已由 _approved_cache 治本缓解
                                    "note": "token 被丢弃，但 _approved_cache 已在用户批准时缓存 code 指纹，下次相同代码命中缓存自动放行，agent 不会重复走审批",
                                })
                            if not proceed:
                                # 已弹过人审被拒绝：直接返回拒绝信息 + feedback，不再创建新 approval_id
                                # 治本：避免 agent 收到 "approval_required" 后重复申请，同时回传用户 feedback
                                if user_reviewed:
                                    _log_detailed({
                                        "event": "mcp_gateway_user_denied",
                                        "operation_id": effective_op, "path": virtual_path,
                                        "user_feedback": user_feedback,
                                        "note": "用户拒绝，agent 收到 user_denied 后可能调 command_guard_request_approval 写 agent_reason 重试",
                                    })
                                    return [
                                        _mcp_types.TextContent(
                                            type="text",
                                            text=json.dumps({
                                                "error": "user_denied",
                                                "message": "用户已拒绝此操作。请尊重用户决定，不要重复申请审批；如需继续请说明理由或改用更安全方案。",
                                                "user_feedback": user_feedback,
                                                "tool_name": effective_op,
                                            }, ensure_ascii=False),
                                        )
                                    ]
                                # 未弹过人审（layer1=skipped，非审查端点），走标准 approval_required 流程
                                # 签发指纹必须与 token 重试验证（L115 _approval_body(arguments)）同源：
                                # 此前签发用 b"" 而验证用全参数 JSON，指纹必然不匹配 → 用户第一次
                                # 批准签发的 token 永远无效，需批准第二次（2026-09-13 code review 2-1）
                                approval_id = check_approval("POST", virtual_path, _approval_body(arguments), "")
                                _log_detailed({
                                    "event": "mcp_gateway_return_approval_required",
                                    "operation_id": effective_op, "path": virtual_path,
                                    "approval_id": approval_id,
                                    "note": "返回 approval_required 给 agent，agent 将调 command_guard_request_approval 写 agent_reason 申请审批",
                                })
                                if approval_id is not None:
                                    return [
                                        _mcp_types.TextContent(
                                            type="text",
                                            text=json.dumps({
                                                "error": "approval_required",
                                                "approval_id": approval_id,
                                                "message": f"工具 {effective_op} 需要 user 审批。请调用 command_guard_request_approval 获取批准后，在 arguments 中添加 _approval_token 重试。",
                                                "tool_name": effective_op,
                                            }, ensure_ascii=False),
                                        )
                                    ]
            except Exception as e:
                logger.error(f"MCP 审批检查失败（fail-closed，阻止调用）: {e}")
                return [
                    _mcp_types.TextContent(
                        type="text",
                        text=json.dumps({
                            "error": "guard_check_failed",
                            "message": f"安全检查异常，已阻止调用。请重试或联系用户。错误: {e}",
                            "tool_name": tool_name,
                        }, ensure_ascii=False),
                    )
                ]

            result = await _original_execute_api_tool(
                client, tool_name, arguments, operation_map, http_request_info
            )
            # 紧凑 JSON 重序列化：fastapi_mcp 内部硬编码 indent=2 输出 TextContent（server.py:549），
            # 对 LLM 来说缩进纯浪费 token（多 ~30% 字符且不影响解析），重新 parse + dump 成紧凑格式。
            # 必须在 ImageContent 转换之前做——后者依赖 json.loads，紧凑格式不影响它。
            try:
                for i, content in enumerate(result):
                    if getattr(content, "type", None) != "text":
                        continue
                    try:
                        parsed = json.loads(_content_text(content))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(parsed, (dict, list)):
                        result[i] = _mcp_types.TextContent(
                            type="text",
                            text=json.dumps(parsed, ensure_ascii=False),
                        )
            except Exception as e:
                logger.warning(f"MCP 响应紧凑化失败（回退为原响应）: {e}")
            # 对返回 mcp_image_block=True 的工具做 ImageContent 转换
            # 支持两种结构：
            #   ① 直连工具：顶层 {mcp_image_block, image, ...}
            #   ② 经 localagent_advanced_tool 网关：{success, tool, result: {mcp_image_block, image, ...}}
            #      网关把响应包成嵌套结构，若不在此展开，base64 会泄漏到 TextContent 撑爆上下文
            try:
                for content in result:
                    if getattr(content, "type", None) != "text":
                        continue
                    try:
                        data = json.loads(_content_text(content))
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    # ① 直连工具：顶层 mcp_image_block
                    if data.get("mcp_image_block") and data.get("image"):
                        summary = {k: v for k, v in data.items() if k not in ("image", "mcp_image_block")}
                        return [
                            _mcp_types.TextContent(
                                type="text",
                                text=json.dumps(summary, ensure_ascii=False),
                            ),
                            _mcp_types.ImageContent(
                                type="image",
                                data=data["image"],
                                mimeType=data.get("mime_type", "image/png"),
                            ),
                        ]
                    # ② 网关包裹：data.result.mcp_image_block
                    nested = data.get("result")
                    if isinstance(nested, dict) and nested.get("mcp_image_block") and nested.get("image"):
                        nested_summary = {k: v for k, v in nested.items() if k not in ("image", "mcp_image_block")}
                        wrapper_summary = {k: v for k, v in data.items() if k != "result"}
                        wrapper_summary["result"] = nested_summary
                        return [
                            _mcp_types.TextContent(
                                type="text",
                                text=json.dumps(wrapper_summary, ensure_ascii=False),
                            ),
                            _mcp_types.ImageContent(
                                type="image",
                                data=nested["image"],
                                mimeType=nested.get("mime_type", "image/png"),
                            ),
                        ]
            except Exception as e:
                logger.warning(f"MCP image-block 转换失败（回退为文本）: {e}")

            # 用户补充指令注入：将待发送的用户消息作为额外 TextContent 追加到结果中
            # 这样 agent 调用任何 MCP 工具时都能看到用户的补充指令
            # 排除 user_message_* 工具自身（避免查询消息时消息被消费）
            try:
                if not tool_name.startswith("user_message_"):
                    supplement = get_supplement_text()
                    if supplement:
                        result = list(result) + [
                            _mcp_types.TextContent(type="text", text=supplement)
                        ]
            except Exception as e:
                logger.warning(f"用户补充指令注入失败: {e}")

            # 响应大小保护：单个 TextContent 超阈值时截断 + 强制上报用户
            try:
                for i, content in enumerate(result):
                    if getattr(content, "type", None) != "text":
                        continue
                    text = getattr(content, "text", "")
                    original_len = len(text)
                    if original_len <= MAX_MCP_RESPONSE_CHARS:
                        continue
                    head = text[:MAX_MCP_RESPONSE_CHARS - MCP_TRUNCATE_TAIL_CHARS - 400]
                    tail = text[-MCP_TRUNCATE_TAIL_CHARS:]
                    omitted = original_len - len(head) - len(tail)
                    marker = (
                        f"\n\n... [响应已截断: 省略 {omitted} 字符，共 {original_len} 字符] ...\n"
                        f"【必须上报用户】工具 `{tool_name}` 返回内容过大（{original_len} 字符）已被截断。\n"
                        f"- 禁止静默尝试其它方案（如缩小参数重试、换工具、改用 exec_python 发 HTTP）。\n"
                        f"- 请立即向用户报告：工具名、原始大小、截断位置，并请求用户指导（是否需要 summary=true 精简模式、或缩小查询范围、或接受截断）。\n"
                        f"- 用户未明确指示前，不要继续基于截断内容做决策。\n\n"
                    )
                    result[i] = _mcp_types.TextContent(type="text", text=head + marker + tail)
                    logger.warning(f"MCP 响应截断: tool={tool_name} original={original_len}chars")
            except Exception as e:
                logger.warning(f"响应大小检查失败（不影响返回）: {e}")

            # 返回结果（可能包含图像转换 + 用户补充指令）
            return result

        mcp._execute_api_tool = _patched_execute_api_tool

        mcp.mount_http()
        logger.info(f"MCP工具接口已挂载: http://127.0.0.1:8766/mcp (直连 {len(_mcp_include)} 个工具，含 ImageContent 补丁)")

        # ===== 注入 TOOL_ANNOTATIONS（MCP 协议规范的工具语义标记）=====
        # TOOL_ANNOTATIONS 在 mcp_whitelist.py 定义但此前从未注入 MCP tools/list 响应（死代码）。
        # 这里遍历 mcp.tools，为每个工具设置 readOnlyHint/destructiveHint/idempotentHint/openWorldHint，
        # 让 MCP 客户端（如仅通过 MCP 连接的外部 agent）能区分只读 vs 破坏性工具。
        try:
            from mcp.types import ToolAnnotations as _MCPToolAnnotations

            from server.mcp_whitelist import TOOL_ANNOTATIONS as _TOOL_ANNOTATIONS
            _annotated = 0
            for _tool in mcp.tools:
                _anns = _TOOL_ANNOTATIONS.get(_tool.name)
                if _anns:
                    # TOOL_ANNOTATIONS 值类型为 dict[str, bool]，与 ToolAnnotations 的
                    # title: str|None 参数不兼容（** 展开会误把 bool 传给 title），显式构造。
                    # 缺失键传 None（保持库默认 unset 语义）而非 False——
                    # False 会把"未声明"翻转为"明确声明非 openWorld/只读/无副作用"，语义不同
                    _tool.annotations = _MCPToolAnnotations(
                        title=None,
                        readOnlyHint=_anns.get("readOnlyHint"),
                        destructiveHint=_anns.get("destructiveHint"),
                        idempotentHint=_anns.get("idempotentHint"),
                        openWorldHint=_anns.get("openWorldHint"),
                    )
                    _annotated += 1
            logger.info(f"MCP annotations 已注入: {_annotated}/{len(mcp.tools)} 个工具")
        except Exception as _e:
            logger.warning(f"注入 TOOL_ANNOTATIONS 失败（不影响 MCP 功能）: {_e}")

        # 构建高级工具注册表：将非直连 REST operation 收录到 localagent_advanced_tool 网关
        # 这样它们仍可通过 MCP 访问（经网关路由），而非完全不可用
        try:
            _adv_count = _build_advanced_registry(app, _mcp_gateway_ops)
            logger.info(f"高级工具网关已就绪: {_adv_count} 个工具可通过 localagent_advanced_tool 访问")
        except Exception as _e:
            logger.warning(f"构建高级工具注册表失败: {_e}")

        return mcp
    except Exception as e:
        logger.warning(f"MCP挂载失败（不影响REST API）: {e}")
        return None
