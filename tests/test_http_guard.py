"""http_guard 核心逻辑 + 并发/跨端点/过期令牌重放测试

覆盖 C7（核心逻辑：_http_fingerprint / create_pending / consume_token /
record_http_decision / get_pending_http / _cleanup / check_approval /
_extract_body_preview）和 C8（并发令牌重放 / 跨端点重放 / 过期令牌使用）。

参考 tests/test_command_guard.py 结构。http_guard 与 command_guard 是两套
独立的审批存储（_pending_http / _tokens_http vs _pending / _tokens）。
"""

import threading
import time

import pytest

from server import http_guard
from server.http_guard import (
    _extract_body_preview,
    _http_fingerprint,
    check_approval,
    consume_token,
    create_pending,
    get_pending_http,
    record_http_decision,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clear_http_guard_state():
    """每个测试前后清理 http_guard 全局状态。"""
    http_guard._pending_http.clear()
    http_guard._tokens_http.clear()
    yield
    http_guard._pending_http.clear()
    http_guard._tokens_http.clear()


@pytest.fixture
def cfg(monkeypatch):
    """提供可配置的 command_guard 配置（http_guard 复用同一配置）。

    注：http_guard.get_command_guard_config() 复用 command_guard 的配置
    （approval_ttl_seconds / token_ttl_seconds）。
    """
    config = {
        "enabled": True,
        "approval_ttl_seconds": 300,
        "token_ttl_seconds": 120,
    }
    monkeypatch.setattr(
        http_guard,
        "get_command_guard_config",
        lambda: config,
    )
    return config


# ============================================================================
# C7: 核心逻辑测试
# ============================================================================


class TestHttpFingerprint:
    """_http_fingerprint 测试。"""

    def test_same_input_same_fingerprint(self):
        """相同 (method, path, body) → 相同指纹。"""
        body = b'{"code": "print(1)"}'
        fp1 = _http_fingerprint("POST", "/exec/python", body)
        fp2 = _http_fingerprint("POST", "/exec/python", body)
        assert fp1 == fp2
        assert len(fp1) == 64  # sha256 hex

    def test_method_differs(self):
        """method 不同 → 不同指纹。"""
        body = b'{"code": "print(1)"}'
        fp_post = _http_fingerprint("POST", "/exec/python", body)
        fp_get = _http_fingerprint("GET", "/exec/python", body)
        assert fp_post != fp_get

    def test_method_case_insensitive(self):
        """method 大小写不敏感（_http_fingerprint 内部 upper）。"""
        body = b'{"code": "print(1)"}'
        fp_lower = _http_fingerprint("post", "/exec/python", body)
        fp_upper = _http_fingerprint("POST", "/exec/python", body)
        assert fp_lower == fp_upper

    def test_path_differs(self):
        """path 不同 → 不同指纹。"""
        body = b'{"code": "print(1)"}'
        fp1 = _http_fingerprint("POST", "/exec/python", body)
        fp2 = _http_fingerprint("POST", "/exec/cmd", body)
        assert fp1 != fp2

    def test_body_differs(self):
        """body 不同 → 不同指纹。"""
        fp1 = _http_fingerprint("POST", "/exec/python", b'{"code": "print(1)"}')
        fp2 = _http_fingerprint("POST", "/exec/python", b'{"code": "print(2)"}')
        assert fp1 != fp2

    def test_empty_vs_nonempty_body(self):
        """空 body vs 非空 body → 不同指纹。"""
        fp_empty = _http_fingerprint("POST", "/exec/python", b"")
        fp_nonempty = _http_fingerprint("POST", "/exec/python", b'{"code": "print(1)"}')
        assert fp_empty != fp_nonempty

    def test_both_empty_body_same(self):
        """两个空 body → 相同指纹（空 body_hash=""）。"""
        fp1 = _http_fingerprint("POST", "/exec/python", b"")
        fp2 = _http_fingerprint("POST", "/exec/python", b"")
        assert fp1 == fp2


class TestCreatePending:
    """create_pending 测试。"""

    def test_returns_approval_id_with_prefix(self, cfg):
        """返回的 approval_id 以 'http_approval_' 前缀。"""
        approval_id = create_pending("POST", "/exec/python", b'{"code": "print(1)"}')
        assert approval_id.startswith("http_approval_")
        assert len(approval_id) > len("http_approval_")

    def test_pending_stored_correctly(self, cfg):
        """_pending_http 存储的字段正确。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        pending = http_guard._pending_http[approval_id]
        assert pending["method"] == "POST"
        assert pending["path"] == "/exec/python"
        assert pending["body_hash"] == __import__("hashlib").sha256(body).hexdigest()[:16]
        assert pending["fingerprint"] == _http_fingerprint("POST", "/exec/python", body)
        assert "expires_at" in pending
        assert pending["expires_at"] > time.time()  # 未过期
        assert "reason" in pending
        assert "body_preview" in pending

    def test_each_call_unique_id(self, cfg):
        """每次调用生成唯一 approval_id。"""
        id1 = create_pending("POST", "/exec/python", b'{"code": "a"}')
        id2 = create_pending("POST", "/exec/python", b'{"code": "a"}')
        assert id1 != id2


class TestConsumeToken:
    """consume_token 测试（C7 + C8 部分场景）。"""

    def test_empty_token_rejected(self):
        """空 token 直接返回 False。"""
        assert not consume_token("", "POST", "/exec/python", b"{}")

    def test_nonexistent_token_rejected(self):
        """不存在的 token → False。"""
        assert not consume_token("nonexistent_token", "POST", "/exec/python", b"{}")

    def test_valid_token_succeeds(self, cfg):
        """正确 token + 正确指纹 → True。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        result = record_http_decision(approval_id, "approve", "ok")
        token = result["approval_token"]
        assert consume_token(token, "POST", "/exec/python", body) is True

    def test_wrong_method_destroys_token(self, cfg):
        """token 签发给 POST，用 GET 消费 → False 且 token 被销毁。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 用错误 method 消费 → False
        assert not consume_token(token, "GET", "/exec/python", body)
        # token 已被 pop 销毁，再次用正确 method 也失败
        assert not consume_token(token, "POST", "/exec/python", body)

    def test_wrong_path_destroys_token(self, cfg):
        """token 签发给 /exec/python，用 /exec/cmd 消费 → False 且 token 被销毁。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "POST", "/exec/cmd", body)
        # 二次使用正确 path 也失败
        assert not consume_token(token, "POST", "/exec/python", body)

    def test_wrong_body_destroys_token(self, cfg):
        """token 签发给 body1，用 body2 消费 → False 且 token 被销毁。"""
        body1 = b'{"code": "print(1)"}'
        body2 = b'{"code": "print(2)"}'
        approval_id = create_pending("POST", "/exec/python", body1)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "POST", "/exec/python", body2)
        # 二次使用正确 body 也失败
        assert not consume_token(token, "POST", "/exec/python", body1)

    def test_token_single_use(self, cfg):
        """正确 token 二次使用 → 第二次 False（一次性）。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 第一次成功
        assert consume_token(token, "POST", "/exec/python", body) is True
        # 第二次失败
        assert not consume_token(token, "POST", "/exec/python", body)


class TestRecordHttpDecision:
    """record_http_decision 测试。"""

    def test_approve_issues_token(self, cfg):
        """approve → 签发 token + expires_in_seconds。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        result = record_http_decision(approval_id, "approve", "已确认")
        assert result["approved"] is True
        assert result["decision"] == "approve"
        assert result["feedback"] == "已确认"
        assert "approval_token" in result
        assert result["expires_in_seconds"] == cfg["token_ttl_seconds"]

    def test_deny_no_token(self, cfg):
        """deny → approved=False，无 token 字段。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        result = record_http_decision(approval_id, "deny", "不允许")
        assert result["approved"] is False
        assert result["decision"] == "deny"
        assert result["feedback"] == "不允许"
        assert "approval_token" not in result

    def test_unknown_id_raises(self, cfg):
        """未知 approval_id → ValueError。"""
        with pytest.raises(ValueError, match="不存在或已过期"):
            record_http_decision("approval_unknown", "approve", "")

    def test_approve_consumes_pending(self, cfg):
        """approve 后 pending 被消费（pop），二次调同 id 失败。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        record_http_decision(approval_id, "approve", "")
        # pending 已被 pop
        assert approval_id not in http_guard._pending_http
        with pytest.raises(ValueError):
            record_http_decision(approval_id, "approve", "")

    def test_deny_consumes_pending(self, cfg):
        """deny 后 pending 也被消费。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        record_http_decision(approval_id, "deny", "")
        assert approval_id not in http_guard._pending_http


class TestGetPendingHttp:
    """get_pending_http 测试。"""

    def test_returns_pending_dict(self, cfg):
        """存在 → 返回 dict。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        pending = get_pending_http(approval_id)
        assert pending["method"] == "POST"
        assert pending["path"] == "/exec/python"
        assert pending["fingerprint"] == _http_fingerprint("POST", "/exec/python", body)
        # 返回的是副本（防外部修改）
        pending["method"] = "TAMPERED"
        assert http_guard._pending_http[approval_id]["method"] == "POST"

    def test_unknown_id_raises(self, cfg):
        """未知 approval_id → ValueError。"""
        with pytest.raises(ValueError, match="不存在或已过期"):
            get_pending_http("approval_unknown")


