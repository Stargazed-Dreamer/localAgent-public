"""记忆模块 API 端点测试（三层记忆系统）

测试 /memory 相关接口的 CRUD、深度合并、列表、搜索、时间线、压缩、
记录、统计、重建索引、维护器、清理等功能。

所有测试使用 "test_" 前缀的 key，并在测试结束后清理。
"""

import json
import time

import pytest

# 测试用 key 前缀，方便清理
TEST_PREFIX = "test_"


def _make_key(name: str) -> str:
    return f"{TEST_PREFIX}{name}"


def _cleanup_key(client, key: str):
    """删除指定 key 的记忆，避免测试污染"""
    client.delete(f"/memory/{key}")


# ==================== 基础端点 ====================

class TestMemoryStatus:
    """记忆系统状态"""

    def test_status_returns_initialized(self, client):
        """GET /memory/status 应返回 initialized=True"""
        resp = client.get("/memory/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["initialized"] is True

    def test_status_has_required_fields(self, client):
        """GET /memory/status 应包含 messages 和 db_size_bytes"""
        resp = client.get("/memory/status")
        data = resp.json()
        assert "messages" in data
        assert "db_size_bytes" in data
        assert isinstance(data["messages"], int)
        assert isinstance(data["db_size_bytes"], int)

    def test_status_has_embedding_info(self, client):
        """GET /memory/status 应包含 embedding_ready 和 embedding_model"""
        resp = client.get("/memory/status")
        data = resp.json()
        assert "embedding_ready" in data
        assert "embedding_model" in data


class TestMemoryList:
    """记忆列表"""

    def test_list_returns_created_memory(self, client):
        """列表应包含已创建的记忆"""
        key = _make_key("list_item")
        try:
            client.post(f"/memory/{key}", json={"data": {"foo": "bar"}})
            resp = client.get("/memory/list")
            assert resp.status_code == 200
            data = resp.json()
            assert "keys" in data
            assert "total" in data
            found = [k for k in data["keys"] if k["key"] == key]
            assert len(found) == 1
        finally:
            _cleanup_key(client, key)

    def test_list_has_metadata_fields(self, client):
        """列表项应包含 key, source, value_preview, updated_at"""
        key = _make_key("list_meta")
        try:
            client.post(f"/memory/{key}", json={"data": {"x": 1}})
            resp = client.get("/memory/list")
            items = [k for k in resp.json()["keys"] if k["key"] == key]
            assert len(items) == 1
            item = items[0]
            assert "key" in item
            assert "source" in item
            assert "value_preview" in item
            assert "updated_at" in item
        finally:
            _cleanup_key(client, key)


# ==================== CRUD ====================

class TestMemoryCRUD:
    """记忆 CRUD 生命周期"""

    def test_create_and_read(self, client):
        """创建后应能读取"""
        key = _make_key("crud_create")
        try:
            resp = client.post(f"/memory/{key}", json={
                "data": {"name": "alice", "age": 30},
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["key"] == key
            assert data["status"] == "ok"

            resp = client.get(f"/memory/{key}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "alice"
            assert data["age"] == 30
        finally:
            _cleanup_key(client, key)

    def test_update_existing(self, client):
        """深度合并更新已有记忆"""
        key = _make_key("crud_update")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"name": "bob", "score": 10},
            })
            resp = client.post(f"/memory/{key}", json={
                "data": {"score": 99},
            })
            assert resp.status_code == 200

            resp = client.get(f"/memory/{key}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "bob"
            assert data["score"] == 99
        finally:
            _cleanup_key(client, key)

    def test_delete_existing(self, client):
        """删除已有记忆应返回 status=deleted"""
        key = _make_key("crud_delete")
        client.post(f"/memory/{key}", json={"data": {"x": 1}})
        resp = client.delete(f"/memory/{key}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == key
        assert data["status"] == "deleted"

        resp = client.get(f"/memory/{key}")
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        """删除不存在的记忆应返回 404"""
        key = _make_key("crud_delete_none")
        resp = client.delete(f"/memory/{key}")
        assert resp.status_code == 404

    def test_get_nonexistent_returns_404(self, client):
        """GET 不存在的 key 应返回 404"""
        key = _make_key("nonexistent_key_xyz")
        resp = client.get(f"/memory/{key}")
        assert resp.status_code == 404

    def test_write_without_merge_overwrites(self, client):
        """merge=false 时应完全覆盖而非深度合并"""
        key = _make_key("crud_overwrite")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"a": 1, "b": 2},
            })
            resp = client.post(f"/memory/{key}", json={
                "data": {"c": 3},
                "merge": False,
            })
            assert resp.status_code == 200

            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["c"] == 3
            assert "a" not in data
            assert "b" not in data
        finally:
            _cleanup_key(client, key)


