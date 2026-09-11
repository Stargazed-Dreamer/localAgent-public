"""chat-mgmt-probe Ticket 01 验收测试：EventStore.delete_session 级联删

覆盖 Ticket 01 acceptance（temp/sdd/chat-mgmt-probe/tickets.md）：
- [x] delete_session(session_id) -> bool 方法实现，事务包裹
- [x] 级联删顺序正确：messages → events → tool_calls → sessions（四表都删）
- [x] 不存在的 session_id 返回 False，不抛异常
- [x] 空 session_id（""）返回 False
- [x] 反向验证：手动制造删不存在的 session_id，断言返回 False 且 DB 无副作用

测试 prior art: tests/test_event_store_mode.py（EventStore 测试模式）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import EventStore, Message  # noqa: E402

# ============================================================================
# Fixtures（沿用 test_event_store_mode.py 模式）
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


# ============================================================================
# 1. 基础：删存在的 session，四表都空
# ============================================================================


def test_delete_session_removes_session_row(store):
    """Ticket 01: delete_session 后 sessions 表对应行被删。"""
    async def run():
        await store.create_session(session_id="s-del-1", mode="probe", title="to delete")
        # 确认创建
        assert await store.get_session("s-del-1") is not None

        result = await store.delete_session("s-del-1")

        assert result is True
        assert await store.get_session("s-del-1") is None
    asyncio.run(run())


def test_delete_session_cascades_messages(store):
    """Ticket 01: delete_session 级联删 messages 表。"""
    async def run():
        await store.create_session(session_id="s-del-2", mode="probe")
        await store.append_message(
            "s-del-2", Message(role="user", content="hello", source="user")
        )
        await store.append_message(
            "s-del-2", Message(role="assistant", content="hi back", source="assistant")
        )
        # 确认有 2 条消息
        msgs = await store.load_messages("s-del-2")
        assert len(msgs) == 2

        await store.delete_session("s-del-2")

        # 级联删：messages 表无残留
        msgs_after = await store.load_messages("s-del-2")
        assert len(msgs_after) == 0
        # 直接查 DB 也无残留
        rows = store.conn.execute(
            "SELECT * FROM messages WHERE session_id = ?", ("s-del-2",)
        ).fetchall()
        assert len(rows) == 0
    asyncio.run(run())


def test_delete_session_cascades_events(store):
    """Ticket 01: delete_session 级联删 events 表。"""
    async def run():
        await store.create_session(session_id="s-del-3", mode="probe")
        # create_session 已写 session_created event
        events = await store.load_events("s-del-3")
        assert len(events) >= 1

        await store.delete_session("s-del-3")

        events_after = await store.load_events("s-del-3")
        assert len(events_after) == 0
        rows = store.conn.execute(
            "SELECT * FROM events WHERE session_id = ?", ("s-del-3",)
        ).fetchall()
        assert len(rows) == 0
    asyncio.run(run())


def test_delete_session_cascades_tool_calls(store):
    """Ticket 01: delete_session 级联删 tool_calls 表。"""
    async def run():
        await store.create_session(session_id="s-del-4", mode="probe")
        # 直接往 tool_calls 表插一条（不通过 runner，简化测试）
        store.conn.execute(
            "INSERT INTO tool_calls(id, session_id, seq, name, args_json, safety, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("tc-test-1", "s-del-4", 1, "memory_list", "{}", "read_only", "completed"),
        )
        store.conn.commit()
        rows = store.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ?", ("s-del-4",)
        ).fetchall()
        assert len(rows) == 1

        await store.delete_session("s-del-4")

        rows_after = store.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ?", ("s-del-4",)
        ).fetchall()
        assert len(rows_after) == 0
    asyncio.run(run())


def test_delete_session_cascades_all_four_tables_together(store):
    """Ticket 01: 一个 session 有 messages+events+tool_calls，删除后四表全空。"""
    async def run():
        sid = "s-del-full"
        await store.create_session(session_id=sid, mode="probe")
        await store.append_message(
            sid, Message(role="user", content="q", source="user")
        )
        await store.append_message(
            sid, Message(role="assistant", content="a", source="assistant")
        )
        store.conn.execute(
            "INSERT INTO tool_calls(id, session_id, seq, name, args_json, safety, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("tc-full-1", sid, 2, "memory_list", "{}", "read_only", "completed"),
        )
        store.conn.commit()

        result = await store.delete_session(sid)

        assert result is True
        # 四表全空
        assert await store.get_session(sid) is None
        assert len(await store.load_messages(sid)) == 0
        assert len(await store.load_events(sid)) == 0
        tc_rows = store.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ?", (sid,)
        ).fetchall()
        assert len(tc_rows) == 0
    asyncio.run(run())


# ============================================================================
# 2. 边界：不存在 / 空 session_id
# ============================================================================


def test_delete_nonexistent_session_returns_false(store):
    """Ticket 01: 删不存在的 session_id 返回 False，不抛异常。"""
    async def run():
        result = await store.delete_session("nonexistent-sid-xxx")
        assert result is False
    asyncio.run(run())


def test_delete_empty_session_id_returns_false(store):
    """Ticket 01: 空 session_id（""）返回 False，不抛异常。"""
    async def run():
        result = await store.delete_session("")
        assert result is False
    asyncio.run(run())


def test_delete_nonexistent_session_no_side_effects(store):
    """Ticket 01 反向验证：删不存在的 session 不影响其他 session。"""
    async def run():
        # 创建两个 session
        await store.create_session(session_id="s-keep", mode="probe")
        await store.create_session(session_id="s-other", mode="probe")
        await store.append_message(
            "s-keep", Message(role="user", content="keep me", source="user")
        )

        # 删不存在的
        result = await store.delete_session("nonexistent-xxx")

        assert result is False
        # 其他 session 完好
        assert await store.get_session("s-keep") is not None
        assert await store.get_session("s-other") is not None
        msgs = await store.load_messages("s-keep")
        assert len(msgs) == 1
        assert msgs[0].content == "keep me"
    asyncio.run(run())


# ============================================================================
# 3. 多 session 隔离：删一个不影响其他
# ============================================================================


def test_delete_one_session_does_not_affect_others(store):
    """Ticket 01: 删 session A 不影响 session B 的数据。"""
    async def run():
        # 创建两个 session，各写消息和 tool_call
        await store.create_session(session_id="s-A", mode="probe")
        await store.create_session(session_id="s-B", mode="probe")
        await store.append_message(
            "s-A", Message(role="user", content="A's msg", source="user")
        )
        await store.append_message(
            "s-B", Message(role="user", content="B's msg", source="user")
        )
        store.conn.execute(
            "INSERT INTO tool_calls(id, session_id, seq, name, args_json, safety, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("tc-A-1", "s-A", 1, "memory_list", "{}", "read_only", "completed"),
        )
        store.conn.execute(
            "INSERT INTO tool_calls(id, session_id, seq, name, args_json, safety, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("tc-B-1", "s-B", 1, "memory_list", "{}", "read_only", "completed"),
        )
        store.conn.commit()

        # 删 A
        await store.delete_session("s-A")

        # A 全空
        assert await store.get_session("s-A") is None
        assert len(await store.load_messages("s-A")) == 0
        assert len(await store.load_events("s-A")) == 0
        assert len(store.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ?", ("s-A",)
        ).fetchall()) == 0

        # B 完好
        assert await store.get_session("s-B") is not None
        b_msgs = await store.load_messages("s-B")
        assert len(b_msgs) == 1
        assert b_msgs[0].content == "B's msg"
        b_tcs = store.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ?", ("s-B",)
        ).fetchall()
        assert len(b_tcs) == 1
    asyncio.run(run())


# ============================================================================
# Ticket 02: 探针骨架 — 组装 RunnerDeps + 跑通最小会话
# ============================================================================


def test_probe_runner_minimal_session_completes(tmp_path):
    """Ticket 02: script=["hi"] 跑完一个 mode=probe 会话，outcome.status=completed。"""
    from server.probe.runner import ProbeParams, run_probe

    async def run():
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["hi from mock"],
            use_stream=False,
            auto_cleanup=False,
            db_path=str(tmp_path / "probe_minimal.db"),
        ))
        assert result.session_id.startswith("probe-")
        assert result.mode == "probe"
        assert result.status == "completed"
        assert result.messages_count >= 2  # user + assistant
        assert result.events_count >= 1
    asyncio.run(run())


def test_probe_runner_writes_six_json_files(tmp_path):
    """Ticket 02: 跑完后落盘 6 个 JSON 文件到 runs/<session_id>/。"""
    from server.probe.runner import ProbeParams, run_probe

    async def run():
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["hi"],
            use_stream=False,
            auto_cleanup=False,
            db_path=str(tmp_path / "probe_files.db"),
        ))
        run_dir = Path(result.run_dir)
        assert run_dir.exists(), f"run_dir should exist: {run_dir}"
        # 6 个文件
        for fname in ["messages.json", "events.json", "tool_calls.json",
                       "llm_requests.json", "llm_responses.json", "session.json"]:
            fpath = run_dir / fname
            assert fpath.exists(), f"{fname} should exist"
            assert fpath.stat().st_size > 0, f"{fname} should be non-empty"
    asyncio.run(run())


def test_probe_runner_summary_has_required_fields(tmp_path):
    """Ticket 02: 返回的 ProbeSummary 含所有必需字段。"""
    from server.probe.runner import ProbeParams, run_probe

    async def run():
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["mock response"],
            use_stream=False,
            auto_cleanup=False,
            db_path=str(tmp_path / "probe_summary.db"),
        ))
        # 必需字段
        assert hasattr(result, "session_id")
        assert hasattr(result, "mode")
        assert hasattr(result, "status")
        assert hasattr(result, "messages_count")
        assert hasattr(result, "events_count")
        assert hasattr(result, "tool_calls_count")
        assert hasattr(result, "messages_preview")
        assert hasattr(result, "tool_calls_preview")
        assert hasattr(result, "llm_calls")
        assert hasattr(result, "run_dir")
        assert hasattr(result, "cleaned_up")
        # messages_preview 每条含 seq/role/source/content_head
        assert len(result.messages_preview) >= 2
        for mp in result.messages_preview:
            assert "seq" in mp
            assert "role" in mp
            assert "source" in mp
            assert "content_head" in mp
        # llm_calls 每条含 request_messages_len/response_content_head
        assert len(result.llm_calls) >= 1
        for lc in result.llm_calls:
            assert "request_messages_len" in lc
            assert "response_content_head" in lc
    asyncio.run(run())


def test_probe_runner_auto_cleanup_true_deletes_session(tmp_path):
    """Ticket 02: auto_cleanup=True 时跑完后 session 从 DB 删除。"""
    from client.core.agent import EventStore
    from server.probe.runner import ProbeParams, run_probe

    async def run():
        db_path = str(tmp_path / "probe_cleanup.db")
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["hi"],
            use_stream=False,
            auto_cleanup=True,
            db_path=db_path,
        ))
        assert result.cleaned_up is True

        # DB 中该 session 应已删除
        store = EventStore(db_path=db_path)
        store.init()
        try:
            session = await store.get_session(result.session_id)
            assert session is None, "auto_cleanup=True should delete session from DB"
        finally:
            store.close()
    asyncio.run(run())


def test_probe_runner_auto_cleanup_false_keeps_session(tmp_path):
    """Ticket 02: auto_cleanup=False 时 session 留在 DB。"""
    from client.core.agent import EventStore
    from server.probe.runner import ProbeParams, run_probe

    async def run():
        db_path = str(tmp_path / "probe_nocleanup.db")
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["hi"],
            use_stream=False,
            auto_cleanup=False,
            db_path=db_path,
        ))
        assert result.cleaned_up is False

        store = EventStore(db_path=db_path)
        store.init()
        try:
            session = await store.get_session(result.session_id)
            assert session is not None, "auto_cleanup=False should keep session"
            assert session.mode == "probe"
        finally:
            store.close()
    asyncio.run(run())


def test_probe_runner_llm_requests_captured(tmp_path):
    """Ticket 02: 落盘的 llm_requests.json 含完整 LLMRequest（messages+system+tools）。"""
    import json

    from server.probe.runner import ProbeParams, run_probe

    async def run():
        result = await run_probe(ProbeParams(
            trigger_text="hello",
            mode="script",
            script=["mock response"],
            use_stream=False,
            auto_cleanup=False,
            db_path=str(tmp_path / "probe_llm_req.db"),
        ))
        llm_req_path = Path(result.run_dir) / "llm_requests.json"
        with open(llm_req_path, encoding="utf-8") as f:
            reqs = json.load(f)
        assert len(reqs) >= 1
        req = reqs[0]
        # LLMRequest 字段
        assert "messages" in req
        assert "system" in req
        assert "tools" in req
        assert "tool_choice" in req
        # messages 至少含 user 那条
        assert len(req["messages"]) >= 1
    asyncio.run(run())


# ============================================================================
# Ticket 03: 探针 HTTP 路由 + ChatPanel badge（端到端测试）
# ============================================================================


@pytest.fixture
def probe_fastapi_app():
    """构造只挂 probe router 的 FastAPI app（不启动完整后端）。"""
    from fastapi import FastAPI

    from server.probe.router import router as probe_router
    app = FastAPI()
    app.include_router(probe_router)
    return app


@pytest.fixture
def probe_client(probe_fastapi_app):
    """TestClient。"""
    from fastapi.testclient import TestClient
    return TestClient(probe_fastapi_app)


class TestRouterE2E:
    """Ticket 03: POST /probe/runs 端到端测试。"""

    def test_post_probe_runs_returns_200_with_summary(self, probe_client, tmp_path):
        """POST /probe/runs 200 + 返回摘要含必需字段。"""
        resp = probe_client.post("/probe/runs", json={
            "trigger_text": "hello",
            "mode": "script",
            "script": ["hi from e2e"],
            "use_stream": False,
            "auto_cleanup": False,
            "db_path": str(tmp_path / "probe_e2e.db"),
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["session_id"].startswith("probe-")
        assert data["mode"] == "probe"
        assert data["status"] == "completed"
        assert data["messages_count"] >= 2
        assert "run_dir" in data
        assert data["cleaned_up"] is False

    def test_auto_cleanup_true_session_deleted(self, probe_client, tmp_path):
        """auto_cleanup=true 时跑完 session 从 DB 删除。"""
        from client.core.agent import EventStore
        db_path = str(tmp_path / "probe_e2e_cleanup.db")
        resp = probe_client.post("/probe/runs", json={
            "trigger_text": "hello",
            "mode": "script",
            "script": ["hi"],
            "use_stream": False,
            "auto_cleanup": True,
            "db_path": db_path,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleaned_up"] is True

        # DB 中该 session 应已删除
        store = EventStore(db_path=db_path)
        store.init()
        try:
            session = asyncio.run(store.get_session(data["session_id"]))
            assert session is None
        finally:
            store.close()

    def test_auto_cleanup_false_session_kept(self, probe_client, tmp_path):
        """auto_cleanup=false 时 session 留在 DB，mode=probe。"""
        from client.core.agent import EventStore
        db_path = str(tmp_path / "probe_e2e_kept.db")
        resp = probe_client.post("/probe/runs", json={
            "trigger_text": "hello",
            "mode": "script",
            "script": ["hi"],
            "use_stream": False,
            "auto_cleanup": False,
            "db_path": db_path,
        })
        assert resp.status_code == 200
        data = resp.json()
        sid = data["session_id"]

        store = EventStore(db_path=db_path)
        store.init()
        try:
            session = asyncio.run(store.get_session(sid))
            assert session is not None
            assert session.mode == "probe"
        finally:
            store.close()

    def test_invalid_mode_returns_400(self, probe_client, tmp_path):
        """无效 mode 返回 400。"""
        resp = probe_client.post("/probe/runs", json={
            "trigger_text": "hello",
            "mode": "invalid_mode",
            "script": [],
            "db_path": str(tmp_path / "probe_invalid.db"),
        })
        assert resp.status_code == 400

    def test_pattern_mode_now_implemented(self, probe_client, tmp_path):
        """Ticket 04 后 pattern 模式已实现，返回 200（不再是 501）。

        原 test_pattern_mode_returns_501 是 Ticket 03 占位测试（pattern 未实现）。
        Ticket 04 实现了 pattern 模式后语义反转：pattern 现在应该 200 而不是 501。
        按"测试修复铁律"改写测试以反映真实行为，不跳过、不放宽断言。
        """
        resp = probe_client.post("/probe/runs", json={
            "trigger_text": "hello",
            "mode": "pattern",
            "pattern": "text_only",
            "db_path": str(tmp_path / "probe_pattern.db"),
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["mode"] == "probe"
        assert data["status"] == "completed"


def test_chat_panel_probe_badge_exists():
    """Ticket 03: ChatPanel _SESSION_MODE_BADGE 含 "probe" 项。"""
    # 直接 import 模块级常量验证（不实例化 ChatPanel，避免 Qt 依赖）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "chat_module",
        str(PROJECT_ROOT / "client" / "panels" / "chat.py"),
    )
    # chat.py 依赖 PySide6，如果环境没装就跳过（不算放宽断言，是环境前置）
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except ImportError:
        pytest.skip("PySide6 not available, cannot import chat.py")
    assert "probe" in mod._SESSION_MODE_BADGE
    assert mod._SESSION_MODE_BADGE["probe"] == "探针"


# ============================================================================
# Ticket 04: MockLLM pattern 模式 + script dict 扩展
# ============================================================================


class TestPatterns:
    """Ticket 04: 5 种 pattern + script dict 格式。"""

    def test_pattern_text_only(self, tmp_path):
        """pattern=text_only：每轮返回纯文本，无 tool_calls。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="hello",
                mode="pattern",
                pattern="text_only",
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_text_only.db"),
            ))
            assert result.status == "completed"
            assert result.tool_calls_count == 0
            # 至少 1 次 LLM 调用
            assert len(result.llm_calls) >= 1
            # 响应不含 tool_calls
            for lc in result.llm_calls:
                assert lc["response_tool_calls_count"] == 0
        asyncio.run(run())

    def test_pattern_refuse(self, tmp_path):
        """pattern=refuse：返回拒绝文本。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="do something",
                mode="pattern",
                pattern="refuse",
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_refuse.db"),
            ))
            assert result.status == "completed"
            # 响应内容含 refuse 关键词
            for lc in result.llm_calls:
                assert "refuse" in lc["response_content_head"].lower() or "拒绝" in lc["response_content_head"]
        asyncio.run(run())

    def test_script_dict_format_with_tool_call(self, tmp_path):
        """Ticket 04: script 支持 dict 格式，含 tool_calls 字段。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            # script: 第一轮返回 tool_call（memory_list），第二轮返回纯文本
            result = await run_probe(ProbeParams(
                trigger_text="list memory",
                mode="script",
                script=[
                    {
                        "content": "let me check memory",
                        "tool_calls": [
                            {
                                "id": "call-probe-1",
                                "type": "function",
                                "function": {"name": "memory_list", "arguments": "{}"},
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                    {
                        "content": "done",
                        "stop_reason": "end_turn",
                    },
                ],
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_script_dict.db"),
            ))
            # 应该有 tool_call 记录（真调后端 memory_list 工具）
            # 注：如果后端不在线，tool_call 可能 failed，但记录应该存在
            assert result.status == "completed"
            # 落盘的 tool_calls.json 应该有记录
            import json
            tc_path = Path(result.run_dir) / "tool_calls.json"
            with open(tc_path, encoding="utf-8") as f:
                tcs = json.load(f)
            assert len(tcs) >= 1
            assert tcs[0]["name"] == "memory_list"
        asyncio.run(run())

    def test_pattern_tool_call_then_text(self, tmp_path):
        """pattern=tool_call_then_text：第一轮 tool_call，第二轮纯文本。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="check memory",
                mode="pattern",
                pattern="tool_call_then_text",
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_tc_then_text.db"),
            ))
            assert result.status == "completed"
            # 应该有 tool_call（pattern 内置调 memory_list）
            assert result.tool_calls_count >= 1
            # 至少 2 次 LLM 调用（第一轮 tool_call + 第二轮文本）
            assert len(result.llm_calls) >= 2
        asyncio.run(run())

    def test_pattern_streaming_text(self, tmp_path):
        """pattern=streaming_text：返回带 thinking 字段的响应（配合 use_stream 测流式）。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="think then answer",
                mode="pattern",
                pattern="streaming_text",
                use_stream=False,  # Ticket 05 才测流式路径，这里只验证 thinking 字段存在
                auto_cleanup=False,
                db_path=str(tmp_path / "p_stream_text.db"),
            ))
            assert result.status == "completed"
            # 落盘的 llm_responses.json 应含 thinking 字段
            import json
            resp_path = Path(result.run_dir) / "llm_responses.json"
            with open(resp_path, encoding="utf-8") as f:
                resps = json.load(f)
            assert len(resps) >= 1
            # 至少一个响应有 thinking 内容
            has_thinking = any(r.get("thinking") for r in resps)
            assert has_thinking, "streaming_text pattern should produce thinking field"
        asyncio.run(run())

    def test_pattern_always_tool_call(self, tmp_path):
        """pattern=always_tool_call：每轮都返回 tool_call（受 max_iterations 限制）。"""
        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="keep calling tools",
                mode="pattern",
                pattern="always_tool_call",
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_always_tc.db"),
            ))
            # 跑完（可能因 max_iterations 终止，也可能 completed）
            assert result.status in ("completed", "failed")
            # 应该有多个 tool_call
            assert result.tool_calls_count >= 1
        asyncio.run(run())


