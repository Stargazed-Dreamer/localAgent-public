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

import asyncio
import logging

import httpx

logger = logging.getLogger("localagent.async_http")

# 模块级单例（懒加载，首次 get_*_client() 时创建）
_async_client: httpx.AsyncClient | None = None
_async_client_loop: asyncio.AbstractEventLoop | None = None
_sync_client: httpx.Client | None = None

# 默认超时（秒）；调用方可在 request 级别覆盖
_DEFAULT_TIMEOUT = 120.0


def get_async_client() -> httpx.AsyncClient:
    """获取全局 httpx.AsyncClient 单例。

    首次调用时创建 client（连接池复用）；若 client 已关闭则重建。
    返回的 client 可被多个 async 端点共享（httpx.AsyncClient 是协程安全的）。
    """
    global _async_client, _async_client_loop
    try:
        running_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    # 2026-09-03 修复：is_closed 检测不到「池内空闲连接绑在已关闭的 event loop
    # 上」——TestClient 等测试基建每个测试新开 loop，跨 loop 复用旧连接必抛
    # RuntimeError（被调用方 except 吞掉后表现为误报断连，如 /browser/status
    # 的 TestBrowserTabs flake）。生产 uvicorn 单 loop 下 running_loop 恒等于
    # 创建时的 loop，不触发重建，零行为漂移。
    loop_changed = running_loop is not None and _async_client_loop is not running_loop
    if _async_client is None or _async_client.is_closed or loop_changed:
        if _async_client is not None and not _async_client.is_closed and loop_changed:
            # 旧 client 绑在已关闭的 loop 上，无法安全 aclose，直接丢弃重建
            logger.debug("get_async_client: event loop 变化，丢弃旧 client 重建")
        _async_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        _async_client_loop = running_loop
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
    global _async_client, _async_client_loop
    if _async_client is not None and not _async_client.is_closed:
        try:
            await _async_client.aclose()
        except Exception as e:
            logger.warning("close_async_client: aclose 失败: %s", e)
    _async_client = None
    _async_client_loop = None


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
