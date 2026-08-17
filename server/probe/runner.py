"""chat-mgmt-probe Ticket 02: 探针主逻辑

组装 RunnerDeps（MockLLM + 真 EventStore + 真 ToolRegistry + 真 HttpClientToolExecutor），
调 SessionFacade.start(mode="probe") 跑完一个会话，采集 messages/events/tool_calls/llm 请求响应，
落盘到 temp/chat_mgmt_probe/runs/<session_id>/，返回结构化摘要。

设计参考：server/activity_tracker/headless_runner.py 的 run_main_agent 组装逻辑。
区别：llm_gateway 用 MockLLM 替代 LLMPoolGateway，mode="probe" 替代 "headless"。

tracer bullet 范围（Ticket 02）：
- 纯文本单轮（script=["hi"]）
- 非流式（use_stream=False）
- 无工具调用（script 不含 tool_calls）
- 无审批 / 无压缩

Ticket 04+ 扩展 pattern 模式 + tool_call。
Ticket 05+ 扩展流式 / 审批 / 压缩。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from client.core.agent import (
    Compactor,
    EventStore,
    HttpClientToolExecutor,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionFacade,
    ToolRegistry,
)
from client.core.agent.compactor import ContextOverflow
from client.core.agent.types import (
    TOOL_SAFETY_APPROVAL_REQUIRED,
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolResultVariant,
)

logger = logging.getLogger("localagent.probe.runner")

# 探针落盘根目录（temp/chat_mgmt_probe/runs/）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = _PROJECT_ROOT / "temp" / "chat_mgmt_probe" / "runs"

# 默认后端 base_url（与 headless_runner.py DEFAULT_BASE_URL 一致）
DEFAULT_BASE_URL = "http://127.0.0.1:8766"

# 摘要中 content 前缀长度（控制上下文压力）
_CONTENT_HEAD_LEN = 80


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class ProbeParams:
    """探针入参（Ticket 02 最小集，Ticket 04/05 扩展）。"""

    trigger_text: str
    mode: str = "script"  # "script" | "pattern"
    script: list[Any] = field(default_factory=list)  # script 模式：每条 str | dict
    pattern: str = ""  # pattern 模式：pattern 名
    use_stream: bool = False  # Ticket 05 扩展
    trigger_approval: bool = False  # Ticket 05 扩展
    max_context_messages: int = 0  # Ticket 05 扩展（0=不限制）
    auto_cleanup: bool = True
    db_path: str = ""  # 空则用默认 data/client/agent.db
    base_url: str = DEFAULT_BASE_URL


@dataclass
class ProbeSummary:
    """探针返回摘要（HTTP 返回 + 落盘索引）。"""

    session_id: str
    mode: str
    status: str
    messages_count: int
    events_count: int
    tool_calls_count: int
    messages_preview: list[dict]  # [{seq, role, source, content_head}]
    tool_calls_preview: list[dict]  # [{name, status}]
    llm_calls: list[dict]  # [{request_messages_len, request_system_head, response_content_head, response_tool_calls_count}]
    run_dir: str
    cleaned_up: bool


# ============================================================================
# 序列化辅助
# ============================================================================


def _content_to_str(content: Any) -> str:
    """Message.content 可能是 str 或 list[dict]（OpenAI content blocks），转成预览字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # OpenAI content blocks: [{"type": "text", "text": "..."}, ...]
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def _message_to_dict(msg: Message) -> dict:
    """Message dataclass → 可 JSON 序列化的 dict。"""
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "seq": msg.seq,
        "role": msg.role,
        "content": msg.content,  # 可能是 str 或 list[dict]，json.dumps 都能处理
        "source": msg.source,
        "visible": msg.visible,
        "tool_call_id": msg.tool_call_id,
        "tool_calls": msg.tool_calls,
        "thinking": msg.thinking,
        "created_at": msg.created_at,
    }


def _llm_request_to_dict(req: LLMRequest) -> dict:
    """LLMRequest dataclass → dict。messages 转成 dict list。"""
    return {
        "messages": [_message_to_dict(m) for m in req.messages],
        "model": req.model,
        "tools": req.tools,
        "tool_choice": req.tool_choice,
        "system": req.system,
        "max_tokens": req.max_tokens,
    }


def _llm_response_to_dict(resp: LLMResponse) -> dict:
    """LLMResponse dataclass → dict。"""
    return {
        "content": resp.content,
        "tool_calls": resp.tool_calls,
        "usage": resp.usage,
        "stop_reason": resp.stop_reason,
        "model": resp.model,
        # B2（spec D2/D3）：删 cost_usd 字段，本地无法准确算 cost
        "thinking": resp.thinking,
    }


# ============================================================================
# 主逻辑
# ============================================================================