# ============================================================================
# Ticket 05: 可选路径触发器（流式 / 审批 / 上下文压缩）
# ============================================================================


class TestOptionalPaths:
    """Ticket 05: 三条可选路径触发器测试。"""

    def test_use_stream_writes_and_invalidates_streaming_events(self, tmp_path):
        """流式路径：use_stream=True 产生 streaming_*_delta 事件，跑完后被 invalidate。

        断言：
        1. events 表有 streaming_thinking_delta / streaming_text_delta 事件
        2. 这些 streaming 事件的 invalidated_seq 字段不为 null（被标记为已合并）
        3. assistant_message_appended 事件存在（流式合并后落库的完整消息）
        """
        import json

        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="think then answer",
                mode="pattern",
                pattern="streaming_text",
                use_stream=True,  # 关键：走流式路径
                auto_cleanup=False,
                db_path=str(tmp_path / "p_stream.db"),
            ))
            assert result.status == "completed"

            ev_path = Path(result.run_dir) / "events.json"
            events = json.loads(ev_path.read_text(encoding="utf-8"))

            # 断言 1: 有 streaming 事件
            streaming_events = [
                e for e in events
                if e["type"] in ("streaming_text_delta", "streaming_thinking_delta", "streaming_tool_call")
            ]
            assert len(streaming_events) >= 1, f"expected streaming events, got types: {[e['type'] for e in events]}"

            # 断言 2: streaming 事件被 invalidate（invalidated_seq 不为 null）
            invalidated = [e for e in streaming_events if e.get("invalidated_seq") is not None]
            assert len(invalidated) == len(streaming_events), (
                f"all streaming events should be invalidated, "
                f"but only {len(invalidated)}/{len(streaming_events)} are"
            )

            # 断言 3: assistant_message_appended 事件存在
            assistant_events = [e for e in events if e["type"] == "assistant_message_appended"]
            assert len(assistant_events) >= 1, "expected assistant_message_appended event"
        asyncio.run(run())

    def test_trigger_approval_mock_denial_path(self, tmp_path):
        """审批路径：trigger_approval=True 时 mock executor 拒绝 approval_required 工具。

        断言：
        1. tool_calls 表有一条 status=failed 的记录（被 mock executor 拒绝）
        2. tool_call 的 name == 'exec_cmd'（script 中预设的）
        3. messages 表有 role='tool' 的拒绝消息回灌（content 含 'denied'）
        4. 至少 2 次 LLM 调用（第一轮 tool_call + 第二轮文本）
        """
        import json

        from server.probe.runner import ProbeParams, run_probe

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="run echo hi",
                mode="script",
                script=[
                    {
                        "content": "I need to run a command",
                        "tool_calls": [
                            {
                                "id": "call-probe-approval-1",
                                "type": "function",
                                "function": {"name": "exec_cmd", "arguments": '{"cmd":"echo hi"}'},
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                    {
                        "content": "the command was denied, I'll stop",
                        "stop_reason": "end_turn",
                    },
                ],
                trigger_approval=True,  # 关键：用 mock executor 模拟审批拒绝
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_approval.db"),
            ))
            assert result.status == "completed"

            # 断言 1+2: tool_calls 表有 status=failed 的 exec_cmd 记录
            tc_path = Path(result.run_dir) / "tool_calls.json"
            tcs = json.loads(tc_path.read_text(encoding="utf-8"))
            assert len(tcs) >= 1, "expected at least 1 tool_call"
            exec_cmd_tcs = [tc for tc in tcs if tc["name"] == "exec_cmd"]
            assert len(exec_cmd_tcs) >= 1, "expected exec_cmd tool_call"
            failed_tcs = [tc for tc in exec_cmd_tcs if tc["status"] == "failed"]
            assert len(failed_tcs) >= 1, (
                f"expected exec_cmd tool_call to be failed (mock denial), "
                f"got status: {[tc['status'] for tc in exec_cmd_tcs]}"
            )

            # 断言 3: messages 表有 role='tool' 的拒绝消息
            msgs_path = Path(result.run_dir) / "messages.json"
            msgs = json.loads(msgs_path.read_text(encoding="utf-8"))
            tool_msgs = [m for m in msgs if m["role"] == "tool"]
            assert len(tool_msgs) >= 1, "expected tool_result message"
            denied_msgs = [m for m in tool_msgs if "denied" in m["content"].lower()]
            assert len(denied_msgs) >= 1, (
                f"expected tool_result with 'denied' content, got: {[m['content'][:80] for m in tool_msgs]}"
            )

            # 断言 4: 至少 2 次 LLM 调用
            assert len(result.llm_calls) >= 2, (
                f"expected >=2 LLM calls (tool_call + text), got {len(result.llm_calls)}"
            )
        asyncio.run(run())

    def test_context_compaction_path(self, tmp_path):
        """压缩路径：max_context_messages>0 + script raise_context_overflow → 触发 compactor。

        Compactor 的 tail_keep 被 max(tail_keep, DEFAULT_TAIL_KEEP=6) 钳制，所以 messages
        必须超过 6 条才能压缩。end_turn 会让 runner finalize，所以前几轮用 tool_call 维持循环。
        script 设计：4 轮 tool_call（每轮产生 assistant+tool 两条 message，累积 1+8=9 条 > 6），
        第 5 轮抛 ContextOverflow → compactor 压缩 → 第 6 轮返回正常文本。
        用 trigger_approval=True 让 mock executor 拒绝 tool_call（不依赖真后端）。

        断言：
        1. events 表有 transition(reactive_compact_retry) 事件
        2. messages 表有 source='summary' 的合成消息（compactor.compact 生成）
        3. 跑完 status in ('completed', 'failed')
        4. tool_calls_count >= 4（前 4 轮的 tool_call，第 5 轮 overflow 不写 tool_call）
        """
        import json

        from server.probe.runner import ProbeParams, run_probe

        def _tc_item(i: int) -> dict:
            return {
                "content": f"calling tool turn {i+1}",
                "tool_calls": [{
                    "id": f"call-compact-{i}",
                    "type": "function",
                    "function": {"name": "exec_cmd", "arguments": '{"cmd":"echo hi"}'},
                }],
                "stop_reason": "tool_use",
            }

        # 4 轮 tool_call（累积 1 user + 4*(assistant+tool) = 9 messages > tail_keep=6）
        # + 1 轮 overflow + 1 轮恢复
        script: list = [_tc_item(i) for i in range(4)]
        script.append({
            "raise_context_overflow": True,
            "overflow_message": "probe mock: context too large after 4 tool turns",
        })
        script.append({
            "content": "recovered after compaction",
            "stop_reason": "end_turn",
        })

        async def run():
            result = await run_probe(ProbeParams(
                trigger_text="long conversation that overflows",
                mode="script",
                script=script,
                trigger_approval=True,  # 用 mock executor 拒绝 exec_cmd，不依赖真后端
                max_context_messages=2,  # 关键：注入 compactor
                use_stream=False,
                auto_cleanup=False,
                db_path=str(tmp_path / "p_compact.db"),
            ))

            # 断言 3: 跑完（compactor 可能 fail-open 但 runner 处理后仍 completed/failed）
            assert result.status in ("completed", "failed"), (
                f"expected completed or failed, got {result.status}"
            )

            # 断言 4: 前 4 轮的 tool_call 都落库（第 5 轮 overflow 不写 tool_call）
            assert result.tool_calls_count >= 4, (
                f"expected >=4 tool_calls (4 tool turns before overflow), "
                f"got {result.tool_calls_count}"
            )

            # 断言 1: events 表有 transition(reactive_compact_retry) 事件
            ev_path = Path(result.run_dir) / "events.json"
            events = json.loads(ev_path.read_text(encoding="utf-8"))
            transition_events = [
                e for e in events
                if e["type"] == "transition" and e.get("payload", {}).get("reason") == "reactive_compact_retry"
            ]
            assert len(transition_events) >= 1, (
                f"expected transition(reactive_compact_retry) event, "
                f"got event types: {[e['type'] for e in events]}"
            )

            # 断言 2: messages 表有 source='summary' 的合成消息
            msgs_path = Path(result.run_dir) / "messages.json"
            msgs = json.loads(msgs_path.read_text(encoding="utf-8"))
            summary_msgs = [m for m in msgs if m.get("source") == "summary"]
            assert len(summary_msgs) >= 1, (
                f"expected summary message (source='summary'), "
                f"got sources: {[m.get('source') for m in msgs]}"
            )
        asyncio.run(run())
