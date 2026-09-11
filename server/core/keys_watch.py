"""keys.json 热重载守护：文件变更 → 自动重建 LLM 池

背景（2026-09-01 入站网关踩坑）：LLM 池只在后端启动时从 `data/llm/keys.json`
读一次，之后改文件（例如给某把出站 key 的 `allowed_uses` 补 `inbound_gateway`）
不会自动生效，必须手动 `POST /llm/pool/init`。忘了重载 → 入站网关每次调用都被
`use_case_eligible` 白名单挡光 → `no available keys`，看起来像「功能坏了」。

本模块提供：
- `watch_keys_json_loop()`：后台 asyncio 任务，轮询 keys.json mtime，变了就热重载
- `check_keys_changed_and_reload()`：单 tick 决策（可单测，不依赖 sleep）
- `warn_use_case_coverage()`：启动自检——注册的 use_case 若无任何 key 授权则告警

设计约束：
- **不改 `server/llm_pool/` 子树**，只用其公开 API（`load_provider_keys` / `init_pool`）
- 池内有在途调用时**让位**（延后到下个 tick），避免重建打断流式；有最长让位保护
- 解析失败 / 空 key 一律保持旧池（fail-safe），仅推进 mtime 基准避免每 tick 刷屏
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("localagent.keys_watch")

# 轮询周期（秒）。改一次 keys.json 最多 20s 后自动生效，成本可忽略（一次 stat）。
DEFAULT_INTERVAL = 20.0
# 在途调用让位的最大连续 tick 数（× interval ≈ 最长等待）。超时后强制重载，
# 避免被一条永不结束的流式连接饿死配置更新。
MAX_INFLIGHT_DEFER_TICKS = 15


def _keys_file_mtime() -> float | None:
    """当前 keys.json 的 mtime；文件不存在或无法 stat 返回 None。"""
    try:
        from lib.secret import get_llm_keys_path
        path = get_llm_keys_path()
        return path.stat().st_mtime if path.exists() else None
    except Exception:
        return None


def _load_keys() -> list[Any]:
    """从 keys.json 读取 provider keys（薄封装，便于单测替换）。"""
    from server.llm_pool import load_provider_keys
    return load_provider_keys()


def _init_pool_with(keys: list[Any]) -> None:
    """用给定 keys 重建全局池（薄封装，便于单测替换）。"""
    from server.llm_pool import (
        _load_default_policy,
        _resolve_default_stats_file,
        init_pool,
    )
    init_pool(
        keys,
        stats_file=_resolve_default_stats_file(),
        default_policy=_load_default_policy(),
    )


def _has_inflight_calls() -> bool:
    """池是否还有在途调用（含流式）。查不到状态时保守返回 False（不阻塞重载）。"""
    try:
        from server.llm_pool import get_pool, is_initialized
        if not is_initialized():
            return False
        return get_pool().get_status().get("current_active", 0) > 0
    except Exception:
        return False


def check_keys_changed_and_reload(
    last_mtime: float | None, *, defer: int = 0, max_defer: int = MAX_INFLIGHT_DEFER_TICKS,
) -> tuple[float | None, bool, int]:
    """比较 keys.json mtime，变更则热重载池。

    返回 `(新的 mtime 基准, 是否发生重载, 新的让位计数)`。

    - mtime 未变 / 取不到 → 原样返回基准，不重载
    - 首次调用（last_mtime=None）→ 只记录基准（正常启动路径），不重载
    - mtime 变了但池有在途调用且未超让位上限 → 延后（defer+1），返回原基准
    - mtime 变了、可重载 → 读新 keys：为空则保持旧池并推进基准（避免刷屏），
      否则重建池、推进基准、重置 defer
    """
    cur = _keys_file_mtime()
    if cur is None:
        return last_mtime, False, defer
    if last_mtime is None:
        # 监听启动时采首个基准（此刻池刚由 _ensure_pool_initialized 载入当前文件）
        return cur, False, defer
    if cur == last_mtime:
        return last_mtime, False, defer

    # 文件变了
    if defer < max_defer and _has_inflight_calls():
        logger.info(
            "keys.json 已变更，但 LLM 池有在途调用，延后热重载（%s/%s）",
            defer + 1, max_defer,
        )
        return last_mtime, False, defer + 1

    keys = _load_keys()
    if not keys:
        logger.warning("keys.json 已变更但解析出 0 个 key，保持旧池不变")
        return cur, False, 0

    _init_pool_with(keys)
    logger.info("✅ keys.json 变更 → LLM 池已热重载（%s 个 key）", len(keys))
    warn_use_case_coverage()
    return cur, True, 0


def warn_use_case_coverage() -> dict[str, int]:
    """启动/重载后自检：每个注册 use_case 有多少 key 授权。

    重点看护 `inbound_gateway`：若授权数为 0，入站网关必然每次 `no available keys`，
    这是「配置错」而非「代码坏」，用醒目告警把根因直接指出来。
    """
    summary: dict[str, int] = {}
    try:
        from server.llm_pool import get_pool, is_initialized
        from server.llm_pool.types import USE_CASE_REGISTRY
        if not is_initialized():
            return summary
        keys = get_pool().keys
        for uc_name in USE_CASE_REGISTRY:
            summary[uc_name] = sum(1 for k in keys if k.use_case_eligible(uc_name))
        n_inbound = summary.get("inbound_gateway", 0)
        if n_inbound == 0:
            logger.warning(
                "⚠️ LLM 池内没有任何 key 授权 use_case=inbound_gateway，"
                "入站网关(/v1)将全部返回 no available keys。"
                "请在 keys.json 对应 key 的 allowed_uses 里加入 \"inbound_gateway\"。"
            )
        else:
            logger.info("use_case 覆盖自检: inbound_gateway 授权 key 数=%s", n_inbound)
    except Exception as e:
        logger.warning("use_case 覆盖自检失败（不影响运行）: %s", e)
    return summary


async def watch_keys_json_loop(
    interval: float = DEFAULT_INTERVAL, *, state: dict[str, Any] | None = None,
) -> None:
    """后台循环：轮询 keys.json，变了就热重载池。异常只记日志不致命。"""
    if state is None:
        state = {"last_mtime": None, "defer": 0}
    # 启动即采基准（此刻 _ensure_pool_initialized 已载入当前文件）
    state["last_mtime"] = _keys_file_mtime()
    logger.info("keys.json 热重载监听已启动（周期 %ss）", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            last, reloaded, defer = check_keys_changed_and_reload(
                state["last_mtime"], defer=state["defer"])
            state["last_mtime"] = last
            state["defer"] = defer
            if reloaded:
                logger.info("LLM 池已因 keys.json 变更完成热重载")
        except asyncio.CancelledError:
            logger.info("keys.json 热重载监听已取消")
            raise
        except Exception as e:
            logger.warning("keys.json 热重载 tick 失败（下个周期重试）: %s", e)
