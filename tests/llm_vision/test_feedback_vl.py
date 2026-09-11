"""VL 反馈闭环测试

覆盖 verify_prompt 的 3 条未验证路径（上一轮会话遗留）：
1. 成功路径：VL 可用 + understand 返回 ok → description 填充
2. 失败路径：VL 可用 + understand 返回 error / 抛异常 / 空回答 → skipped with reason
3. 降级路径：VL 不可用 → skipped with "远程 VL 不可用"

测试对象：
- server.vl.feedback_vl.describe_after_action — 核心函数，browser.py 和 screen/routes.py 都调它
- server.browser._maybe_vl_feedback — 浏览器集成助手，含 config/截图/异常/文件清理逻辑

screen/routes.py 的 VL 逻辑是内联的，结构与 _maybe_vl_feedback 一致，由
describe_after_action 测试 + 助手测试共同覆盖，无需真实屏幕操作。
"""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from server.vl.feedback_vl import _MAX_DESC_CHARS, describe_after_action

# ==================== Fixtures ====================

@pytest.fixture
def sample_image():
    """小尺寸测试图片"""
    return Image.new("RGB", (100, 100), (255, 255, 255))


@pytest.fixture
def mock_vl(monkeypatch):
    """Mock remote_vl 单例（feedback_vl 模块内的引用）。

    默认：available=True, understand 返回 ok + answer。
    各测试可覆盖 .available / .understand.return_value / .understand.side_effect。
    """
    mock = MagicMock()
    mock.reload_config = MagicMock()
    mock.available = True
    mock.understand = MagicMock(
        return_value={"status": "ok", "answer": "【状态】符合预期\n【观察】正常\n【建议】继续"}
    )
    monkeypatch.setattr("server.vl.feedback_vl.remote_vl", mock)
    return mock


@pytest.fixture
def shot_file():
    """创建临时 PNG 截图文件，返回路径。测试结束后自动清理。"""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="vl_test_")
    os.close(fd)
    Image.new("RGB", (50, 50), (200, 200, 200)).save(path, format="PNG")
    yield path
    if os.path.exists(path):
        os.remove(path)


# ==================== describe_after_action: 成功路径 ====================

class TestDescribeSuccess:
    def test_vl_ok_returns_description(self, sample_image, mock_vl):
        """VL 返回 ok → status=ok, description 填充"""
        mock_vl.understand.return_value = {"status": "ok", "answer": "画面正常"}
        result = describe_after_action(sample_image, "期望状态", True, "操作成功")
        assert result["status"] == "ok"
        assert result["description"] == "画面正常"
        assert result["reason"] == ""
        assert isinstance(result["elapsed_ms"], int)
        assert result["elapsed_ms"] >= 0

    def test_vl_ok_on_failed_action(self, sample_image, mock_vl):
        """操作失败时仍调用 VL（核心价值：失败时描述尤其有价值）"""
        mock_vl.understand.return_value = {"status": "ok", "answer": "看到登录弹窗"}
        result = describe_after_action(sample_image, "期望状态", False, "点击失败")
        assert result["status"] == "ok"
        assert result["description"] == "看到登录弹窗"
        mock_vl.understand.assert_called_once()

    def test_prompt_contains_success_label(self, sample_image, mock_vl):
        """action_succeeded=True → prompt 含 'success' 标签"""
        describe_after_action(sample_image, "期望状态", True, "操作成功")
        prompt = mock_vl.understand.call_args[0][1]
        assert "success" in prompt
        assert "操作成功" in prompt
        assert "期望状态" in prompt

    def test_prompt_contains_fail_label(self, sample_image, mock_vl):
        """action_succeeded=False → prompt 含 'fail' 标签"""
        describe_after_action(sample_image, "期望状态", False, "点击失败")
        prompt = mock_vl.understand.call_args[0][1]
        assert "fail" in prompt
        assert "点击失败" in prompt

    def test_prompt_truncates_long_action_message(self, sample_image, mock_vl):
        """action_message > 300 字 → prompt 中只含前 300 字"""
        long_msg = "X" * 500
        describe_after_action(sample_image, "期望", True, long_msg)
        prompt = mock_vl.understand.call_args[0][1]
        assert "X" * 300 in prompt
        assert "X" * 301 not in prompt

    def test_understand_called_with_correct_params(self, sample_image, mock_vl):
        """understand 接收 image/prompt/timeout/use_case 正确传参"""
        describe_after_action(sample_image, "期望", True, "msg", timeout=15)
        mock_vl.understand.assert_called_once()
        kwargs = mock_vl.understand.call_args[1]
        assert kwargs["timeout"] == 15
        assert kwargs["use_case"] == "vl_vision"
        assert mock_vl.understand.call_args[0][0] is sample_image

    def test_description_truncated_when_too_long(self, sample_image, mock_vl):
        """VL 回答 > _MAX_DESC_CHARS → 截断 + '…'"""
        long_answer = "A" * (_MAX_DESC_CHARS + 100)
        mock_vl.understand.return_value = {"status": "ok", "answer": long_answer}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "ok"
        assert len(result["description"]) == _MAX_DESC_CHARS + 1  # +1 for "…"
        assert result["description"].endswith("…")