def _generate_probe_session_id() -> str:
    """生成 probe-xxx session_id。"""
    return f"probe-{uuid.uuid4().hex[:12]}"


def _build_mock_llm(params: ProbeParams) -> MockLLM:
    """根据 params 构造 MockLLM。

    Ticket 02：script 模式（每条 str）。
    Ticket 04：script dict 格式 + pattern 模式（5 种 callable）。
    Ticket 05：script dict 支持 raise_context_overflow 字段（这一轮抛 ContextOverflow）。
    """
    if params.mode == "script":
        # Ticket 05: 如果任何 script item 含 raise_context_overflow，转 callable 模式
        # （因为 MockLLM.script 模式只能消费 str | LLMResponse，不能抛异常）
        has_overflow = any(
            isinstance(item, dict) and item.get("raise_context_overflow")
            for item in params.script
        )
        if has_overflow:
            return MockLLM(callable=_build_script_callable_with_overflow(params.script))
        # 无 overflow：原 script 模式
        script_items: list[str | LLMResponse] = []
        for item in params.script:
            if isinstance(item, str):
                script_items.append(item)
            elif isinstance(item, dict):
                # Ticket 04: dict → LLMResponse 转换
                script_items.append(_dict_to_llm_response(item))
            else:
                script_items.append(str(item))
        return MockLLM(script=script_items)
    elif params.mode == "pattern":
        # Ticket 04: pattern 模式用 callable
        return MockLLM(callable=_build_pattern_callable(params.pattern))
    else:
        raise ValueError(f"unknown mode: {params.mode}")


def _build_script_callable_with_overflow(script: list[Any]):
    """Ticket 05: 构造 script callable，支持 raise_context_overflow 字段。

    每次调用消费一条 script item：
    - str → LLMResponse(content=str)
    - dict with raise_context_overflow=True → 抛 ContextOverflow
    - dict 其他 → _dict_to_llm_response 转换
    - LLMResponse → 原样返回
    """
    state = {"index": 0}

    def _callable(req: LLMRequest) -> LLMResponse:
        if state["index"] >= len(script):
            raise StopIteration(
                f"probe script exhausted: index={state['index']}, len={len(script)}"
            )
        item = script[state["index"]]
        state["index"] += 1

        if isinstance(item, str):
            return LLMResponse(
                content=item,
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        if isinstance(item, dict):
            if item.get("raise_context_overflow"):
                raise ContextOverflow(
                    item.get("overflow_message", "probe mock context overflow"),
                    status_code=item.get("status_code", 413),
                )
            return _dict_to_llm_response(item)
        if isinstance(item, LLMResponse):
            return item
        # 兜底
        return LLMResponse(content=str(item), model="mock-llm")

    return _callable


def _dict_to_llm_response(d: dict) -> LLMResponse:
    """Ticket 04: dict → LLMResponse 转换。

    支持字段：
    - content: str（默认 ""）
    - tool_calls: list[dict]（OpenAI 格式，默认 []）
    - stop_reason: str（默认 "end_turn" 或 "tool_use" 如果有 tool_calls）
    - thinking: str（默认 ""）
    - usage: dict（默认空）
    - raise_context_overflow: bool（Ticket 05 新增，True 时这一轮抛 ContextOverflow）
      注：raise_context_overflow 不在 LLMResponse 字段里，由 _build_mock_llm 检测并转 callable
    """
    content = d.get("content", "")
    tool_calls = d.get("tool_calls", [])
    stop_reason = d.get("stop_reason", "tool_use" if tool_calls else "end_turn")
    thinking = d.get("thinking", "")
    usage = d.get("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        thinking=thinking,
        usage=usage,
        model="mock-llm",
    )


def _build_approval_mock_executor() -> MockToolExecutor:
    """Ticket 05: 构造审批拒绝路径的 mock ToolExecutor。

    真实审批路径需要 server 弹 GUI 弹窗，不适合探针场景。
    本函数返回 MockToolExecutor，模拟"approval_required 工具被用户拒绝"路径：
    - tool_call.safety == "approval_required" 或 name 属于典型写入操作（exec_cmd/exec_apply_patch/exec_python）
      → 返回 is_error=True "User denied approval: probe mock"
    - 其他工具 → 返回 is_error=True "Tool not executed (probe mock executor)"

    这样可以测试 runner 对审批拒绝路径的处理是否完善（tool_calls 表 status=failed + transition(next_turn) + 模型回灌 tool_result）。
    """
    approval_required_names = {"exec_cmd", "exec_apply_patch", "exec_python", "exec_kill", "exec_send_input"}

    def _mock_execute(tc: ToolCall) -> ToolResult:
        is_approval = (
            tc.safety == TOOL_SAFETY_APPROVAL_REQUIRED
            or tc.name in approval_required_names
        )
        if is_approval:
            return ToolResult(
                tool_call_id=tc.id,
                content="User denied approval: probe mock (trigger_approval=True)",
                variant=ToolResultVariant.USER_DENIED,
            )
        # 非 approval 工具也拒绝（探针在 trigger_approval 模式下不走真 HTTP）
        return ToolResult(
            tool_call_id=tc.id,
            content=f"Tool '{tc.name}' not executed (probe mock executor in approval mode)",
            variant=ToolResultVariant.ERROR,
            error_type="probe_mock_blocked",
        )

    return MockToolExecutor(callable=_mock_execute)


def _make_openai_tool_call(call_id: str, name: str, args: dict | None = None) -> dict:
    """构造 OpenAI 格式的 tool_call dict（参考 tests/test_v6_lite_t02.py）。"""
    import json as _json
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(args or {})},
    }


