"""LLM 并发池模块

管理多个 API key，提供统一的 LLM 调用接口。
支持 per-key 并发限制、429 冷却、自动 key 切换、余额耗尽自动剔除。

可被后端、独立脚本、批处理工具统一导入使用。

用法:
    from server.llm_pool import get_pool, call_llm, call_llm_simple

    # 简单调用
    result = call_llm_simple("请回复OK")
    # 带系统提示
    result = call_llm_simple("分析这段代码", system_prompt="你是代码专家")
    # 原始调用（获取 usage 等）
    result = call_llm([{"role":"user","content":"hello"}])
    # 查看池状态
    status = get_pool().get_status()

v12 重构：原 key_manager.py compat shim 已删除，函数迁入 key_store.py。
"""

# 数据结构
# 后端代理客户端
from server.llm_pool.backend_client import (
    call_via_backend,
    call_via_backend_full,
    check_backend_pool,
    get_backend_pool_status,
    print_backend_pool_status,
)

# v14：压缩模块（OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）
from server.llm_pool.compression import (
    CompressionStats,
    compress_messages,
    estimate_tokens,
    is_cjk,
)

# Key 管理与测试（v12: 原 key_manager.py 已合并到 key_store.py）
from server.llm_pool.key_store import (
    HEALTH_CHECK_INTERVAL_DAYS,
    HEALTH_CHECK_MAX_FAILS,
    _collect_key_records_from_files,
    _extract_key_records_from_file,
    _get_status_dict,
    _load_default_policy,
    _load_keys_records,
    _read_text_lines,
    should_run_health_check,
)
from server.llm_pool.key_store import (
    check_health as check_keys_health,
)
from server.llm_pool.key_store import (
    cleanup_expired as cleanup_expired_keys,
)
from server.llm_pool.key_store import (
    load_llm_keys_for_pool as load_provider_keys,
)

# 并发池核心
from server.llm_pool.pool import LLMPool

# v14：评分路由模块（OmniRoute Phase 3.3 Auto 评分路由借鉴，ADR-0001）
from server.llm_pool.scoring import (
    MODE_PACKS,
    WEIGHT_COST,
    WEIGHT_HEALTH,
    WEIGHT_LATENCY,
    WEIGHT_LKGP,
    WEIGHT_QUOTA,
    WEIGHT_TIER_MATCH,
    score_key,
    score_keys,
)

# 全局单例
from server.llm_pool.singleton import (
    _global_pool,
    _pool_lock,
    call_llm,
    call_llm_simple,
    get_pool,
    init_pool,
    is_initialized,
)

# 统计
from server.llm_pool.stats import _resolve_default_stats_file
from server.llm_pool.types import KeyStats, LLMKey, ProviderPolicy, _normalize_list

__all__ = [
    # 数据结构
    "LLMKey", "KeyStats", "ProviderPolicy", "_normalize_list",
    # 并发池
    "LLMPool",
    # 单例
    "init_pool", "get_pool", "is_initialized", "call_llm", "call_llm_simple",
    "_global_pool", "_pool_lock",
    # Key 管理（v12: 兼容名称仍导出，调用方可逐步切换到 key_store 原名）
    "load_provider_keys", "check_keys_health", "cleanup_expired_keys",
    "should_run_health_check",
    "HEALTH_CHECK_INTERVAL_DAYS", "HEALTH_CHECK_MAX_FAILS",
    "_collect_key_records_from_files",
    "_extract_key_records_from_file", "_get_status_dict", "_load_keys_records",
    "_load_default_policy", "_load_keys_records", "_read_text_lines",
    # 后端代理
    "check_backend_pool", "call_via_backend", "call_via_backend_full",
    "get_backend_pool_status", "print_backend_pool_status",
    # 统计
    "_resolve_default_stats_file",
    # v14：压缩模块
    "CompressionStats", "compress_messages", "estimate_tokens", "is_cjk",
    # v14：评分路由模块
    "score_key", "score_keys", "MODE_PACKS",
    "WEIGHT_HEALTH", "WEIGHT_QUOTA", "WEIGHT_LATENCY",
    "WEIGHT_COST", "WEIGHT_TIER_MATCH", "WEIGHT_LKGP",
]
