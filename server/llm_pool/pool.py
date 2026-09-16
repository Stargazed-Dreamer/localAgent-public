"""LLM 并发池核心：多 key 并发调度、重试、token 统计"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from lib.async_http import get_async_client, get_sync_client
from lib.llm_usage import collect_usage, collect_usage_anthropic, text_from_messages
from server.llm_pool.opencode import build_opencode_headers, derive_session_id
from server.llm_pool.stats import _resolve_default_stats_file

# v12: USE_CASE_REGISTRY 从 types.py 导入（原从 key_store.py lazy import，引发循环依赖）
from server.llm_pool.types import USE_CASE_REGISTRY, CallRecord, KeyStats, LLMKey, ProviderPolicy, _infer_is_free

logger = logging.getLogger("localagent.llm_pool")

# 近期调用历史的最大保留条数（环形缓冲）
RECENT_CALL_HISTORY_MAX = 200

# 透传原则：temperature / max_tokens 默认 None =「不写进请求体」，由 provider 用自己的
# 默认值（OpenAI 兼容协议两者都是可选字段）。调用方想约束就显式传值，池不替调用方决定。
# 唯一例外是 Anthropic —— /v1/messages 把 max_tokens 列为必填字段，省略会 400，
# 因此该协议下未指定时回退到此常量。取值与项目内其他 reasoning 场景一致（16384）。
ANTHROPIC_REQUIRED_MAX_TOKENS = 16384


def _is_model_free(model_name: str, privacy_warning: str) -> bool:
    """便捷包装：判断单个 model 是否免费（基于 model 名 + privacy_warning）"""
    return _infer_is_free(model_name, privacy_warning)


# =====================================================================
# v12：上游响应头饱和信号解析（OmniRoute Saturation Reflow 借鉴）
# =====================================================================

# 当 effective saturation 超过此阈值时，_acquire 会优先选其他可用 key
SATURATION_PREFER_OTHERS_THRESHOLD = 0.7


def _parse_openai_rate_limit_saturation(headers) -> float:
    """解析 OpenAI 风格响应头的饱和度（0-1）

    支持的 header（OpenAI / OpenRouter / 大多数 OpenAI 兼容服务）：
    - x-ratelimit-remaining-requests / x-ratelimit-limit-requests
    - x-ratelimit-remaining-tokens / x-ratelimit-limit-tokens

    饱和度 = 1 - (remaining / limit)，取 requests 和 tokens 中更紧张的一个。
    任一字段缺失则忽略，全部缺失返回 0.0（未知）。
    """
    if not headers:
        return 0.0

    def _get(name: str) -> float | None:
        v = headers.get(name) if hasattr(headers, "get") else None
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    sat_requests = 0.0
    rem_r = _get("x-ratelimit-remaining-requests")
    lim_r = _get("x-ratelimit-limit-requests")
    if rem_r is not None and lim_r is not None and lim_r > 0:
        sat_requests = max(0.0, 1.0 - rem_r / lim_r)

    sat_tokens = 0.0
    rem_t = _get("x-ratelimit-remaining-tokens")
    lim_t = _get("x-ratelimit-limit-tokens")
    if rem_t is not None and lim_t is not None and lim_t > 0:
        sat_tokens = max(0.0, 1.0 - rem_t / lim_t)

    # 取更紧张的（即更高的 saturation）
    return max(sat_requests, sat_tokens)


def _parse_anthropic_rate_limit_saturation(headers) -> float:
    """解析 Anthropic 风格响应头的饱和度（0-1）

    优先使用 anthropic-ratelimit-unified-*-utilization（已是百分比）。
    回退到 anthropic-ratelimit-tokens-limit / -tokens-remaining 计算。
    任一字段缺失返回 0.0。
    """
    if not headers:
        return 0.0

    def _get(name: str) -> float | None:
        v = headers.get(name) if hasattr(headers, "get") else None
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 优先用 unified utilization（已是 0-1 范围，但有些版本是 0-100，需归一化）
    # 支持 5h / 1h / requests / tokens 等任意 unified 子字段，按字母序取第一个非空
    util: float | None = None
    if hasattr(headers, "items"):
        for name, val in headers.items():
            if isinstance(name, str) and name.startswith("anthropic-ratelimit-unified-") and name.endswith("-utilization"):
                try:
                    util = float(val)
                    break  # 取第一个即可
                except (TypeError, ValueError):
                    continue
    if util is not None:
        # unified 头是百分比（如 0.42 表示 42%），需归一化
        if util > 1.0:
            util = util / 100.0
        return max(0.0, min(1.0, util))

    # 回退到 tokens 字段计算
    lim_t = _get("anthropic-ratelimit-tokens-limit")
    rem_t = _get("anthropic-ratelimit-tokens-remaining")
    if lim_t is not None and rem_t is not None and lim_t > 0:
        return max(0.0, 1.0 - rem_t / lim_t)

    return 0.0


def _percentile_of(samples, p: float) -> float | None:
    """计算任意样本列表的延迟分位（nearest-rank，p ∈ [0, 100]）

    用于跨 key 聚合时把多个 KeyStats.latency_samples 合并后计算分位。
    """
    if not samples:
        return None
    n = len(samples)
    if n == 1:
        return float(samples[0])
    import math
    rank = max(1, math.ceil(p / 100.0 * n))
    sorted_samples = sorted(samples)
    return float(sorted_samples[min(rank - 1, n - 1)])


# =====================================================================
# v13：Provider 级 Circuit Breaker（OmniRoute CLOSED/HALF_OPEN/OPEN 借鉴）
# =====================================================================

# 熔断器状态常量
CB_CLOSED = "closed"        # 正常放行
CB_HALF_OPEN = "half_open"  # 探针放行（限制并发数）
CB_OPEN = "open"           # 全部拒绝（等待 open_seconds 后转 HALF_OPEN）


class CircuitBreaker:
    """单个 provider（base_url）的熔断器

    状态转换：
        CLOSED  --连续 N 次失败-->  OPEN
        OPEN    --经过 open_seconds-->  HALF_OPEN（放 probe 探针）
        HALF_OPEN --探针成功连续 success_threshold 次-->  CLOSED
        HALF_OPEN --探针失败-->  OPEN（重置 open_seconds 计时）

    线程安全：所有方法假定调用方持有外部锁（LLMPool.lock）。
    """

    __slots__ = ("fail_threshold", "open_seconds", "half_open_probe_count",
                 "success_threshold", "state", "_consecutive_fails",
                 "_opened_at", "_probe_in_flight", "_half_open_successes")

    def __init__(self, fail_threshold: int = 5, open_seconds: float = 60.0,
                 half_open_probe_count: int = 1, success_threshold: int = 2):
        self.fail_threshold = max(1, fail_threshold)
        self.open_seconds = max(1.0, open_seconds)
        self.half_open_probe_count = max(1, half_open_probe_count)
        self.success_threshold = max(1, success_threshold)
        self.state: str = CB_CLOSED
        self._consecutive_fails: int = 0
        self._opened_at: float = 0.0
        self._probe_in_flight: int = 0
        self._half_open_successes: int = 0

    def _maybe_half_open(self, now: float) -> None:
        """OPEN 状态经过 open_seconds 后转 HALF_OPEN（探针预算重置）"""
        if self.state == CB_OPEN and (now - self._opened_at) >= self.open_seconds:
            self.state = CB_HALF_OPEN
            self._probe_in_flight = 0
            self._half_open_successes = 0

    def allow_request(self, now: float) -> bool:
        """是否允许放行请求（调用方持有外部锁）

        - CLOSED：总是允许
        - OPEN：先检查是否该转 HALF_OPEN；转后按 HALF_OPEN 规则
        - HALF_OPEN：若 probe_in_flight < half_open_probe_count 则允许（占用一个探针位）
        """
        self._maybe_half_open(now)
        if self.state == CB_CLOSED:
            return True
        if self.state == CB_OPEN:
            return False
        # HALF_OPEN：限流探针
        if self._probe_in_flight < self.half_open_probe_count:
            self._probe_in_flight += 1
            return True
        return False

    def record_success(self, now: float) -> None:
        """记录一次成功（调用方持有外部锁）"""
        if self.state == CB_HALF_OPEN:
            self._half_open_successes += 1
            self._probe_in_flight = max(0, self._probe_in_flight - 1)
            if self._half_open_successes >= self.success_threshold:
                # 探针连续成功达标 → 转 CLOSED（恢复全量流量）
                self.state = CB_CLOSED
                self._consecutive_fails = 0
                self._half_open_successes = 0
                self._probe_in_flight = 0
        elif self.state == CB_CLOSED:
            # 成功重置失败计数
            self._consecutive_fails = 0

    def record_failure(self, now: float) -> None:
        """记录一次失败（调用方持有外部锁）"""
        if self.state == CB_HALF_OPEN:
            # 探针失败 → 立即回退 OPEN，重置计时
            self.state = CB_OPEN
            self._opened_at = now
            self._probe_in_flight = 0
            self._half_open_successes = 0
            return
        if self.state == CB_CLOSED:
            self._consecutive_fails += 1
            if self._consecutive_fails >= self.fail_threshold:
                # 达到失败阈值 → 转 OPEN
                self.state = CB_OPEN
                self._opened_at = now

    def snapshot(self, now: float) -> dict:
        """返回监控快照"""
        self._maybe_half_open(now)
        return {
            "state": self.state,
            "consecutive_fails": self._consecutive_fails,
            "fail_threshold": self.fail_threshold,
            "open_seconds": self.open_seconds,
            "half_open_probe_count": self.half_open_probe_count,
            "success_threshold": self.success_threshold,
            "opened_at": self._opened_at,
            "seconds_since_opened": round(now - self._opened_at, 1) if self._opened_at > 0 else None,
            "probe_in_flight": self._probe_in_flight,
            "half_open_successes": self._half_open_successes,
        }


# =====================================================================
# T05: OpenAI ↔ Anthropic 工具格式转换
# =====================================================================

def _openai_tools_to_anthropic(tools: list[dict] | None) -> list[dict]:
    """OpenAI tools 格式转 Anthropic tools 格式。

    OpenAI: [{"type": "function", "function": {"name", "description", "parameters"}}]
    Anthropic: [{"name", "description", "input_schema"}]
    """
    if not tools:
        return []
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _openai_tool_choice_to_anthropic(tool_choice) -> dict | None:
    """OpenAI tool_choice 转 Anthropic tool_choice。

    - "auto" → {"type": "auto"}
    - "none" → None（不传 tools 即可）
    - "required" → {"type": "any"}
    - {"type": "function", "function": {"name": "x"}} → {"type": "tool", "name": "x"}
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "none":
            return None
        if tool_choice == "required":
            return {"type": "any"}
        return None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            fn = tool_choice.get("function") or {}
            name = fn.get("name")
            if name:
                return {"type": "tool", "name": name}
        # 已经是 Anthropic 风格直接返回
        if tool_choice.get("type") in {"auto", "any", "tool"}:
            return tool_choice
    return None


def _anthropic_tool_use_to_openai_tool_calls(content_blocks: list) -> list[dict]:
    """把 Anthropic 响应 content 中的 tool_use 块转成 OpenAI tool_calls 格式。

    Anthropic: {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    OpenAI:    {"id": "...", "type": "function", "function": {"name": "...", "arguments": "<json>"}}
    """
    out: list[dict] = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        args = block.get("input")
        try:
            args_json = json.dumps(args, ensure_ascii=False) if args is not None else "{}"
        except (TypeError, ValueError):
            args_json = "{}"
        out.append({
            "id": block.get("id", ""),
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": args_json,
            },
        })
    return out


