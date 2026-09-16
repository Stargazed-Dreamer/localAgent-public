"""测试 MemoryManager.find_consumable_memories 和 agent_guide 的 first_action 注入。

覆盖：
- find_consumable_memories 按 consumption_contexts 精确匹配
- find_consumable_memories 按 "*" 通配匹配
- find_consumable_memories 按 "scope.*" 通配匹配
- find_consumable_memories 按 trigger_keywords 匹配
- find_consumable_memories 旧记忆无新字段时跳过（兼容性）
- find_consumable_memories 不增加 access_count
- _build_task_guide 在 memory_index 非空时注入"【必读记忆】"
- _build_task_guide 在 memory_index 为空时不注入
- memory_set + merge 保留 consumption_contexts 字段
"""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from server.memory.config import MemoryConfig
from server.memory.store import MemoryStore

# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()


@pytest.fixture
def manager(store):
    """创建一个轻量 MemoryManager，mock 掉 EmbeddingEngine 等重组件。

    find_consumable_memories 只依赖 self.store 和 self.config，不需要 embedding。
    MemoryManager 会创建自己的 MemoryStore（与 fixture 的 store 共享同一 db_path），
    teardown 时必须关闭 mgr.store 的连接，否则 Windows 上 tmp_db 文件无法删除。
    """
    cfg = MemoryConfig(db_path=store.db_path)
    with patch("server.memory.manager.EmbeddingEngine"), \
         patch("server.memory.manager.SemanticSearch"), \
         patch("server.memory.manager.CompressionPipeline"), \
         patch("server.memory.manager.MemoryMaintainer"), \
         patch("server.memory.manager.InteractionRecorder"), \
         patch("server.memory.manager.RecentMemory"):
        from server.memory.manager import MemoryManager
        mgr = MemoryManager(cfg)
        mgr._initialized = True
        yield mgr
        if mgr.store._conn is not None:
            mgr.store.close()


def _set_fact(store, key, value_dict):
    """辅助：写入一条 fact"""
    store.conn.execute(
        "INSERT OR REPLACE INTO facts (key, value, source, updated_at, access_count) "
        "VALUES (?, ?, ?, ?, 0)",
        (key, json.dumps(value_dict, ensure_ascii=False), "test", "2026-07-12 10:00:00"),
    )
    store._commit()


# ==================== find_consumable_memories 测试 ====================

class TestFindConsumableMemoriesByContext:
    def test_exact_task_type_match(self, manager, store):
        _set_fact(store, "project.accounting_tip", {
            "type": "project",
            "name": "记账退款对冲",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": ["退款"],
        })
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 1
        assert results[0]["key"] == "project.accounting_tip"
        assert results[0]["matched_by"] == "context"

    def test_no_match_for_different_task_type(self, manager, store):
        _set_fact(store, "project.accounting_tip", {
            "type": "project",
            "name": "记账退款对冲",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": [],
        })
        results = manager.find_consumable_memories("recurring.yihuan_gacha")
        assert len(results) == 0


class TestFindConsumableMemoriesWildcard:
    def test_star_matches_all(self, manager, store):
        _set_fact(store, "preferences.curl_usage", {
            "type": "preference",
            "name": "curl.exe 优先",
            "consumption_contexts": ["*"],
            "trigger_keywords": [],
        })
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 1
        assert results[0]["matched_by"] == "context"

    def test_scope_wildcard_matches_same_scope(self, manager, store):
        _set_fact(store, "preferences.recurring_rule", {
            "type": "preference",
            "name": "周期任务通用规则",
            "consumption_contexts": ["recurring.*"],
            "trigger_keywords": [],
        })
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 1
        assert results[0]["matched_by"] == "context"

    def test_scope_wildcard_no_match_different_scope(self, manager, store):
        _set_fact(store, "preferences.recurring_rule", {
            "type": "preference",
            "name": "周期任务通用规则",
            "consumption_contexts": ["recurring.*"],
            "trigger_keywords": [],
        })
        results = manager.find_consumable_memories("adhoc.web_archive")
        assert len(results) == 0


