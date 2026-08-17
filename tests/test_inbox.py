"""收件箱模块测试 — InboxStore CRUD + 清理 + REST 端点

InboxStore 用独立 SQLite（data/inbox.db），存储 Loop 推送的待审查条目。
测试用临时 db 文件隔离，不污染真实数据。
"""


import pytest

from server.inbox import (
    InboxStore,
    reset_store,
)


@pytest.fixture
def store(tmp_path):
    """临时 InboxStore，测试完自动关闭"""
    db_path = tmp_path / "test_inbox.db"
    s = InboxStore(str(db_path))
    s.initialize()
    yield s
    s.close()


@pytest.fixture
def isolated_inbox(monkeypatch, tmp_path):
    """monkeypatch inbox 配置指向临时 db，并 reset 全局单例"""
    db_path = tmp_path / "rest_inbox.db"
    monkeypatch.setattr("server.inbox.get_inbox_config", lambda: {
        "db_path": str(db_path), "auto_cleanup_days": 30,
    })
    reset_store()
    yield
    reset_store()


# ========== InboxStore CRUD ==========

class TestInboxStoreCRUD:
    def test_create_and_get(self, store):
        """创建并获取条目"""
        item = store.create({
            "source": "test_loop",
            "category": "move",
            "title": "测试条目",
            "description": "描述",
            "payload": {"files": ["a.txt", "b.txt"]},
        })
        assert item["id"].startswith("inbox_")
        assert item["source"] == "test_loop"
        assert item["status"] == "pending"
        assert item["payload"] == {"files": ["a.txt", "b.txt"]}
        assert item["created_at"] is not None

        # get 验证
        fetched = store.get(item["id"])
        assert fetched["title"] == "测试条目"
        assert fetched["payload"] == {"files": ["a.txt", "b.txt"]}

    def test_create_with_custom_id(self, store):
        """自定义 ID"""
        item = store.create({
            "id": "custom_001",
            "source": "test",
            "category": "inspect",
            "title": "自定义 ID",
        })
        assert item["id"] == "custom_001"

    def test_create_default_description(self, store):
        """description 默认空字符串"""
        item = store.create({
            "source": "test", "category": "move", "title": "无描述",
        })
        assert item["description"] == ""

    def test_create_default_payload(self, store):
        """payload 默认空 dict"""
        item = store.create({
            "source": "test", "category": "move", "title": "无 payload",
        })
        assert item["payload"] == {}

    def test_get_nonexistent(self, store):
        """获取不存在的条目返回 None"""
        assert store.get("nonexistent_id") is None

    def test_list_all(self, store):
        """列出所有条目"""
        for i in range(5):
            store.create({
                "source": "test", "category": "move", "title": f"条目{i}",
            })
        items = store.list()
        assert len(items) == 5
        # 按 created_at DESC 排序，最新的在前
        assert items[0]["title"] == "条目4"

    def test_list_by_status(self, store):
        """按状态过滤"""
        item1 = store.create({"source": "t", "category": "move", "title": "p1"})
        store.create({"source": "t", "category": "move", "title": "p2"})
        store.update(item1["id"], {"status": "resolved"})

        pending = store.list(status="pending")
        resolved = store.list(status="resolved")
        assert len(pending) == 1
        assert len(resolved) == 1
        assert pending[0]["title"] == "p2"
        assert resolved[0]["title"] == "p1"

    def test_list_by_source(self, store):
        """按来源过滤"""
        store.create({"source": "loop_a", "category": "move", "title": "a1"})
        store.create({"source": "loop_b", "category": "move", "title": "b1"})
        items = store.list(source="loop_a")
        assert len(items) == 1
        assert items[0]["title"] == "a1"

    def test_list_limit(self, store):
        """limit 限制返回数量"""
        for i in range(10):
            store.create({"source": "t", "category": "move", "title": f"i{i}"})
        items = store.list(limit=3)
        assert len(items) == 3

    def test_update_status(self, store):
        """更新状态 → 记录 resolved_at"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        updated = store.update(item["id"], {"status": "resolved"})
        assert updated["status"] == "resolved"
        assert updated["resolved_at"] is not None
        assert updated["updated_at"] is not None

    def test_update_ignored_sets_resolved_at(self, store):
        """ignored 状态也记录 resolved_at"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        updated = store.update(item["id"], {"status": "ignored"})
        assert updated["status"] == "ignored"
        assert updated["resolved_at"] is not None

    def test_update_resolution(self, store):
        """更新 resolution（JSON 字段）"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        updated = store.update(item["id"], {
            "status": "resolved",
            "resolution": {"action": "moved", "target": "/archived"},
        })
        assert updated["resolution"] == {"action": "moved", "target": "/archived"}

    def test_update_title_description(self, store):
        """更新 title 和 description"""
        item = store.create({"source": "t", "category": "move", "title": "原标题"})
        updated = store.update(item["id"], {
            "title": "新标题",
            "description": "新描述",
        })
        assert updated["title"] == "新标题"
        assert updated["description"] == "新描述"

    def test_update_nonexistent(self, store):
        """更新不存在的条目返回 None"""
        assert store.update("nonexistent", {"status": "resolved"}) is None

    def test_update_empty_dict(self, store):
        """空 updates 返回当前条目"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        result = store.update(item["id"], {})
        assert result["id"] == item["id"]

    def test_update_ignores_unknown_fields(self, store):
        """未知字段被忽略"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        result = store.update(item["id"], {"unknown_field": "value", "title": "更新"})
        assert result["title"] == "更新"

    def test_delete(self, store):
        """删除条目"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        assert store.delete(item["id"]) is True
        assert store.get(item["id"]) is None

    def test_delete_nonexistent(self, store):
        """删除不存在的条目返回 False"""
        assert store.delete("nonexistent") is False