class TestMemoryDeepMerge:
    """深度合并"""

    def test_deep_merge_nested_dicts(self, client):
        """嵌套字典应深度合并，而非替换"""
        key = _make_key("deep_merge")
        try:
            client.post(f"/memory/{key}", json={
                "data": {
                    "user": {"name": "carol", "settings": {"theme": "dark", "lang": "en"}},
                    "version": 1,
                },
            })
            client.post(f"/memory/{key}", json={
                "data": {"user": {"settings": {"theme": "light"}}},
            })

            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["user"]["settings"]["theme"] == "light"
            assert data["user"]["settings"]["lang"] == "en"
            assert data["user"]["name"] == "carol"
            assert data["version"] == 1
        finally:
            _cleanup_key(client, key)

    def test_deep_merge_overwrites_non_dict(self, client):
        """非字典类型应直接覆盖"""
        key = _make_key("deep_merge_scalar")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"count": 5, "label": "old"},
            })
            client.post(f"/memory/{key}", json={
                "data": {"count": 10},
            })
            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["count"] == 10
            assert data["label"] == "old"
        finally:
            _cleanup_key(client, key)

    def test_deep_merge_dict_over_scalar(self, client):
        """字典覆盖标量值"""
        key = _make_key("deep_merge_dict_over_scalar")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"field": "scalar"},
            })
            client.post(f"/memory/{key}", json={
                "data": {"field": {"nested": True}},
            })
            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["field"] == {"nested": True}
        finally:
            _cleanup_key(client, key)


# ==================== v3 结构化字段 ====================