def _build_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI messages → (system_text, user_messages) for Anthropic /v1/messages.

    非流式 `_call_anthropic` 与原生流式 `_stream_anthropic_sse` 共用，避免重复：
    - system 消息提取到顶层 system（多条以空行拼接）
    - content 为 list（多模态）时取 text 分片拼接
    - role=tool → user 消息含 tool_result 块
    - role=assistant 且带 tool_calls → content 含 tool_use 块
    - 保证至少一条 user 消息
    返回 (system_text, user_messages)。
    """
    system_text = ""
    user_messages: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content_val = m.get("content", "")
        # content 可以是 str 或 list（多模态），先转 str 兜底
        if isinstance(content_val, list):
            # 提取 text 块拼接
            parts = []
            for block in content_val:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            content_str = "\n".join(parts)
        else:
            content_str = str(content_val) if content_val else ""

        if role == "system":
            # 累积 system 消息（可能有多条）
            if system_text:
                system_text += "\n\n" + content_str
            else:
                system_text = content_str
        elif role == "tool":
            # T05+: OpenAI tool 结果消息 → Anthropic user 消息含 tool_result 块
            tool_call_id = m.get("tool_call_id", "")
            blocks = [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content_str}]
            user_messages.append({"role": "user", "content": blocks})
        elif role == "assistant" and m.get("tool_calls"):
            # T05+: OpenAI assistant tool_calls → Anthropic content 含 tool_use 块
            blocks: list[dict] = []
            if content_str:
                blocks.append({"type": "text", "text": content_str})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments", "{}")
                try:
                    args_obj = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except (TypeError, ValueError):
                    args_obj = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args_obj,
                })
            user_messages.append({"role": "assistant", "content": blocks})
        else:
            user_messages.append({"role": role, "content": content_str})

    # Anthropic 要求 messages 至少含一条 user 消息
    if not user_messages:
        user_messages = [{"role": "user", "content": "(empty)"}]
    return system_text, user_messages


# =====================================================================
# v6-lite-streaming-gui T00: 真流式支持
# =====================================================================
# 注：T00 时代的 requests 桥接三件套（_iter_lines_async/_safe_next_line/
# _STREAM_SENTINEL）在 T07 改 httpx 原生 async 流后已无生产调用，5-13 删除；
# 存档测试中的对应用例同步移除。

class ToolCallAccumulator:
    """OpenAI SSE stream 的 tool_call 半包累积器（D04）。

    provider SSE 流的 tool_calls delta 是半包碎片（arguments 字符串可能分多个 chunk）。
    按 index 累积，流结束时 build_all() 返回完整 tool_calls 列表。

    Delta 格式（OpenAI stream）：
        {"index": 0, "id": "call_xxx", "type": "function",
         "function": {"name": "foo", "arguments": "{\"a\":"}}
    后续 delta 只有 arguments 增量：
        {"index": 0, "function": {"arguments": " 1}"}}
    """

    def __init__(self):
        self._calls: dict[int, dict] = {}  # index -> {"id","name","arguments"}

    def on_delta(self, delta: dict) -> None:
        idx = delta.get("index", 0)
        if idx not in self._calls:
            self._calls[idx] = {"id": "", "name": "", "arguments": ""}
        call = self._calls[idx]
        if delta.get("id"):
            call["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            call["name"] = fn["name"]
        if fn.get("arguments"):
            call["arguments"] += fn["arguments"]

    def build_all(self) -> list[dict]:
        """流结束时返回所有完整 tool_calls（OpenAI 格式）"""
        result = []
        for idx in sorted(self._calls.keys()):
            call = self._calls[idx]
            result.append({
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            })
        return result


class LLMPool:
    """多 key 并发池，线程安全"""

    def __init__(self, keys: list[LLMKey], stats_file: str | None = None,
                 default_policy: ProviderPolicy | None = None,
                 *, provider_policy: ProviderPolicy | None = None):
        """初始化键池、线程锁、轮询索引和 per-project token 统计。

        Args:
            keys: LLMKey 列表
            stats_file: per-project token 统计的持久化文件路径
            default_policy: 全局默认 pool 策略（key 无 pool 段时 fallback 用此策略）
            provider_policy: 已废弃，等价于 default_policy（向后兼容旧调用方）
        """
        if stats_file is None:
            stats_file = _resolve_default_stats_file()
        self.keys = keys
        self.lock = threading.Condition()  # Condition 替代 Lock，支持 _acquire 等待时被 _release 唤醒
        self._rr = 0
        # 每个项目的token统计，持久化到JSON文件
        self.stats_file = stats_file
        self.project_stats: dict[str, dict] = {}
        self._stats_dirty_count = 0  # 自上次落盘以来的变更次数（批量保存用）
        self._stats_dirty_threshold = 10  # 每 N 次调用才落盘
        self._load_stats()
        # v14: default_policy 取代 provider_policy；provider_policy 作为废弃别名保留
        self.default_policy = default_policy or provider_policy or ProviderPolicy(name="default")
        # v14: 为 pool_policy=None 的 key 填充全局默认（per-key 策略生效点）
        for k in self.keys:
            if k.pool_policy is None:
                k.pool_policy = self.default_policy
        # v10 新增：近期调用历史环形缓冲（线程安全，由 self.lock 保护）
        self._recent_calls: deque[CallRecord] = deque(maxlen=RECENT_CALL_HISTORY_MAX)
        # v12 新增：LKGP 会话粘性（OmniRoute LKGP 借鉴）
        # session_id → {"key_id": str, "model": str, "last_used": float}
        # 同一会话优先复用上次成功的 (key, model)，减少上下文切换与缓存击穿
        self._session_stickiness: dict[str, dict] = {}
        self._session_stickiness_ttl = 1800.0  # 30 分钟未活跃自动失效
        self._session_stickiness_max = 500     # 防内存膨胀（LRU 上限）
        # v13 新增：多维配额跟踪配置（OmniRoute 多维配额借鉴）
        # 由 _release 在每次调用后写入 key.stats.quota_counters / model_stats[m].quota_counters
        self._quota_tracking_enabled: bool = True
        self._quota_dims: list[tuple[str, int]] = []  # [(dim_str, window_seconds), ...]
        try:
            from server.config import get_llm_quota_tracking_config
            qt_cfg = get_llm_quota_tracking_config()
            self._quota_tracking_enabled = qt_cfg.get("enabled", True)
            self._quota_dims = qt_cfg.get("dimensions", [])
        except Exception:
            # 配置加载失败时 fail-open：禁用配额跟踪（不阻断主流程）
            self._quota_tracking_enabled = False
            self._quota_dims = []
        # v13 新增：Provider 级 Circuit Breaker 配置（OmniRoute 三态熔断借鉴）
        self._breaker_enabled: bool = True
        self._breaker_fail_threshold: int = 5
        self._breaker_open_seconds: float = 60.0
        self._breaker_half_open_probe_count: int = 1
        self._breaker_success_threshold: int = 2
        try:
            from server.config import get_llm_circuit_breaker_config
            cb_cfg = get_llm_circuit_breaker_config()
            self._breaker_enabled = cb_cfg.get("enabled", True)
            self._breaker_fail_threshold = cb_cfg.get("fail_threshold", 5)
            self._breaker_open_seconds = cb_cfg.get("open_seconds", 60.0)
            self._breaker_half_open_probe_count = cb_cfg.get("half_open_probe_count", 1)
            self._breaker_success_threshold = cb_cfg.get("success_threshold", 2)
        except Exception:
            # 配置加载失败时 fail-open：禁用熔断器（不阻断主流程）
            self._breaker_enabled = False
        # v13 新增：Provider 级熔断状态（base_url → CircuitBreaker）
        self._breakers: dict[str, CircuitBreaker] = {}
        # v14 新增：消息压缩配置（OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）
        # call() 在发请求前对 messages 应用压缩；Bloat Protection 保证不反向增加长度
        self._compression_mode: str = "off"
        self._compression_min_length: int = 2000
        self._compression_stats_total: int = 0           # 累计压缩次数
        self._compression_saved_chars_total: int = 0     # 累计节省字符数
        try:
            from server.config import get_llm_compression_config
            comp_cfg = get_llm_compression_config()
            self._compression_mode = comp_cfg.get("mode", "off")
            self._compression_min_length = comp_cfg.get("min_length", 2000)
        except Exception:
            # 配置加载失败时 fail-open：禁用压缩（不阻断主流程）
            self._compression_mode = "off"
        # v14 新增：路由策略配置（OmniRoute Phase 3.3 Auto 评分路由借鉴）
        # strategy="auto" 时用 6 因子加权评分替代 round-robin 主选路（ADR-0001）
        self._routing_strategy: str = "round_robin"
        self._routing_mode_pack: str = "balanced"
        self._last_scores: dict[str, float] = {}  # key.key_id → 上次评分（监控用）
        try:
            from server.config import get_llm_routing_config
            r_cfg = get_llm_routing_config()
            self._routing_strategy = r_cfg.get("strategy", "round_robin")
            self._routing_mode_pack = r_cfg.get("mode_pack", "balanced")
        except Exception:
            # 配置加载失败时 fail-open：默认 round_robin
            self._routing_strategy = "round_robin"
        # v15 新增：model 级降级配置（consecutive_fails + disabled + 探针恢复 + 错误类型退避）
        # 由 _release 在失败时写 model_cooldowns + consecutive_fails + 达阈值 mark_model_disabled
        # 由 call() 在选 model 时排除 disabled（C 保险），并通过 try_acquire_probe 放探针
        self._model_health_enabled: bool = True
        self._model_health_threshold: int = 5
        self._model_health_probe_interval: float = 300.0
        self._model_health_probe_max_concurrency: int = 1
        self._cooldown_base_timeout: float = 5.0
        self._cooldown_base_http_5xx: float = 10.0
        self._cooldown_base_http_other: float = 10.0
        self._cooldown_base_empty_content: float = 15.0
        self._cooldown_base_exception: float = 20.0
        self._cooldown_max: float = 60.0
        try:
            from server.config import get_model_health_config
            mh_cfg = get_model_health_config()
            self._model_health_enabled = mh_cfg.get("enabled", True)
            self._model_health_threshold = int(mh_cfg.get("consecutive_fail_threshold", 5))
            self._model_health_probe_interval = float(mh_cfg.get("probe_interval_seconds", 300.0))
            self._model_health_probe_max_concurrency = int(mh_cfg.get("probe_max_concurrency", 1))
            self._cooldown_base_timeout = float(mh_cfg.get("cooldown_base_timeout", 5.0))
            self._cooldown_base_http_5xx = float(mh_cfg.get("cooldown_base_http_5xx", 10.0))
            self._cooldown_base_http_other = float(mh_cfg.get("cooldown_base_http_other", 10.0))
            self._cooldown_base_empty_content = float(mh_cfg.get("cooldown_base_empty_content", 15.0))
            self._cooldown_base_exception = float(mh_cfg.get("cooldown_base_exception", 20.0))
            self._cooldown_max = float(mh_cfg.get("cooldown_max", 60.0))
        except Exception:
            # 配置加载失败时 fail-open：用默认值（不阻断主流程）
            pass

    @property
    def provider_policy(self) -> ProviderPolicy:
        """已废弃：等价于 default_policy（向后兼容旧调用方和测试）"""
        return self.default_policy

    # ---- per-project token 统计 ----

    def _load_stats(self):
        """从 JSON 文件加载历史 token 统计（支持跨进程续累）"""
        try:
            p = Path(self.stats_file)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                self.project_stats = data.get("projects", {})
        except Exception:
            self.project_stats = {}

    def _save_stats(self):
        """持久化 token 统计到 JSON 文件（合并模式，支持多进程并发写）"""
        try:
            p = Path(self.stats_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            # 读取现有文件数据并合并（避免多进程互相覆盖）
            existing = {}
            if p.exists():
                try:
                    existing = json.loads(p.read_text(encoding="utf-8")).get("projects", {})
                except Exception:
                    existing = {}
            # 合并：以本进程内存数据为准，但保留其他进程写入的项目
            merged = dict(existing)
            for proj, st in self.project_stats.items():
                merged[proj] = st  # 本进程的数据是最新的
            self.project_stats = merged  # 回写到内存，保持同步
            out = {
                "projects": merged,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _record_usage(self, project: str, usage: dict):
        """记录单次调用的 token 消耗到对应 project（批量保存，每 N 次才落盘）"""
        if not project or not usage:
            return
        with self.lock:
            if project not in self.project_stats:
                self.project_stats[project] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                }
            s = self.project_stats[project]
            s["prompt_tokens"] += usage.get("prompt_tokens", 0)
            s["completion_tokens"] += usage.get("completion_tokens", 0)
            s["total_tokens"] += usage.get("total_tokens", 0)
            s["calls"] += 1
            self._stats_dirty_count += 1
            if self._stats_dirty_count >= self._stats_dirty_threshold:
                self._save_stats()
                self._stats_dirty_count = 0

    def flush_stats(self):
        """强制把内存中的统计落盘（shutdown 时调用）"""
        with self.lock:
            self._save_stats()
            self._stats_dirty_count = 0

    # ---- v12: LKGP 会话粘性管理 ----

    def _record_session_stickiness(self, session_id: str, key: LLMKey, model: str):
        """记录会话粘性（在 self.lock 外调用，内部加锁）

        同一 session_id 后续调用会优先复用该 (key, model)。
        超过 _session_stickiness_max 时按 LRU 淘汰最旧记录。
        """
        with self.lock:
            # LRU 淘汰：超过上限时移除最旧的（按 last_used 排序）
            if len(self._session_stickiness) >= self._session_stickiness_max:
                oldest = min(self._session_stickiness.items(), key=lambda x: x[1].get("last_used", 0))
                self._session_stickiness.pop(oldest[0], None)
            self._session_stickiness[session_id] = {
                "key_id": key.key_id or key.name,
                "model": model,
                "last_used": time.time(),
            }

    def _clear_session_stickiness(self, session_id: str):
        """清理会话粘性（在 self.lock 外调用，内部加锁）"""
        with self.lock:
            self._session_stickiness.pop(session_id, None)

    def clear_session(self, session_id: str):
        """公开接口：主动断开会话粘性（用户主动结束会话或换主题时调用）"""
        self._clear_session_stickiness(session_id)

    def get_session_stickiness(self) -> dict:
        """获取当前会话粘性快照（监控/调试用）"""
        with self.lock:
            now = time.time()
            return {
                "total_sessions": len(self._session_stickiness),
                "ttl_seconds": self._session_stickiness_ttl,
                "max_sessions": self._session_stickiness_max,
                "sessions": [
                    {
                        "session_id": sid,
                        "key_id": info.get("key_id"),
                        "model": info.get("model"),
                        "last_used": info.get("last_used", 0),
                        "age_seconds": round(now - info.get("last_used", 0), 1),
                        "expires_in_seconds": round(
                            self._session_stickiness_ttl - (now - info.get("last_used", 0)), 1
                        ),
                    }
                    for sid, info in self._session_stickiness.items()
                ],
            }

    def get_project_stats(self) -> dict:
        """获取 per-project token 统计"""
        with self.lock:
            return {
                "projects": dict(self.project_stats),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    def import_project_stats(self, project: str, prompt_tokens: int,
                             completion_tokens: int, total_tokens: int, calls: int):
        """手动导入历史 token 数据（用于从 checkpoint 等外部数据源补录）"""
        with self.lock:
            if project not in self.project_stats:
                self.project_stats[project] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                }
            s = self.project_stats[project]
            s["prompt_tokens"] += prompt_tokens
            s["completion_tokens"] += completion_tokens
            s["total_tokens"] += total_tokens
            s["calls"] += calls
            self._save_stats()

    def _acquire(self, timeout: float = 300, model: str | None = None,
                 use_case: str | None = None, tier=None,
                 session_id: str | None = None) -> LLMKey | None:
        """获取一个可用的 key，阻塞等待直到有 key 可用或超时

        使用 threading.Condition.wait() 替代 sleep 循环，_release 时 notify 立即唤醒。

        Args:
            model: 软偏好——优先选含此 model 的 key；同 tier 内若无此 model 自动 fallback 到其它 model
            use_case: 若指定，只从 use_case_eligible() 通过的 key 里选（敏感用途过滤）
            tier: 硬过滤——支持 int(1-5)/str/tuple(min,max)。
                  None 时若 use_case 指定，自动查 USE_CASE_REGISTRY.default_tier（范围）。
            session_id: v12 LKGP 会话粘性——同一会话优先复用上次成功的 (key, model)。
                  命中条件：粘性记录未过期 + key 仍 enabled + 该 model 在 candidates 中 + 该 model 未在 model_cooldown 中。
                  命中失败时回退到正常 round-robin 流程（不阻塞）。
        """
        from server.llm_pool.key_store import _normalize_tier_range

        # tier 解析为范围：显式传入 > use_case.default_tier
        tier_range = _normalize_tier_range(tier)
        if tier_range == (0, 0) and use_case:
            # v12: USE_CASE_REGISTRY 已从 types.py 顶部导入
            uc = USE_CASE_REGISTRY.get(use_case)
            if uc:
                tier_range = uc.default_tier

        deadline = time.time() + timeout
        with self.lock:
            while time.time() < deadline:
                # 候选 key：use_case 过滤 + tier 范围匹配
                candidates = self.keys
                if use_case:
                    candidates = [k for k in candidates if k.use_case_eligible(use_case)]
                if tier_range != (0, 0):
                    # tier 范围过滤：key 必须含至少一个 tier 在范围内的 model
                    candidates = [k for k in candidates if k.has_tier_in_range(tier_range[0], tier_range[1])]
                    if not candidates:
                        return None  # 没有 tier 在范围内的 key

                # model 软偏好：优先选含此 model 的 key，但不强制（同 tier 内可 fallback）
                if model:
                    preferred = [k for k in candidates if model in k.models]
                    if preferred:
                        candidates = preferred
                    # 若 preferred 为空，继续用 candidates（fallback 到同 tier 其它 model）

                # v12：LKGP 会话粘性——同一会话优先复用上次成功的 (key, model)
                # 命中条件：粘性记录存在 + 未 TTL 过期 + key 在 candidates 中 + key 可用 + 该 model 未在 model_cooldown
                # 失败时静默回退到 round-robin，不阻塞调用方
                if session_id:
                    sticky = self._session_stickiness.get(session_id)
                    if sticky:
                        now_ts = time.time()
                        if now_ts - sticky.get("last_used", 0) > self._session_stickiness_ttl:
                            # TTL 过期，清理
                            self._session_stickiness.pop(session_id, None)
                        else:
                            sticky_key_id = sticky.get("key_id")
                            sticky_model = sticky.get("model")
                            sticky_key = next(
                                (k for k in candidates
                                 if k.key_id == sticky_key_id or k.name == sticky_key_id),
                                None
                            )
                            # 命中：key 存在 + 可用 + model 在 key 的 models 中 + 该 model 未在 model_cooldown
                            if (sticky_key and sticky_key.is_available
                                and sticky_model in sticky_key.models
                                and sticky_model not in (
                                    {m for m, cd in sticky_key.model_cooldowns.items() if cd > now_ts}
                                )):
                                # 软偏好 model 校验：若调用方指定了 model 且与粘性 model 不同，仍尊重调用方
                                # （粘性只在调用方未指定 model 或指定 model 与粘性 model 一致时生效）
                                if not model or model == sticky_model:
                                    sticky_key.active_count += 1
                                    # 更新 last_used 维持粘性活跃（仅命中时刷新，未命中不刷新）
                                    sticky["last_used"] = now_ts
                                    return sticky_key

                # round-robin 找可用 key（v12：饱和信号优先选 saturation 低的）
                # 策略：先在低 saturation (< threshold) 的可用 key 中 round-robin；
                #       若全无低 saturation 可用 key，则在所有可用 key 中 round-robin（含高 saturation）。
                # 这样既保留公平性，又主动避免硬 429。
                # v13：Circuit Breaker OPEN 的 key 不进入 available_now（provider 已熔断）
                # v15：A 保险——跳过 tier 范围内无可用 model 且无 disabled model 的 key
                #       （disabled model 保留用于探针 fallback，由 call() 里 try_acquire_probe 处理）
                now_ts = time.time()
                available_now: list[LLMKey] = []
                low_sat_available: list[LLMKey] = []
                # v15：A 保险用的 tier 范围（未指定 tier 时用 (1,5) 全范围）
                _a_min_t, _a_max_t = tier_range if tier_range != (0, 0) else (1, 5)
                for k in candidates:
                    if not k.is_available:
                        continue
                    # v13：跳过熔断器 OPEN 的 key（HALF_OPEN 仍允许，由 acquire 时 allow_request 限流探针）
                    if self._breaker_enabled and k.base_url:
                        b = self._breakers.get(k.base_url)
                        if b is not None:
                            b._maybe_half_open(now_ts)
                            if b.state == CB_OPEN:
                                continue
                    # v15：A 保险——检查 tier 范围内是否有可用 model（排除 disabled + cooldown）
                    # v16：补 cooldown 检查——tier 匹配 model 全在 429 冷却时也应跳过该 key，
                    #       避免 _acquire 放行后 call() 的 fallback 降级到非 tier 范围 model
                    if self._model_health_enabled:
                        has_avail = any(
                            _a_min_t <= k.model_tiers.get(m, 3) <= _a_max_t
                            and m not in k.model_disabled
                            and (m not in k.model_cooldowns or now_ts >= k.model_cooldowns[m])
                            for m in k.models
                        )
                        if not has_avail:
                            # 无可用 model，检查是否有 disabled model 在 tier 范围内（探针 fallback）
                            has_disabled = any(
                                _a_min_t <= k.model_tiers.get(m, 3) <= _a_max_t
                                and m in k.model_disabled
                                for m in k.models
                            )
                            if not has_disabled:
                                continue  # 跳过该 key（无可用 model 且无 disabled model）
                    available_now.append(k)
                    if k.effective_saturation() < SATURATION_PREFER_OTHERS_THRESHOLD:
                        low_sat_available.append(k)

                pick_pool = low_sat_available if low_sat_available else available_now
                if pick_pool:
                    # v14：路由策略分支
                    # - round_robin（默认）: 按 candidates 顺序 round-robin
                    # - auto: 用 6 因子加权评分（ADR-0001）选最高分 key
                    if self._routing_strategy == "auto":
                        picked = self._pick_by_score(
                            pick_pool, use_case=use_case, tier_range=tier_range,
                            session_id=session_id, now_ts=now_ts,
                        )
                        if picked is not None:
                            picked.active_count += 1
                            return picked
                        # 评分选路失败（如所有候选都被 HALF_OPEN 限流）→ 等待并重试
                    else:
                        # 在 pick_pool 内 round-robin（按 candidates 顺序找首个在 pick_pool 中的 key）
                        # v13：HALF_OPEN 探针限流——allow_request 原子检查+占用探针位
                        for _ in range(len(candidates)):
                            k = candidates[self._rr % len(candidates)]
                            self._rr += 1
                            if k in pick_pool:
                                # v13：熔断器探针检查（CLOSED 总通过；HALF_OPEN 占用探针位；OPEN 已被过滤）
                                if self._breaker_blocks_key(k, now_ts):
                                    continue
                                k.active_count += 1
                                return k
                        # 兜底：round-robin 未命中 pick_pool 时，取首个未被熔断器阻止的 key
                        for k in pick_pool:
                            if not self._breaker_blocks_key(k, now_ts):
                                k.active_count += 1
                                return k
                        # 所有候选都被 HALF_OPEN 探针限流阻止 → 等待并重试

                # 全部忙或冷却中，找最快可用的（优先 saturation 低的）
                available = [k for k in candidates if not k.is_expired]
                if not available:
                    return None  # 所有 key 都过期了

                # v12：等待时优先选 saturation 低 + cooldown 早 + active 少的 key
                best = min(available, key=lambda k: (
                    k.effective_saturation(),
                    k.cooldown_until,
                    k.active_count,
                ))
                wait = max(0, best.cooldown_until - time.time())
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # Condition.wait 释放锁让 _release 能修改状态，被 notify 唤醒
                self.lock.wait(timeout=min(max(wait, 0.05), remaining, 5.0))

        return None  # 超时

    def _release(self, key: LLMKey, success: bool = False,
                 rate_limited: bool = False, cooldown: float = 0,
                 expired: bool = False, tokens: int = 0,
                 model: str = "", error: str = "",
                 saturation: float | None = None,
                 duration_ms: float | None = None,
                 error_type: str = ""):
        """释放 key（并唤醒 _acquire 中等待的线程）

        v10 新增 model 参数：同时更新 per-model 统计，区分配置 vs 实际可用性。
        v12 新增 Model Lockout：rate_limited 时同时写 model 级 cooldown，
        单 model 429 不再冻结整个 key 上其他可用 tier 模型。
        v12 新增 saturation 参数：上游响应头饱和信号写回（OmniRoute Saturation Reflow）。
        v13 新增 duration_ms 参数：记录到 key.stats 和 model_stats 的 latency_samples，
        供 p50/p95/p99 延迟分位计算（成功/失败均记录，便于观察尾延迟）。
        v15 新增 error_type + model 级降级：
        - error_type ∈ {"timeout", "http_5xx", "http_other", "empty_content", "exception", ""}
          用于按 D2 退避映射表计算 cooldown base（timeout=5s / http_5xx=10s / http_other=10s
          / empty_content=15s / exception=20s），再按 D3 指数退避 min(base*2^attempt, cooldown_max)。
        - 失败时 model_stats[m].consecutive_fails += 1；达阈值 mark_model_disabled。
        - 成功时 model_stats[m].consecutive_fails = 0；若该 model 处于探针中，调 resolve_probe(True)。
        - 429/401/402 不计 consecutive_fails（已有 model_cooldown / expired 机制）。
        """
        with self.lock:
            key.active_count = max(0, key.active_count - 1)
            key.stats.last_used = time.time()
            if success:
                key.stats.ok += 1
                key.stats.total_tokens += tokens
            elif rate_limited:
                key.stats.rate_limited += 1
                # key 级 cooldown 保留作 fallback 信号（_acquire 会用）
                key.cooldown_until = max(key.cooldown_until, time.time() + cooldown)
                # v12：model 级 cooldown（精细化隔离，不波及同 key 其他 model）
                if model and cooldown > 0:
                    key.record_model_cooldown(model, time.time() + cooldown)
                # v12：429 时上游必已饱和，写入 saturation=1.0（若未显式传入）
                if saturation is None:
                    saturation = 1.0
            elif expired:
                key.stats.expired = True
                key.stats.last_error = "expired"
            else:
                key.stats.fail += 1
                # v15：非 429/401/402 失败 → 错误类型分类 + 退避 + consecutive_fails + 探针 resolve
                if self._model_health_enabled and model:
                    self._apply_model_health_failure(key, model, error, error_type)
            # v10：同步更新 per-model 统计
            if model:
                key.record_model_stats(
                    model, success=success, rate_limited=rate_limited,
                    expired=expired, tokens=tokens, error=error,
                    duration_ms=duration_ms,
                )
            # v15：成功时清零 consecutive_fails + 探针 resolve（在 record_model_stats 之后，
            # 因为 record_model_stats 不会清零 consecutive_fails）
            if success and model and self._model_health_enabled:
                ms = key.model_stats.get(model)
                if ms is not None and ms.consecutive_fails > 0:
                    ms.consecutive_fails = 0
                # 若该 model 处于 disabled 探针中，标记探针成功
                if model in key.model_disabled:
                    key.resolve_probe(model, success=True)
            # v13：key 级延迟样本（与 model_stats 独立，反映整个 key 的延迟分布）
            if duration_ms is not None:
                key.stats.record_latency(duration_ms)
            # v12：上游饱和信号写回（在 self.lock 内保证线程安全）
            if saturation is not None:
                key.update_saturation(saturation)
            # v13：多维配额跟踪（OmniRoute percent/requests/tokens × 5h/h/d/w/mo 借鉴）
            # 每次 _release 都计入 requests/* 维度；tokens>0 时计入 tokens/* 维度
            # 失败调用也计 requests（反映真实请求速率，包含重试）
            if self._quota_tracking_enabled and self._quota_dims:
                key.stats.record_quota(self._quota_dims, tokens=tokens)
                if model:
                    ms = key.model_stats.get(model)
                    if ms is not None:
                        ms.record_quota(self._quota_dims, tokens=tokens)
            # v13：Provider 级 Circuit Breaker 状态更新（OmniRoute 三态熔断借鉴）
            # success → record_success（CLOSED 重置计数 / HALF_OPEN 探针累计成功）
            # rate_limited / fail → record_failure（达到阈值转 OPEN / HALF_OPEN 探针失败回退 OPEN）
            # expired 不计失败（key 失效不是 provider 故障，由调度器跳过该 key 即可）
            if self._breaker_enabled and key.base_url:
                breaker = self._get_or_create_breaker(key.base_url)
                now_ts = time.time()
                if success:
                    breaker.record_success(now_ts)
                elif rate_limited or (not expired):
                    breaker.record_failure(now_ts)
            self.lock.notify_all()  # 唤醒 _acquire 中等待的线程

    # ---- v15: model 级降级辅助 ----

    # 错误类型 → cooldown base 映射（D2 退避映射表）
    _ERROR_TYPE_COOLDOWN_BASE: dict[str, str] = {
        "model_unavailable": "_cooldown_base_exception",
        "timeout": "_cooldown_base_timeout",
        "http_5xx": "_cooldown_base_http_5xx",
        "http_other": "_cooldown_base_http_other",
        "empty_content": "_cooldown_base_empty_content",
        "exception": "_cooldown_base_exception",
    }

    def _infer_error_type(self, error: str, error_type: str) -> str:
        """v15：从 error 字段启发式推断错误类型（调用方未传 error_type 时使用）

        推断规则（按优先级）：
        - 显式传入 error_type 非空 → 直接返回
        - error 含 "timeout" → "timeout"
        - error 含 "empty content" → "empty_content"
        - error 含 "HTTP 5" / "HTTP 500" / "HTTP 502" / "HTTP 503" → "http_5xx"
        - error 含 "HTTP" → "http_other"
        - 其他 → "exception"
        """
        if error_type:
            return error_type
        if not error:
            return "exception"
        e = error.lower()
        # v17：model_not_found / no available channel / has no provider supported
        # → 模型已下线/中转站无渠道，不会自愈，直接 disable（跳过 consecutive_fails 阈值）
        if any(kw in e for kw in (
            "model_not_found", "model not found",
            "no available channel for model",
            "has no provider supported",
        )):
            return "model_unavailable"
        if "timeout" in e:
            return "timeout"
        if "empty content" in e:
            return "empty_content"
        # HTTP 状态码启发式
        if "http 5" in e or "http 500" in e or "http 502" in e or "http 503" in e:
            return "http_5xx"
        if "http" in e:
            return "http_other"
        return "exception"

    def _apply_model_health_failure(self, key: LLMKey, model: str,
                                    error: str, error_type: str) -> None:
        """v15：对非 429/401/402 失败应用 model 级降级（调用方持有 self.lock）

        副作用：
        1. 按错误类型 + 指数退避写 model_cooldowns[model]
        2. model_stats[m].consecutive_fails += 1
        3. consecutive_fails 达阈值 → mark_model_disabled(model)
        4. 若该 model 处于探针中（probe_in_flight=True），标记探针失败 → resolve_probe(False)
        """
        if not model:
            return
        # 探针失败：若该 model 当前是探针（probe_in_flight=True），resolve_probe(success=False)
        # 注意：探针失败也算 consecutive_fails += 1（避免探针一直失败但不累计）
        was_probe = (model in key.model_disabled
                     and key.model_disabled[model].get("probe_in_flight", False))

        resolved_type = self._infer_error_type(error, error_type)
        base_attr = self._ERROR_TYPE_COOLDOWN_BASE.get(resolved_type, "_cooldown_base_exception")
        base = float(getattr(self, base_attr, self._cooldown_base_exception))

        # v17：model_unavailable（model_not_found / no available channel / has no provider supported）
        # → 模型已下线/中转站无渠道，不会自愈，直接 disable（跳过 consecutive_fails 阈值）
        # 探针机制仍然存在：probe_interval（默认 300s）后会探针尝试，若模型恢复则自动解除 disabled
        if resolved_type == "model_unavailable":
            key.mark_model_disabled(model)
            if was_probe:
                key.resolve_probe(model, success=False)
            return

        ms = key.model_stats.get(model)
        if ms is None:
            ms = KeyStats()
            key.model_stats[model] = ms
        # 指数退避：attempt = 当前 consecutive_fails（增加前的值）
        # attempt=0 首次失败 → base*1 = base；attempt=1 → base*2；attempt=2 → base*4...
        attempt = max(0, ms.consecutive_fails)
        cooldown = min(base * (2 ** attempt), self._cooldown_max)
        key.record_model_cooldown(model, time.time() + cooldown)

        ms.consecutive_fails += 1
        if ms.consecutive_fails >= self._model_health_threshold:
            key.mark_model_disabled(model)

        # 探针失败 resolve（保留 disabled 状态，重置 probe_in_flight + last_probe_at）
        if was_probe:
            key.resolve_probe(model, success=False)

    # ---- v13: Provider 级 Circuit Breaker 辅助 ----

    def _get_or_create_breaker(self, base_url: str) -> CircuitBreaker:
        """获取或创建指定 base_url 的熔断器（调用方持有 self.lock）"""
        b = self._breakers.get(base_url)
        if b is None:
            b = CircuitBreaker(
                fail_threshold=self._breaker_fail_threshold,
                open_seconds=self._breaker_open_seconds,
                half_open_probe_count=self._breaker_half_open_probe_count,
                success_threshold=self._breaker_success_threshold,
            )
            self._breakers[base_url] = b
        return b

    def _breaker_blocks_key(self, key: LLMKey, now: float) -> bool:
        """检查 key 的 base_url 熔断器是否阻止此次请求（调用方持有 self.lock）

        - 熔断器 disabled 或 base_url 为空 → 不阻止
        - 熔断器 OPEN → 阻止
        - 熔断器 HALF_OPEN 且探针预算已用尽 → 阻止
        - 否则 → 不阻止（同时由 allow_request 占用探针位）
        """
        if not self._breaker_enabled or not key.base_url:
            return False
        b = self._breakers.get(key.base_url)
        if b is None:
            return False
        return not b.allow_request(now)

    def get_breakers_snapshot(self) -> dict:
        """返回所有熔断器状态快照（监控用）"""
        with self.lock:
            now = time.time()
            return {
                "enabled": self._breaker_enabled,
                "fail_threshold": self._breaker_fail_threshold,
                "open_seconds": self._breaker_open_seconds,
                "half_open_probe_count": self._breaker_half_open_probe_count,
                "success_threshold": self._breaker_success_threshold,
                "breakers": {
                    base_url: b.snapshot(now)
                    for base_url, b in self._breakers.items()
                },
            }

    def _pick_by_score(
        self,
        pick_pool: list[LLMKey],
        *,
        use_case: str | None,
        tier_range: tuple[int, int],
        session_id: str | None,
        now_ts: float,
    ) -> LLMKey | None:
        """v14：用 6 因子加权评分（ADR-0001）从 pick_pool 选最高分 key（调用方持有 self.lock）

        - 评分在已通过基础过滤（熔断器 OPEN / expired / cooldown / max_concurrency 满）的可用 key 间做
        - HALF_OPEN 探针限流仍由 _breaker_blocks_key 原子检查
        - 失败时返回 None（调用方等待并重试）
        - 更新 self._last_scores 供监控（key_id/name → score）
        """
        try:
            from server.llm_pool.scoring import score_keys
        except Exception:
            # 模块加载失败 → fail-open 回退到 round-robin（取 pick_pool[0]）
            return pick_pool[0] if pick_pool else None
        # 取 sticky_key_id 供 lkgp_bonus 因子使用
        sticky_key_id: str | None = None
        if session_id:
            sticky = self._session_stickiness.get(session_id)
            if sticky and now_ts - sticky.get("last_used", 0) <= self._session_stickiness_ttl:
                sticky_key_id = sticky.get("key_id")
        scored = score_keys(
            pick_pool,
            use_case=use_case,
            resolved_tier_range=tier_range,
            session_id=session_id,
            sticky_key_id=sticky_key_id,
            now=now_ts,
        )
        # 更新 last_scores 监控字段
        for k, s in scored:
            self._last_scores[k.key_id or k.name] = round(s, 4)
        # 按评分降序找首个未被熔断器阻止的 key
        for k, _score in scored:
            if not self._breaker_blocks_key(k, now_ts):
                return k
        return None

    def _record_call(self, key_name: str, model: str, success: bool,
                     tokens: int = 0, error: str = "", duration_ms: int = 0) -> None:
        """记录一次调用到近期历史环形缓冲（在 self.lock 内调用）

        v10 新增：供监控面板展示 per-model 近期调用情况。
        """
        rec = CallRecord(
            ts=time.time(),
            key_name=key_name,
            model=model,
            success=success,
            tokens=tokens,
            error=error[:200] if error else "",
            duration_ms=duration_ms,
        )
        self._recent_calls.append(rec)

    def get_recent_calls(self, limit: int = 50, model: str | None = None,
                         key_name: str | None = None, only_failed: bool = False) -> list[dict]:
        """获取近期调用历史（最新在前）

        Args:
            limit: 最多返回条数（默认 50，最大 200）
            model: 若指定，只返回该 model 的调用
            key_name: 若指定，只返回该 key 的调用
            only_failed: 若 True，只返回失败的调用
        """
        with self.lock:
            items = list(self._recent_calls)
        items.reverse()  # 最新在前
        if model:
            items = [r for r in items if r.model == model]
        if key_name:
            items = [r for r in items if r.key_name == key_name]
        if only_failed:
            items = [r for r in items if not r.success]
        limit = max(1, min(int(limit), 200))
        return [
            {
                "ts": r.ts,
                "ts_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts)),
                "key_name": r.key_name,
                "model": r.model,
                "success": r.success,
                "tokens": r.tokens,
                "error": r.error,
                "duration_ms": r.duration_ms,
            }
            for r in items[:limit]
        ]

    def _compute_retry_delay(self, attempt: int, retry_after: float = 0.0,
                             key: LLMKey | None = None) -> float:
        policy = key.pool_policy if (key and key.pool_policy) else self.default_policy
        if retry_after and retry_after > 0:
            return min(max(float(retry_after), policy.rate_limit_cooldown_seconds), policy.rate_limit_max_cooldown)
        delay = policy.retry_base_delay * (policy.rate_limit_backoff_multiplier ** attempt)
        delay = min(delay, policy.retry_max_delay)
        if policy.retry_jitter > 0:
            delay += min(delay * policy.retry_jitter, 1.0)
        delay = max(delay, policy.rate_limit_cooldown_seconds)
        return min(delay, policy.rate_limit_max_cooldown)

    def call(self, messages: list[dict], temperature: float | None = None,
             max_tokens: int | None = None, timeout: int = 120,
             retries: int | None = None, project: str = "default",
             model: str | None = None, response_format: dict | None = None,
             use_case: str | None = None, tier=None,
             session_id: str | None = None,
             tools: list[dict] | None = None,
             tool_choice: str | dict | None = None,
             top_p: float | None = None,
             stop: str | list[str] | None = None,
             presence_penalty: float | None = None,
             frequency_penalty: float | None = None,
             seed: int | None = None,
             parallel_tool_calls: bool | None = None) -> dict:
        """
        统一 LLM 调用接口。
        自动选择 key、重试、冷却切换。

        Args:
            temperature: None = 不下发该字段，用 provider 默认；要约束就显式传值。
            max_tokens: 同上。Anthropic 协议下该字段为 API 必填，未指定时回退
                        ANTHROPIC_REQUIRED_MAX_TOKENS。
            project: 项目标签，用于 per-project token 统计
            model: 软偏好——优先选含此 model 的 key；同 tier 内若无此 model 自动 fallback
            use_case: 指定 use_case 筛选 key（敏感用途自动排除 privacy_warning key）
            tier: 硬过滤——key 必须含至少一个 tier 在范围内的 model。
                  支持 int(1-5)/str("default"/"cheap"/"powerful")/tuple(min,max)。
                  None 时若 use_case 指定，自动查 USE_CASE_REGISTRY.default_tier（范围）。
            session_id: v12 LKGP 会话粘性——同一会话优先复用上次成功的 (key, model)。
                  传入后：成功调用自动记录粘性；rate_limited/expired 自动清理粘性；
                  失败（HTTP 500/timeout/exception）不清理粘性（保留可能可恢复的会话）。
            tools: T05+ 工具定义列表（OpenAI 格式），传入后 LLM 可返回 tool_calls
            tool_choice: T05+ 工具选择策略（"auto"/"none"/specific tool dict）
            top_p/stop/presence_penalty/frequency_penalty/seed/parallel_tool_calls:
                  A 档采样参数透传（2026-09-08 白名单扩展）：None = 不下发。
                  Anthropic 协议仅支持 top_p/stop（stop→stop_sequences），其余不下发。

        Returns:
            {"ok": True, "content": str, "usage": dict, "model": str, "key_name": str,
             "tool_calls": list[dict], "finish_reason": str}
            {"ok": False, "error": str}
        """
        # tier 标准化为范围
        from server.llm_pool.key_store import _normalize_tier_range
        resolved_range = _normalize_tier_range(tier)
        # 若 tier 未指定，查 use_case.default_tier（治本：_normalize_tier_range((0,0)) 会
        # 被错误解析为 (1,1) 导致 use_case tier 范围失效，这里显式 fallback 确保 tier 范围正确）
        if resolved_range == (0, 0) and use_case:
            # v12: USE_CASE_REGISTRY 已从 types.py 顶部导入
            uc = USE_CASE_REGISTRY.get(use_case)
            if uc:
                resolved_range = uc.default_tier

        last_error = ""
        failed_models: set[str] = set()  # 本次调用周期内失败的 model，重试时排除以触发 tier 升级 fallback
        policy_retries = self.default_policy.retry_count if retries is None else retries
        for attempt in range(policy_retries):
            key = self._acquire(model=model, use_case=use_case, tier=resolved_range,
                                 session_id=session_id)
            if key is None:
                tier_hint = f", tier={resolved_range}" if resolved_range != (0, 0) else ""
                err = f"no available keys (use_case={use_case}{tier_hint})"
                # v15：_acquire=None 时记 recent_calls（避免失败调用在面板"消失"）
                self._record_call("", "", success=False, error=err)
                return {"ok": False, "error": err}

            try:
                # 5-2: 每轮入口先置 None——模型选择段（下方至 attempt_start_ts 赋值前）
                # 若中途抛异常，except 处理器不会引用上一轮 stale 值记账/冷却到错误
                # model，也不会因变量未绑定抛 UnboundLocalError 导致 _release 未执行
                req_model = None
                attempt_start_ts = None
                # model 优先级：调用方传入（且未失败且未冷却且未 disabled）> key 的范围内最低 tier model（排除已失败 + 已冷却 + disabled）
                # 失败 model 会被加入 failed_models，重试时自动升级到更高 tier model（治本：避免反复重试坏 model）
                # v12：Model Lockout——单 model 429 时 cooldown 写入 model_cooldowns，此处自动跳过同 key 其他可用 model
                # v15：C 保险——disabled model 也加入 exclude_set，防 default_model fallback 选到 disabled model
                # v16：tier 约束激活时，范围内无可用 model 不降级到非 tier 范围 model（修 tier 泄漏 bug）
                disabled_models = set(key.model_disabled.keys()) if self._model_health_enabled else set()
                cooldown_models = {m for m, cd in key.model_cooldowns.items() if cd > time.time()}
                exclude_set = failed_models | cooldown_models | disabled_models
                # 辅助：从 key.models 中取首个未排除的（兜底 fallback，仅在无 tier 约束时使用）
                _first_available = next((m for m in key.models if m not in exclude_set), "")
                _safe_default = key.default_model if key.default_model not in exclude_set else ""
                tier_active = resolved_range != (0, 0)
                # tier 范围内最低 tier 的可用 model（排除已失败/冷却/disabled）
                best_in_range = (key.get_best_model_in_range(resolved_range[0], resolved_range[1], exclude=exclude_set)
                                 if tier_active else "")
                if model and model not in exclude_set:
                    # 软偏好：调用方显式指定 model 时优先用之
                    if model in key.models:
                        req_model = model
                    elif best_in_range:
                        req_model = best_in_range
                    elif tier_active:
                        req_model = ""  # tier 约束激活但范围内无可用 model，不降级
                    else:
                        req_model = _safe_default or _first_available
                else:
                    if best_in_range:
                        req_model = best_in_range
                    elif tier_active:
                        req_model = ""  # tier 约束激活但范围内无可用 model，不降级
                    else:
                        req_model = _safe_default or _first_available
                # v15：探针 fallback——若 req_model 为空（所有 model 都 disabled/cooldown/failed），
                # 尝试 disabled 中是否有到探针窗口的，放探针（允许该 model 被选中一次）
                # 探针成功 → _release(success=True) 调 resolve_probe(True) 解除 disabled
                # 探针失败 → _release(fail) 调 resolve_probe(False) 重置探针计时
                # v16：tier 约束激活时，只探针 tier 范围内的 disabled model
                if not req_model and self._model_health_enabled:
                    for m in key.models:
                        if m in disabled_models:
                            if tier_active:
                                m_tier = key.model_tiers.get(m, 3)
                                if not (resolved_range[0] <= m_tier <= resolved_range[1]):
                                    continue
                            if key.try_acquire_probe(m, self._model_health_probe_interval,
                                                     self._model_health_probe_max_concurrency):
                                req_model = m
                                break
                if not req_model:
                    # key 没有任何可用 model（含探针），释放并 continue
                    self._release(key)
                    last_error = f"no available model in key {key.name}"
                    continue
                attempt_start_ts = time.time()

                # v14：消息压缩（OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）
                # 仅在 mode != "off" 时启用；Bloat Protection 保证压缩后不反向增加长度
                # Fail-Open：压缩异常时使用原 messages，不阻塞主调用链
                effective_messages = messages
                if self._compression_mode != "off":
                    try:
                        from server.llm_pool.compression import compress_messages
                        effective_messages, comp_stats = compress_messages(
                            messages, mode=self._compression_mode,
                            min_length=self._compression_min_length,
                        )
                        if comp_stats.applied:
                            self._compression_stats_total += 1
                            self._compression_saved_chars_total += comp_stats.saved_chars
                    except Exception:
                        # Fail-Open：压缩异常时使用原 messages
                        effective_messages = messages

                # v11：根据 key.protocol 分发到对应协议处理器
                # OpenAI 协议：POST /chat/completions，Bearer token，messages[0] 可含 system role
                # Anthropic 协议：POST /v1/messages，x-api-key header，system 单独字段
                protocol = (key.protocol or "openai").lower()
                if protocol == "anthropic":
                    call_result = self._call_anthropic(
                        key=key, req_model=req_model, messages=effective_messages,
                        temperature=temperature, max_tokens=max_tokens,
                        timeout=timeout, response_format=response_format,
                        attempt_start_ts=attempt_start_ts, attempt=attempt,
                        failed_models=failed_models, project=project,
                        tools=tools, tool_choice=tool_choice,
                        top_p=top_p, stop=stop,
                        presence_penalty=presence_penalty,
                        frequency_penalty=frequency_penalty,
                        seed=seed, parallel_tool_calls=parallel_tool_calls,
                        session_id=session_id,
                    )
                else:
                    call_result = self._call_openai(
                        key=key, req_model=req_model, messages=effective_messages,
                        temperature=temperature, max_tokens=max_tokens,
                        timeout=timeout, response_format=response_format,
                        attempt_start_ts=attempt_start_ts, attempt=attempt,
                        failed_models=failed_models, project=project,
                        tools=tools, tool_choice=tool_choice,
                        top_p=top_p, stop=stop,
                        presence_penalty=presence_penalty,
                        frequency_penalty=frequency_penalty,
                        seed=seed, parallel_tool_calls=parallel_tool_calls,
                        session_id=session_id,
                    )

                # call_result 是 dict，含状态码：
                #   {"status": "ok", "content", "usage", "model", "tokens"} → 成功
                #   {"status": "rate_limited", "cooldown", "error"} → 429
                #   {"status": "expired", "error"} → 401/402
                #   {"status": "fail", "error"} → 其他错误
                status = call_result.get("status")
                if status == "ok":
                    # v12：成功时记录 LKGP 会话粘性（key_id + model）
                    if session_id:
                        self._record_session_stickiness(session_id, key, call_result.get("model", req_model))
                    return call_result
                elif status == "rate_limited":
                    last_error = call_result.get("error", "rate_limited")
                    # v12：rate_limited 时清理粘性（该 (key, model) 已不可用，下次换 key）
                    if session_id:
                        self._clear_session_stickiness(session_id)
                    continue
                elif status == "expired":
                    last_error = call_result.get("error", "expired")
                    # v12：expired 时清理粘性（key 失效，下次换 key）
                    if session_id:
                        self._clear_session_stickiness(session_id)
                    continue
                else:
                    last_error = call_result.get("error", "unknown error")
                    # v12：HTTP 500 等非限流失败不清粘性——key 仍可能可用，保留会话连续性
                    continue

            except httpx.TimeoutException:
                if req_model is None or attempt_start_ts is None:
                    # 5-2: 模型选择段异常（HTTP 请求尚未发起），仅归还槽位不记账
                    self._release(key)
                    last_error = "timeout during model selection"
                    continue
                err_msg = "timeout"
                self._release(key, model=req_model, error=err_msg,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000),
                              error_type="timeout")
                last_error = f"timeout (key={key.name}, model={req_model})"
                self._record_call(key.name, req_model, success=False,
                                  error=last_error,
                                  duration_ms=int((time.time() - attempt_start_ts) * 1000))
                continue
            except Exception as e:
                if req_model is None or attempt_start_ts is None:
                    # 5-2: 模型选择段异常——此前此处会因 req_model/attempt_start_ts
                    # 未绑定抛 UnboundLocalError 且 _release 未执行，key.active_count
                    # 永不递减（并发槽位泄漏）。仅归还槽位，不记 model 级失败
                    self._release(key)
                    last_error = f"{type(e).__name__}: {str(e)[:120]}"
                    continue
                # model 级异常（如响应格式错 'NoneType' not subscriptable、JSON 解析失败），
                # 加入 failed_models 让重试换 model（治本：避免坏 model 反复重试）
                failed_models.add(req_model)
                err_msg = f"{type(e).__name__}: {str(e)[:120]}"
                self._release(key, model=req_model, error=err_msg,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000),
                              error_type="exception")
                last_error = err_msg
                self._record_call(key.name, req_model, success=False,
                                  error=last_error,
                                  duration_ms=int((time.time() - attempt_start_ts) * 1000))
                continue

        if last_error.startswith("429"):
            return {"ok": False, "error": "no available keys"}
        return {"ok": False, "error": last_error}

    # ==================== v6-lite-streaming-gui T00: 真 SSE 流式 ====================

    async def stream(self, messages: list[dict], temperature: float | None = None,
                     max_tokens: int | None = None, timeout: int = 120,
                     project: str = "default", model: str | None = None,
                     use_case: str | None = None, tier=None,
                     session_id: str | None = None,
                     tools: list[dict] | None = None,
                     tool_choice: str | dict | None = None,
                     response_format: dict | None = None,
                     top_p: float | None = None,
                     stop: str | list[str] | None = None,
                     presence_penalty: float | None = None,
                     frequency_penalty: float | None = None,
                     seed: int | None = None,
                     parallel_tool_calls: bool | None = None):
        """真 SSE 流式调用（D02/D03/D04/D14）。

        temperature / max_tokens 同 call()：None = 不下发，用 provider 默认。
        response_format: 流式此前恒传 None（签名里根本没有该参数），客户端传了会被
            静默丢弃；现补上入参并透传给 OpenAI 协议分支。
        top_p 等 A 档采样参数同 call()：None = 不下发（2026-09-08 白名单扩展）。

        对接 provider stream=True，透传 SSE 事件。yield dict 事件（D03 7 种类型）：
        - {"type": "text_delta", "delta": "..."}
        - {"type": "thinking_delta", "delta": "..."}（合并 reasoning_content + thinking）
        - {"type": "tool_call_delta", "tool_call": {...}}（D04 累积完整后 yield，OpenAI 格式）
        - {"type": "usage", "prompt_tokens", "completion_tokens", "total_tokens"}
        - {"type": "provider_error", "error": "..."}
        - {"type": "done", "finish_reason": "..."}

        OpenAI 协议：真流式（httpx.AsyncClient.stream + aiter_lines，T07 后原 requests 桥接已移除）。
        Anthropic 协议：真流式（原生 event-stream 解析，message_start/message_delta 采集 usage）。

        try/finally 确保 key/semaphore 释放（D14）。不做重试（单次调用，失败 yield error）。
        """
        from server.llm_pool.key_store import _normalize_tier_range
        resolved_range = _normalize_tier_range(tier)
        if resolved_range == (0, 0) and use_case:
            uc = USE_CASE_REGISTRY.get(use_case)
            if uc:
                resolved_range = uc.default_tier

        # _acquire 用 threading.Condition.wait() 阻塞等槽位（最长 timeout 秒）。
        # stream() 的消费方在事件循环里，必须挪到工作线程等，否则并发流超过
        # provider max_concurrency 时，排队中的流会冻死整个服务的事件循环。
        key = await asyncio.to_thread(self._acquire, model=model, use_case=use_case,
                                      tier=resolved_range, session_id=session_id)
        if key is None:
            yield {"type": "provider_error", "error": "no available keys"}
            yield {"type": "done", "finish_reason": "error"}
            return

        # 选 model（简化版，无重试/探针/failed_models）
        tier_active = resolved_range != (0, 0)
        if tier_active:
            req_model = key.get_best_model_in_range(resolved_range[0], resolved_range[1]) or ""
        elif model and model in key.models:
            req_model = model
        else:
            req_model = key.default_model or ""
        if not req_model and key.models:
            req_model = key.models[0]
        if not req_model:
            self._release(key)
            yield {"type": "provider_error", "error": f"no available model in key {key.name}"}
            yield {"type": "done", "finish_reason": "error"}
            return

        attempt_start_ts = time.time()
        success = False
        captured_usage: dict | None = None  # 流式 usage 事件捕获，供 finally 回填池统计
        # 5-4：捕获 provider_error 事件附带的 HTTP 状态码，供 finally 按码映射
        # _release 参数（rate_limited/expired/error_type），与非流式 _call_openai 对齐。
        provider_error_status: int | None = None
        protocol = (key.protocol or "openai").lower()
        # OpenAI 与 Anthropic 均走各自原生 SSE 生成器，共用「持有 key → 捕获 usage
        # 事件 → finally 统一 release+record」模型。此前 Anthropic 走 call() 伪流式并
        # 提前 release（key 置 None 避免 call() 内部二次记账）；改原生后不再需要该特例。
        if protocol == "anthropic":
            gen = self._stream_anthropic_sse(
                key=key, req_model=req_model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                project=project, attempt_start_ts=attempt_start_ts,
                tools=tools, tool_choice=tool_choice,
                top_p=top_p, stop=stop,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                seed=seed, parallel_tool_calls=parallel_tool_calls,
                session_id=session_id,
            )
        else:
            gen = self._stream_openai_sse(
                key=key, req_model=req_model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                response_format=response_format, project=project,
                attempt_start_ts=attempt_start_ts,
                tools=tools, tool_choice=tool_choice,
                top_p=top_p, stop=stop,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                seed=seed, parallel_tool_calls=parallel_tool_calls,
                session_id=session_id,
            )
        try:
            async for event in gen:
                # 可观测性（纯附加，不改选路/控制流）：把实际选中的 key/model 附到事件上，
                # 供入站网关等在日志中记录「兜底后真实走的 key 与模型」。老消费方忽略未知字段。
                event.setdefault("key_name", key.name)
                event.setdefault("model", req_model)
                if event.get("type") == "usage":
                    captured_usage = event
                if (event.get("type") == "provider_error"
                        and isinstance(event.get("status_code"), int)):
                    provider_error_status = event["status_code"]
                if event.get("type") == "done" and event.get("finish_reason") not in ("error", None):
                    success = True
                yield event
        finally:
            # 流式 token 记账：把 usage 事件的 total 回填到 key/model 统计（此前恒 0）。
            # OpenAI 与 Anthropic 原生流式都走这里（与非流式 call() 的 _record_usage 对齐）。
            tokens = int(captured_usage.get("total_tokens") or 0) if (success and captured_usage) else 0
            duration_ms = int((time.time() - attempt_start_ts) * 1000)
            if not success and provider_error_status == 429:
                # 5-4：流式上游 429 → rate_limited（写 key/model cooldown、不计
                # consecutive_fails）。此前误走 _apply_model_health_failure 可把 model
                # 误 disable。流式无重试轮次，attempt 取 0；Retry-After 头未透出，用 0。
                cd = self._compute_retry_delay(0, retry_after=0.0, key=key)
                self._release(key, rate_limited=True, cooldown=cd, model=req_model,
                              duration_ms=duration_ms)
            elif not success and provider_error_status in (401, 402):
                # 5-4：401/402 → expired（key 失效，不计失败、不进 model 降级），与非流式对齐
                self._release(key, expired=True, model=req_model, duration_ms=duration_ms)
            elif not success and provider_error_status is not None:
                # 5-4：其他 HTTP 错误（5xx 等）→ 带 error_type 的常规失败，按 D2 退避映射
                error_type = "http_5xx" if 500 <= provider_error_status < 600 else "http_other"
                self._release(key, model=req_model,
                              error=f"HTTP {provider_error_status}",
                              duration_ms=duration_ms, error_type=error_type)
            else:
                self._release(key, success=success, model=req_model,
                              duration_ms=duration_ms, tokens=tokens)
            if success and captured_usage and project:
                self._record_usage(project, {
                    "prompt_tokens": int(captured_usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(captured_usage.get("completion_tokens") or 0),
                    "total_tokens": int(captured_usage.get("total_tokens") or 0),
                })
            if success and session_id:
                # 5-5: 流式成功路径补记 LKGP 会话粘性——此前仅 call() 记录，而
                # chat panel / 入站网关主路径均为流式，粘性对最主要流量形态静默失效。
                # usage 事件在 stream() 循环里已 setdefault("model", req_model)
                model_used = (captured_usage.get("model") or req_model) if captured_usage else req_model
                self._record_session_stickiness(session_id, key, model_used)

    async def _stream_openai_sse(self, *, key, req_model, messages, temperature,
                                  max_tokens, timeout, response_format, project,
                                  attempt_start_ts, tools=None, tool_choice=None,
                                  top_p=None, stop=None,
                                  presence_penalty=None, frequency_penalty=None,
                                  seed=None, parallel_tool_calls=None,
                                  session_id: str | None = None
                                  ) -> AsyncIterator[dict]:
        """OpenAI 协议真 SSE 流式（D02/D03/D04）。

        T07：用 httpx.AsyncClient.stream 原生异步流式（替代 requests.post + _iter_lines_async 桥接）。
        解析 OpenAI SSE chunk，yield D03 事件。tool_call 半包累积（D04）。
        A 档采样参数（top_p/stop/penalties/seed/parallel_tool_calls）：None = 不下发。
        """
        url = key.base_url.rstrip("/") + "/chat/completions"
        request_body: dict = {
            "model": req_model,
            "messages": messages,
            "stream": True,  # 真流式（D02，原 _call_openai 是 False）
            "stream_options": {"include_usage": True},  # 请求 usage（部分 provider 支持）
        }
        # 透传：None 表示调用方未指定，不下发字段，交给 provider 自己的默认值
        if temperature is not None:
            request_body["temperature"] = temperature
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        if response_format:
            request_body["response_format"] = response_format
        if tools:
            request_body["tools"] = tools
            if tool_choice is not None:
                request_body["tool_choice"] = tool_choice
        # A 档采样参数透传（2026-09-08 白名单扩展）
        if top_p is not None:
            request_body["top_p"] = top_p
        if stop is not None:
            request_body["stop"] = stop
        if presence_penalty is not None:
            request_body["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            request_body["frequency_penalty"] = frequency_penalty
        if seed is not None:
            request_body["seed"] = seed
        if parallel_tool_calls is not None:
            request_body["parallel_tool_calls"] = parallel_tool_calls

        headers = {
            "Authorization": f"Bearer {key.key}",
            "Content-Type": "application/json",
        }
        # opencode 端点会话亲和（2026-09-06 上游强制 x-opencode-session，缺失即 400）。
        # session_id 缺省时内容派生兜底，保证内部调用方（不走入站网关）同样合规。
        headers.update(build_opencode_headers(
            key.base_url,
            session_id or derive_session_id(messages, namespace=project),
        ))
        # 流式 usage 采集：prompt 文本用于精确字符数；completion_parts 累积输出文本
        # （content + thinking + tool 参数）；provider_usage 记住最后一条**非零** usage
        # chunk（兼容代理常在流里塞全零 usage chunk，须过滤，见 lib/llm_usage 说明）。
        prompt_text = text_from_messages(messages)
        completion_parts: list[str] = []
        provider_usage: dict | None = None
        client = get_async_client()
        try:
            # httpx 原生 async stream：context manager 自动管理连接释放
            async with client.stream(
                "POST", url, headers=headers, json=request_body, timeout=timeout
            ) as resp:
                # 错误状态码处理（429/401/402/5xx）
                # 5-4：附带 status_code 供 stream() finally 按码映射 _release 参数
                #（rate_limited/expired/error_type，与非流式 _call_openai 对齐）；
                # 纯附加字段，不改变既有 error 字段语义，客户端忽略未知字段。
                if resp.status_code in (401, 402, 429) or resp.status_code >= 500:
                    try:
                        await resp.aread()
                        err_body = resp.text[:300]
                    except Exception:
                        err_body = "(unreadable)"
                    yield {"type": "provider_error", "status_code": resp.status_code,
                           "error": f"HTTP {resp.status_code}: {err_body}"}
                    yield {"type": "done", "finish_reason": "error"}
                    return

                if resp.status_code != 200:
                    try:
                        await resp.aread()
                        err_body = resp.text[:300]
                    except Exception:
                        err_body = "(unreadable)"
                    yield {"type": "provider_error", "status_code": resp.status_code,
                           "error": f"HTTP {resp.status_code}: {err_body}"}
                    yield {"type": "done", "finish_reason": "error"}
                    return

                accumulator = ToolCallAccumulator()
                stream_finish_reason: str | None = None
                # httpx aiter_lines 原生异步迭代，无需 run_in_executor 桥接
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip() if isinstance(raw_line, str) else ""
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    # usage chunk（OpenAI 规范：usage 常在 finish_reason **之后**另发一条
                    # choices 为空的独立尾 chunk；也有 provider 直接挂在 finish_reason chunk
                    # 上）。只记非零值，兼容代理的全零 usage chunk 忽略；多条时取最后一条。
                    # 关键：不能一看到 finish_reason 就 break，否则读不到尾部 usage chunk，
                    # 会把明明给了 provider usage 的流式误记成 provider_missing（历史 bug）。
                    u = chunk.get("usage")
                    if u and (u.get("prompt_tokens") or u.get("completion_tokens")
                              or u.get("total_tokens")):
                        provider_usage = u

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}

                    # text delta
                    if delta.get("content"):
                        completion_parts.append(delta["content"])
                        yield {"type": "text_delta", "delta": delta["content"]}

                    # thinking delta（DeepSeek-R1 用 reasoning_content，xAI 用 thinking）
                    thinking = delta.get("reasoning_content") or delta.get("thinking")
                    if thinking:
                        completion_parts.append(thinking)
                        yield {"type": "thinking_delta", "delta": thinking}

                    # tool_call delta（半包累积 D04，流结束时一次性 yield）
                    if delta.get("tool_calls"):
                        for tc_delta in delta["tool_calls"]:
                            accumulator.on_delta(tc_delta)

                    fr = choices[0].get("finish_reason")
                    if fr:
                        stream_finish_reason = fr
                        # 快速路径：usage 已随 finish_reason chunk 一起到达 → 立即收尾。
                        # 否则继续读到 [DONE]/流结束，以捕获独立的尾部 usage chunk（OpenAI 标准姿势）。
                        if provider_usage is not None:
                            break
                # 统一收尾（[DONE]、快速路径 break 或流自然结束都走这里）：先吐累积完整的
                # tool_calls（D04），再 emit 一条 usage（provider 给了用实测，否则
                # provider_missing 记 0），最后 emit done。
                for tc in accumulator.build_all():
                    completion_parts.append(
                        str((tc.get("function") or {}).get("arguments") or ""))
                    yield {"type": "tool_call_delta", "tool_call": tc}
                usage_evt = {"type": "usage", "raw": provider_usage}
                usage_evt.update(collect_usage(
                    provider_usage, prompt_text, "".join(completion_parts)))
                yield usage_evt
                yield {"type": "done", "finish_reason": stream_finish_reason or "stop"}
        except httpx.TimeoutException:
            yield {"type": "provider_error", "error": f"timeout after {timeout}s"}
            yield {"type": "done", "finish_reason": "error"}
            return
        except Exception as e:
            yield {"type": "provider_error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
            yield {"type": "done", "finish_reason": "error"}
            return

    async def _stream_anthropic_sse(self, *, key, req_model, messages, temperature,
                                     max_tokens, timeout, project, attempt_start_ts,
                                     tools=None, tool_choice=None,
                                     top_p=None, stop=None,
                                     presence_penalty=None, frequency_penalty=None,
                                     seed=None, parallel_tool_calls=None,
                                     session_id: str | None = None) -> AsyncIterator[dict]:
        """Anthropic 协议原生 SSE 流式（真增量 + usage 采集）。

        Anthropic /v1/messages（stream=true）事件（按 data JSON 的 type 字段分派）：
        - message_start：message.usage.input_tokens（prompt 侧权威值，部分也带 output）
        - content_block_start：tool_use 块给出 id/name
        - content_block_delta：text_delta（正文）/ thinking_delta（思考）/
          input_json_delta（tool_use 参数半包，累积后在收尾一次性吐）
        - message_delta：usage.output_tokens 为**累计**值（取最新非零）+ delta.stop_reason
        - message_stop：终止

        usage 采集口径（纯采集器，不估算）：input=output 各自取流中出现的非零值，
        两者皆缺 → provider_missing（记 0）。与非流式 `_call_anthropic` 保持同一键名。
        """
        system_text, user_messages = _build_anthropic_messages(messages)
        base = key.base_url.rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        body: dict = {
            "model": req_model,
            "messages": user_messages,
            # Anthropic 把 max_tokens 列为必填（省略直接 400），未指定时回退常量
            "max_tokens": (max_tokens if max_tokens is not None
                           else ANTHROPIC_REQUIRED_MAX_TOKENS),
            "stream": True,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if system_text:
            body["system"] = system_text
        # A 档采样参数协议适配（2026-09-08 白名单扩展）：Anthropic 仅支持
        # top_p / stop_sequences；presence/frequency_penalty、seed、parallel_tool_calls
        # 无对应概念，不下发（OpenAI 独有，Anthropic 上游静默忽略也不合规，干脆不发）。
        if top_p is not None:
            body["top_p"] = top_p
        if stop is not None:
            # OpenAI stop（str | list[str]）→ Anthropic stop_sequences（list[str]）
            body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
        if tools:
            anth_tools = _openai_tools_to_anthropic(tools)
            if anth_tools:
                body["tools"] = anth_tools
                anth_choice = _openai_tool_choice_to_anthropic(tool_choice)
                if anth_choice is not None:
                    body["tool_choice"] = anth_choice

        headers = {
            "x-api-key": key.key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        # opencode 端点会话亲和（与 _stream_openai_sse 同口径，见 opencode.py）
        headers.update(build_opencode_headers(
            key.base_url,
            session_id or derive_session_id(messages, namespace=project),
        ))
        # usage 采集：prompt 文本用于精确字符数；completion_parts 累积输出文本
        prompt_text = text_from_messages(messages)
        completion_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        # cache_read_input_tokens：prompt 缓存命中（message_start 给初始值，
        # message_delta 的 usage 可能带累计值，取最新非零，与 output_tokens 同口径）
        cache_read_tokens: int | None = None
        # cache_creation_input_tokens：缓存写入（计费 1.25x/2x），同口径取最新非空
        cache_creation_tokens: int | None = None
        # 原始 usage 合并视图（message_start/message_delta 键不重叠，update 合并），
        # 随 usage 事件透出供网关 usage_json 落库兜底
        raw_usage_merged: dict = {}
        stop_reason = ""
        # tool_use 半包累积：content block index → {"id","name","json"}
        tool_blocks: dict[int, dict] = {}
        client = get_async_client()
        try:
            async with client.stream(
                "POST", url, headers=headers, json=body, timeout=timeout
            ) as resp:
                if resp.status_code != 200:
                    try:
                        await resp.aread()
                        err_body = resp.text[:300]
                    except Exception:
                        err_body = "(unreadable)"
                    # 5-4：附带 status_code 供 stream() finally 按码映射 _release 参数（同 OpenAI 分支）
                    yield {"type": "provider_error", "status_code": resp.status_code,
                           "error": f"HTTP {resp.status_code}: {err_body}"}
                    yield {"type": "done", "finish_reason": "error"}
                    return

                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip() if isinstance(raw_line, str) else ""
                    # Anthropic SSE 有 "event: xxx" 行，只需带 JSON 的 "data:" 行
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    et = data.get("type")

                    if et == "message_start":
                        u = ((data.get("message") or {}).get("usage")) or {}
                        if u.get("input_tokens"):
                            input_tokens = int(u["input_tokens"])
                        if u.get("output_tokens"):
                            output_tokens = int(u["output_tokens"])
                        if u.get("cache_read_input_tokens") is not None:
                            cache_read_tokens = int(u["cache_read_input_tokens"])
                        if u.get("cache_creation_input_tokens") is not None:
                            cache_creation_tokens = int(u["cache_creation_input_tokens"])
                        raw_usage_merged.update(u)
                    elif et == "content_block_start":
                        blk = data.get("content_block") or {}
                        if blk.get("type") == "tool_use":
                            tool_blocks[data.get("index", 0)] = {
                                "id": blk.get("id", ""),
                                "name": blk.get("name", ""),
                                "json": "",
                            }
                    elif et == "content_block_delta":
                        delta = data.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            t = delta.get("text") or ""
                            if t:
                                completion_parts.append(t)
                                yield {"type": "text_delta", "delta": t}
                        elif dtype == "thinking_delta":
                            th = delta.get("thinking") or ""
                            if th:
                                completion_parts.append(th)
                                yield {"type": "thinking_delta", "delta": th}
                        elif dtype == "input_json_delta":
                            tb = tool_blocks.setdefault(
                                data.get("index", 0), {"id": "", "name": "", "json": ""})
                            tb["json"] += delta.get("partial_json") or ""
                    elif et == "message_delta":
                        u = data.get("usage") or {}
                        if u.get("input_tokens"):
                            input_tokens = int(u["input_tokens"])
                        if u.get("output_tokens"):  # 累计值，最新即最大
                            output_tokens = int(u["output_tokens"])
                        if u.get("cache_read_input_tokens") is not None:
                            cache_read_tokens = int(u["cache_read_input_tokens"])
                        if u.get("cache_creation_input_tokens") is not None:
                            cache_creation_tokens = int(u["cache_creation_input_tokens"])
                        raw_usage_merged.update(u)
                        stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason
                    elif et == "message_stop":
                        break
                    elif et == "error":
                        err = data.get("error") or {}
                        yield {"type": "provider_error",
                               "error": f"{err.get('type', 'error')}: {err.get('message', '')}"[:300]}
                        yield {"type": "done", "finish_reason": "error"}
                        return

                # 统一收尾（正常 message_stop 或流自然结束都走这里）：
                # 先吐完整的 tool_calls（半包累积完毕），再吐 usage + done。
                for idx in sorted(tool_blocks):
                    tb = tool_blocks[idx]
                    completion_parts.append(tb["json"])
                    yield {"type": "tool_call_delta", "tool_call": {
                        "id": tb["id"], "type": "function",
                        "function": {"name": tb["name"], "arguments": tb["json"] or "{}"}}}
                usage_evt = {"type": "usage", "raw": raw_usage_merged or None}
                usage_evt.update(collect_usage_anthropic(
                    input_tokens, output_tokens, prompt_text, "".join(completion_parts),
                    cached_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens))
                yield usage_evt
                yield {"type": "done", "finish_reason": stop_reason or "stop"}
        except httpx.TimeoutException:
            yield {"type": "provider_error", "error": f"timeout after {timeout}s"}
            yield {"type": "done", "finish_reason": "error"}
            return
        except Exception as e:
            yield {"type": "provider_error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
            yield {"type": "done", "finish_reason": "error"}
            return


    def _call_openai(self, *, key, req_model, messages, temperature, max_tokens,
                     timeout, response_format, attempt_start_ts, attempt,
                     failed_models, project="default",
                     tools=None, tool_choice=None,
                     top_p=None, stop=None,
                     presence_penalty=None, frequency_penalty=None,
                     seed=None, parallel_tool_calls=None,
                     session_id: str | None = None) -> dict:
        """OpenAI 协议调用（POST {base_url}/chat/completions）

        T05+ 支持 tools/tool_choice 参数，解析 tool_calls 响应。
        A 档采样参数（top_p/stop/penalties/seed/parallel_tool_calls）：None = 不下发。

        返回 dict：
        - {"status": "ok", "content", "usage", "model", "tokens", "tool_calls", "finish_reason"}
        - {"status": "rate_limited", "cooldown", "error"}
        - {"status": "expired", "error"}  → 401/402
        - {"status": "fail", "error"}    → 其他错误
        """
        url = key.base_url.rstrip("/") + "/chat/completions"
        # T05+: 构造请求体，含 tools/tool_choice（仅当 tools 非空时传入）
        request_body: dict = {
            "model": req_model,
            "messages": messages,
            "stream": False,
            **({"response_format": response_format} if response_format is not None else {}),
        }
        # 透传：None = 调用方未指定 → 不下发，用 provider 默认（与流式分支同口径）
        if temperature is not None:
            request_body["temperature"] = temperature
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        if tools:
            request_body["tools"] = tools
            if tool_choice is not None:
                request_body["tool_choice"] = tool_choice
        # A 档采样参数透传（2026-09-08 白名单扩展）
        if top_p is not None:
            request_body["top_p"] = top_p
        if stop is not None:
            request_body["stop"] = stop
        if presence_penalty is not None:
            request_body["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            request_body["frequency_penalty"] = frequency_penalty
        if seed is not None:
            request_body["seed"] = seed
        if parallel_tool_calls is not None:
            request_body["parallel_tool_calls"] = parallel_tool_calls
        # T07：同步 httpx.Client（连接池复用），call() 调用方已在 asyncio.to_thread 中
        _oc_headers = build_opencode_headers(
            key.base_url,
            session_id or derive_session_id(messages, namespace=project),
        )
        resp = get_sync_client().post(
            url,
            headers={
                "Authorization": f"Bearer {key.key}",
                "Content-Type": "application/json",
                **_oc_headers,
            },
            json=request_body,
            timeout=timeout,
        )

        if resp.status_code == 429:
            retry_after = 0.0
            try:
                retry_after = float(resp.headers.get("Retry-After", "0") or 0)
            except Exception:
                retry_after = 0.0
            cd = self._compute_retry_delay(attempt, retry_after=retry_after, key=key)
            self._release(key, rate_limited=True, cooldown=cd, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"429 rate_limited (key={key.name}, model={req_model}, cd={cd}s)"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "rate_limited", "cooldown": cd, "error": err}

        if resp.status_code == 402:
            self._release(key, expired=True, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"402 insufficient_balance (key={key.name}, model={req_model})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "expired", "error": err}

        if resp.status_code == 401:
            self._release(key, expired=True, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"401 unauthorized (key={key.name}, model={req_model})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "expired", "error": err}

        if resp.status_code != 200:
            failed_models.add(req_model)
            err_msg = f"HTTP {resp.status_code}: {resp.text[:150]}"
            # v15：按状态码分类错误类型（5xx → http_5xx，其他 → http_other）
            error_type = "http_5xx" if 500 <= resp.status_code < 600 else "http_other"
            self._release(key, model=req_model, error=err_msg,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000),
                          error_type=error_type)
            self._record_call(key.name, req_model, success=False,
                              error=err_msg,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "fail", "error": err_msg}

        data = resp.json()
        # 防御 choices=None
        choices = data.get("choices") or []
        message = (choices[0].get("message", {}) if choices else {})
        content = message.get("content", "")
        # T05+: 解析 tool_calls（OpenAI 格式，可能为 None）
        tool_calls = message.get("tool_calls") or []
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", 0)

        # 兜底：HTTP 200 但 content 为空且无 tool_calls 时
        # T05+ 修复：有 tool_calls 时 content 可以为空（合法的 tool_use 响应）
        if not content and not tool_calls:
            finish_reason = (choices[0].get("finish_reason", "") if choices else "")
            logger.warning(
                "LLM 返回空内容 | model=%s key=%s tokens=%s finish_reason=%s",
                req_model, key.name, tokens, finish_reason or "(空)"
            )
            failed_models.add(req_model)
            err_msg = f"empty content (finish_reason={finish_reason})"
            self._release(key, model=req_model, error=err_msg,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000),
                          error_type="empty_content")
            err = f"empty content (model={req_model}, key={key.name}, finish_reason={finish_reason})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "fail", "error": err}

        actual_model = data.get("model", req_model)
        finish_reason = (choices[0].get("finish_reason", "") if choices else "")
        # 思考内容（DeepSeek reasoning_content / xAI thinking），与流式采集口径对齐
        thinking = message.get("reasoning_content") or message.get("thinking") or ""
        # v12：解析 OpenAI 风格响应头饱和信号
        saturation = _parse_openai_rate_limit_saturation(resp.headers)
        self._release(key, success=True, tokens=tokens, model=req_model, saturation=saturation,
                      duration_ms=int((time.time() - attempt_start_ts) * 1000))
        self._record_usage(project, usage)
        self._record_call(key.name, actual_model, success=True,
                          tokens=tokens,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
        return {
            "status": "ok",
            "ok": True,
            "content": content or "",
            "thinking": thinking,
            "usage": usage,
            # provider 原始 usage 原样透出（网关 usage_json 落库兜底，新字段离线可查）
            "usage_raw": usage,
            "model": actual_model,
            "key_name": key.name,
            "finish_reason": finish_reason,
            "tool_calls": tool_calls,  # T05+: OpenAI 格式 list[dict]
        }

    def _call_anthropic(self, *, key, req_model, messages, temperature, max_tokens,
                        timeout, response_format, attempt_start_ts, attempt,
                        failed_models, project="default",
                        tools=None, tool_choice=None,
                        top_p=None, stop=None,
                        presence_penalty=None, frequency_penalty=None,
                        seed=None, parallel_tool_calls=None,
                        session_id: str | None = None) -> dict:
        """Anthropic 协议调用（POST {base_url}/v1/messages 或 {base_url}/messages）

        Anthropic API 特点：
        - Endpoint: POST /v1/messages（base_url 通常已含 /v1 后缀，自动适配）
        - Headers: x-api-key + anthropic-version
        - system 消息从 messages 数组提取到顶层 system 字段
        - content 是数组（[{"type": "text", "text": "..."}]）
        - usage: {input_tokens, output_tokens}
        - T05+: tools 格式转换（OpenAI → Anthropic input_schema）+ tool_use 响应解析
        - A 档采样参数：仅 top_p/stop（→stop_sequences）可映射，其余 OpenAI 独有不下发

        返回 dict 结构与 _call_openai 一致（含 tool_calls 字段，OpenAI 格式）。
        """
        # system 提取 + tool_calls/tool_result 格式转换，与非流式共用同一 helper
        system_text, user_messages = _build_anthropic_messages(messages)

        # 构造 endpoint：若 base_url 已含 /v1 则追加 /messages，否则追加 /v1/messages
        base = key.base_url.rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")

        # 构造请求体
        body: dict = {
            "model": req_model,
            "messages": user_messages,
            # Anthropic 把 max_tokens 列为必填（省略直接 400），未指定时回退常量
            "max_tokens": (max_tokens if max_tokens is not None
                           else ANTHROPIC_REQUIRED_MAX_TOKENS),
        }
        if temperature is not None:
            body["temperature"] = temperature
        if system_text:
            body["system"] = system_text
        # A 档采样参数协议适配（2026-09-08 白名单扩展，与 _stream_anthropic_sse 同口径）：
        # Anthropic 仅支持 top_p / stop_sequences；penalties/seed/parallel_tool_calls 不下发
        if top_p is not None:
            body["top_p"] = top_p
        if stop is not None:
            # OpenAI stop（str | list[str]）→ Anthropic stop_sequences（list[str]）
            body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
        # T05+: tools/tool_choice 透传（OpenAI 格式 → Anthropic 格式）
        if tools:
            anth_tools = _openai_tools_to_anthropic(tools)
            if anth_tools:
                body["tools"] = anth_tools
                anth_choice = _openai_tool_choice_to_anthropic(tool_choice)
                if anth_choice is not None:
                    body["tool_choice"] = anth_choice

        # T07：同步 httpx.Client（连接池复用），call() 调用方已在 asyncio.to_thread 中
        _oc_headers = build_opencode_headers(
            key.base_url,
            session_id or derive_session_id(messages, namespace=project),
        )
        resp = get_sync_client().post(
            url,
            headers={
                "x-api-key": key.key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                **_oc_headers,
            },
            json=body,
            timeout=timeout,
        )

        if resp.status_code == 429:
            retry_after = 0.0
            try:
                retry_after = float(resp.headers.get("Retry-After", "0") or 0)
            except Exception:
                retry_after = 0.0
            cd = self._compute_retry_delay(attempt, retry_after=retry_after, key=key)
            self._release(key, rate_limited=True, cooldown=cd, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"429 rate_limited (anthropic key={key.name}, model={req_model}, cd={cd}s)"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "rate_limited", "cooldown": cd, "error": err}

        if resp.status_code == 402:
            self._release(key, expired=True, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"402 insufficient_balance (anthropic key={key.name}, model={req_model})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "expired", "error": err}

        if resp.status_code == 401:
            self._release(key, expired=True, model=req_model,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
            err = f"401 unauthorized (anthropic key={key.name}, model={req_model})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "expired", "error": err}

        if resp.status_code != 200:
            failed_models.add(req_model)
            err_msg = f"HTTP {resp.status_code} (anthropic): {resp.text[:150]}"
            # v15：按状态码分类错误类型（5xx → http_5xx，其他 → http_other）
            error_type = "http_5xx" if 500 <= resp.status_code < 600 else "http_other"
            self._release(key, model=req_model, error=err_msg,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000),
                          error_type=error_type)
            self._record_call(key.name, req_model, success=False,
                              error=err_msg,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "fail", "error": err_msg}

        data = resp.json()
        # Anthropic 响应：content 是数组 [{"type": "text", "text": "..."}]
        content_blocks = data.get("content") or []
        parts: list[str] = []
        think_parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    parts.append(t)
            elif block.get("type") == "thinking":
                # extended thinking 思考块：此前被静默丢弃（客户端非流式拿不到思考内容、
                # completion_chars 少记），现与流式 thinking_delta 采集口径对齐
                th = block.get("thinking", "")
                if th:
                    think_parts.append(th)
        content = "".join(parts)
        thinking = "".join(think_parts)
        # T05+: 解析 tool_use 块 → OpenAI 兼容 tool_calls
        tool_calls = _anthropic_tool_use_to_openai_tool_calls(content_blocks)
        # usage 转换：{input_tokens, output_tokens} → OpenAI 风格 {prompt_tokens, completion_tokens, total_tokens}
        raw_usage = data.get("usage", {}) or {}
        input_tokens = int(raw_usage.get("input_tokens", 0))
        output_tokens = int(raw_usage.get("output_tokens", 0))
        # cache_read_input_tokens → 顶层 cached_tokens（collect_usage 兼容此形态透传）
        cache_raw = raw_usage.get("cache_read_input_tokens")
        # cache_creation_input_tokens → cache_creation_tokens（缓存写入，计费 1.25x/2x，
        # 此前被丢弃导致成本核算偏低）
        ccreate_raw = raw_usage.get("cache_creation_input_tokens")
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached_tokens": int(cache_raw) if cache_raw is not None else None,
            "cache_creation_tokens": int(ccreate_raw) if ccreate_raw is not None else None,
        }
        tokens = usage["total_tokens"]

        # 兜底：空 content 且无 tool_calls 时
        # T05+ 修复：有 tool_calls 时 content 可以为空（合法的 tool_use 响应）
        if not content and not tool_calls:
            stop_reason = data.get("stop_reason", "")
            logger.warning(
                "Anthropic LLM 返回空内容 | model=%s key=%s tokens=%s stop_reason=%s",
                req_model, key.name, tokens, stop_reason or "(空)"
            )
            failed_models.add(req_model)
            err_msg = f"empty content (stop_reason={stop_reason})"
            self._release(key, model=req_model, error=err_msg,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000),
                          error_type="empty_content")
            err = f"empty content (model={req_model}, key={key.name}, stop_reason={stop_reason})"
            self._record_call(key.name, req_model, success=False,
                              error=err,
                              duration_ms=int((time.time() - attempt_start_ts) * 1000))
            return {"status": "fail", "error": err}

        actual_model = data.get("model", req_model)
        stop_reason = data.get("stop_reason", "")
        # v12：解析 Anthropic 风格响应头饱和信号
        saturation = _parse_anthropic_rate_limit_saturation(resp.headers)
        self._release(key, success=True, tokens=tokens, model=req_model, saturation=saturation,
                      duration_ms=int((time.time() - attempt_start_ts) * 1000))
        self._record_usage(project, usage)
        self._record_call(key.name, actual_model, success=True,
                          tokens=tokens,
                          duration_ms=int((time.time() - attempt_start_ts) * 1000))
        return {
            "status": "ok",
            "ok": True,
            "content": content or "",
            "thinking": thinking,
            "usage": usage,
            # provider 原始 usage 原样透出（网关 usage_json 落库兜底，与 _call_openai 同口径）
            "usage_raw": raw_usage,
            "model": actual_model,
            "key_name": key.name,
            "finish_reason": stop_reason,
            "tool_calls": tool_calls,  # T05+: OpenAI 格式 list[dict]
        }

    def call_simple(self, prompt: str, system_prompt: str | None = None,
                    temperature: float | None = None, max_tokens: int | None = None,
                    timeout: int = 120, retries: int | None = None,
                    project: str = "default", model: str | None = None,
                    use_case: str | None = None, tier=None,
                    session_id: str | None = None) -> str | None:
        """简化调用：传入 prompt 文本，返回结果文本（失败返回 None）

        temperature / max_tokens 同 call()：None = 不下发，用 provider 默认。

        tier 参数同 call()，支持 int(1-5)/str/tuple(min,max)。
        session_id 参数同 call() v12，传入后会启用 LKGP 会话粘性。
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        result = self.call(messages, temperature=temperature,
                           max_tokens=max_tokens, timeout=timeout, retries=retries,
                           project=project, model=model, use_case=use_case, tier=tier,
                           session_id=session_id)
        if result.get("ok"):
            return result["content"]
        # 失败原因必须进 server.log，不能只 print 到 stdout（否则日志缺失无法定位根因）。
        # call() 内部对每次重试只做了 _record_call（进内存环形缓冲供监控面板看），
        # 各错误分支（429/402/401/HTTP 500/timeout/exception）只更新 last_error 不打日志；
        # 只有 "empty content" 路径有 WARNING。所以此处必须把最终聚合 error 打出来，
        # 下次失败时据此即可判断是 429 限流 / timeout / HTTP 错误 / no available keys。
        logger.warning("call_simple 最终失败 | project=%s use_case=%s error=%s",
                       project, use_case, result.get("error", "?"))
        return None

    def get_status(self) -> dict:
        """获取池状态（含 per-project token 统计 + per-model 详情 + is_free 标识）

        v10 增强：
        - keys[].models：每个 key 的所有 model 详情（含 tier、is_free、per-model stats）
        - keys[].is_free：是否免费服务
        - keys[].model_stats：per-model 统计（ok/fail/rate_limited/last_used/last_error）
        v11 增强：
        - keys[].protocol：连接协议（openai/anthropic）
        - keys[].models[].display_name：展示名（UI 显示用；缺失则等于 name）
        v12 增强（OmniRoute Pool-Deduped）：
        - keys[].pool_key：上游池去重标识（同 pool_key 的 key 共享上游配额）
        - total_max_concurrency_deduped：按 pool_key 分组取 max(max_concurrency) 之和
          （真实可并发数，避免把同一上游池的多个 key 当成独立配额）
        - pool_keys：每个 pool_key 的并发汇总（deduped_concurrency / active / keys_count）
        """
        with self.lock:
            now = time.time()
            active_keys = [k for k in self.keys if not k.is_expired]
            # v12：按 pool_key 分组计算去重后的真实并发上限
            # 同 pool_key 的多个 key 共享上游配额，取该组 max(max_concurrency) 作为该组实际并发
            # 空 pool_key 视为独立组（每个独立计 max_concurrency）
            pool_groups: dict[str, list] = {}
            for k in active_keys:
                pk = k.pool_key or k.key_id or k.name  # 空 pool_key 时每 key 独立成组
                pool_groups.setdefault(pk, []).append(k)
            total_max_concurrency_deduped = sum(
                max(grp, key=lambda x: x.max_concurrency).max_concurrency
                for grp in pool_groups.values()
            )
            # v12：每个 pool_key 的并发汇总（deduped_concurrency = 组内 max，active = 组内 active_count 之和）
            pool_keys_summary = [
                {
                    "pool_key": pk,
                    "keys_count": len(grp),
                    "deduped_concurrency": max(grp, key=lambda x: x.max_concurrency).max_concurrency,
                    "active_count": sum(k.active_count for k in grp),
                    "key_names": [k.name for k in grp],
                }
                for pk, grp in pool_groups.items()
            ]
            return {
                "total_keys": len(self.keys),
                "active_keys": len(active_keys),
                "expired_keys": len(self.keys) - len(active_keys),
                "total_max_concurrency": sum(k.max_concurrency for k in active_keys),
                # v12：去重后的真实可并发数（同 pool_key 组取 max）
                "total_max_concurrency_deduped": total_max_concurrency_deduped,
                # v12：dedup_ratio = deduped / naive（< 1.0 说明池中存在共享上游配额的 key）
                "dedup_ratio": round(total_max_concurrency_deduped / sum(k.max_concurrency for k in active_keys), 3)
                                if active_keys else 1.0,
                # v12：每个 pool_key 的并发汇总
                "pool_keys": pool_keys_summary,
                "current_active": sum(k.active_count for k in self.keys),
                "total_tokens_consumed": sum(k.stats.total_tokens for k in self.keys),
                "free_keys_count": sum(1 for k in self.keys if k.is_free),
                "paid_keys_count": sum(1 for k in self.keys if not k.is_free),
                "recent_calls_count": len(self._recent_calls),
                # v12：LKGP 会话粘性摘要（详情通过 get_session_stickiness() 查询）
                "session_stickiness_count": len(self._session_stickiness),
                "session_stickiness_ttl_seconds": self._session_stickiness_ttl,
                "session_stickiness_max": self._session_stickiness_max,
                # v13：多维配额跟踪摘要（详情通过 key.stats.quota_snapshot() 查询）
                "quota_tracking_enabled": self._quota_tracking_enabled,
                "quota_tracking_dimensions": [d for d, _ in self._quota_dims],
                # v13：Provider 级 Circuit Breaker 摘要（详情通过 get_breakers_snapshot() 查询）
                "circuit_breaker_enabled": self._breaker_enabled,
                "circuit_breaker_open_count": sum(
                    1 for b in self._breakers.values() if b.state == CB_OPEN
                ),
                "circuit_breaker_half_open_count": sum(
                    1 for b in self._breakers.values() if b.state == CB_HALF_OPEN
                ),
                "circuit_breaker_total_providers": len(self._breakers),
                # v14：消息压缩摘要（OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）
                "compression_mode": self._compression_mode,
                "compression_min_length": self._compression_min_length,
                "compression_total": self._compression_stats_total,
                "compression_saved_chars": self._compression_saved_chars_total,
                # v14：路由策略摘要（OmniRoute Phase 3.3 Auto 评分路由借鉴，ADR-0001）
                "routing_strategy": self._routing_strategy,
                "routing_mode_pack": self._routing_mode_pack,
                "routing_last_scores": dict(self._last_scores),
                # v15：model 级降级摘要（详情见 keys[].models[].disabled / consecutive_fails）
                "model_health_enabled": self._model_health_enabled,
                "model_health_disabled_count": sum(
                    1 for k in self.keys for _ in k.model_disabled
                ),
                "model_health_threshold": self._model_health_threshold,
                "model_health_probe_interval": self._model_health_probe_interval,
                "project_stats": dict(self.project_stats),
                "keys": [
                    {
                        "name": k.name,
                        "model": k.model,
                        "protocol": k.protocol,  # v11
                        "pool_key": k.pool_key,  # v12：上游池去重标识
                        "models": [
                            {
                                "name": m,
                                "display_name": k.get_display_name(m),  # v11
                                "tier": k.model_tiers.get(m, 3),
                                "is_free": _is_model_free(m, k.privacy_warning),
                                # v12：Model Lockout——model 级 cooldown 信息
                                "cooldown_remaining": round(k.model_cooldown_remaining(m), 1),
                                "in_model_cooldown": k.model_cooldown_remaining(m) > 0,
                                # v15：model 级降级状态（disabled / consecutive_fails / probe_in_flight）
                                "disabled": m in k.model_disabled,
                                "disabled_info": k.model_disabled_info(m),  # None 或 {disabled_at, last_probe_at, probe_in_flight}
                                "consecutive_fails": (k.model_stats.get(m) and k.model_stats[m].consecutive_fails) or 0,
                                "probe_in_flight": bool(
                                    k.model_disabled.get(m, {}).get("probe_in_flight", False)
                                ),
                                "stats": {
                                    "ok": (k.model_stats.get(m) and k.model_stats[m].ok) or 0,
                                    "fail": (k.model_stats.get(m) and k.model_stats[m].fail) or 0,
                                    "rate_limited": (k.model_stats.get(m) and k.model_stats[m].rate_limited) or 0,
                                    "total_tokens": (k.model_stats.get(m) and k.model_stats[m].total_tokens) or 0,
                                    "last_used": (k.model_stats.get(m) and k.model_stats[m].last_used) or 0,
                                    "last_error": (k.model_stats.get(m) and k.model_stats[m].last_error) or "",
                                    # v13：延迟分位（无样本时为 None，调用方需做空值处理）
                                    "latency_p50": (k.model_stats.get(m) and k.model_stats[m].p50()),
                                    "latency_p95": (k.model_stats.get(m) and k.model_stats[m].p95()),
                                    "latency_p99": (k.model_stats.get(m) and k.model_stats[m].p99()),
                                    "latency_sample_count": (k.model_stats.get(m) and k.model_stats[m].latency_sample_count()) or 0,
                                },
                            }
                            for m in k.models
                        ],
                        # v12：model_cooldowns 整体快照（model_name → cooldown_remaining 秒数）
                        "model_cooldowns": {
                            m: round(max(0.0, cd - now), 1)
                            for m, cd in k.model_cooldowns.items() if cd > now
                        },
                        "base_url": k.base_url,
                        "max_concurrency": k.max_concurrency,
                        "active_count": k.active_count,
                        "cooldown_remaining": round(max(0, k.cooldown_until - now), 1),
                        # v12：上游饱和信号
                        "saturation": round(k.saturation, 3),
                        "effective_saturation": round(k.effective_saturation(), 3),
                        "saturation_updated_at": k.saturation_updated_at,
                        "expired": k.is_expired,
                        "is_available": k.is_available,
                        "is_free": k.is_free,
                        "privacy_warning": k.privacy_warning,
                        "stats": {
                            "ok": k.stats.ok,
                            "fail": k.stats.fail,
                            "rate_limited": k.stats.rate_limited,
                            "total_tokens": k.stats.total_tokens,
                            "last_error": k.stats.last_error,
                            # v13：延迟分位（无样本时为 None）
                            "latency_p50": k.stats.p50(),
                            "latency_p95": k.stats.p95(),
                            "latency_p99": k.stats.p99(),
                            "latency_sample_count": k.stats.latency_sample_count(),
                            # v13：多维配额快照（dimension → {sum, count, window_seconds}）
                            "quota": k.stats.quota_snapshot(now),
                        },
                    }
                    for k in self.keys
                ],
            }

    def get_models_summary(self) -> dict:
        """跨 key 聚合 per-model 统计（按 model 名聚合）

        返回每个 model 的：
        - tier: 该 model 在不同 key 中的最低 tier（用于展示）
        - is_free: 任一 key 标记 is_free=True 则为 True
        - keys: 使用此 model 的 key 名列表
        - stats: 跨 key 聚合的 ok/fail/rate_limited/total_tokens
        - last_used: 最近一次调用时间戳
        - last_error: 最近一次错误描述
        - availability: 简单可用性指标（成功率 = ok / (ok + fail + rate_limited)）
        v11 增强：
        - display_name: 该 model 在任一 key 中设置的展示名（取第一个非空值）
        v12 增强（OmniRoute Pool-Deduped）：
        - deduped_max_concurrency: 按 pool_key 去重后的真实并发上限
          （同 pool_key 组内取 max，避免把同一上游池的多个 key 当独立配额）
        - dedup_ratio: deduped / naive（< 1.0 说明存在共享上游池的 key）
        - keys[].pool_key: 每个 key 的上游池去重标识
        - available_now: 考虑去重后是否仍有真实可用并发位
        """
        with self.lock:
            now = time.time()
            agg: dict[str, dict] = {}
            for k in self.keys:
                for m in k.models:
                    if m not in agg:
                        agg[m] = {
                            "name": m,
                            "display_name": k.get_display_name(m),  # v11
                            "tier": k.model_tiers.get(m, 3),
                            "is_free": _is_model_free(m, k.privacy_warning),
                            "keys": [],
                            "stats": {"ok": 0, "fail": 0, "rate_limited": 0, "total_tokens": 0},
                            "last_used": 0.0,
                            "last_error": "",
                        }
                    else:
                        # tier 取最低值（更省 cost 的）
                        agg[m]["tier"] = min(agg[m]["tier"], k.model_tiers.get(m, 3))
                        if not agg[m]["is_free"]:
                            agg[m]["is_free"] = _is_model_free(m, k.privacy_warning)
                        # v11：display_name 取第一个非空值（多个 key 有不同 display_name 时取先到的）
                        if not agg[m].get("display_name") or agg[m]["display_name"] == m:
                            dn = k.get_display_name(m)
                            if dn and dn != m:
                                agg[m]["display_name"] = dn
                    ms = k.model_stats.get(m)
                    agg[m]["keys"].append({
                        "name": k.name,
                        "is_expired": k.is_expired,
                        "in_cooldown": k.cooldown_until > now,
                        # v12：model 级 cooldown 信息
                        "in_model_cooldown": k.model_cooldown_remaining(m) > 0,
                        "model_cooldown_remaining": round(k.model_cooldown_remaining(m), 1),
                        "active_count": k.active_count,
                        # v12：上游池去重标识
                        "pool_key": k.pool_key,
                        # v13：per-key-per-model 延迟分位（无样本时为 None）
                        "latency_p50": ms.p50() if ms else None,
                        "latency_p95": ms.p95() if ms else None,
                        "latency_p99": ms.p99() if ms else None,
                        "latency_sample_count": ms.latency_sample_count() if ms else 0,
                        # v15：per-key-per-model 降级状态
                        "disabled": m in k.model_disabled,
                        "consecutive_fails": ms.consecutive_fails if ms else 0,
                        "probe_in_flight": bool(
                            k.model_disabled.get(m, {}).get("probe_in_flight", False)
                        ),
                    })
                    if ms:
                        agg[m]["stats"]["ok"] += ms.ok
                        agg[m]["stats"]["fail"] += ms.fail
                        agg[m]["stats"]["rate_limited"] += ms.rate_limited
                        agg[m]["stats"]["total_tokens"] += ms.total_tokens
                        if ms.last_used > agg[m]["last_used"]:
                            agg[m]["last_used"] = ms.last_used
                        if ms.last_error and not agg[m]["last_error"]:
                            agg[m]["last_error"] = ms.last_error
                        # v13：收集 latency 样本用于跨 key 聚合分位计算
                        agg[m].setdefault("_latency_samples", []).extend(ms.latency_samples)
                        # v15：聚合 consecutive_fails（取所有 key 中最大值，反映最差状态）
                        agg[m]["consecutive_fails_max"] = max(
                            agg[m].get("consecutive_fails_max", 0), ms.consecutive_fails
                        )
            # 计算可用性指标
            for _m, info in agg.items():
                s = info["stats"]
                total = s["ok"] + s["fail"] + s["rate_limited"]
                info["total_attempts"] = total
                info["success_rate"] = round(s["ok"] / total, 4) if total > 0 else None
                # v13：跨 key 聚合的延迟分位（合并所有 key 的 latency_samples 后计算）
                merged_samples = info.pop("_latency_samples", [])
                s["latency_p50"] = _percentile_of(merged_samples, 50)
                s["latency_p95"] = _percentile_of(merged_samples, 95)
                s["latency_p99"] = _percentile_of(merged_samples, 99)
                s["latency_sample_count"] = len(merged_samples)
                info["last_used_str"] = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["last_used"]))
                    if info["last_used"] else ""
                )
                # 当前是否可用：至少有一个 key 未 expired 且未在 key cooldown 且未在 model cooldown 且未 disabled
                # v12：增加 in_model_cooldown 判断（单 model 429 不再让整个 model 显示为不可用，因为同 model 在其他 key 上可能可用）
                # v15：增加 disabled 判断（disabled model 不算可用，需等探针恢复）
                info["available_now"] = any(
                    not k_info["is_expired"]
                    and not k_info["in_cooldown"]
                    and not k_info.get("in_model_cooldown", False)
                    and not k_info.get("disabled", False)
                    for k_info in info["keys"]
                )
                # v15：disabled_count（多少个 key 将此 model 标 disabled）
                info["disabled_count"] = sum(1 for k_info in info["keys"] if k_info.get("disabled", False))
                info["probe_in_flight_count"] = sum(
                    1 for k_info in info["keys"] if k_info.get("probe_in_flight", False)
                )
                # v12：按 pool_key 去重后的真实并发上限
                # 同 pool_key 组的多个 key 共享上游配额，取组内 max(max_concurrency) 作为该组实际并发
                # 空 pool_key 视为独立组（每个独立计 max_concurrency）
                # 计算 deduped_max_concurrency 时仅考虑未 expired 的 key
                pool_groups_m: dict[str, list] = {}
                for k_info in info["keys"]:
                    if k_info["is_expired"]:
                        continue
                    pk = k_info.get("pool_key") or k_info["name"]  # 空 pool_key 独立成组
                    pool_groups_m.setdefault(pk, []).append(k_info)
                # 用 key_id 反查 max_concurrency（agg[m].keys 不带 max_concurrency，需要从 self.keys 找）
                # 这里直接用 key 名匹配 self.keys 的方式（同 model 的 key 一般不超过 10 个，性能可接受）
                name_to_mc = {k.name: k.max_concurrency for k in self.keys if not k.is_expired}
                deduped = 0
                naive = 0
                for _pk, grp in pool_groups_m.items():
                    mc_values = [name_to_mc.get(k_info["name"], 0) for k_info in grp]
                    if mc_values:
                        deduped += max(mc_values)
                        naive += sum(mc_values)
                info["naive_max_concurrency"] = naive
                info["deduped_max_concurrency"] = deduped
                info["dedup_ratio"] = round(deduped / naive, 3) if naive > 0 else 1.0
            # 按总调用次数排序（最常用的在前）
            models_list = sorted(agg.values(),
                                 key=lambda x: -x["total_attempts"])
            return {
                "total_models": len(models_list),
                "free_models_count": sum(1 for m in models_list if m["is_free"]),
                # v15：model 级降级摘要
                "disabled_models_count": sum(1 for m in models_list if m.get("disabled_count", 0) > 0),
                "probe_in_flight_count": sum(m.get("probe_in_flight_count", 0) for m in models_list),
                "models": models_list,
            }

    def list_models(self) -> dict:
        """返回按 tier 分组的可用 model 清单（供前端模型选择器拉取）

        v6-lite-chat-fix T02：模型选择器后端端点数据源。

        返回结构：
            {
                "grouped_by_tier": {
                    "tier1-lightest": [{"model": "...", "tier": 1, "provider": "..."}, ...],
                    "tier3-medium":   [{"model": "...", "tier": 3, "provider": "..."}, ...],
                    ...
                },
                "default_tier": "tier3-medium",
                "default_model": "medium-a",
            }

        设计决策：
        - default_tier 取 USE_CASE_REGISTRY['agent_chat'].default_tier 的 min（省 cost 优先）
          agent_chat.default_tier = (3, 5)，min=3 → TIER_NAMES[3] = "tier3-medium"
        - default_model 取 default_tier 分组中首个 model（按 tier 升序 + provider 名排序，保证确定性）
        - 同名 model 在多个 key 中出现时，每个 (model, provider) 组合都列出（前端可显示冗余度）
        - tier 名取自 TIER_NAMES 字典的值（tier1-lightest / tier2-light / ... / tier5-powerful）
        """
        from server.llm_pool.types import TIER_NAMES, USE_CASE_REGISTRY

        # 1. 收集所有 (model, tier, provider) 三元组（持锁读 keys）
        entries: list[tuple[str, int, str]] = []
        with self.lock:
            for k in self.keys:
                for m in k.models:
                    tier = k.model_tiers.get(m, 3)
                    entries.append((m, tier, k.name))

        # 2. 按 tier 分组（key 用 TIER_NAMES 的值，如 "tier3-medium"）
        grouped_by_tier: dict[str, list[dict]] = {}
        for model_name, tier, provider in entries:
            tier_name = TIER_NAMES.get(tier, f"tier{tier}-unknown")
            grouped_by_tier.setdefault(tier_name, []).append({
                "model": model_name,
                "tier": tier,
                "provider": provider,
            })

        # 3. 计算 default_tier（agent_chat.default_tier 的 min，对应 TIER_NAMES）
        agent_chat = USE_CASE_REGISTRY.get("agent_chat")
        default_tier_num = agent_chat.default_tier[0] if agent_chat else 3
        default_tier = TIER_NAMES.get(default_tier_num, "tier3-medium")

        # 4. 计算 default_model（default_tier 分组中首个 model；分组为空时回退到任意首个可用 model）
        default_tier_entries = grouped_by_tier.get(default_tier, [])
        if default_tier_entries:
            default_model = default_tier_entries[0]["model"]
        else:
            # default_tier 分组为空（如池中无 tier 3 model）→ 回退到 grouped_by_tier 中首个非空分组的首个 model
            default_model = ""
            for _tier_name, entries_list in grouped_by_tier.items():
                if entries_list:
                    default_model = entries_list[0]["model"]
                    break

        return {
            "grouped_by_tier": grouped_by_tier,
            "default_tier": default_tier,
            "default_model": default_model,
        }

    def print_status(self):
        """打印池状态（终端友好）"""
        s = self.get_status()
        print(f"\n{'='*60}")
        print(f"LLM Pool: {s['active_keys']}/{s['total_keys']} keys active, "
              f"concurrency {s['current_active']}/{s['total_max_concurrency']}, "
              f"tokens={s['total_tokens_consumed']}")
        for k in s["keys"]:
            status = "EXPIRED" if k["expired"] else (
                f"CD {k['cooldown_remaining']}s" if k["cooldown_remaining"] > 0
                else f"{k['active_count']}/{k['max_concurrency']}"
            )
            print(f"  {k['name']:15s} {k['model']:16s} {status:12s} "
                  f"ok={k['stats']['ok']:4d} fail={k['stats']['fail']:3d} "
                  f"429={k['stats']['rate_limited']:3d} "
                  f"tok={k['stats']['total_tokens']}")
            if k.get('privacy_warning'):
                print(f"    [!] 隐私警告: {k['privacy_warning']}")
        # per-project token 统计
        ps = s.get("project_stats", {})
        if ps:
            print("\n  --- Per-Project Token 统计 ---")
            print(f"  {'Project':25s} {'Calls':>7s} {'Prompt':>10s} {'Compl':>10s} {'Total':>10s}")
            for proj, st in sorted(ps.items(), key=lambda x: -x[1].get("total_tokens", 0)):
                print(f"  {proj:25s} {st.get('calls',0):7d} "
                      f"{st.get('prompt_tokens',0):10d} "
                      f"{st.get('completion_tokens',0):10d} "
                      f"{st.get('total_tokens',0):10d}")
            tot_pt = sum(v.get("prompt_tokens", 0) for v in ps.values())
            tot_ct = sum(v.get("completion_tokens", 0) for v in ps.values())
            tot_tt = sum(v.get("total_tokens", 0) for v in ps.values())
            tot_cl = sum(v.get("calls", 0) for v in ps.values())
            print(f"  {'-'*65}")
            print(f"  {'TOTAL':25s} {tot_cl:7d} {tot_pt:10d} {tot_ct:10d} {tot_tt:10d}")
        print(f"{'='*60}\n")
