"""asyncio 后台任务安全工具。

问题：asyncio.create_task() 返回的 task 如果未被存入强引用变量，
Python GC 可能回收它，导致任务莫名消失。

解决：spawn_background_task() 将 task 存入全局 set，done 回调自动清理 + 记录异常。
所有 asyncio.create_task 调用应改用此函数。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("localagent.async_utils")

# 全局强引用集合：防止 GC 回收未完成的 task
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background_task[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
) -> asyncio.Task[T]:
    """创建后台 task 并持有强引用，防止被 GC 回收。

    Args:
        coro: 要运行的协程
        name: task 名称（用于日志/调试，对应 asyncio.Task 的 name 参数）

    Returns:
        创建的 asyncio.Task

    行为：
        - task 存入全局 _background_tasks 集合（强引用）
        - task 完成后自动从集合中移除
        - task 抛异常时记录到日志（不会静默丢失）
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """task 完成回调：从全局集合移除 + 记录异常。"""
    _background_tasks.discard(task)
    if task.cancelled():
        logger.debug("background task cancelled: %s", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background task failed: %s: %s: %s",
            task.get_name(),
            type(exc).__name__,
            exc,
            exc_info=exc,
        )
