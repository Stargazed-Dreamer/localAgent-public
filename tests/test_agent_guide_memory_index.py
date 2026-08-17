"""测试 agent_guide 的 memory_index 和 memory_generation_workflow 字段。

覆盖：
- recurring.memory_generation 条目存在且含 workflow
- _build_task_guide 返回 memory_index 字段
- _build_task_guide 返回 memory_generation_workflow 和 do_not_store
- system.task_reminder 的 first_action 指向 todos_due
- system.task_closure 条目存在且含 6 步 workflow
- GENERAL_GUIDE 含 session_closure 字段
"""


from server.agent_guide import GENERAL_GUIDE, GUIDE_REGISTRY, _build_task_guide


class TestMemoryGenerationEntry:
    def test_entry_exists(self):
        assert "recurring.memory_generation" in GUIDE_REGISTRY

    def test_entry_has_workflow(self):
        entry = GUIDE_REGISTRY["recurring.memory_generation"]
        assert "memory_generation_workflow" in entry
        wf = entry["memory_generation_workflow"]
        assert "step_1_recall" in wf
        assert "step_2_classify" in wf
        assert "step_3_dedup" in wf
        assert "step_4_write" in wf
        assert "step_5_optional_maintain" in wf

    def test_entry_has_do_not_store(self):
        entry = GUIDE_REGISTRY["recurring.memory_generation"]
        assert "do_not_store" in entry
        assert len(entry["do_not_store"]) > 0

    def test_entry_memory_key_is_none(self):
        entry = GUIDE_REGISTRY["recurring.memory_generation"]
        assert entry["memory_key"] is None


class TestBuildTaskGuide:
    def test_returns_memory_index_field(self):
        result = _build_task_guide("recurring.accounting")
        assert "memory_index" in result
        assert isinstance(result["memory_index"], list)

    def test_returns_memory_generation_workflow(self):
        result = _build_task_guide("recurring.memory_generation")
        assert "memory_generation_workflow" in result
        assert "step_1_recall" in result["memory_generation_workflow"]

    def test_returns_do_not_store(self):
        result = _build_task_guide("recurring.memory_generation")
        assert "do_not_store" in result
        assert len(result["do_not_store"]) > 0

    def test_memory_index_empty_for_none_key(self, monkeypatch):
        # 隔离数据库：mock MemoryManager 不可用，确保动态关联（consumption_contexts
        # 反向查询）不注入记忆。memory_key=None 时静态关联本就为空，
        # 动态关联受数据库内容影响，单独测试无意义，此处只验证静态行为。
        monkeypatch.setattr("server.memory.manager.get_memory_manager", lambda: None)
        result = _build_task_guide("recurring.memory_generation")
        assert result["memory_index"] == []


class TestTaskReminderUpdated:
    def test_first_action_mentions_todos_due(self):
        result = _build_task_guide("system.task_reminder")
        assert "todos_due" in result["first_action"]

    def test_memory_key_is_none(self):
        result = _build_task_guide("system.task_reminder")
        assert result["memory_key"] is None

    def test_mcp_tools_include_todos_due(self):
        # mcp_tools_priority 改为 include_workflow=true 才返（默认不返避免匹配错误时白返）
        result = _build_task_guide("system.task_reminder", include_workflow=True)
        assert "todos_due" in result["mcp_tools_priority"]
        # 默认调用不应返 mcp_tools_priority
        result_default = _build_task_guide("system.task_reminder")
        assert "mcp_tools_priority" not in result_default


class TestWipTrackingUpdated:
    def test_first_action_mentions_wip_list(self):
        result = _build_task_guide("system.wip_tracking")
        assert "wip_list" in result["first_action"]

    def test_memory_key_is_none(self):
        result = _build_task_guide("system.wip_tracking")
        assert result["memory_key"] is None


class TestTaskClosureEntry:
    def test_entry_exists(self):
        assert "system.task_closure" in GUIDE_REGISTRY

    def test_entry_has_workflow(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        assert "task_closure_workflow" in entry
        wf = entry["task_closure_workflow"]
        assert "step_1_assess" in wf
        assert "step_2_wip" in wf
        assert "step_3_extract" in wf
        assert "step_4_dedup_write" in wf
        assert "step_5_doc_sync" in wf
        assert "step_6_closure_report" in wf

    def test_entry_has_do_not_store(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        assert "do_not_store" in entry
        assert len(entry["do_not_store"]) > 0

    def test_entry_memory_key_is_none(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        assert entry["memory_key"] is None

    def test_build_task_guide_returns_workflow(self):
        result = _build_task_guide("system.task_closure")
        assert "task_closure_workflow" in result
        assert "step_1_assess" in result["task_closure_workflow"]

    def test_build_task_guide_returns_do_not_store(self):
        result = _build_task_guide("system.task_closure")
        assert "do_not_store" in result
        assert len(result["do_not_store"]) > 0

    def test_keywords_include_closure(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        assert "收尾" in entry["keywords"]
        assert "task closure" in entry["keywords"]

    def test_step3_mentions_consumption_contexts(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        wf = entry["task_closure_workflow"]
        instruction = wf["step_3_extract"]["instruction"]
        assert "consumption_contexts" in instruction
        assert "trigger_keywords" in instruction

    def test_step5_mentions_no_user_prompt(self):
        entry = GUIDE_REGISTRY["system.task_closure"]
        wf = entry["task_closure_workflow"]
        instruction = wf["step_5_doc_sync"]["instruction"]
        assert "不询问用户" in instruction or "自行对照" in instruction


class TestSessionClosure:
    def test_general_guide_has_session_closure(self):
        assert "session_closure" in GENERAL_GUIDE
        assert isinstance(GENERAL_GUIDE["session_closure"], list)
        assert len(GENERAL_GUIDE["session_closure"]) > 0

    def test_session_closure_mentions_task_closure(self):
        closure = GENERAL_GUIDE["session_closure"]
        text = " ".join(closure)
        assert "system.task_closure" in text or "task_closure" in text

    def test_session_startup_has_preferences_step(self):
        startup = GENERAL_GUIDE["session_startup"]
        text = " ".join(startup)
        assert "preferences" in text.lower()

    def test_session_closure_mentions_consumption_contexts(self):
        closure = GENERAL_GUIDE["session_closure"]
        text = " ".join(closure)
        assert "consumption_contexts" in text or "trigger_keywords" in text
