"""v6-lite T08 验收测试：W5 流式 + L0/L2 压缩

覆盖 T08 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] L0ArtifactStore 大结果落盘（>8KB → preview+path）
- [x] HttpClientToolExecutor 集成 L0（成功响应 >8KB 自动落盘）
- [x] Compactor should_compact（85% 阈值）
- [x] Compactor compact（head + summary + tail 重建）
- [x] Compactor fail-open（LLM 错误返回原 messages）
- [x] ContextOverflow 异常可被 raise / catch
- [x] LLMPoolGateway 413 → raise ContextOverflow
- [x] SessionRunner reactive_compact_retry（首次 413 → 压缩 → 重试成功）
- [x] SessionRunner 二次 413 → fail（防死循环硬规则）
- [x] SessionRunner 无 compactor → fail（context_overflow_no_compactor）
- [x] SessionRunner compactor noop（消息太少）→ fail（compact_noop）
- [x] EventStore.replace_messages（压缩后持久化）
- [x] transition_reason=reactive_compact_retry 事件写入
- [x] has_attempted_reactive_compact 标志位 retry 时不重置
- [x] SSE stream 事件解析（text_delta / tool_call_delta / usage / done）
- [x] SSE stream HTTP 错误 → provider_error + done

测试策略：
- 不接真实 server，用 MockLLM + MockToolExecutor + MockCompactorLLM
- L0ArtifactStore 用 tmp_path 隔离
- LLMPoolGateway 用 unittest.mock 模拟 requests.Response
- SSE stream 用 mock response with iter_lines
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    Compactor,
    ContextOverflow,
    EventStore,
    HttpClientToolExecutor,
    L0ArtifactStore,
    LLMPoolGateway,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.llm_pool_gateway import (  # noqa: E402
    SSE_EVENT_CONTEXT_OVERFLOW,
    SSE_EVENT_DONE,
    SSE_EVENT_PROVIDER_ERROR,
    SSE_EVENT_TEXT_DELTA,
    SSE_EVENT_USAGE,
)
from client.core.agent.types import (  # noqa: E402
    TRANSITION_REACTIVE_COMPACT_RETRY,
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "agent_t08.db"))
    store.init()
    return store


class _MockResponse:
    """模拟 requests.Response。"""

    def __init__(self, status_code: int = 200, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._lines = []
        # 支持迭代 SSE
        if json_data is not None:
            self.text = json.dumps(json_data, ensure_ascii=False)

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        pass


class _MockLLMThatRaises:
    """LLMGateway 实现：每次 call 都 raise ContextOverflow（用于测试 413 路径）。"""

    def __init__(self, *, raise_overflow: bool = True, fallback_text: str = "ok"):
        self.raise_overflow = raise_overflow
        self.fallback_text = fallback_text
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if self.raise_overflow:
            raise ContextOverflow("mock context overflow", status_code=413)
        return LLMResponse(content=self.fallback_text, stop_reason="end_turn", model="mock")

    async def stream(self, request: LLMRequest):
        """v6-lite-streaming-gui T02: stream() 兼容——call_count 共享，yield 事件。"""
        self.call_count += 1
        if self.raise_overflow:
            yield {"type": SSE_EVENT_CONTEXT_OVERFLOW, "error": "mock context overflow", "status_code": 413}
            yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            return
        yield {"type": SSE_EVENT_TEXT_DELTA, "delta": self.fallback_text}
        yield {"type": SSE_EVENT_DONE, "finish_reason": "stop"}


class _ScriptedCompactorLLM:
    """Compactor 用的 LLM Gateway：摘要轮返回 summary，其他轮 raise/fallback。

    用于测试 SessionRunner reactive_compact_retry：
    - 第 1 次 call（runner 主循环）→ raise ContextOverflow
    - 第 2 次 call（compactor 生成摘要）→ 返回摘要文本
    - 第 3 次 call（runner 重试主循环）→ 返回正常文本

    v6-lite-streaming-gui T02: runner 用 stream()，compactor 用 call()，call_count 共享。
    """

    def __init__(self, *, summary_text: str = "MOCK SUMMARY", final_text: str = "ok"):
        self.summary_text = summary_text
        self.final_text = final_text
        self.call_count = 0
        self.requests: list[LLMRequest] = []

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 1:
            # 首次：模拟 413
            raise ContextOverflow("mock context overflow on first call", status_code=413)
        if self.call_count == 2:
            # 第二次：compactor 调用，返回摘要
            return LLMResponse(
                content=self.summary_text, stop_reason="end_turn", model="mock-summary",
            )
        # 第三次+：runner 重试，返回正常文本
        return LLMResponse(
            content=self.final_text, stop_reason="end_turn", model="mock",
        )

    async def stream(self, request: LLMRequest):
        """v6-lite-streaming-gui T02: stream() 兼容——call_count 共享，yield 事件。"""
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 1:
            yield {"type": SSE_EVENT_CONTEXT_OVERFLOW, "error": "mock context overflow on first call", "status_code": 413}
            yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            return
        if self.call_count == 2:
            yield {"type": SSE_EVENT_TEXT_DELTA, "delta": self.summary_text}
            yield {"type": SSE_EVENT_DONE, "finish_reason": "stop"}
            return
        yield {"type": SSE_EVENT_TEXT_DELTA, "delta": self.final_text}
        yield {"type": SSE_EVENT_DONE, "finish_reason": "stop"}


class _AlwaysOverflowCompactorLLM:
    """每次 call 都 raise ContextOverflow（用于测试二次 413 防死循环）。"""

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        raise ContextOverflow(f"overflow #{self.call_count}", status_code=413)

    async def stream(self, request: LLMRequest):
        """v6-lite-streaming-gui T02: stream() 兼容——总是 yield context_overflow。"""
        self.call_count += 1
        yield {"type": SSE_EVENT_CONTEXT_OVERFLOW, "error": f"overflow #{self.call_count}", "status_code": 413}
        yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}


# ============================================================================
# T08-A: L0ArtifactStore（大工具结果落盘）
# ============================================================================


class TestL0ArtifactStore:
    """L0ArtifactStore：单条工具结果 >8 KiB 落盘到 data/client/artifacts/。"""

    def test_small_content_not_persisted(self, tmp_path):
        """content <= 8KB 时不落盘，原样返回。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        small = "x" * 100  # 100 字节
        new_content, artifact_path = store.maybe_persist(
            session_id="sess-1", tool_call_id="call-1", content=small,
        )
        assert new_content == small
        assert artifact_path is None
        # 目录都不应该被创建
        assert not (tmp_path / "artifacts").exists()

    def test_large_content_persisted(self, tmp_path):
        """content > 8KB 时落盘，返回 preview + path 提示。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        large = "x" * (8 * 1024 + 100)  # 8KB+100B
        new_content, artifact_path = store.maybe_persist(
            session_id="sess-1", tool_call_id="call-1", content=large,
        )
        assert artifact_path is not None
        assert "[artifact_saved_at:" in new_content
        # 文件确实落盘
        p = Path(artifact_path)
        assert p.exists()
        assert p.read_text(encoding="utf-8") == large
        # 路径包含 session_id 和 tool_call_id
        assert "sess-1" in artifact_path
        assert "call-1" in artifact_path

    def test_idempotent_overwrite(self, tmp_path):
        """同 tool_call_id 多次保存覆盖（不报错）。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        large1 = "A" * (8 * 1024 + 10)
        large2 = "B" * (8 * 1024 + 20)
        _, path1 = store.maybe_persist(
            session_id="sess-1", tool_call_id="call-1", content=large1,
        )
        _, path2 = store.maybe_persist(
            session_id="sess-1", tool_call_id="call-1", content=large2,
        )
        assert path1 == path2
        assert Path(path1).read_text(encoding="utf-8") == large2  # 后写覆盖

    def test_path_sanitization(self, tmp_path):
        """session_id / tool_call_id 含特殊字符时被清理（防目录穿越）。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        large = "x" * (8 * 1024 + 10)
        # 含路径分隔符和特殊字符
        new_content, artifact_path = store.maybe_persist(
            session_id="../evil/session", tool_call_id="../../etc/passwd", content=large,
        )
        assert artifact_path is not None
        # 不能逃出 artifacts_dir
        abs_artifact = Path(artifact_path).resolve()
        abs_base = (tmp_path / "artifacts").resolve()
        assert str(abs_artifact).startswith(str(abs_base))
        # 文件存在
        assert abs_artifact.exists()

    def test_load_artifact(self, tmp_path):
        """load() 读取落盘的完整内容。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        large = "hello world " * 1000  # > 8KB
        _, artifact_path = store.maybe_persist(
            session_id="sess-1", tool_call_id="call-1", content=large,
        )
        loaded = store.load(artifact_path)
        assert loaded == large

    def test_load_nonexistent_returns_none(self, tmp_path):
        """load() 不存在的文件返回 None。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        loaded = store.load(str(tmp_path / "nonexistent.txt"))
        assert loaded is None


# ============================================================================
# T08-B: HttpClientToolExecutor + L0 集成
# ============================================================================


class TestHttpClientToolExecutorL0:
    """HttpClientToolExecutor 注入 L0ArtifactStore 后大结果自动落盘。"""

    def test_large_response_persisted_via_l0(self, tmp_path):
        """工具返回 >8KB → L0 落盘 → ToolResult.content 是 preview+path。"""
        from client.core.agent.tool_registry import ToolEntry, ToolRegistry

        # 构造 mock registry 和 entry
        reg = MagicMock(spec=ToolRegistry)
        entry = MagicMock(spec=ToolEntry)
        entry.method = "GET"
        entry.build_request.return_value = ("/test/large", None, None)
        reg.lookup.return_value = entry

        # mock HTTP 响应（>8KB JSON）
        large_data = {"data": "x" * (8 * 1024 + 100)}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = large_data
        mock_resp.text = json.dumps(large_data, ensure_ascii=False)

        l0 = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        executor = HttpClientToolExecutor(registry=reg, base_url="http://mock", l0_store=l0)
        executor._session = MagicMock()
        executor._session.get.return_value = mock_resp

        tool_call = ToolCall(id="call-l0", name="test_large", session_id="sess-l0")
        result = _run(executor.execute(tool_call))

        assert result.is_error is False
        assert result.artifact_path is not None
        assert "[artifact_saved_at:" in result.content
        assert Path(result.artifact_path).exists()

    def test_small_response_not_persisted(self, tmp_path):
        """工具返回 <8KB → 不落盘 → ToolResult.content 是原内容。"""
        from client.core.agent.tool_registry import ToolEntry, ToolRegistry

        reg = MagicMock(spec=ToolRegistry)
        entry = MagicMock(spec=ToolEntry)
        entry.method = "GET"
        entry.build_request.return_value = ("/test/small", None, None)
        reg.lookup.return_value = entry

        small_data = {"ok": True}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = small_data
        mock_resp.text = json.dumps(small_data, ensure_ascii=False)

        l0 = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        executor = HttpClientToolExecutor(registry=reg, base_url="http://mock", l0_store=l0)
        executor._session = MagicMock()
        executor._session.get.return_value = mock_resp

        tool_call = ToolCall(id="call-small", name="test_small", session_id="sess-small")
        result = _run(executor.execute(tool_call))

        assert result.is_error is False
        assert result.artifact_path is None
        assert "ok" in result.content

    def test_error_response_not_persisted(self, tmp_path):
        """错误响应不落盘（让模型看到完整错误信息）。"""
        from client.core.agent.tool_registry import ToolEntry, ToolRegistry

        reg = MagicMock(spec=ToolRegistry)
        entry = MagicMock(spec=ToolEntry)
        entry.method = "GET"
        entry.build_request.return_value = ("/test/err", None, None)
        reg.lookup.return_value = entry

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "x" * (8 * 1024 + 100)  # >8KB 错误响应
        mock_resp.json.side_effect = ValueError("not json")

        l0 = L0ArtifactStore(artifacts_dir=str(tmp_path / "artifacts"))
        executor = HttpClientToolExecutor(registry=reg, base_url="http://mock", l0_store=l0)
        executor._session = MagicMock()
        executor._session.get.return_value = mock_resp

        tool_call = ToolCall(id="call-err", name="test_err", session_id="sess-err")
        result = _run(executor.execute(tool_call))

        assert result.is_error is True
        assert result.artifact_path is None  # 错误响应不落盘
        assert "HTTP 500" in result.content


# ============================================================================
# T08-C: Compactor
# ============================================================================


class TestCompactor:
    """Compactor：L2 上下文压缩。"""

    def test_should_compact_below_threshold(self):
        """tokens < 85% 阈值 → 不需要压缩。"""
        compactor = Compactor(max_context_tokens=10000)  # 85% = 8500
        # 4 char/token → 1000 chars = 250 tokens，远低于 8500
        messages = [
            Message(role="user", content="x" * 1000, source="user"),
            Message(role="assistant", content="y" * 500, source="assistant"),
        ]
        assert compactor.should_compact(messages) is False

    def test_should_compact_above_threshold(self):
        """tokens > 85% 阈值 → 需要压缩。"""
        compactor = Compactor(max_context_tokens=1000)  # 85% = 850
        # 4 char/token → 4000 chars = 1000 tokens > 850
        messages = [
            Message(role="user", content="x" * 2000, source="user"),
            Message(role="assistant", content="y" * 2000, source="assistant"),
        ]
        assert compactor.should_compact(messages) is True

    def test_should_compact_empty_messages(self):
        """空 messages → 不需要压缩。"""
        compactor = Compactor(max_context_tokens=1000)
        assert compactor.should_compact([]) is False

    def test_compact_skip_too_few_messages(self, tmp_path):
        """messages 数量 <= tail_keep → 原样返回（无需压缩，spec D8 无 head 保留）。"""
        compactor = Compactor(max_context_tokens=1000, tail_keep=6)
        # 6 条消息 = tail_keep，刚好不压缩（旧逻辑是 tail_keep+1，D8 移除 head 后阈值降 1）
        messages = [
            Message(role="user", content=f"msg {i}", source="user")
            for i in range(6)
        ]
        mock_llm = MockLLM(default_text="summary")
        result = _run(compactor.compact("sess-test", messages, mock_llm))
        assert result is messages  # 原样返回
        assert mock_llm.call_count == 0  # 没调 LLM

    def test_compact_produces_summary_tail(self, tmp_path):
        """正常压缩：summary + tail 重建（spec D8：不保留 head）。

        注意：Compactor __init__ 强制 tail_keep = max(arg, DEFAULT_TAIL_KEEP=6)，
        所以传入 tail_keep=4 时实际用 6。
        """
        compactor = Compactor(max_context_tokens=1000, tail_keep=4)
        # 12 条消息：middle(6) + tail(6)（tail_keep 实际为 6，无 head 保留）
        # messages[0] 是旧 user prompt（"TASK GOAL"），应进摘要而非保留原貌
        messages = [
            Message(role="system", content="TASK GOAL", source="system"),
        ] + [
            Message(role="user", content=f"middle msg {i}", source="user")
            for i in range(5)
        ] + [
            Message(role="user", content="tail msg 1", source="user"),
            Message(role="assistant", content="tail msg 2", source="assistant"),
            Message(role="user", content="tail msg 3", source="user"),
            Message(role="assistant", content="tail msg 4", source="assistant"),
            Message(role="user", content="tail msg 5", source="user"),
            Message(role="assistant", content="tail msg 6", source="assistant"),
        ]
        assert len(messages) == 12

        mock_llm = MockLLM(default_text="MOCK SUMMARY TEXT")
        result = _run(compactor.compact("sess-test", messages, mock_llm))

        # 调了一次 LLM
        assert mock_llm.call_count == 1
        # 结果是 summary + tail = 1 + 6 = 7（无 head，spec D8）
        assert len(result) == 7
        # result[0] 是 summary（不是旧 user prompt "TASK GOAL"）
        assert result[0].source == "summary"
        assert "MOCK SUMMARY TEXT" in result[0].content
        assert "[CONTEXT_SUMMARY]" in result[0].content
        assert result[0].content != "TASK GOAL"  # 旧 head 不再以原貌出现
        # tail 保留最后 6 条
        assert result[1].content == "tail msg 1"
        assert result[6].content == "tail msg 6"
        # 旧 messages[0]（"TASK GOAL"）的内容进摘要请求文本
        assert "TASK GOAL" in mock_llm.requests[0].messages[0].content

    def test_compact_fail_open_on_llm_error(self):
        """LLM 调用返回 error → fail-open 返回原 messages。"""
        compactor = Compactor(max_context_tokens=1000, tail_keep=4)
        messages = [
            Message(role="system", content="head", source="system"),
        ] + [
            Message(role="user", content=f"m {i}", source="user")
            for i in range(5)
        ] + [
            Message(role="user", content="tail", source="user"),
        ] * 4

        # mock LLM 返回 error
        mock_llm = MockLLM(callable=lambda req: LLMResponse(
            content="llm error", stop_reason="error", model="err",
        ))
        result = _run(compactor.compact("sess-test", messages, mock_llm))
        assert result is messages  # fail-open 原样返回

    def test_compact_fail_open_on_llm_exception(self):
        """LLM 调用抛异常 → fail-open 返回原 messages。"""
        compactor = Compactor(max_context_tokens=1000, tail_keep=4)
        messages = [
            Message(role="system", content="head", source="system"),
        ] + [
            Message(role="user", content=f"m {i}", source="user")
            for i in range(5)
        ] + [
            Message(role="user", content="tail", source="user"),
        ] * 4

        class _BoomLLM:
            async def call(self, request):
                raise RuntimeError("network down")
        result = _run(compactor.compact("sess-test", messages, _BoomLLM()))
        assert result is messages

    def test_compact_summary_request_has_no_tools(self):
        """压缩请求显式 tool_choice=none（NO_TOOLS_PREAMBLE 防压缩触发工具）。"""
        compactor = Compactor(max_context_tokens=1000, tail_keep=4)
        messages = [
            Message(role="system", content="head", source="system"),
        ] + [
            Message(role="user", content=f"m {i}", source="user")
            for i in range(5)
        ] + [
            Message(role="user", content="tail", source="user"),
        ] * 4

        captured_request: list[LLMRequest] = []

        def _capture(req: LLMRequest) -> LLMResponse:
            captured_request.append(req)
            return LLMResponse(content="summary", stop_reason="end_turn", model="m")

        mock_llm = MockLLM(callable=_capture)
        _run(compactor.compact("sess-test", messages, mock_llm))

        assert len(captured_request) == 1
        req = captured_request[0]
        assert req.tool_choice == "none"
        assert req.tools == []
        assert "Do NOT call any tools" in req.system


# ============================================================================
# T08-D: ContextOverflow 异常
# ============================================================================


class TestContextOverflow:
    """ContextOverflow 异常 + LLMPoolGateway 413 检测。"""

    def test_context_overflow_is_exception(self):
        """ContextOverflow 是 Exception 子类。"""
        assert issubclass(ContextOverflow, Exception)
        exc = ContextOverflow("test", status_code=413)
        assert str(exc) == "test"
        assert exc.status_code == 413

    def test_context_overflow_default_status_code(self):
        """无 status_code 时默认 413。"""
        exc = ContextOverflow("test")
        assert exc.status_code == 413

    def test_llm_pool_gateway_raises_on_413(self):
        """LLMPoolGateway.call() HTTP 413 → raise ContextOverflow。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 413
        mock_resp.text = "Request Entity Too Large: context too long"
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        with pytest.raises(ContextOverflow) as exc_info:
            _run(gw.call(request))
        assert exc_info.value.status_code == 413

    def test_llm_pool_gateway_raises_on_context_length_keyword(self):
        """HTTP 400 但 body 含 context_length → raise ContextOverflow。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error": {"message": "This model\'s maximum context length is 8192 tokens"}}'
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        with pytest.raises(ContextOverflow):
            _run(gw.call(request))

    def test_llm_pool_gateway_other_errors_return_response(self):
        """HTTP 500 → 不 raise，返回 LLMResponse(stop_reason=error)。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        result = _run(gw.call(request))
        assert result.stop_reason == "error"
        assert "HTTP 500" in result.content


