"""T01: lib/schema.py + lib/async_utils.py 测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from lib.async_utils import _background_tasks, spawn_background_task
from lib.schema import BaseSchema


def _run(coro):
    """手动 event loop 执行协程（项目模式：不用 pytest-asyncio）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# BaseSchema 测试
# ============================================================================


class _DummyModel(BaseSchema):
    """测试用模型。"""

    name: str
    value: int = 0


class TestBaseSchema:
    def test_extra_forbid_rejects_unknown_field(self):
        """传未定义字段应报 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            _DummyModel(name="test", unknown_field="oops")
        assert "extra_forbidden" in str(exc_info.value) or "extra" in str(exc_info.value).lower()

    def test_extra_forbid_accepts_defined_fields(self):
        """传定义字段应正常创建。"""
        m = _DummyModel(name="test", value=42)
        assert m.name == "test"
        assert m.value == 42

    def test_extra_forbid_rejects_typo_field(self):
        """拼写错误的字段名应报错（而非静默丢弃）。"""
        with pytest.raises(ValidationError):
            _DummyModel(name="test", valu=42)  # typo: valu → value

    def test_model_config_has_extra_forbid(self):
        """model_config 应包含 extra='forbid'。"""
        assert _DummyModel.model_config.get("extra") == "forbid"


# ============================================================================
# spawn_background_task 测试
# ============================================================================


async def _dummy_coro():
    """简单协程，返回 42。"""
    await asyncio.sleep(0.01)
    return 42


async def _failing_coro():
    """抛异常的协程。"""
    await asyncio.sleep(0.01)
    raise RuntimeError("intentional failure")


async def _test_task_held_in_set_impl():
    """task 应被存入全局集合（强引用），done 后移除。"""
    task = spawn_background_task(_dummy_coro(), name="test-hold")
    assert task in _background_tasks
    await task
    assert task not in _background_tasks


async def _test_task_removed_after_done_impl():
    """task 完成后应从全局集合移除。"""
    task = spawn_background_task(_dummy_coro(), name="test-cleanup")
    await task
    assert task not in _background_tasks


async def _test_task_exception_logged_impl():
    """task 抛异常应被记录到日志（不静默丢失）。"""
    with patch("lib.async_utils.logger") as mock_logger:
        task = spawn_background_task(_failing_coro(), name="test-fail")
        # 等待 task 完成；task 本身会 raise，但 done_callback 记录异常
        try:
            await task
        except RuntimeError:
            pass
        # done callback 应被调用且记录了 error
        mock_logger.error.assert_called_once()


async def _test_task_cancelled_impl():
    """task 被取消时应正确清理。"""
    async def _long_coro():
        await asyncio.sleep(100)

    task = spawn_background_task(_long_coro(), name="test-cancel")
    assert task in _background_tasks
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task not in _background_tasks


async def _test_task_name_set_impl():
    """task name 应被正确设置。"""
    task = spawn_background_task(_dummy_coro(), name="my-task")
    assert task.get_name() == "my-task"
    await task


class TestSpawnBackgroundTask:
    def test_task_held_in_set(self):
        _run(_test_task_held_in_set_impl())

    def test_task_removed_after_done(self):
        _run(_test_task_removed_after_done_impl())

    def test_task_exception_logged(self):
        _run(_test_task_exception_logged_impl())

    def test_task_cancelled(self):
        _run(_test_task_cancelled_impl())

    def test_task_name_set(self):
        _run(_test_task_name_set_impl())