# ========== cleanup_resolved ==========

class TestInboxCleanup:
    def test_cleanup_removes_old_resolved(self, store):
        """清理已解决超 N 天的条目"""
        item = store.create({"source": "t", "category": "move", "title": "old"})
        store.update(item["id"], {"status": "resolved"})

        # 手动改 resolved_at 为 31 天前
        from datetime import datetime, timedelta
        old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
        store.conn.execute(
            "UPDATE inbox_items SET resolved_at = ? WHERE id = ?",
            (old_date, item["id"]),
        )
        store.conn.commit()

        deleted = store.cleanup_resolved(days=30)
        assert deleted == 1
        assert store.get(item["id"]) is None

    def test_cleanup_keeps_recent_resolved(self, store):
        """保留最近解决的条目"""
        item = store.create({"source": "t", "category": "move", "title": "recent"})
        store.update(item["id"], {"status": "resolved"})
        deleted = store.cleanup_resolved(days=30)
        assert deleted == 0
        assert store.get(item["id"]) is not None

    def test_cleanup_keeps_pending(self, store):
        """pending 状态不被清理"""
        item = store.create({"source": "t", "category": "move", "title": "pending"})
        deleted = store.cleanup_resolved(days=0)
        assert deleted == 0
        assert store.get(item["id"]) is not None


# ========== batch_op ==========

class TestInboxBatch:
    def test_batch_resolve_by_ids(self, store):
        """批量按 ids 标记 resolved"""
        ids = []
        for i in range(3):
            it = store.create({"source": "t", "category": "move", "title": f"item{i}"})
            ids.append(it["id"])
        result = store.batch_op("resolve", ids=ids)
        assert result["affected"] == 3
        for iid in ids:
            assert store.get(iid)["status"] == "resolved"
            assert store.get(iid)["resolved_at"] is not None

    def test_batch_ignore_by_source(self, store):
        """按 source 批量 ignore"""
        for i in range(2):
            store.create({"source": "loop_alert", "category": "warning", "title": f"w{i}"})
        store.create({"source": "other", "category": "warning", "title": "keep"})
        result = store.batch_op("ignore", source="loop_alert")
        assert result["affected"] == 2
        # other 来源不受影响
        remaining = store.list()
        other = [x for x in remaining if x["source"] == "other"]
        assert len(other) == 1 and other[0]["status"] == "pending"

    def test_batch_delete_by_ids(self, store):
        """批量删除"""
        ids = []
        for i in range(3):
            it = store.create({"source": "t", "category": "move", "title": f"d{i}"})
            ids.append(it["id"])
        result = store.batch_op("delete", ids=ids)
        assert result["affected"] == 3
        for iid in ids:
            assert store.get(iid) is None

    def test_batch_pending_resets_resolved_at(self, store):
        """还原 pending 时清空 resolved_at"""
        it = store.create({"source": "t", "category": "move", "title": "x"})
        store.update(it["id"], {"status": "resolved"})
        assert store.get(it["id"])["resolved_at"] is not None
        store.batch_op("pending", ids=[it["id"]])
        assert store.get(it["id"])["status"] == "pending"
        assert store.get(it["id"])["resolved_at"] is None

    def test_batch_invalid_action_raises(self, store):
        """未知 action 抛 ValueError"""
        with pytest.raises(ValueError):
            store.batch_op("explode", ids=["x"])

    def test_batch_requires_ids_or_filter(self, store):
        """无 ids 且无筛选条件抛 ValueError"""
        with pytest.raises(ValueError):
            store.batch_op("delete")

    def test_batch_empty_ids_raises(self, store):
        """空 ids 列表抛 ValueError"""
        with pytest.raises(ValueError):
            store.batch_op("delete", ids=[])


# ========== get_stats ==========

class TestInboxStats:
    def test_stats_empty(self, store):
        """空库统计"""
        stats = store.get_stats()
        assert stats["available"] is True
        assert stats["total"] == 0
        assert stats["pending"] == 0

    def test_stats_with_items(self, store):
        """有数据的统计"""
        i1 = store.create({"source": "t", "category": "move", "title": "p1"})
        i2 = store.create({"source": "t", "category": "move", "title": "p2"})
        store.create({"source": "t", "category": "move", "title": "p3"})
        store.update(i1["id"], {"status": "resolved"})
        store.update(i2["id"], {"status": "ignored"})

        stats = store.get_stats()
        assert stats["total"] == 3
        assert stats["pending"] == 1
        assert stats["resolved"] == 1
        assert stats["ignored"] == 1