# ============================================================================
# T08-E: EventStore.replace_messages
# ============================================================================


class TestEventStoreReplaceMessages:
    """EventStore.replace_messages：L2 压缩后持久化。"""

    def test_replace_messages_rewrites_visible(self, tmp_path):
        """replace_messages 删除原 visible 消息，重新插入新消息。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            # 插入 5 条原消息
            for i in range(5):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"original {i}", source="user",
                )))
            original = _run(store.load_messages("sess-1"))
            assert len(original) == 5

            # 替换为 2 条新消息
            new_msgs = [
                Message(role="system", content="COMPACTED HEAD", source="summary"),
                Message(role="user", content="tail msg", source="user"),
            ]
            _run(store.replace_messages("sess-1", new_msgs))

            # 重新加载
            result = _run(store.load_messages("sess-1"))
            assert len(result) == 2
            assert result[0].content == "COMPACTED HEAD"
            assert result[1].content == "tail msg"
            # seq 从 1 重新分配
            assert result[0].seq == 1
            assert result[1].seq == 2
        finally:
            store.close()

    def test_replace_messages_writes_context_compacted_event(self, tmp_path):
        """replace_messages 写 context_compacted 事件（审计用）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            for i in range(3):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"msg {i}", source="user",
                )))
            new_msgs = [Message(role="user", content="new", source="user")]
            _run(store.replace_messages("sess-1", new_msgs))

            events = _run(store.load_events("sess-1"))
            event_types = [e.type for e in events]
            assert "context_compacted" in event_types
            # 找到 context_compacted 事件
            cc_event = next(e for e in events if e.type == "context_compacted")
            assert cc_event.payload["original_count"] == 3
            assert cc_event.payload["new_count"] == 1
        finally:
            store.close()

    def test_replace_messages_updates_last_compaction_prompt_index(self, tmp_path):
        """replace_messages 自增 last_compaction_prompt_index。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            _run(store.append_message("sess-1", Message(
                role="user", content="msg", source="user",
            )))
            _run(store.replace_messages("sess-1", [
                Message(role="user", content="new", source="user"),
            ]))
            # 查 sessions 表
            row = store.conn.execute(
                "SELECT last_compaction_prompt_index FROM sessions WHERE id = ?",
                ("sess-1",),
            ).fetchone()
            assert row["last_compaction_prompt_index"] == 1
        finally:
            store.close()

    def test_replace_messages_preserves_invisible_synthetic(self, tmp_path):
        """replace_messages 只删 visible=1，保留 invisible 的 synthetic 消息。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            # 插入 visible 消息
            _run(store.append_message("sess-1", Message(
                role="user", content="visible msg", source="user", visible=True,
            )))
            # 插入 invisible synthetic 消息（直接插 DB 绕过 visible 默认值）
            store.conn.execute(
                "INSERT INTO messages(id, session_id, seq, role, content_json, source, visible, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("synth-1", "sess-1", 2, "synthetic", '"synthetic msg"', "synthetic", 0, 0.0),
            )
            store.conn.commit()

            # 替换 visible 消息
            _run(store.replace_messages("sess-1", [
                Message(role="user", content="new visible", source="user"),
            ]))

            # visible 消息只有 1 条新的
            visible = _run(store.load_messages("sess-1"))
            assert len(visible) == 1
            assert visible[0].content == "new visible"

            # invisible synthetic 仍在
            all_msgs = _run(store.load_messages("sess-1", include_invisible=True))
            # 1 new visible + 1 preserved synthetic = 2
            assert len(all_msgs) == 2
            sources = {m.source for m in all_msgs}
            assert "synthetic" in sources
        finally:
            store.close()