class TestStructuredFacts:
    """v3 结构化字段：fact_type / occurred_at / consumption_contexts / trigger_keywords

    验证 memory_set 顶层字段 → facts 表独立列 → memory_get 返回字段
    """

    def test_set_with_structured_fields_top_level(self, client):
        """顶层 structured 字段持久化到 facts 表独立列

        v6.1 T29：fact_type 收敛为 5 类 closed（user/feedback/project/reference/experience）。
        旧值 'preference' 已由 migrate_fact_type_v6_1.py 迁移为 'user'；
        此处用新值 'user' 验证，符合 closed taxonomy 君子协议。
        """
        key = _make_key("struct_top")
        try:
            resp = client.post(f"/memory/{key}", json={
                "data": {"name": "test_struct"},
                "fact_type": "user",
                "occurred_at": "2026-07-18T10:00:00",
                "consumption_contexts": ["recurring.accounting", "adhoc.web_archive"],
                "trigger_keywords": ["退款", "curl"],
            })
            assert resp.status_code == 200

            # GET 应该返回结构化字段
            resp = client.get(f"/memory/{key}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "test_struct"
            assert data["fact_type"] == "user"
            assert data["occurred_at"] == "2026-07-18T10:00:00"
            # consumption_contexts / trigger_keywords 在 fact 表是 JSON 字符串，需解析
            contexts = json.loads(data["consumption_contexts"]) if isinstance(data["consumption_contexts"], str) else data["consumption_contexts"]
            assert contexts == ["recurring.accounting", "adhoc.web_archive"]
            keywords = json.loads(data["trigger_keywords"]) if isinstance(data["trigger_keywords"], str) else data["trigger_keywords"]
            assert keywords == ["退款", "curl"]
        finally:
            _cleanup_key(client, key)

    def test_set_with_structured_fields_inside_data(self, client):
        """data 内部含结构化字段时，extract_facts_from_kv 自动提取到独立列"""
        key = _make_key("struct_in_data")
        try:
            # 在 data 内部传结构化字段（向后兼容场景）
            resp = client.post(f"/memory/{key}", json={
                "data": {
                    "name": "test_in_data",
                    "fact_type": "project",
                    "consumption_contexts": ["adhoc.test"],
                    "trigger_keywords": ["keyword1"],
                },
            })
            assert resp.status_code == 200

            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["fact_type"] == "project"
            # data 内部的字段也会被存到独立列
            contexts = json.loads(data["consumption_contexts"]) if isinstance(data["consumption_contexts"], str) else data["consumption_contexts"]
            assert contexts == ["adhoc.test"]
        finally:
            _cleanup_key(client, key)

    def test_top_level_overrides_data(self, client):
        """顶层 structured 字段优先于 data 内部同名字段"""
        key = _make_key("struct_override")
        try:
            resp = client.post(f"/memory/{key}", json={
                "data": {
                    "name": "conflict_test",
                    "fact_type": "preference",  # data 内部的值
                    "consumption_contexts": ["old_context"],
                },
                "fact_type": "project",  # 顶层覆盖
                "consumption_contexts": ["new_context"],  # 顶层覆盖
            })
            assert resp.status_code == 200

            resp = client.get(f"/memory/{key}")
            data = resp.json()
            # 顶层应胜出
            assert data["fact_type"] == "project"
            contexts = json.loads(data["consumption_contexts"]) if isinstance(data["consumption_contexts"], str) else data["consumption_contexts"]
            assert contexts == ["new_context"]
        finally:
            _cleanup_key(client, key)

    def test_partial_structured_update_preserves_existing(self, client):
        """部分更新 structured 字段，未传的保留原值"""
        key = _make_key("struct_partial")
        try:
            # 第一次写入所有结构化字段
            client.post(f"/memory/{key}", json={
                "data": {"v": 1},
                "fact_type": "preference",
                "consumption_contexts": ["ctx1"],
                "trigger_keywords": ["kw1"],
            })
            # 第二次只更新 fact_type，其他应保留
            client.post(f"/memory/{key}", json={
                "data": {"v": 2},
                "fact_type": "project",
            })
            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert data["fact_type"] == "project"
            contexts = json.loads(data["consumption_contexts"]) if isinstance(data["consumption_contexts"], str) else data["consumption_contexts"]
            assert contexts == ["ctx1"]  # 保留
            keywords = json.loads(data["trigger_keywords"]) if isinstance(data["trigger_keywords"], str) else data["trigger_keywords"]
            assert keywords == ["kw1"]  # 保留
        finally:
            _cleanup_key(client, key)

    def test_get_returns_mentioned_at(self, client):
        """GET 后 mentioned_at 应该被更新（非空）"""
        key = _make_key("struct_mentioned")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"x": 1},
                "fact_type": "reference",
            })
            resp = client.get(f"/memory/{key}")
            data = resp.json()
            assert "mentioned_at" in data
            assert data["mentioned_at"] is not None
            # ISO 时间格式
            assert "T" in str(data["mentioned_at"]) or "-" in str(data["mentioned_at"])
        finally:
            _cleanup_key(client, key)

    def test_find_consumable_memories_via_index(self, client):
        """find_consumable_memories 用 SQL 索引快速过滤"""
        from server.memory.manager import get_memory_manager

        key = _make_key("struct_find")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"name": "findable"},
                "fact_type": "experience",
                "consumption_contexts": ["recurring.accounting"],
                "trigger_keywords": ["账单"],
            })

            mgr = get_memory_manager()
            # 模拟 agent_guide 调用：查找 recurring.accounting 应该消费的记忆
            results = mgr.find_consumable_memories("recurring.accounting", "处理账单")
            found = [r for r in results if r["key"] == key]
            assert len(found) == 1
            assert found[0]["matched_by"] in ("context", "context+keyword")
            assert found[0]["fact_type"] == "experience"
        finally:
            _cleanup_key(client, key)

    def test_find_consumable_memories_wildcard(self, client):
        """consumption_contexts='*' 通配，所有 task_type 都能匹配"""
        from server.memory.manager import get_memory_manager

        key = _make_key("struct_wildcard")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"name": "global_pref"},
                "consumption_contexts": ["*"],
            })

            mgr = get_memory_manager()
            # 任何 task_type 都应该匹配到这条
            results = mgr.find_consumable_memories("adhoc.anything", "any query")
            found = [r for r in results if r["key"] == key]
            assert len(found) == 1
            assert found[0]["matched_by"] == "context"
        finally:
            _cleanup_key(client, key)

    def test_find_consumable_memories_scope_wildcard(self, client):
        """consumption_contexts='recurring.*' 通配 scope"""
        from server.memory.manager import get_memory_manager

        key = _make_key("struct_scope_wc")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"name": "scope_pref"},
                "consumption_contexts": ["recurring.*"],
            })

            mgr = get_memory_manager()
            # recurring.* 下的任何 task_type 都应匹配
            results = mgr.find_consumable_memories("recurring.accounting")
            found = [r for r in results if r["key"] == key]
            assert len(found) == 1
            results = mgr.find_consumable_memories("recurring.memory_generation")
            found = [r for r in results if r["key"] == key]
            assert len(found) == 1
            # adhoc 的不应匹配
            results = mgr.find_consumable_memories("adhoc.something")
            found = [r for r in results if r["key"] == key]
            assert len(found) == 0
        finally:
            _cleanup_key(client, key)

    def test_extract_facts_writes_full_dict_to_value(self, client):
        """v3 修复：整个 dict 作为 fact.value 写入（不再只拆标量子字段）"""
        from server.memory.manager import get_memory_manager

        key = _make_key("struct_full_dict")
        try:
            client.post(f"/memory/{key}", json={
                "data": {
                    "nested": {"deep": {"value": 42}},
                    "list_field": [1, 2, 3],
                    "scalar": "hello",
                },
            })
            # 直接查 facts 表，验证 value 是整个 dict 的 JSON
            mgr = get_memory_manager()
            fact = mgr.store.get_fact_meta(key)
            assert fact is not None
            value = json.loads(fact["value"])
            assert isinstance(value, dict)
            assert value["nested"]["deep"]["value"] == 42
            assert value["list_field"] == [1, 2, 3]
            assert value["scalar"] == "hello"
        finally:
            _cleanup_key(client, key)


