"""用户消息注入模块测试

测试消息存储（线程安全）、路径过滤（should_inject）、
格式化（get_supplement_text）、REST 端点。
"""


import pytest

import server.user_message as um


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试前清空消息状态"""
    um.clear_all()
    yield
    um.clear_all()


# ========== 消息存储 ==========

class TestMessageStorage:
    def test_add_message(self):
        """添加消息"""
        msg = um.add_message("测试指令")
        assert msg["id"] >= 1
        assert msg["text"] == "测试指令"
        assert "timestamp" in msg

    def test_add_multiple_messages(self):
        """添加多条消息，ID 递增"""
        msg1 = um.add_message("第一条")
        msg2 = um.add_message("第二条")
        assert msg2["id"] > msg1["id"]

    def test_get_pending(self):
        """获取待发送消息（不清除）"""
        um.add_message("msg1")
        um.add_message("msg2")
        pending = um.get_pending()
        assert len(pending) == 2
        # 再次获取仍在
        assert len(um.get_pending()) == 2

    def test_consume_pending(self):
        """获取并清除消息"""
        um.add_message("msg1")
        um.add_message("msg2")
        consumed = um.consume_pending()
        assert len(consumed) == 2
        # 清除后为空
        assert um.consume_pending() == []

    def test_clear_all(self):
        """清除所有消息"""
        um.add_message("a")
        um.add_message("b")
        count = um.clear_all()
        assert count == 2
        assert um.get_pending() == []

    def test_clear_all_empty(self):
        """清空空列表返回 0"""
        assert um.clear_all() == 0

    def test_clear_by_id(self):
        """按 ID 删除单条"""
        msg = um.add_message("delete me")
        assert um.clear_by_id(msg["id"]) is True
        assert um.get_pending() == []

    def test_clear_by_id_nonexistent(self):
        """删除不存在的 ID 返回 False"""
        assert um.clear_by_id(99999) is False

    def test_clear_by_id_only_one(self):
        """只删除指定 ID，不影响其他"""
        um.add_message("keep")
        m2 = um.add_message("delete")
        um.clear_by_id(m2["id"])
        pending = um.get_pending()
        assert len(pending) == 1
        assert pending[0]["text"] == "keep"

    def test_has_pending(self):
        """是否有待发送"""
        assert um.has_pending() is False
        um.add_message("x")
        assert um.has_pending() is True
        um.clear_all()
        assert um.has_pending() is False


# ========== should_inject 路径过滤 ==========

class TestShouldInject:
    def test_inject_normal_paths(self):
        """普通路径应注入"""
        assert um.should_inject("/health") is True
        assert um.should_inject("/screen/capture") is True
        assert um.should_inject("/ocr/path/json") is True
        assert um.should_inject("/loop/tasks") is True

    def test_no_inject_excluded_prefixes(self):
        """排除路径不注入"""
        assert um.should_inject("/llm/pool/status") is False
        assert um.should_inject("/mcp") is False
        assert um.should_inject("/mcp/something") is False
        assert um.should_inject("/user/message") is False
        assert um.should_inject("/static/index.html") is False
        assert um.should_inject("/output/abc123") is False
        assert um.should_inject("/docs") is False
        assert um.should_inject("/openapi.json") is False
        assert um.should_inject("/redoc") is False

    def test_inject_exec_paths(self):
        """/exec 路径不排除（注释中已取消排除）"""
        # EXCLUDED_PREFIXES 中 /exec 被注释掉，所以应该注入
        assert um.should_inject("/exec/python") is True

    def test_inject_shutdown(self):
        """/shutdown 不排除（注释中已取消排除）"""
        assert um.should_inject("/shutdown") is True


# ========== get_supplement_text ==========

class TestSupplementText:
    def test_no_messages_returns_none(self):
        """无消息返回 None"""
        assert um.get_supplement_text() is None

    def test_single_message(self):
        """单条消息格式"""
        um.add_message("开始记账")
        text = um.get_supplement_text()
        assert text is not None
        assert "[用户补充指令]" in text
        assert "开始记账" in text

    def test_multiple_messages(self):
        """多条消息带编号"""
        m1 = um.add_message("第一条")
        m2 = um.add_message("第二条")
        text = um.get_supplement_text()
        assert text is not None
        assert f"#{m1['id']}" in text
        assert f"#{m2['id']}" in text
        assert "第一条" in text
        assert "第二条" in text

    def test_supplement_consumes_messages(self):
        """get_supplement_text 消费消息"""
        um.add_message("once")
        text1 = um.get_supplement_text()
        text2 = um.get_supplement_text()
        assert text1 is not None
        assert text2 is None  # 已消费


# ========== REST 端点 ==========

class TestUserMessageREST:
    def test_send_message(self, client):
        """POST /user/message 发送消息"""
        resp = client.post("/user/message", json={"text": "测试指令"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["message"]["text"] == "测试指令"
        assert data["pending_count"] >= 1

    def test_send_empty_rejected(self, client):
        """空文本被拒绝（min_length=1）"""
        resp = client.post("/user/message", json={"text": ""})
        assert resp.status_code == 422

    def test_list_messages(self, client):
        """GET /user/message 列出消息"""
        client.post("/user/message", json={"text": "msg1"})
        client.post("/user/message", json={"text": "msg2"})

        resp = client.get("/user/message")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2

    def test_clear_all_messages(self, client):
        """DELETE /user/message 清除所有"""
        client.post("/user/message", json={"text": "to clear"})
        resp = client.delete("/user/message")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_clear_one_message(self, client):
        """DELETE /user/message/{id} 删除单条"""
        r = client.post("/user/message", json={"text": "delete me"})
        msg_id = r.json()["message"]["id"]

        resp = client.delete(f"/user/message/{msg_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_clear_one_nonexistent(self, client):
        """删除不存在的 ID 返回 not_found"""
        resp = client.delete("/user/message/99999")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"