# ==================== describe_after_action: 失败路径 ====================

class TestDescribeFailure:
    def test_vl_returns_error_status(self, sample_image, mock_vl):
        """understand 返回 status=error → result status=error, reason 透传 detail"""
        mock_vl.understand.return_value = {"status": "error", "detail": "所有 provider 不可用"}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "error"
        assert result["description"] is None
        assert "所有 provider 不可用" in result["reason"]

    def test_vl_returns_error_without_detail(self, sample_image, mock_vl):
        """understand 返回 error 但无 detail → 使用默认原因"""
        mock_vl.understand.return_value = {"status": "error"}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "error"
        assert "VL 调用失败" in result["reason"]

    def test_vl_raises_exception(self, sample_image, mock_vl):
        """understand 抛异常 → status=error, reason 含异常信息"""
        mock_vl.understand.side_effect = Exception("网络超时")
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "error"
        assert result["description"] is None
        assert "网络超时" in result["reason"]

    def test_vl_returns_empty_answer(self, sample_image, mock_vl):
        """understand 返回 ok 但 answer 为空 → status=error"""
        mock_vl.understand.return_value = {"status": "ok", "answer": ""}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "error"
        assert "空回答" in result["reason"]

    def test_vl_returns_whitespace_answer(self, sample_image, mock_vl):
        """understand 返回 ok 但 answer 仅空白 → strip 后为空 → status=error"""
        mock_vl.understand.return_value = {"status": "ok", "answer": "   \n  "}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "error"
        assert "空回答" in result["reason"]


# ==================== describe_after_action: VL 不可用降级 ====================

class TestDescribeUnavailable:
    def test_vl_unavailable_returns_skipped(self, sample_image, mock_vl):
        """remote_vl.available=False → status=skipped, 不调 understand"""
        mock_vl.available = False
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "skipped"
        assert result["description"] is None
        assert "远程 VL 不可用" in result["reason"]
        mock_vl.understand.assert_not_called()

    def test_reload_config_exception_still_checks_available(self, sample_image, mock_vl):
        """reload_config 抛异常 → 记录 warning 但继续检查 available（不阻塞）"""
        mock_vl.reload_config.side_effect = Exception("config 读取失败")
        mock_vl.understand.return_value = {"status": "ok", "answer": "正常"}
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "ok"
        assert result["description"] == "正常"
        mock_vl.reload_config.assert_called_once()
        mock_vl.understand.assert_called_once()

    def test_reload_config_exception_and_unavailable(self, sample_image, mock_vl):
        """reload_config 抛异常 + VL 不可用 → status=skipped"""
        mock_vl.reload_config.side_effect = Exception("config 读取失败")
        mock_vl.available = False
        result = describe_after_action(sample_image, "期望", True, "msg")
        assert result["status"] == "skipped"
        mock_vl.understand.assert_not_called()


# ==================== _maybe_vl_feedback (browser.py) ====================

