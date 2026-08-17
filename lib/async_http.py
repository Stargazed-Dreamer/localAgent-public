"""模块级 httpx client 单例（连接池复用，避免每次请求新建 client）

T07：后端 async 端点中的同步 requests/urllib 全改 httpx。
本模块提供全局 AsyncClient + Client 单例，连接池复用，进程退出时关闭。

设计决策（spec Implementation Decisions #3）：
- 后端用模块级 client 单例（连接池复用），不每次创建
- 进程退出时未关闭可能 warning，可接受

使用方式：
    from lib.async_http import get_async_client, get_sync_client, close_async_client

    # async 端点中：
    client = get_async_client()
    resp = await client.get(url, headers=..., timeout=...)

    # 同步代码中（如被 asyncio.to_thread 包装的函数）：
    client = get_sync_client()
    resp = client.get(url, headers=..., timeout=...)

    # server/main.py shutdown 时：
    await close_async_client()
    close_sync_client()
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("localagent.async_http")

# 模块级单例（懒加载，首次 get_*_client() 时创建）
_async_client: httpx.AsyncClient | None = None
_sync_client: httpx.Client | None = None

# 默认超时（秒）；调用方可在 request 级别覆盖
_DEFAULT_TIMEOUT = 120.0


def get_async_client() -> httpx.AsyncClient:
    """获取全局 httpx.AsyncClient 单例。

    首次调用时创建 client（连接池复用）；若 client 已关闭则重建。
    返回的 client 可被多个 async 端点共享（httpx.AsyncClient 是协程安全的）。
    """
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _async_client


def get_sync_client() -> httpx.Client:
    """获取全局 httpx.Client 同步单例。

    供同步代码使用（如被 asyncio.to_thread 包装的函数、LLMPool.call 同步链路）。
    httpx.Client 是线程安全的，可被多线程共享。
    """
    global _sync_client
    if _sync_client is None or _sync_client.is_closed:
        _sync_client = httpx.Client(timeout=_DEFAULT_TIMEOUT)
    return _sync_client


async def close_async_client() -> None:
    """关闭全局 httpx.AsyncClient 单例（server/main.py shutdown 时调用）。

    幂等：client 已关闭或未创建时安全调用。
    """
    global _async_client
    if _async_client is not None and not _async_client.is_closed:
        try:
            await _async_client.aclose()
        except Exception as e:
            logger.warning("close_async_client: aclose 失败: %s", e)
    _async_client = None


def close_sync_client() -> None:
    """关闭全局 httpx.Client 同步单例（server/main.py shutdown 时调用）。

    幂等：client 已关闭或未创建时安全调用。
    """
    global _sync_client
    if _sync_client is not None and not _sync_client.is_closed:
        try:
            _sync_client.close()
        except Exception as e:
            logger.warning("close_sync_client: close 失败: %s", e)
    _sync_client = None