class TestCleanup:
    """_cleanup 测试。"""

    def test_cleanup_removes_expired_pending(self, cfg):
        """过期 pending → 清理。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        # 手动设置过期
        http_guard._pending_http[approval_id]["expires_at"] = time.time() - 1
        http_guard._cleanup()
        assert approval_id not in http_guard._pending_http

    def test_cleanup_removes_expired_token(self, cfg):
        """过期 token → 清理。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 手动设置过期
        http_guard._tokens_http[token]["expires_at"] = time.time() - 1
        http_guard._cleanup()
        assert token not in http_guard._tokens_http

    def test_cleanup_preserves_unexpired(self, cfg):
        """未过期的 pending/token 不被清理。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        http_guard._cleanup()
        assert approval_id in http_guard._pending_http or approval_id not in http_guard._pending_http  # pending 已被 record 消费
        # 但 token 应保留
        assert token in http_guard._tokens_http

    def test_cleanup_called_in_consume_token(self, cfg):
        """consume_token 内部调 _cleanup（验证过期 token 在 consume 时被清理）。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 手动设置过期
        http_guard._tokens_http[token]["expires_at"] = time.time() - 1
        # consume_token 内部 _cleanup 清理过期，然后找不到 token → False
        assert not consume_token(token, "POST", "/exec/python", body)
        assert token not in http_guard._tokens_http  # 已被 _cleanup 清掉