class TestFindConsumableMemoriesByKeyword:
    def test_keyword_match(self, manager, store):
        _set_fact(store, "reference.paddleocr_bbox", {
            "type": "reference",
            "name": "OCR bbox 修正",
            "consumption_contexts": [],
            "trigger_keywords": ["PaddleOCR", "bbox", "OCR"],
        })
        results = manager.find_consumable_memories("adhoc.html_debug", task_query="调试 OCR 识别问题")
        assert len(results) == 1
        assert results[0]["matched_by"] == "keyword"

    def test_keyword_no_match(self, manager, store):
        _set_fact(store, "reference.paddleocr_bbox", {
            "type": "reference",
            "name": "OCR bbox 修正",
            "consumption_contexts": [],
            "trigger_keywords": ["PaddleOCR"],
        })
        results = manager.find_consumable_memories("adhoc.html_debug", task_query="调试浏览器")
        assert len(results) == 0

    def test_context_and_keyword_both_match(self, manager, store):
        _set_fact(store, "project.accounting_refund", {
            "type": "project",
            "name": "退款对冲规则",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": ["退款", "对冲"],
        })
        results = manager.find_consumable_memories("recurring.accounting", task_query="处理退款")
        assert len(results) == 1
        assert results[0]["matched_by"] == "context+keyword"


class TestFindConsumableMemoriesCompatibility:
    def test_old_memory_without_new_fields_skipped(self, manager, store):
        _set_fact(store, "old_key", {
            "type": "project",
            "name": "旧记忆无消费场景字段",
        })
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 0

    def test_empty_contexts_and_keywords_skipped(self, manager, store):
        _set_fact(store, "empty_key", {
            "type": "project",
            "name": "空消费场景",
            "consumption_contexts": [],
            "trigger_keywords": [],
        })
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 0

    def test_non_dict_value_skipped(self, manager, store):
        store.conn.execute(
            "INSERT OR REPLACE INTO facts (key, value, source, updated_at, access_count) "
            "VALUES (?, ?, ?, ?, 0)",
            ("non_dict_key", "just a string", "test", "2026-07-12 10:00:00"),
        )
        store._commit()
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 0

    def test_invalid_json_skipped(self, manager, store):
        store.conn.execute(
            "INSERT OR REPLACE INTO facts (key, value, source, updated_at, access_count) "
            "VALUES (?, ?, ?, ?, 0)",
            ("bad_json_key", "{not valid json", "test", "2026-07-12 10:00:00"),
        )
        store._commit()
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 0


class TestFindConsumableMemoriesNoAccessCountIncrement:
    def test_does_not_increment_access_count(self, manager, store):
        _set_fact(store, "project.tip", {
            "type": "project",
            "name": "测试记忆",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": [],
        })
        manager.find_consumable_memories("recurring.accounting")
        cur = store.conn.execute("SELECT access_count FROM facts WHERE key = ?", ("project.tip",))
        row = cur.fetchone()
        assert row[0] == 0


# ==================== 混合候选集回归测试（2026-09-13 code review 5-1） ====================