class TestMaybeVlFeedback:
    """测试 browser.py 的 _maybe_vl_feedback 助手。

    覆盖：空 verify_prompt、config 关闭、截图缺失、VL 成功/失败/异常、临时文件清理。
    不需要真实浏览器连接——直接调用异步助手函数。
    """

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def test_empty_verify_prompt_returns_none(self, shot_file):
        """verify_prompt 为空 → (None, None, None)，向后兼容"""
        from server.browser import _maybe_vl_feedback
        assert self._run(_maybe_vl_feedback("", shot_file, True, "msg")) == (None, None, None)

    def test_none_verify_prompt_returns_none(self, shot_file):
        """verify_prompt 为 None → (None, None, None)"""
        from server.browser import _maybe_vl_feedback
        assert self._run(_maybe_vl_feedback(None, shot_file, True, "msg")) == (None, None, None)

    @patch("server.browser.vl_feedback.get_browser_config")
    def test_config_disabled_skips(self, mock_cfg, shot_file):
        """feedback_vl_enabled=False → skipped with config 关闭原因"""
        mock_cfg.return_value = {"feedback_vl_enabled": False}
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert result == (None, True, "feedback_vl_enabled=false（config 关闭）")

    @patch("server.browser.vl_feedback.get_browser_config")
    def test_shot_path_none_skips(self, mock_cfg):
        """shot_path=None → skipped"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", None, True, "msg"))
        assert result == (None, True, "截图未生成（页面可能已不可用）")

    @patch("server.browser.vl_feedback.get_browser_config")
    def test_shot_path_not_exist_skips(self, mock_cfg):
        """shot_path 指向不存在的文件 → skipped"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", "/nonexistent/shot.png", True, "msg"))
        assert result == (None, True, "截图未生成（页面可能已不可用）")

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_vl_ok_returns_description(self, mock_cfg, mock_describe, shot_file):
        """describe_after_action 返回 ok → (description, False, None)"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {
            "status": "ok", "description": "画面正常", "reason": "", "elapsed_ms": 50
        }
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", shot_file, True, "操作成功"))
        assert result == ("画面正常", False, None)

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_vl_error_returns_skip_reason(self, mock_cfg, mock_describe, shot_file):
        """describe_after_action 返回 error → (None, True, reason)"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {
            "status": "error", "description": None, "reason": "所有 provider 不可用", "elapsed_ms": 50
        }
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert result == (None, True, "所有 provider 不可用")

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_vl_skipped_returns_skip_reason(self, mock_cfg, mock_describe, shot_file):
        """describe_after_action 返回 skipped（VL 不可用）→ (None, True, reason)"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {
            "status": "skipped", "description": None, "reason": "远程 VL 不可用", "elapsed_ms": 5
        }
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert result == (None, True, "远程 VL 不可用")

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_vl_exception_returns_skip_reason(self, mock_cfg, mock_describe, shot_file):
        """describe_after_action 抛异常 → (None, True, 'VL 反馈异常: ...')"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.side_effect = Exception("内部错误")
        from server.browser import _maybe_vl_feedback
        result = self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert result == (None, True, "VL 反馈异常: 内部错误")

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_shot_file_deleted_after_success(self, mock_cfg, mock_describe, shot_file):
        """VL 成功后临时截图文件被删除（finally 块）"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {"status": "ok", "description": "正常", "reason": "", "elapsed_ms": 10}
        from server.browser import _maybe_vl_feedback
        assert os.path.exists(shot_file)
        self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert not os.path.exists(shot_file)

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_shot_file_deleted_after_error(self, mock_cfg, mock_describe, shot_file):
        """VL 失败后临时截图文件仍被删除（finally 块）"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {"status": "error", "description": None, "reason": "失败", "elapsed_ms": 10}
        from server.browser import _maybe_vl_feedback
        assert os.path.exists(shot_file)
        self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert not os.path.exists(shot_file)

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_shot_file_deleted_after_exception(self, mock_cfg, mock_describe, shot_file):
        """describe_after_action 抛异常后临时截图文件仍被删除（finally 块）"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.side_effect = Exception("崩溃")
        from server.browser import _maybe_vl_feedback
        assert os.path.exists(shot_file)
        self._run(_maybe_vl_feedback("期望", shot_file, True, "msg"))
        assert not os.path.exists(shot_file)

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_action_succeeded_propagated(self, mock_cfg, mock_describe, shot_file):
        """action_succeeded 参数正确传递给 describe_after_action"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {"status": "ok", "description": "x", "reason": "", "elapsed_ms": 1}
        from server.browser import _maybe_vl_feedback
        self._run(_maybe_vl_feedback("期望", shot_file, False, "操作失败"))
        # describe_after_action 经 asyncio.to_thread 调用，位置参数: (image, verify_prompt, action_succeeded, action_message)
        call_args = mock_describe.call_args
        assert call_args[0][2] is False  # action_succeeded
        assert call_args[0][3] == "操作失败"

    @patch("server.browser.vl_feedback.describe_after_action")
    @patch("server.browser.vl_feedback.get_browser_config")
    def test_verify_prompt_propagated(self, mock_cfg, mock_describe, shot_file):
        """verify_prompt 参数正确传递给 describe_after_action"""
        mock_cfg.return_value = {"feedback_vl_enabled": True}
        mock_describe.return_value = {"status": "ok", "description": "x", "reason": "", "elapsed_ms": 1}
        from server.browser import _maybe_vl_feedback
        self._run(_maybe_vl_feedback("搜索结果应出现", shot_file, True, "msg"))
        call_args = mock_describe.call_args
        assert call_args[0][1] == "搜索结果应出现"