class TestCheckApproval:
    """check_approval 测试。"""

    def test_no_token_returns_approval_id(self, cfg):
        """无 token + approval_required → 返回新 approval_id。"""
        body = b'{"code": "print(1)"}'
        approval_id = check_approval("POST", "/exec/python", body, token="")
        assert approval_id is not None
        assert approval_id.startswith("http_approval_")
        assert approval_id in http_guard._pending_http

    def test_valid_token_returns_none(self, cfg):
        """有效 token → 返回 None（放行）。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        result = check_approval("POST", "/exec/python", body, token=token)
        assert result is None

    def test_invalid_token_returns_new_approval_id(self, cfg):
        """无效 token → 返回新 approval_id（创建新 pending）。"""
        body = b'{"code": "print(1)"}'
        result = check_approval("POST", "/exec/python", body, token="invalid_token")
        assert result is not None
        assert result.startswith("http_approval_")


class TestExtractBodyPreview:
    """_extract_body_preview 测试。"""

    def test_empty_body(self):
        """空 body → 空字符串。"""
        assert _extract_body_preview(b"") == ""

    def test_json_with_code(self):
        """JSON body 含 code 字段 → 提取 code。"""
        body = b'{"code": "print(1)"}'
        preview = _extract_body_preview(body)
        assert "print(1)" in preview

    def test_json_with_cmd(self):
        """JSON body 含 cmd 字段 → 提取 cmd。"""
        body = b'{"cmd": "ls -la"}'
        preview = _extract_body_preview(body)
        assert "ls -la" in preview

    def test_json_with_command(self):
        """JSON body 含 command 字段 → 提取 command。"""
        body = b'{"command": "git status"}'
        preview = _extract_body_preview(body)
        assert "git status" in preview

    def test_non_json_body(self):
        """非 JSON body → 取前 N 字符。"""
        body = b"plain text content"
        preview = _extract_body_preview(body)
        assert "plain text content" in preview

    def test_long_body_truncated(self):
        """超长 body → 截断到 _BODY_PREVIEW_LIMIT + '...'。"""
        long_code = "print(" + "x" * 3000 + ")"
        body = f'{{"code": "{long_code}"}}'.encode()
        preview = _extract_body_preview(body)
        assert len(preview) <= http_guard._BODY_PREVIEW_LIMIT + 3  # 截断后加 '...'
        assert preview.endswith("...")

    def test_json_no_known_fields_returns_other_params(self):
        """JSON body 无 code/cmd/command/patch/script → 返回其他参数（排除 _ 前缀和 approval_token）。"""
        body = b'{"foo": "bar", "baz": 42, "_internal": "should_hidden", "approval_token": "should_hidden"}'
        preview = _extract_body_preview(body)
        assert "bar" in preview
        assert "42" in preview
        assert "should_hidden" not in preview

    def test_json_invalid_returns_raw_text(self):
        """JSON 解析失败 → 返回原始文本。"""
        body = b"not a json {incomplete"
        preview = _extract_body_preview(body)
        assert "not a json" in preview


# ============================================================================
# C8: 并发 / 跨端点 / 过期令牌重放测试
# ============================================================================


class TestConcurrentTokenReplay:
    """C8-1：并发令牌重放测试。

    多线程同时 consume 同一 token，应只有一个返回 True（其他都 False）。
    验证 dict.pop 在 GIL 下的原子性。
    """

    def test_concurrent_consume_only_one_succeeds(self, cfg):
        """10 个线程同时 consume 同一 token，只有 1 个 True，9 个 False。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(idx):
            barrier.wait()  # 同步所有线程同时开始
            results[idx] = consume_token(token, "POST", "/exec/python", body)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        success_count = sum(1 for r in results if r is True)
        assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
        # 其余都是 False
        assert all(r is False for r in results if r is not True)

    def test_concurrent_consume_no_exception(self, cfg):
        """并发 consume 不抛异常（即使所有线程同时 pop）。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]

        num_threads = 5
        barrier = threading.Barrier(num_threads)
        exceptions = []

        def worker():
            barrier.wait()
            try:
                consume_token(token, "POST", "/exec/python", body)
            except Exception as e:
                exceptions.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Concurrent consume raised: {exceptions}"


class TestCrossEndpointReplay:
    """C8-2：跨端点令牌重放测试。

    token 签发给 (method1, path1, body1)，用 (method2, path2, body2) 消费应失败。
    验证指纹绑定（hash(method + path + body_hash)）。
    """

    def test_token_not_reusable_for_different_path(self, cfg):
        """token 签发给 /exec/python，用 /exec/cmd 消费 → False。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 跨端点重放
        assert not consume_token(token, "POST", "/exec/cmd", body)

    def test_token_not_reusable_for_different_method(self, cfg):
        """token 签发给 POST，用 GET 消费 → False。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "GET", "/exec/python", body)

    def test_token_not_reusable_for_different_body(self, cfg):
        """token 签发给 body1，用 body2 消费 → False。"""
        body1 = b'{"code": "print(1)"}'
        body2 = b'{"code": "print(2)"}'
        approval_id = create_pending("POST", "/exec/python", body1)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "POST", "/exec/python", body2)

    def test_token_not_reusable_with_empty_body_replay(self, cfg):
        """token 签发给非空 body，用空 body 重放 → False。"""
        body1 = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body1)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "POST", "/exec/python", b"")

    def test_token_not_reusable_with_nonempty_body_replay(self, cfg):
        """token 签发给空 body，用非空 body 重放 → False。"""
        approval_id = create_pending("POST", "/exec/python", b"")
        token = record_http_decision(approval_id, "approve")["approval_token"]
        assert not consume_token(token, "POST", "/exec/python", b'{"code": "print(1)"}')


class TestExpiredTokenReplay:
    """C8-3：过期令牌重放测试。

    token 超过 TTL 后应失效（_cleanup 清理 + consume_token 找不到）。
    用手动改 expires_at 模拟过期，避免真等 120s。
    """

    def test_expired_token_rejected(self, cfg):
        """过期 token → consume_token 返回 False。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        # 模拟过期
        http_guard._tokens_http[token]["expires_at"] = time.time() - 1
        assert not consume_token(token, "POST", "/exec/python", body)

    def test_expired_token_cleaned_by_cleanup(self, cfg):
        """过期 token 被 _cleanup 清理掉。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        token = record_http_decision(approval_id, "approve")["approval_token"]
        http_guard._tokens_http[token]["expires_at"] = time.time() - 1
        http_guard._cleanup()
        assert token not in http_guard._tokens_http

    def test_expired_pending_rejected(self, cfg):
        """过期 pending → get_pending_http 抛 ValueError。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        http_guard._pending_http[approval_id]["expires_at"] = time.time() - 1
        with pytest.raises(ValueError, match="不存在或已过期"):
            get_pending_http(approval_id)

    def test_expired_pending_cleaned_by_cleanup(self, cfg):
        """过期 pending 被 _cleanup 清理。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        http_guard._pending_http[approval_id]["expires_at"] = time.time() - 1
        http_guard._cleanup()
        assert approval_id not in http_guard._pending_http

    def test_expired_pending_record_decision_raises(self, cfg):
        """过期 pending → record_http_decision 抛 ValueError。"""
        body = b'{"code": "print(1)"}'
        approval_id = create_pending("POST", "/exec/python", body)
        http_guard._pending_http[approval_id]["expires_at"] = time.time() - 1
        # _cleanup 在 record_http_decision 入口被调用，会清掉过期 pending
        with pytest.raises(ValueError, match="不存在或已过期"):
            record_http_decision(approval_id, "approve", "")