# ==================== 新增端点（P2-24 补齐） ====================

class TestMemorySearch:
    """POST /memory/search — 语义搜索记忆"""

    def test_search_returns_results(self, client):
        """搜索应返回结果列表"""
        key = _make_key("search_test")
        try:
            client.post(f"/memory/{key}", json={
                "data": {"topic": "Python programming language basics"},
            })
            resp = client.post("/memory/search", json={
                "query": "Python programming",
                "top_k": 5,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "results" in data
            assert "total" in data
            assert "query" in data
            assert data["query"] == "Python programming"
            assert isinstance(data["results"], list)
        finally:
            _cleanup_key(client, key)

    def test_search_empty_query_rejected(self, client):
        """空查询应被拒绝（422）"""
        resp = client.post("/memory/search", json={"query": "", "top_k": 5})
        assert resp.status_code == 422

    def test_search_with_time_range(self, client):
        """带时间范围的搜索"""
        now = time.time()
        resp = client.post("/memory/search", json={
            "query": "test",
            "top_k": 5,
            "since": now - 3600,
            "until": now + 3600,
        })
        assert resp.status_code == 200

    def test_search_invalid_top_k(self, client):
        """top_k 超出范围应被拒绝"""
        resp = client.post("/memory/search", json={"query": "test", "top_k": 0})
        assert resp.status_code == 422

    def test_search_with_source_filter(self, client):
        """按来源过滤搜索"""
        resp = client.post("/memory/search", json={
            "query": "test",
            "top_k": 5,
            "source": "manual",
        })
        assert resp.status_code == 200


class TestMemoryTimeline:
    """GET /memory/timeline — 时间线查询"""

    def test_timeline_returns_messages(self, client):
        """时间线应返回消息列表"""
        key = _make_key("timeline_test")
        try:
            client.post(f"/memory/{key}", json={"data": {"event": "test"}})
            resp = client.get("/memory/timeline?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert "messages" in data
            assert "total" in data
            assert isinstance(data["messages"], list)
        finally:
            _cleanup_key(client, key)

    def test_timeline_with_source_filter(self, client):
        """按来源过滤时间线"""
        resp = client.get("/memory/timeline?source=manual&limit=5")
        assert resp.status_code == 200

    def test_timeline_invalid_limit(self, client):
        """limit=0 应被拒绝"""
        resp = client.get("/memory/timeline?limit=0")
        assert resp.status_code == 422

    def test_timeline_with_time_range(self, client):
        """带时间范围的时间线"""
        now = time.time()
        resp = client.get(
            f"/memory/timeline?since={now - 3600}&until={now + 3600}&limit=10"
        )
        assert resp.status_code == 200


class TestMemoryCompress:
    """POST /memory/compress — 手动触发记忆压缩"""

    def test_compress_returns_result(self, client):
        """压缩应返回结果字典"""
        resp = client.post("/memory/compress")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_compress_force(self, client):
        """force=true 应正常返回"""
        resp = client.post("/memory/compress?force=true")
        assert resp.status_code == 200


class TestMemoryRecord:
    """POST /memory/record — 手动记录交互"""

    def test_record_returns_id(self, client):
        """记录应返回消息 id"""
        resp = client.post("/memory/record", json={
            "content": "test recording message",
            "source": "manual",
            "role": "assistant",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "ok"
        assert isinstance(data["id"], int)

    def test_record_empty_content_rejected(self, client):
        """空内容应被拒绝"""
        resp = client.post("/memory/record", json={"content": ""})
        assert resp.status_code == 422

    def test_record_with_key(self, client):
        """带 key 的记录应能通过 key 读取"""
        key = _make_key("record_with_key")
        try:
            resp = client.post("/memory/record", json={
                "content": json.dumps({"data": "test"}),
                "source": "manual",
                "key": key,
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
        finally:
            _cleanup_key(client, key)

    def test_record_default_source(self, client):
        """不传 source 时应默认 manual"""
        resp = client.post("/memory/record", json={"content": "default source test"})
        assert resp.status_code == 200


class TestMemoryStats:
    """GET /memory/stats — 详细记忆统计"""

    def test_stats_returns_extended_info(self, client):
        """stats 应包含 bm25 和 recent_summaries"""
        resp = client.get("/memory/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert "bm25" in data
        assert "recent_summaries" in data

    def test_stats_bm25_has_fields(self, client):
        """bm25 统计应包含 total_docs 和 avg_dl"""
        resp = client.get("/memory/stats")
        data = resp.json()
        assert "total_docs" in data["bm25"]
        assert "avg_dl" in data["bm25"]

    def test_stats_has_db_size(self, client):
        """stats 应包含 db_size_bytes"""
        resp = client.get("/memory/stats")
        data = resp.json()
        assert "db_size_bytes" in data
        assert data["db_size_bytes"] > 0


class TestMemoryReindex:
    """POST /memory/reindex — 重建语义索引"""

    def test_reindex_returns_ok(self, client):
        """重建索引应返回 ok"""
        resp = client.post("/memory/reindex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "vectors" in data
        assert "bm25" in data

    def test_reindex_idempotent(self, client):
        """连续重建不应出错"""
        client.post("/memory/reindex")
        resp = client.post("/memory/reindex")
        assert resp.status_code == 200


class TestMemoryMaintain:
    """记忆维护器"""

    def test_maintain_status_returns_dict(self, client):
        """GET /memory/maintain/status 应返回维护器状态"""
        resp = client.get("/memory/maintain/status")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_maintain_without_force(self, client):
        """POST /memory/maintain 不带 force 时可能跳过"""
        resp = client.post("/memory/maintain", json={"force": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "skipped")

    def test_maintain_force(self, client):
        """POST /memory/maintain force=true 应执行"""
        resp = client.post("/memory/maintain", json={"force": True})
        assert resp.status_code == 200
        assert "status" in resp.json()


class TestMemoryCleanup:
    """POST /memory/cleanup/orphaned — 清理孤立记忆"""

    def test_cleanup_returns_status(self, client):
        """清理应返回状态和删除计数"""
        resp = client.post("/memory/cleanup/orphaned")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "deleted_messages" in data
        assert "message_cutoff_days" in data
        assert "summary_cutoff_days" in data
        assert isinstance(data["deleted_messages"], int)

    def test_cleanup_safe_on_empty(self, client):
        """空数据库清理不应崩溃"""
        resp = client.post("/memory/cleanup/orphaned")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ==================== _KEY_PATTERN 回归测试 ====================
# SECURITY-RISKS.md E11 缺口 C 修复（2026-08-06）：补 _KEY_PATTERN 回归测试，
# 防止未来误删 pattern= 参数（路径遍历/特殊字符/超长等攻击向量回归）。
# 覆盖 4 个验证点：
#   1. memory_get(key) Path 参数 pattern
#   2. memory_set(key) Path 参数 pattern
#   3. memory_delete(key) Path 参数 pattern
#   4. MemoryRecordRequest.key Pydantic 字段 pattern（C4 修复，body 内验证）


# 路由能匹配单段 /{key} 且能进 pattern 验证的非法字符（不含会被 URL/路由提前处理的字符）
# 行为基线（exec_python 实测，2026-08-06）：以下字符在 path 端点全部返回 422（pattern 拒）
_INVALID_KEYS_PATH = [
    ".hidden",           # 点开头
    "a b",               # 空格
    "a:b",               # 冒号
    "a@b",               # @
    "a!b",               # !
    "a.b",               # 点号
    "a+b",               # 加号
    "a=b",               # 等号
    "a&b",               # &
    "a．b",              # Unicode 全角点
    "a／b",              # Unicode 全角斜杠
    "ＡＢＣ",             # 全角字母
    "a" * 129,           # 超长（129 字符）
]

# path 端点无法测试的非法字符（URL 解析/路由分段提前处理，根本到不了 pattern）
# 这些字符在 path 端点会返回 404（路由阻断）或 ERROR（无法构造 URL），
# 不是 pattern 在拒绝，因此不放在 _INVALID_KEYS_PATH 中。
# 但在 body 端点（POST /memory/record 的 MemoryRecordRequest.key）会触发 pattern 返回 422。
# 行为基线（exec_python 实测，2026-08-06）：
#   path 端点：'..' / 'a%62' / 'a/b' / '../'  → 404（路由/URL 解码提前处理）
#               'a\nb' / 'a\x00b'              → httpx.InvalidURL（无法构造 URL）
#   body 端点：所有 → 422（pattern 拒）
_INVALID_KEYS_BODY_ONLY = [
    "..",                # 点段（path: 404，Starlette 路由处理点段）
    "a%62",              # % 字符（path: 404，URL 解码 %62 → 'b' 后变成 'ab' 匹配其他路由）
    "a\nb",              # 换行（path: httpx.InvalidURL，非 printable ASCII）
    "a\x00b",            # Null 字节（path: httpx.InvalidURL，非 printable ASCII）
    "a/b",               # 路径分隔符（path: 404，破坏路由分段）
    "a\\b",              # 反斜杠（path: 422 实测可进 pattern，但保守放 body 端点覆盖）
    "../",               # 路径遍历（path: 404，路由分段）
    "..\\",              # Windows 路径遍历（path: 422 实测可进 pattern，保守放 body 端点覆盖）
]


class TestKeyPatternValidation:
    """_KEY_PATTERN 回归测试（E11 缺口 C，2026-08-06 补）。

    验证 4 处 pattern 验证点：
      - GET /memory/{key}（Path 参数 pattern）
      - POST /memory/{key}（Path 参数 pattern）
      - DELETE /memory/{key}（Path 参数 pattern）
      - POST /memory/record（MemoryRecordRequest.key 字段 pattern，C4 修复）
    """

    VALID_KEYS = [
        "a",                # 单字符
        "abc123",           # 字母数字
        "snake_case",       # 下划线
        "kebab-case",       # 连字符
        "mixed-1_2_3",      # 混合
        "A" * 128,          # 最大长度（128 字符边界）
    ]

    @pytest.mark.parametrize("key", VALID_KEYS)
    def test_valid_key_accepted_by_get(self, client, key):
        """合法 key 在 GET 中应通过 pattern（404 = 不存在但 pattern 通过，200 = 存在）"""
        try:
            resp = client.get(f"/memory/{key}")
            assert resp.status_code in (200, 404), (
                f"valid key {key[:30]!r} should pass pattern (got {resp.status_code})"
            )
            assert resp.status_code != 422, f"valid key {key[:30]!r} wrongly rejected by pattern"
        finally:
            if not key.startswith("A" * 50):  # 不清理 "A" * 128 这种纯边界测试
                _cleanup_key(client, key)

    @pytest.mark.parametrize("key", VALID_KEYS)
    def test_valid_key_accepted_by_set(self, client, key):
        """合法 key 在 POST 中应通过 pattern（200 = 创建成功）"""
        try:
            resp = client.post(f"/memory/{key}", json={"data": {"x": 1}})
            assert resp.status_code == 200, (
                f"valid key {key[:30]!r} should pass pattern (got {resp.status_code})"
            )
            assert resp.status_code != 422, f"valid key {key[:30]!r} wrongly rejected by pattern"
        finally:
            if not key.startswith("A" * 50):
                _cleanup_key(client, key)

    @pytest.mark.parametrize("key", _INVALID_KEYS_PATH)
    def test_invalid_key_rejected_by_get(self, client, key):
        """非法 key 在 GET 中应被 422 拒绝"""
        resp = client.get(f"/memory/{key}")
        assert resp.status_code == 422, (
            f"invalid key {key[:30]!r} should be rejected by pattern (got {resp.status_code})"
        )

    @pytest.mark.parametrize("key", _INVALID_KEYS_PATH)
    def test_invalid_key_rejected_by_set(self, client, key):
        """非法 key 在 POST 中应被 422 拒绝"""
        resp = client.post(f"/memory/{key}", json={"data": {"x": 1}})
        assert resp.status_code == 422, (
            f"invalid key {key[:30]!r} should be rejected by pattern (got {resp.status_code})"
        )

    @pytest.mark.parametrize("key", _INVALID_KEYS_PATH)
    def test_invalid_key_rejected_by_delete(self, client, key):
        """非法 key 在 DELETE 中应被 422 拒绝"""
        resp = client.delete(f"/memory/{key}")
        assert resp.status_code == 422, (
            f"invalid key {key[:30]!r} should be rejected by pattern (got {resp.status_code})"
        )

    @pytest.mark.parametrize("key", _INVALID_KEYS_PATH + _INVALID_KEYS_BODY_ONLY)
    def test_invalid_key_rejected_by_record_body(self, client, key):
        """非法 key 在 POST /memory/record 的 MemoryRecordRequest.key 字段中应被 422 拒绝

        这是 C4 修复（2026-06-06）补的 body 字段 pattern 验证。覆盖所有攻击向量，
        包括路径分隔符（path 端点会路由分段失败，但 body 端点能正确传递字符串给 Pydantic）。
        """
        resp = client.post("/memory/record", json={
            "content": "test",
            "key": key,
        })
        assert resp.status_code == 422, (
            f"invalid key {key[:30]!r} should be rejected by MemoryRecordRequest.key pattern "
            f"(got {resp.status_code})"
        )

    def test_empty_key_in_body_rejected(self, client):
        """空字符串 key 在 MemoryRecordRequest 中应被 422 拒绝（{1,128} 下限）"""
        resp = client.post("/memory/record", json={
            "content": "test",
            "key": "",
        })
        assert resp.status_code == 422

    def test_none_key_in_body_accepted(self, client):
        """None（不传 key 字段）在 MemoryRecordRequest 中应被接受（可选字段）"""
        resp = client.post("/memory/record", json={
            "content": "test_pattern_none_key",
        })
        # 接受 = 200（手动记录不要求 key 字段）
        assert resp.status_code == 200