# ============================================================================
# T08-F: SessionRunner reactive_compact_retry（核心验收）
# ============================================================================


class TestReactiveCompactRetry:
    """SessionRunner reactive_compact_retry（v6-lite §3 W5）。

    验收路径：
    - 首次 413 → has_attempted_reactive_compact=True → compactor.compact → replace_messages → transition event → 重试
    - 重试成功 → outcome.status=completed
    - 二次 413 → fail（防死循环）
    """

    def test_first_overflow_then_compact_then_success(self, tmp_path):
        """首次 413 → 压缩 → 重试成功。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            # 插入足够多的消息让 compactor 可以压缩（> tail_keep + 1）
            for i in range(10):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"long msg {i} " * 50, source="user",
                )))
                _run(store.append_message("sess-1", Message(
                    role="assistant", content=f"resp {i} " * 50, source="assistant",
                )))

            # ScriptedLLM：第1次 overflow → 第2次返回 summary → 第3次返回正常文本
            llm = _ScriptedCompactorLLM(summary_text="SUMMARY", final_text="final answer")
            compactor = Compactor(max_context_tokens=1000, tail_keep=4)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert outcome.stop_reason == "end_turn"
            assert runner.has_attempted_reactive_compact is True
            assert llm.call_count == 3  # overflow + summary + final
            # 最后一次返回的内容是 "final answer"
            last_msg = _run(store.load_messages("sess-1"))[-1]
            assert last_msg.content == "final answer"
            assert last_msg.role == "assistant"
        finally:
            store.close()

    def test_first_overflow_writes_transition_event(self, tmp_path):
        """首次 413 → 写 transition(reactive_compact_retry) 事件。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            for i in range(10):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"msg {i} " * 50, source="user",
                )))
                _run(store.append_message("sess-1", Message(
                    role="assistant", content=f"resp {i} " * 50, source="assistant",
                )))

            llm = _ScriptedCompactorLLM(summary_text="SUMMARY", final_text="ok")
            compactor = Compactor(max_context_tokens=1000, tail_keep=4)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            outcome = _run(runner.run())
            assert outcome.status == "completed"

            # 验证 transition event 写入
            events = _run(store.load_events("sess-1"))
            transition_events = [
                e for e in events
                if e.type == "transition" and e.payload.get("reason") == TRANSITION_REACTIVE_COMPACT_RETRY
            ]
            assert len(transition_events) == 1
            payload = transition_events[0].payload
            assert "original_messages_count" in payload
            assert "compacted_messages_count" in payload
            assert payload["compacted_messages_count"] < payload["original_messages_count"]
            assert runner.last_transition_reason == TRANSITION_REACTIVE_COMPACT_RETRY
        finally:
            store.close()

    def test_second_overflow_fails_no_loop(self, tmp_path):
        """二次 413 → fail（防死循环硬规则，v6-02 §2.1）。

        构造：LLM 每次 call 都 overflow（包括 compactor 调用）。
        - 第 1 次（runner 主循环）→ overflow → has_attempted=True → compactor.compact
        - compactor.compact 调 LLM → 也 overflow → compactor fail-open（返回原 messages）
        - runner 检测 compacted is messages → compact_noop fail
        或：
        - 第 1 次 overflow → compact 成功（用 fallback summary）→ replace → 重试
        - 第 2 次（runner 重试）→ 仍 overflow → has_attempted=True → fail
        """
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            for i in range(10):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"msg {i} " * 50, source="user",
                )))
                _run(store.append_message("sess-1", Message(
                    role="assistant", content=f"resp {i} " * 50, source="assistant",
                )))

            # 用 _AlwaysOverflowCompactorLLM：所有 call 都 overflow
            # compactor.compact() 调 LLM → overflow → fail-open 返回原 messages
            # runner 检测 compacted is messages → compact_noop → fail
            llm = _AlwaysOverflowCompactorLLM()
            compactor = Compactor(max_context_tokens=1000, tail_keep=4)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "failed"
            # 停在 compact_noop（因为 compactor fail-open 后返回原 messages）
            assert outcome.stop_reason in ("compact_noop", "context_overflow_after_compact")
            assert runner.has_attempted_reactive_compact is True
        finally:
            store.close()

    def test_second_overflow_after_successful_compact_fails(self, tmp_path):
        """压缩成功后重试，重试时仍 413 → fail（context_overflow_after_compact）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            for i in range(10):
                _run(store.append_message("sess-1", Message(
                    role="user", content=f"msg {i} " * 50, source="user",
                )))
                _run(store.append_message("sess-1", Message(
                    role="assistant", content=f"resp {i} " * 50, source="assistant",
                )))

            # ScriptedLLM：
            # 1: overflow → trigger compact
            # 2: compact summary call → return summary
            # 3: retry → overflow again → has_attempted=True → fail
            call_count = [0]

            class _LLM:
                async def call(self, req):
                    call_count[0] += 1
                    if call_count[0] == 2:
                        # compactor 调用，返回 summary
                        return LLMResponse(
                            content="MOCK SUMMARY", stop_reason="end_turn", model="m",
                        )
                    # 1 和 3 都 overflow
                    raise ContextOverflow("overflow", status_code=413)

                async def stream(self, req):
                    """v6-lite-streaming-gui T02: stream() 兼容——call_count 共享。"""
                    call_count[0] += 1
                    if call_count[0] == 2:
                        yield {"type": SSE_EVENT_TEXT_DELTA, "delta": "MOCK SUMMARY"}
                        yield {"type": SSE_EVENT_DONE, "finish_reason": "stop"}
                        return
                    yield {"type": SSE_EVENT_CONTEXT_OVERFLOW, "error": "overflow", "status_code": 413}
                    yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}

            compactor = Compactor(max_context_tokens=1000, tail_keep=4)
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=_LLM(), event_store=store,
                    tool_executor=MockToolExecutor(),
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "failed"
            assert outcome.stop_reason == "context_overflow_after_compact"
            assert runner.has_attempted_reactive_compact is True
            # 写了 context_overflow_after_compact 事件
            events = _run(store.load_events("sess-1"))
            assert any(e.type == "context_overflow_after_compact" for e in events)
        finally:
            store.close()

    def test_overflow_without_compactor_fails(self, tmp_path):
        """无 compactor 注入 → 413 → 直接失败（context_overflow_no_compactor）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            _run(store.append_message("sess-1", Message(
                role="user", content="hi", source="user",
            )))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=_MockLLMThatRaises(raise_overflow=True),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                    compactor=None,  # 无 compactor
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "failed"
            assert outcome.stop_reason == "context_overflow_no_compactor"
            assert runner.has_attempted_reactive_compact is False  # 没到 set 那步
        finally:
            store.close()

    def test_flag_not_reset_across_runs(self, tmp_path):
        """has_attempted_reactive_compact 跨多次 run() 不重置（防死循环硬规则）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            _run(store.append_message("sess-1", Message(
                role="user", content="hi", source="user",
            )))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="ok"),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            # 模拟已尝试过 reactive_compact
            runner.has_attempted_reactive_compact = True

            _run(runner.run())

            # 入口没重置
            assert runner.has_attempted_reactive_compact is True
        finally:
            store.close()


# ============================================================================
# T08-G: LLMPoolGateway SSE stream（伪流式）
# ============================================================================


class TestSSEStream:
    """LLMPoolGateway.stream() SSE 事件解析（伪流式）。"""

    def test_stream_parses_text_delta_and_done(self):
        """正常流：text_delta 事件 + done 事件。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = iter([
            f'data: {json.dumps({"type": SSE_EVENT_TEXT_DELTA, "delta": "Hello"})}',
            "",
            f'data: {json.dumps({"type": SSE_EVENT_TEXT_DELTA, "delta": " world"})}',
            "",
            f'data: {json.dumps({"type": SSE_EVENT_USAGE, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})}',
            "",
            f'data: {json.dumps({"type": SSE_EVENT_DONE, "finish_reason": "end_turn"})}',
        ])
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        events = []
        async def _collect():
            async for ev in gw.stream(request):
                events.append(ev)
        _run(_collect())

        assert len(events) == 4
        assert events[0]["type"] == SSE_EVENT_TEXT_DELTA
        assert events[0]["delta"] == "Hello"
        assert events[1]["type"] == SSE_EVENT_TEXT_DELTA
        assert events[1]["delta"] == " world"
        assert events[2]["type"] == SSE_EVENT_USAGE
        assert events[3]["type"] == SSE_EVENT_DONE
        assert events[3]["finish_reason"] == "end_turn"

    def test_stream_http_error_yields_provider_error_and_done(self):
        """HTTP 错误 → provider_error + done 事件（不抛异常）。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        events = []
        async def _collect():
            async for ev in gw.stream(request):
                events.append(ev)
        _run(_collect())

        assert len(events) == 2
        assert events[0]["type"] == SSE_EVENT_PROVIDER_ERROR
        assert "HTTP 500" in events[0]["error"]
        assert events[1]["type"] == SSE_EVENT_DONE
        assert events[1]["finish_reason"] == "error"

    def test_stream_network_error_yields_provider_error_and_done(self):
        """网络异常 → provider_error + done 事件（不抛异常）。"""
        import requests as _requests
        gw = LLMPoolGateway(base_url="http://mock")
        gw._session = MagicMock()
        gw._session.post.side_effect = _requests.exceptions.Timeout("timeout")

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        events = []
        async def _collect():
            async for ev in gw.stream(request):
                events.append(ev)
        _run(_collect())

        assert len(events) == 2
        assert events[0]["type"] == SSE_EVENT_PROVIDER_ERROR
        assert "timed out" in events[0]["error"]
        assert events[1]["type"] == SSE_EVENT_DONE
        assert events[1]["finish_reason"] == "error"

    def test_stream_skips_unparseable_lines(self):
        """坏 JSON 行被跳过（不抛异常）。"""
        gw = LLMPoolGateway(base_url="http://mock")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = iter([
            "data: not-json",
            f'data: {json.dumps({"type": SSE_EVENT_TEXT_DELTA, "delta": "ok"})}',
            "",
            "data: ",
            f'data: {json.dumps({"type": SSE_EVENT_DONE, "finish_reason": "end_turn"})}',
        ])
        gw._session = MagicMock()
        gw._session.post.return_value = mock_resp

        request = LLMRequest(messages=[Message(role="user", content="hi", source="user")])
        events = []
        async def _collect():
            async for ev in gw.stream(request):
                events.append(ev)
        _run(_collect())

        # 2 个有效事件：text_delta + done（坏 JSON 和空 data 被跳过）
        assert len(events) == 2
        assert events[0]["type"] == SSE_EVENT_TEXT_DELTA
        assert events[1]["type"] == SSE_EVENT_DONE


# ============================================================================
# T08-H: 包导入与导出
# ============================================================================


class TestExports:
    """T08 新增类型在 client.core.agent 包中可导入。"""

    def test_compactor_exported(self):
        from client.core.agent import Compactor as _C
        assert _C is Compactor

    def test_context_overflow_exported(self):
        from client.core.agent import ContextOverflow as _CO
        assert _CO is ContextOverflow

    def test_l0_artifact_store_exported(self):
        from client.core.agent import L0ArtifactStore as _L0
        assert _L0 is L0ArtifactStore

    def test_compactor_in_all(self):
        import client.core.agent as pkg
        assert "Compactor" in pkg.__all__
        assert "ContextOverflow" in pkg.__all__
        assert "L0ArtifactStore" in pkg.__all__
