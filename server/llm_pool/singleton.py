"""LLM 并发池全局单例管理"""

import threading

from server.llm_pool.pool import LLMPool
from server.llm_pool.types import LLMKey, ProviderPolicy

_global_pool: LLMPool | None = None
_pool_lock = threading.Lock()


def init_pool(keys: list[LLMKey], stats_file: str | None = None,
              default_policy: ProviderPolicy | None = None,
              *, provider_policy: ProviderPolicy | None = None):
    """初始化全局池

    Args:
        keys: LLMKey 列表
        stats_file: per-project token 统计的持久化文件路径
        default_policy: 全局默认 pool 策略（key 无 pool 段时 fallback 用此策略）
        provider_policy: 已废弃，等价于 default_policy（向后兼容旧调用方）
    """
    global _global_pool
    with _pool_lock:
        _global_pool = LLMPool(keys, stats_file=stats_file,
                               default_policy=default_policy or provider_policy)


def get_pool() -> LLMPool:
    """获取全局池实例"""
    global _global_pool
    if _global_pool is None:
        raise RuntimeError("LLM Pool 未初始化，请先调用 init_pool()")
    return _global_pool


def is_initialized() -> bool:
    """检查池是否已初始化。

    必须用此函数而非 `from llm_pool import _global_pool` 后检查 None，
    因为 `init_pool` 通过 `global` 重新赋值 singleton._global_pool，
    而 `from ... import _global_pool` 是值绑定，不会反映后续赋值。
    """
    return _global_pool is not None


def call_llm(messages: list[dict], **kwargs) -> dict:
    """通过全局池调用 LLM（支持 project 参数用于 per-project token 统计）"""
    return get_pool().call(messages, **kwargs)


def call_llm_simple(prompt: str, system_prompt: str | None = None, **kwargs) -> str | None:
    """通过全局池简化调用（支持 project 参数用于 per-project token 统计）"""
    return get_pool().call_simple(prompt, system_prompt, **kwargs)