class TestFindConsumableMemoriesMixedCandidates:
    """SQL LIKE 命中 ≥1 行时，纯 keyword 记忆和旧数据（列 NULL）不能被漏掉。

    历史缺陷：候选集只含 LIKE 命中行，回退条件 `if not rows` 仅在 SQL 空结果时触发，
    导致库里存在任一 context 命中记忆时，keyword-only/旧数据记忆永远不被召回。
    既有测试的 _set_fact 直插使列全为 NULL，全部走回退路径，未覆盖此场景。
    """

    def _set_fact_with_columns(self, store, key, value_dict, contexts=None, keywords=None):
        """辅助：写入带独立列的 fact（模拟 memory_set 走 extract_facts_from_kv 的产物）"""
        store.conn.execute(
            "INSERT OR REPLACE INTO facts (key, value, source, updated_at, access_count, "
            "consumption_contexts, trigger_keywords) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (
                key,
                json.dumps(value_dict, ensure_ascii=False),
                "test",
                "2026-07-12 10:00:00",
                json.dumps(contexts, ensure_ascii=False) if contexts is not None else None,
                json.dumps(keywords, ensure_ascii=False) if keywords is not None else None,
            ),
        )
        store._commit()

    def test_context_hit_does_not_shadow_keyword_memory(self, manager, store):
        # 场景复现：一条 context 命中记忆（独立列有值）+ 一条纯 keyword 记忆（列 NULL）
        self._set_fact_with_columns(
            store, "project.accounting_tip",
            {"type": "project", "name": "记账提示", "consumption_contexts": ["recurring.accounting"]},
            contexts=["recurring.accounting"], keywords=[],
        )
        _set_fact(store, "reference.refund_rule", {
            "type": "reference", "name": "退款规则",
            "consumption_contexts": [], "trigger_keywords": ["退款"],
        })
        results = manager.find_consumable_memories(
            "recurring.accounting", task_query="处理退款"
        )
        keys = {r["key"] for r in results}
        assert "project.accounting_tip" in keys
        assert "reference.refund_rule" in keys

    def test_context_hit_does_not_shadow_empty_context_list_column(self, manager, store):
        # contexts 列为 '[]'（非 NULL）+ keywords 列有值：也必须被召回
        self._set_fact_with_columns(
            store, "project.accounting_tip",
            {"type": "project", "name": "记账提示", "consumption_contexts": ["recurring.accounting"]},
            contexts=["recurring.accounting"], keywords=[],
        )
        self._set_fact_with_columns(
            store, "reference.ocr_lesson",
            {"type": "reference", "name": "OCR 经验", "trigger_keywords": ["PaddleOCR"]},
            contexts=[], keywords=["PaddleOCR"],
        )
        results = manager.find_consumable_memories(
            "recurring.accounting", task_query="调试 PaddleOCR"
        )
        keys = {r["key"] for r in results}
        assert "reference.ocr_lesson" in keys

    def test_context_hit_does_not_shadow_legacy_memory(self, manager, store):
        # v3 旧数据：两列均 NULL，消费场景只存在于 value JSON
        self._set_fact_with_columns(
            store, "project.accounting_tip",
            {"type": "project", "name": "记账提示", "consumption_contexts": ["recurring.accounting"]},
            contexts=["recurring.accounting"], keywords=[],
        )
        _set_fact(store, "legacy.tip", {
            "type": "preference", "name": "旧记忆",
            "consumption_contexts": ["recurring.accounting"],
        })
        results = manager.find_consumable_memories("recurring.accounting")
        keys = {r["key"] for r in results}
        assert "legacy.tip" in keys

    def test_non_matching_context_column_still_excluded(self, manager, store):
        # contexts 列有值但不匹配：不应被召回（候选集扩展不能放行不相关记忆）
        self._set_fact_with_columns(
            store, "project.other_scope",
            {"type": "project", "name": "别的 scope"},
            contexts=["adhoc.web_archive"], keywords=[],
        )
        results = manager.find_consumable_memories("recurring.accounting")
        assert len(results) == 0



# ==================== first_action 注入测试 ====================

class TestFirstActionInjection:
    def test_first_action_injected_when_memory_index_nonempty(self, manager, store):
        _set_fact(store, "preferences", {
            "type": "preference",
            "name": "用户偏好",
            "consumption_contexts": ["*"],
            "trigger_keywords": [],
        })
        from server.agent_guide import _build_task_guide
        with patch("server.memory.manager.get_memory_manager", return_value=manager):
            result = _build_task_guide("recurring.accounting")
        assert "【必读记忆】" in result["first_action"]
        assert "preferences" in result["first_action"]

    def test_first_action_not_injected_when_memory_index_empty(self, manager, store):
        from server.agent_guide import _build_task_guide
        with patch("server.memory.manager.get_memory_manager", return_value=manager):
            result = _build_task_guide("recurring.accounting")
        assert "【必读记忆】" not in result["first_action"]

    def test_memory_index_includes_dynamic_matches(self, manager, store):
        _set_fact(store, "project.tip", {
            "type": "project",
            "name": "记账提示",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": [],
        })
        from server.agent_guide import _build_task_guide
        with patch("server.memory.manager.get_memory_manager", return_value=manager):
            result = _build_task_guide("recurring.accounting")
        keys = [m["key"] for m in result["memory_index"]]
        assert "project.tip" in keys


# ==================== memory_set + merge 保留新字段测试 ====================

class TestMemorySetMergePreservesFields:
    def test_set_with_consumption_contexts_persists(self, store):
        value = {
            "type": "preference",
            "name": "测试",
            "consumption_contexts": ["recurring.accounting"],
            "trigger_keywords": ["退款"],
        }
        store.upsert_fact("test_key", json.dumps(value, ensure_ascii=False), source="test")
        fact = store.get_fact("test_key")
        parsed = json.loads(fact["value"])
        assert parsed["consumption_contexts"] == ["recurring.accounting"]
        assert parsed["trigger_keywords"] == ["退款"]
