"""Ticket 32：agent_guide 录制包消费层路由测试。

验证：
- GUIDE_REGISTRY 新增 recording.consume 和 recording.discover 条目
- recording.consume 不预设 routes_to（agent 自主决定后续路由，D055 变更）
- recording.discover 的 first_action 指向 list_recordings()
- agent_guide(task="看一下录制") 返回 recording.consume 或 recording.discover 路由
- agent_guide(task="发现录制") 返回 recording.discover 路由
"""

from __future__ import annotations

from server.agent_guide import GUIDE_REGISTRY, _build_task_guide


class TestRecordingDiscoverEntry:
    """recording.discover 条目结构验证。"""

    def test_entry_exists(self):
        assert "recording.discover" in GUIDE_REGISTRY

    def test_entry_has_required_fields(self):
        entry = GUIDE_REGISTRY["recording.discover"]
        required = {
            "skill", "name", "skill_file", "keywords", "description",
            "memory_key", "first_action", "workflow_summary",
            "mcp_tools_priority", "key_pitfalls", "level",
        }
        assert required.issubset(entry.keys()), (
            f"缺失字段: {required - set(entry.keys())}"
        )

    def test_first_action_mentions_list_recordings(self):
        """first_action 应指向 list_recordings()（D055 变更：移除后端端点）。"""
        entry = GUIDE_REGISTRY["recording.discover"]
        assert "list_recordings" in entry["first_action"]
        # 不应再引用 HTTP 端点
        assert "/recordings/list" not in entry["first_action"]

    def test_memory_key_is_none(self):
        entry = GUIDE_REGISTRY["recording.discover"]
        assert entry["memory_key"] is None

    def test_keywords_include_recording(self):
        entry = GUIDE_REGISTRY["recording.discover"]
        assert any("录制" in kw for kw in entry["keywords"])


class TestRecordingConsumeEntry:
    """recording.consume 条目结构验证。"""

    def test_entry_exists(self):
        assert "recording.consume" in GUIDE_REGISTRY

    def test_entry_has_required_fields(self):
        entry = GUIDE_REGISTRY["recording.consume"]
        required = {
            "skill", "name", "skill_file", "keywords", "description",
            "memory_key", "first_action", "workflow_summary",
            "mcp_tools_priority", "key_pitfalls", "level",
        }
        assert required.issubset(entry.keys()), (
            f"缺失字段: {required - set(entry.keys())}"
        )

    def test_no_routes_to_field(self):
        """recording.consume 不应预设 routes_to（D055 变更：agent 自主决定）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        assert "routes_to" not in entry, (
            "recording.consume 不应预设 routes_to（agent 自主决定后续路由）"
        )

    def test_first_action_mentions_get_merged_view(self):
        """first_action 应指向 get_merged_view（D055 变更：移除后端端点）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        assert "get_merged_view" in entry["first_action"]
        # 不应再引用 HTTP 端点
        assert "/recordings/" not in entry["first_action"]

    def test_first_action_mentions_agent_autonomous(self):
        """first_action 应提示 agent 自主决定后续（不预设路由）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        assert "自主" in entry["first_action"]

    def test_first_action_mentions_vl_protocol(self):
        """first_action 应描述多轮 VL 协议（select_vl_candidates / build_vl_question）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        assert "select_vl_candidates" in entry["first_action"]
        assert "build_vl_question" in entry["first_action"]
        assert "log_consumption" in entry["first_action"]

    def test_key_pitfalls_mention_d005_and_d054(self):
        """key_pitfalls 应含 D005（不预存 VL 结果）和 D054（VL 统计预警）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        pitfalls_text = " ".join(entry["key_pitfalls"])
        assert "D005" in pitfalls_text
        assert "D054" in pitfalls_text

    def test_mcp_tools_priority_includes_vl(self):
        """mcp_tools_priority 应含 understand_image（VL 看图工具）。"""
        entry = GUIDE_REGISTRY["recording.consume"]
        tools_text = " ".join(entry["mcp_tools_priority"])
        assert "understand_image" in tools_text

    def test_memory_key_is_none(self):
        entry = GUIDE_REGISTRY["recording.consume"]
        assert entry["memory_key"] is None


class TestBuildTaskGuideForRecording:
    """_build_task_guide 对录制条目的处理。"""

    def test_build_discover_returns_basic_fields(self):
        result = _build_task_guide("recording.discover")
        assert result["task_type"] == "recording.discover"
        assert result["matched_skill"] == "recording"
        assert "list_recordings" in result["first_action"]
        # discover 不应有 routes_to
        assert "routes_to" not in result

    def test_build_consume_does_not_return_routes_to(self):
        """_build_task_guide 对 recording.consume 不应返回 routes_to（D055 变更）。"""
        result = _build_task_guide("recording.consume")
        assert "routes_to" not in result, (
            "recording.consume 不应返回 routes_to（agent 自主决定后续路由）"
        )

    def test_build_consume_returns_correct_skill(self):
        result = _build_task_guide("recording.consume")
        assert result["matched_skill"] == "recording"
        assert result["task_type"] == "recording.consume"


class TestAgentGuideHttpRoute:
    """通过 HTTP /guide 端点验证录制包路由。"""

    def test_guide_with_recording_keyword_returns_recording_route(self, client):
        """agent_guide(task='看一下录制') 应匹配到 recording.* 路由。"""
        resp = client.get("/guide", params={"task": "看一下录制"})
        assert resp.status_code == 200
        data = resp.json()
        # 应该匹配到 recording.discover 或 recording.consume
        if data.get("mode") == "task":
            task_type = data.get("task_type", "")
            assert task_type.startswith("recording."), (
                f"应匹配 recording.* 路由，实际: {task_type}"
            )
        else:
            # general 模式时检查 candidates
            candidates = data.get("candidates", [])
            recording_candidates = [
                c for c in candidates if c.get("task_type", "").startswith("recording.")
            ]
            assert recording_candidates, (
                f"candidates 中应含 recording.* 条目，实际 candidates: "
                f"{[c.get('task_type') for c in candidates]}"
            )

    def test_guide_with_discover_keyword(self, client):
        """agent_guide(task='发现录制') 应匹配到 recording.discover。"""
        resp = client.get("/guide", params={"task": "发现录制"})
        assert resp.status_code == 200
        data = resp.json()
        if data.get("mode") == "task":
            assert data["task_type"] == "recording.discover"
        else:
            candidates = data.get("candidates", [])
            task_types = [c.get("task_type") for c in candidates]
            assert "recording.discover" in task_types, (
                f"candidates 应含 recording.discover，实际: {task_types}"
            )

    def test_guide_with_consume_keyword(self, client):
        """agent_guide(task='分析录制') 应匹配到 recording.consume。"""
        resp = client.get("/guide", params={"task": "分析录制"})
        assert resp.status_code == 200
        data = resp.json()
        if data.get("mode") == "task":
            assert data["task_type"] == "recording.consume"
        else:
            candidates = data.get("candidates", [])
            task_types = [c.get("task_type") for c in candidates]
            assert "recording.consume" in task_types, (
                f"candidates 应含 recording.consume，实际: {task_types}"
            )

    def test_guide_with_task_type_param(self, client):
        """agent_guide(task_type='recording.consume') 直接命中。"""
        resp = client.get("/guide", params={"task_type": "recording.consume"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "task"
        assert data["task_type"] == "recording.consume"
        # D055 变更：不预设 routes_to
        assert "routes_to" not in data