# ========== JSON 序列化 ==========

class TestInboxSerialization:
    def test_payload_unicode(self, store):
        """payload 含中文"""
        item = store.create({
            "source": "t", "category": "move", "title": "中文测试",
            "payload": {"路径": "C:/下载/文件.txt", "类型": "文档"},
        })
        fetched = store.get(item["id"])
        assert fetched["payload"]["路径"] == "C:/下载/文件.txt"
        assert fetched["payload"]["类型"] == "文档"

    def test_payload_nested(self, store):
        """payload 嵌套结构"""
        item = store.create({
            "source": "t", "category": "move", "title": "nested",
            "payload": {"level1": {"level2": {"level3": [1, 2, 3]}}},
        })
        fetched = store.get(item["id"])
        assert fetched["payload"]["level1"]["level2"]["level3"] == [1, 2, 3]

    def test_resolution_null(self, store):
        """resolution 初始为 None"""
        item = store.create({"source": "t", "category": "move", "title": "x"})
        fetched = store.get(item["id"])
        assert fetched["resolution"] is None


# ========== REST 端点 ==========

class TestInboxREST:
    def test_create_via_api(self, isolated_inbox, client):
        """POST /inbox 创建条目"""
        resp = client.post("/inbox", json={
            "source": "rest_test",
            "category": "move",
            "title": "REST 创建",
            "description": "通过 API 创建",
            "payload": {"key": "value"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"].startswith("inbox_")
        assert data["status"] == "pending"
        assert data["payload"] == {"key": "value"}

    def test_list_via_api(self, isolated_inbox, client):
        """GET /inbox 列出条目"""
        client.post("/inbox", json={"source": "t", "category": "move", "title": "item1"})
        client.post("/inbox", json={"source": "t", "category": "move", "title": "item2"})

        resp = client.get("/inbox")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2

    def test_list_filter_status(self, isolated_inbox, client):
        """GET /inbox?status=pending 过滤"""
        r = client.post("/inbox", json={"source": "t", "category": "move", "title": "p1"})
        client.post("/inbox", json={"source": "t", "category": "move", "title": "p2"})
        client.patch(f"/inbox/{r.json()['id']}", json={"status": "resolved"})

        resp = client.get("/inbox?status=pending")
        assert len(resp.json()["items"]) == 1

    def test_get_single(self, isolated_inbox, client):
        """GET /inbox/{id}"""
        r = client.post("/inbox", json={"source": "t", "category": "move", "title": "single"})
        item_id = r.json()["id"]

        resp = client.get(f"/inbox/{item_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "single"

    def test_get_nonexistent_404(self, isolated_inbox, client):
        """GET /inbox/不存在 → 404"""
        resp = client.get("/inbox/nonexistent_id")
        assert resp.status_code == 404

    def test_update_via_api(self, isolated_inbox, client):
        """PATCH /inbox/{id} 更新"""
        r = client.post("/inbox", json={"source": "t", "category": "move", "title": "orig"})
        item_id = r.json()["id"]

        resp = client.patch(f"/inbox/{item_id}", json={
            "status": "resolved",
            "resolution": {"action": "done"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resolved"
        assert data["resolution"] == {"action": "done"}

    def test_update_nonexistent_404(self, isolated_inbox, client):
        """PATCH 不存在 → 404"""
        resp = client.patch("/inbox/nonexistent", json={"status": "resolved"})
        assert resp.status_code == 404

    def test_delete_via_api(self, isolated_inbox, client):
        """DELETE /inbox/{id}"""
        r = client.post("/inbox", json={"source": "t", "category": "move", "title": "del"})
        item_id = r.json()["id"]

        resp = client.delete(f"/inbox/{item_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # 确认已删除
        assert client.get(f"/inbox/{item_id}").status_code == 404

    def test_delete_nonexistent_404(self, isolated_inbox, client):
        """DELETE 不存在 → 404"""
        resp = client.delete("/inbox/nonexistent")
        assert resp.status_code == 404

    def test_cleanup_via_api(self, isolated_inbox, client):
        """POST /inbox/cleanup"""
        resp = client.post("/inbox/cleanup")
        assert resp.status_code == 200
        data = resp.json()
        assert "deleted" in data
        assert "cleanup_days" in data

    def test_batch_via_api(self, isolated_inbox, client):
        """POST /inbox/batch 批量 resolve"""
        ids = []
        for i in range(3):
            r = client.post("/inbox", json={"source": "t", "category": "move", "title": f"b{i}"})
            ids.append(r.json()["id"])
        resp = client.post("/inbox/batch", json={"action": "resolve", "ids": ids})
        assert resp.status_code == 200
        assert resp.json()["affected"] == 3
        for iid in ids:
            assert client.get(f"/inbox/{iid}").json()["status"] == "resolved"

    def test_batch_via_api_invalid_action(self, isolated_inbox, client):
        """未知 action → 422"""
        resp = client.post("/inbox/batch", json={"action": "explode", "ids": ["x"]})
        assert resp.status_code == 422