def _build_pattern_callable(pattern: str):
    """Ticket 04: 构造 pattern callable（根据 pattern 名返回 LLMRequest→LLMResponse 的函数）。

    5 种 pattern：
    - text_only：每轮返回纯文本 "MockLLM text_only response #N"
    - tool_call_then_text：第一轮 tool_call（memory_list），第二轮纯文本
    - always_tool_call：每轮都返回 tool_call（memory_list）
    - streaming_text：返回带 thinking 字段的响应（配合 use_stream 测流式合并）
    - refuse：返回 "I refuse to do this"
    """
    if pattern == "text_only":
        def _text_only(req: LLMRequest) -> LLMResponse:
            # 简化：不追踪编号，每次返回固定文本
            return LLMResponse(
                content="MockLLM text_only response",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _text_only

    elif pattern == "tool_call_then_text":
        # 用闭包变量追踪是否已经调过 tool_call
        state = {"called_tool": False}
        def _tc_then_text(req: LLMRequest) -> LLMResponse:
            if not state["called_tool"]:
                state["called_tool"] = True
                return LLMResponse(
                    content="let me check memory",
                    tool_calls=[_make_openai_tool_call("call-probe-tc-1", "memory_list", {})],
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    stop_reason="tool_use",
                    model="mock-llm",
                )
            return LLMResponse(
                content="done checking memory",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _tc_then_text

    elif pattern == "always_tool_call":
        def _always_tc(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="calling tool again",
                tool_calls=[_make_openai_tool_call(f"call-probe-always-{id(req)}", "memory_list", {})],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="tool_use",
                model="mock-llm",
            )
        return _always_tc

    elif pattern == "streaming_text":
        def _streaming_text(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="final answer after thinking",
                thinking="let me think about this... the answer is straightforward",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _streaming_text

    elif pattern == "refuse":
        def _refuse(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="I refuse to do this",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _refuse

    else:
        raise ValueError(f"unknown pattern: {pattern}. Available: text_only / tool_call_then_text / always_tool_call / streaming_text / refuse")


async def run_probe(params: ProbeParams) -> ProbeSummary:
    """探针主入口：组装 deps → 跑 facade.start → 采集 → 落盘 → 摘要。

    Args:
        params: ProbeParams

    Returns:
        ProbeSummary
    """
    sid = _generate_probe_session_id()
    logger.info(
        "probe start: session=%s mode=%s trigger_len=%d use_stream=%s auto_cleanup=%s",
        sid, params.mode, len(params.trigger_text), params.use_stream, params.auto_cleanup,
    )

    # 组装 MockLLM
    mock_llm = _build_mock_llm(params)

    # 组装 EventStore（共享 agent.db 或独立 db）
    db_path = params.db_path or None  # None 让 EventStore 用默认路径
    store = EventStore(db_path=db_path) if db_path else EventStore()
    store.init()

    # 组装 ToolRegistry + HttpClientToolExecutor（真工具调用）
    # Ticket 05: trigger_approval=True 时改用 mock executor 模拟审批拒绝路径
    # （真实审批路径需要 server 弹 GUI 弹窗，不适合探针场景）
    registry = ToolRegistry(base_url=params.base_url)
    if params.trigger_approval:
        # mock 模式：不 refresh registry（避免依赖后端在线）
        executor = _build_approval_mock_executor()
        logger.info("probe trigger_approval=True: using mock executor (no real HTTP)")
    else:
        try:
            registry.refresh()
        except Exception as e:
            logger.warning("ToolRegistry refresh failed (will run without tools): %s", e)
        executor = HttpClientToolExecutor(registry=registry, base_url=params.base_url)

    # 组装 interrupt_event
    interrupt_event = asyncio.Event()

    # 组装 RunnerDeps
    # Ticket 05: max_context_messages>0 时注入 Compactor，配合 script raise_context_overflow 测压缩路径
    deps_kwargs: dict[str, Any] = {
        "llm_gateway": mock_llm,
        "event_store": store,
        "tool_executor": executor,
        "tool_registry": registry,
        "interrupt_event": interrupt_event,
    }
    if params.max_context_messages > 0:
        # Compactor 的 max_context_tokens 用很小值（让 should_compact 容易达到阈值）
        # 实际触发由 MockLLM 抛 ContextOverflow 主动驱动（runner 捕获后调 compactor.compact）
        deps_kwargs["compactor"] = Compactor(
            max_context_tokens=params.max_context_messages * 100,  # 粗估：每条 ~100 tokens
            tail_keep=2,  # 保留尾部 2 条（便于测试观察压缩效果）
        )
        logger.info(
            "probe max_context_messages=%d: compactor injected (max_tokens=%d, tail_keep=2)",
            params.max_context_messages,
            params.max_context_messages * 100,
        )
    deps = RunnerDeps(**deps_kwargs)

    # 组装 RunnerConfig（frozen dataclass）
    # Ticket 02：use_stream=False（tracer bullet 最窄路径）
    runner_config = RunnerConfig(
        session_id=sid,
        model="mock-llm",  # MockLLM 不在乎 model
        max_iterations=10,  # 探针场景不需要 50 轮
        # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost
        use_stream=params.use_stream,
    )

    # 调 SessionFacade.start（mode="probe"）
    facade = SessionFacade(deps=deps, config=runner_config)
    cleaned_up = False
    try:
        outcome = await facade.start(sid, params.trigger_text, mode="probe")
        status = outcome.status
        logger.info(
            "probe completed: session=%s status=%s iterations=%d",
            sid, status, outcome.iterations,
        )
    except Exception:
        logger.exception("probe failed: session=%s", sid)
        status = "failed"
    finally:
        # 采集 + 落盘（即使失败也要采集已生成的数据）
        pass

    # 采集
    messages = await store.load_messages(sid)
    events = await store.load_events(sid)
    tool_calls = await store.load_tool_calls(sid)
    llm_requests = mock_llm.requests
    llm_responses = mock_llm.responses
    session = await store.get_session(sid)

    # 落盘
    run_dir = RUNS_DIR / sid
    run_dir.mkdir(parents=True, exist_ok=True)
    _dump_json(run_dir / "messages.json", [_message_to_dict(m) for m in messages])
    _dump_json(run_dir / "events.json", [asdict(e) for e in events])
    _dump_json(run_dir / "tool_calls.json", [asdict(tc) for tc in tool_calls])
    _dump_json(run_dir / "llm_requests.json", [_llm_request_to_dict(r) for r in llm_requests])
    _dump_json(run_dir / "llm_responses.json", [_llm_response_to_dict(r) for r in llm_responses])
    _dump_json(run_dir / "session.json", asdict(session) if session else {})

    # auto_cleanup
    if params.auto_cleanup:
        try:
            deleted = await store.delete_session(sid)
            cleaned_up = deleted
            logger.info("probe auto_cleanup: session=%s deleted=%s", sid, deleted)
        except Exception as e:
            logger.warning("probe auto_cleanup failed: session=%s %s", sid, e)
            cleaned_up = False

    # 关闭 store
    try:
        store.close()
    except Exception:
        pass

    # 构造摘要
    summary = ProbeSummary(
        session_id=sid,
        mode="probe",
        status=status,
        messages_count=len(messages),
        events_count=len(events),
        tool_calls_count=len(tool_calls),
        messages_preview=[
            {
                "seq": m.seq,
                "role": m.role,
                "source": m.source,
                "content_head": _content_to_str(m.content)[:_CONTENT_HEAD_LEN],
            }
            for m in messages
        ],
        tool_calls_preview=[
            {"name": tc.name, "status": tc.status}
            for tc in tool_calls
        ],
        llm_calls=[
            {
                "request_messages_len": len(req.messages),
                "request_system_head": (req.system or "")[:_CONTENT_HEAD_LEN],
                "response_content_head": (resp.content or "")[:_CONTENT_HEAD_LEN],
                "response_tool_calls_count": len(resp.tool_calls),
            }
            for req, resp in zip(llm_requests, llm_responses, strict=False)
        ],
        run_dir=str(run_dir),
        cleaned_up=cleaned_up,
    )
    logger.info(
        "probe summary: session=%s messages=%d events=%d tool_calls=%d llm_calls=%d cleaned_up=%s",
        sid, summary.messages_count, summary.events_count,
        summary.tool_calls_count, len(summary.llm_calls), summary.cleaned_up,
    )
    return summary


def _dump_json(path: Path, data: Any) -> None:
    """JSON 落盘辅助。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
