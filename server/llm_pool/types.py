"""LLM 并发池数据结构定义

含数据类（KeyStats/LLMKey/ProviderPolicy）和 use_case 注册表（UseCaseDef/USE_CASE_REGISTRY）。
USE_CASE_REGISTRY 原位于 key_store.py，v12 重构迁入 types.py 以打破 types ↔ key_store 循环依赖
（types.LLMKey.use_case_eligible 原本 lazy import key_store.USE_CASE_REGISTRY）。
"""

import time
from collections import deque
from dataclasses import dataclass, field

# =====================================================================
# v13：多维配额跟踪（OmniRoute percent/requests/tokens × 5h/h/d/w/mo 借鉴）
# =====================================================================

# 时间单位 → 秒数映射（dimension 字符串 "requests/hour" 的 "hour" 部分用此查表）
_QUOTA_UNIT_SECONDS: dict[str, int] = {
    "minute": 60,
    "5min": 300,
    "hour": 3600,
    "6h": 21600,
    "12h": 43200,
    "day": 86400,
    "week": 604800,
    "month": 2592000,  # 30 天近似
}


def parse_quota_dimension(dimension: str) -> tuple[str, int] | None:
    """解析配额维度字符串

    格式：<metric>/<unit>，如 "requests/hour"、"tokens/day"、"tokens/5h"。
    返回 (metric, window_seconds) 或 None（格式不合法时）。
    """
    if not dimension or "/" not in dimension:
        return None
    parts = dimension.split("/", 1)
    if len(parts) != 2:
        return None
    metric, unit = parts[0].strip().lower(), parts[1].strip().lower()
    if not metric or unit not in _QUOTA_UNIT_SECONDS:
        return None
    return metric, _QUOTA_UNIT_SECONDS[unit]


@dataclass
class SlidingWindowCounter:
    """滑动窗口计数器（用于多维配额跟踪）

    维护 (timestamp, value) 元组的 deque，查询时自动淘汰过期条目。
    用于跟踪近 N 秒内的请求/token 累计消耗，反映 key 的真实配额使用速率。
    """
    window_seconds: int
    # deque[(timestamp, value)]：每条记录代表一次配额消耗（值 = 1 for requests / token 数 for tokens）
    _samples: deque = field(default_factory=deque)
    _sum_cache: float = 0.0  # 累计和缓存（淘汰时减去过期值，查询时 O(1)）
    _last_evict_ts: float = 0.0  # 上次淘汰时间（避免每次查询都遍历）

    def add(self, value: float = 1.0, now: float | None = None) -> None:
        """记录一次配额消耗

        value: 请求计数时传 1；token 计数时传 token 数。
        """
        if value <= 0:
            return
        ts = now if now is not None else time.time()
        self._samples.append((ts, float(value)))
        self._sum_cache += float(value)
        # 顺便淘汰过期（按需触发，避免每次 add 都遍历）
        if ts - self._last_evict_ts > 5.0:
            self._evict_expired(ts)

    def _evict_expired(self, now: float) -> None:
        """淘汰过期样本（pop 左侧直到第一个未过期）"""
        threshold = now - self.window_seconds
        while self._samples and self._samples[0][0] < threshold:
            _, v = self._samples.popleft()
            self._sum_cache -= v
        self._last_evict_ts = now
        # 浮点累计可能产生微小误差，归零保护
        if self._sum_cache < 0:
            self._sum_cache = 0.0

    def sum(self, now: float | None = None) -> float:
        """返回当前窗口内的累计消耗值"""
        ts = now if now is not None else time.time()
        self._evict_expired(ts)
        return self._sum_cache

    def count(self, now: float | None = None) -> int:
        """返回当前窗口内的样本数（请求次数）"""
        ts = now if now is not None else time.time()
        self._evict_expired(ts)
        return len(self._samples)

    def snapshot(self, now: float | None = None) -> dict:
        """返回监控快照（sum + count + window_seconds）"""
        ts = now if now is not None else time.time()
        return {
            "sum": self.sum(ts),
            "count": self.count(ts),
            "window_seconds": self.window_seconds,
        }


# =====================================================================
# Use case 注册表（原 key_store.py 迁入）
# =====================================================================


@dataclass(frozen=True)
class UseCaseDef:
    """单个 use_case 的属性声明"""
    name: str
    scope: str           # "llm" | "vl" | "aigc_image" | "aigc_video"
    sensitive: bool      # True = 排除带 privacy_warning 的 key
    default_tier: tuple[int, int]  # (min, max) 范围，优先选范围内更低 tier 省 cost
    desc: str


USE_CASE_REGISTRY: dict[str, UseCaseDef] = {
    # ── LLM use cases ──
    # 范围 [min, max]：tier 在范围内即匹配，优先选更低 tier（省 cost）
    # 日常任务 [3,5]：默认层够用，更强也行；强推理任务 [4,5]：最低用 tier 4 保证质量
    "agent_chat":          UseCaseDef("agent_chat",          "llm", False, (3, 5), "Agent 对话/评分端点"),
    "community_summarize": UseCaseDef("community_summarize", "llm", False, (3, 5), "社群帖子批量总结"),
    # v15：memory_compress / memory_validate 统一 tier (2,4)，retries=1（调用方传入）
    # 废弃 memory_maintainer use_case，拆分为 memory_validate（验证单条记忆 facts）
    "memory_compress":   UseCaseDef("memory_compress",   "llm", True,  (2, 4), "记忆压缩（处理个人对话历史）"),
    "memory_validate":   UseCaseDef("memory_validate",   "llm", True,  (2, 4), "记忆验证（处理个人记忆 facts）"),
    "hourly_summarize":    UseCaseDef("hourly_summarize",    "llm", False, (3, 5), "每小时活动总结（tier 放宽到 3-5，让 deepseek-v4-flash-free 作为 tier 4-5 quota 耗尽时的 fallback）"),
    "download_watcher":   UseCaseDef("download_watcher",   "llm", True,  (4, 5), "下载文件分类（含文件内容，敏感）"),
    "command_guard":      UseCaseDef("command_guard",      "llm", False, (3, 5), "命令审批 LLM 预审"),
    # 入站网关转发（Cline/Cherry Studio 等 harness）。default_tier (0,0)=不带 tier 约束，
    # 兜底与否由入站 key 的按别名兜底范围决定；显式注册让出站 key 的 allowed_uses
    # 白名单对入站流量生效（未注册前网关不传 use_case，白名单形同虚设）。
    "inbound_gateway":    UseCaseDef("inbound_gateway",    "llm", False, (0, 0), "入站网关转发（harness 流量）"),
    # ── VL use cases（全部 sensitive=true，预防性）──
    # [2,5]：VL 模型已多样化（glm-4.6v-flash tier2 / agnes-2.0-flash tier2 / Qwen3-VL-235B tier5 / gpt-5.6-luna tier5）
    # 放宽 tier 范围让所有 VL 模型都能参与 fallback，避免单一 provider 429/503 时无其他 provider 可用
    "vl_ocr":              UseCaseDef("vl_ocr",              "vl",  True,  (2, 5), "VL OCR 文字识别"),
    "vl_vision":           UseCaseDef("vl_vision",           "vl",  True,  (2, 5), "VL UI 元素解析"),
    "vl_activity_tracker": UseCaseDef("vl_activity_tracker", "vl",  True,  (2, 5), "VL 活动追踪分析"),
    # ── AIGC use cases（图片/视频生成，非 sensitive）──
    # [3,3]：agnes-2.0-flash 系列，目前唯一可用的 AIGC 模型
    "aigc_image_gen":      UseCaseDef("aigc_image_gen",      "aigc_image", False, (3, 5), "文生图/图生图"),
    "aigc_image_edit":     UseCaseDef("aigc_image_edit",     "aigc_image", False, (3, 5), "图像编辑"),
    "aigc_video_gen":      UseCaseDef("aigc_video_gen",      "aigc_video", False, (3, 5), "视频生成"),
}


def _load_component_use_cases() -> dict[str, "UseCaseDef"]:
    """从 workspace manifests 动态加载组件声明的 LLM use cases

    通过 manifest [[llm_use_cases]] 段发现组件声明的 use cases，
    动态注册到 USE_CASE_REGISTRY。
    删除 workspace/<module>/ 后 manifest 消失，use cases 自动注销。

    异常时返回空 dict，不影响核心 use cases。
    """
    result: dict[str, UseCaseDef] = {}
    try:
        from server.component_manifest import load_manifests
        manifests = load_manifests()
        for m in manifests.values():
            for uc in m.llm_use_cases:
                result[uc.name] = UseCaseDef(
                    name=uc.name,
                    scope=uc.scope,
                    sensitive=uc.sensitive,
                    default_tier=uc.default_tier,
                    desc=uc.desc,
                )
    except Exception:
        pass
    return result


# 动态合并组件声明的 use cases（启动时执行一次）
USE_CASE_REGISTRY.update(_load_component_use_cases())


# 旧 tier 名 → 新 tier 数字的映射（向后兼容旧调用方）
_LEGACY_TIER_MAP: dict[str, int] = {
    "cheap": 2,
    "default": 3,
    "powerful": 5,
}

# tier 数字 → 显示名（供 /status 等端点展示用）
TIER_NAMES: dict[int, str] = {
    1: "tier1-lightest",
    2: "tier2-light",
    3: "tier3-medium",
    4: "tier4-strong",
    5: "tier5-powerful",
}


SENSITIVE_USES: set[str] = {uc.name for uc in USE_CASE_REGISTRY.values() if uc.sensitive}


def _get_allow_privacy_warning() -> bool:
    """读取隐私放行开关（resolve_keys 和 use_case_eligible 共用，避免逻辑分歧）

    读取 config.toml [llm.privacy] allow_privacy_warning_for_sensitive。
    true 时 sensitive + privacy_warning 非空的 key 放行；false 时拒绝。
    """
    try:
        from server.config import get_privacy_config
        return get_privacy_config().get("allow_privacy_warning_for_sensitive", False)
    except Exception:
        return False


@dataclass
class KeyStats:
    """单个 key 的统计信息

    v13 增强（OmniRoute per-connection p50/p95/p99 借鉴）：
    - latency_samples: 最近 N 次调用的延迟样本（毫秒，滑动窗口 deque）
      用于计算 p50/p95/p99 延迟分位，反映 key 的稳定性与尾延迟
    - record_latency(duration_ms): 记录单次调用延迟
    - p50()/p95()/p99(): 返回对应分位延迟，无样本时返回 None

    v13 增强（OmniRoute 多维配额 percent/requests/tokens × 5h/h/d/w/mo 借鉴）：
    - quota_counters: 按维度索引的 SlidingWindowCounter（lazy 创建）
      维度格式 "requests/hour" / "tokens/day" 等，由 LLMPool 配置注入
    - record_quota(dimensions, tokens): 按 configured dimensions 累计请求/token 消耗
    - quota_snapshot(): 返回所有维度的当前快照（监控用）
    """
    ok: int = 0
    fail: int = 0
    rate_limited: int = 0
    total_tokens: int = 0
    expired: bool = False
    last_error: str = ""
    last_used: float = 0.0
    # v13: 延迟样本（滑动窗口，最多 100 个样本，溢出淘汰最旧）
    latency_samples: deque = field(default_factory=lambda: deque(maxlen=100))
    # v13: 多维配额跟踪（dimension_str → SlidingWindowCounter，lazy 创建）
    quota_counters: dict = field(default_factory=dict)
    # v15: 连续失败计数（仅计 timeout/HTTP5xx/HTTP其他/空响应/异常，不计 429/401/402）
    # 成功一次清零；达 consecutive_fail_threshold 触发 model_disabled
    consecutive_fails: int = 0

    def record_latency(self, duration_ms: float) -> None:
        """记录单次调用延迟（毫秒）

        失败调用也记录（含 timeout / 429 / 5xx），便于观察尾延迟。
        """
        if duration_ms is None or duration_ms < 0:
            return
        self.latency_samples.append(float(duration_ms))

    def _percentile(self, p: float) -> float | None:
        """计算延迟分位（p ∈ [0, 100]）

        采用 nearest-rank 方法（不插值），样本数 < 2 时返回唯一值或 None。
        """
        n = len(self.latency_samples)
        if n == 0:
            return None
        if n == 1:
            return self.latency_samples[0]
        # nearest-rank: rank = ceil(p/100 * n)，1-indexed
        import math
        rank = max(1, math.ceil(p / 100.0 * n))
        sorted_samples = sorted(self.latency_samples)
        return sorted_samples[min(rank - 1, n - 1)]

    def p50(self) -> float | None:
        """中位延迟（毫秒）"""
        return self._percentile(50)

    def p95(self) -> float | None:
        """95 分位延迟（毫秒）—— 尾延迟主要观察值"""
        return self._percentile(95)

    def p99(self) -> float | None:
        """99 分位延迟（毫秒）—— 极端尾延迟"""
        return self._percentile(99)

    def latency_sample_count(self) -> int:
        """当前样本数（用于判断分位可信度，< 20 时置信度低）"""
        return len(self.latency_samples)

    # ---- v13: 多维配额跟踪 ----

    def record_quota(self, dimensions: list[tuple[str, int]], tokens: int = 0) -> None:
        """按维度记录一次配额消耗

        dimensions: [(dimension_str, window_seconds), ...] 由 LLMPool 注入
        tokens: 本次消耗的 token 数（>0 时才计入 tokens/* 维度）

        约定：
        - metric 含 "requests" 的维度 +1（每次调用都计）
        - metric 含 "tokens" 的维度 +tokens（仅 tokens > 0 时计）
        """
        if not dimensions:
            return
        now = time.time()
        for dim_str, win_sec in dimensions:
            metric = dim_str.split("/", 1)[0].strip().lower() if "/" in dim_str else dim_str
            counter = self.quota_counters.get(dim_str)
            if counter is None:
                counter = SlidingWindowCounter(window_seconds=win_sec)
                self.quota_counters[dim_str] = counter
            if metric == "requests":
                counter.add(1.0, now=now)
            elif metric == "tokens":
                if tokens > 0:
                    counter.add(float(tokens), now=now)
            else:
                # 未知 metric，按请求数 +1 兜底（保守计数）
                counter.add(1.0, now=now)

    def quota_snapshot(self, now: float | None = None) -> dict:
        """返回所有配额维度的快照（监控用）"""
        ts = now if now is not None else time.time()
        return {
            dim: counter.snapshot(ts)
            for dim, counter in self.quota_counters.items()
        }


@dataclass
class CallRecord:
    """单次调用记录（用于近期调用历史环形缓冲）

    存储最近 N 次调用的元信息，供监控面板展示 per-model 调用情况。
    """
    ts: float                      # 调用时间戳
    key_name: str                  # 使用的 key 名
    model: str                     # 实际调用的 model
    success: bool                  # 是否成功
    tokens: int = 0               # 本次调用消耗的 token 数
    error: str = ""                # 失败时的错误描述
    duration_ms: int = 0           # 调用耗时（毫秒）


# 免费模型推断关键字：model 名或 privacy_warning 含以下任意关键字 → is_free=True
_FREE_KEYWORDS = (
    "free", "免费", "trial", "试用",
)


def _infer_is_free(model_name: str, privacy_warning: str) -> bool:
    """根据 model 名或 privacy_warning 推断是否免费服务

    启发式：
    - model 名含 "-free" / "free-" / "free_" / "_free" → True
    - privacy_warning 含"免费"/"free"/"试用"/"trial" → True
    - 其他情况 → False（视为付费）
    """
    name_lower = (model_name or "").lower()
    if any(kw in name_lower for kw in _FREE_KEYWORDS):
        return True
    warning_lower = (privacy_warning or "").lower()
    return any(kw.lower() in warning_lower for kw in _FREE_KEYWORDS)


@dataclass
class LLMKey:
    """单个 API key 的配置和运行时状态

    v8 schema: KeyRecord.models 改为 list[dict]（model 级 scope + tier），
    LLMKey.models 仍保持 list[str]（仅含 LLM scope 的 model 名）。
    LLMKey.model_tiers 是 model_name → tier(1-5) 的映射，供 _acquire 做 tier 硬匹配。
    load_llm_keys_for_pool 转换时过滤出 LLM model 名传入。
    model 属性返回 default_model 或 models[0]，可继续用 k.model 访问。

    v10 增强：
    - model_stats: dict[str, KeyStats] per-model 统计（ok/fail/rate_limited/last_used/last_error）
      区别于 key.stats（key 级聚合），用于精准定位哪个 model 失败率高
    - is_free: bool 推断此 key 是否为免费服务（基于 privacy_warning 或 model 名）
    - protocol: str 连接协议（"openai" | "anthropic"），调用时按协议构造请求
    - model_display_names: dict[str, str] model_name → display_name 映射
      UI 展示用 display_name；调用仍用 model_name（连接名）
    """
    key: str
    base_url: str
    models: list[str] = field(default_factory=list)
    name: str = ""
    max_concurrency: int = 3
    active_count: int = 0
    cooldown_until: float = 0.0
    stats: KeyStats = field(default_factory=KeyStats)
    privacy_warning: str = ""  # 隐私警告：非空表示该 key 会泄露数据，不可传输敏感信息
    allowed_uses: list[str] = field(default_factory=list)  # use_case 白名单，空=无限制
    key_id: str = ""  # 关联 keys.json record id
    default_model: str = ""  # 未指定 model 时使用（通常 = models[0]）
    model_tiers: dict = field(default_factory=dict)  # v8: {model_name: tier(1-5)}
    # v10 新增：per-model 统计（按 model 名索引，运行时按需创建）
    model_stats: dict = field(default_factory=dict)
    # v10 新增：是否免费服务（基于 privacy_warning 或 model 名推断）
    is_free: bool = False
    # v11 新增：连接协议（默认 "openai"，支持 "anthropic"）
    protocol: str = "openai"
    # v11 新增：model_name → display_name 映射（UI 展示用，缺失则用 model_name）
    model_display_names: dict = field(default_factory=dict)
    # v12 新增：上游共享池标识（OmniRoute pool-deduped 借鉴）
    # 同一 pool_key 的多个 key 共享上游配额池（如多个 OpenRouter key 访问同一 Anthropic 池），
    # 在 get_status 计算总容量时按 pool_key 取 max，避免重复计数。
    # 空字符串 = 独立池（按 key 个体计数）。
    pool_key: str = ""
    # v12 新增：per-model cooldown（OmniRoute Model Lockout 借鉴）
    # 单 model 429 时只冻结该二元组，不波及同 key 其他可用 tier 模型
    # 结构：{model_name: cooldown_until_timestamp}
    model_cooldowns: dict = field(default_factory=dict)
    # v12 新增：上游饱和信号（OmniRoute Saturation Reflow 借鉴）
    # 0.0 = 充裕/未知；1.0 = 已耗尽。来源：解析上游响应头
    # x-ratelimit-remaining-* / anthropic-ratelimit-*-utilization 等。
    # _acquire 在同可用性下优先选 saturation 低的 key，主动避免硬 429。
    saturation: float = 0.0
    saturation_updated_at: float = 0.0
    # v15 新增：per-model disabled 状态（model_disabled + 退避 + 探针恢复）
    # 结构：{model_name: {"disabled_at": ts, "last_probe_at": ts, "probe_in_flight": bool}}
    # consecutive_fails 存于 model_stats[m].consecutive_fails，达阈值后在此字典登记。
    # disabled 后定时放探针（probe_interval_seconds），成功解除，失败重置 last_probe_at。
    model_disabled: dict = field(default_factory=dict)
    # v14 新增：per-key pool 策略（重试/限流参数）
    # None = 使用全局默认 [llm.pool]（由 LLMPool.__init__ 填充）
    # 非 None = 使用 key 级自定义策略（从 keys.json pool 段加载）
    pool_policy: "ProviderPolicy | None" = None

    def __post_init__(self):
        """规范化字段：models 至少为空列表；default_model 默认取 models[0]；name 默认值；
        初始化 model_stats 字典；推断 is_free；规范化 protocol；初始化 model_cooldowns。"""
        if not self.models:
            self.models = []
        if not self.default_model and self.models:
            self.default_model = self.models[0]
        if not self.name:
            self.name = self.key[:10] + "..."
        if not self.model_tiers:
            self.model_tiers = {}
        # 初始化 per-model stats 字典（每个 model 一个独立 KeyStats）
        if not self.model_stats:
            self.model_stats = {m: KeyStats() for m in self.models}
        # 推断 is_free（若未显式传入 True，则根据 model 名 + privacy_warning 推断）
        if not self.is_free:
            for m in self.models:
                if _infer_is_free(m, self.privacy_warning):
                    self.is_free = True
                    break
        # v11：规范化 protocol（默认 openai，未知值回退 openai）
        if not self.protocol or not isinstance(self.protocol, str):
            self.protocol = "openai"
        self.protocol = self.protocol.lower().strip()
        if self.protocol not in ("openai", "anthropic"):
            self.protocol = "openai"
        # v11：规范化 model_display_names
        if not isinstance(self.model_display_names, dict):
            self.model_display_names = {}
        # v12：规范化 model_cooldowns（必须是 dict，过滤掉已过期的条目避免字典无限增长）
        if not isinstance(self.model_cooldowns, dict):
            self.model_cooldowns = {}
        else:
            now = time.time()
            # 清理已过期的 model cooldown（避免字典无限增长）
            self.model_cooldowns = {
                m: cd for m, cd in self.model_cooldowns.items() if cd > now
            }

    @property
    def model(self) -> str:
        """向后兼容：返回 default_model 或 models[0]"""
        return self.default_model or (self.models[0] if self.models else "")

    @property
    def is_available(self) -> bool:
        """是否可分配新请求"""
        return (not self.stats.expired
                and time.time() >= self.cooldown_until
                and self.active_count < self.max_concurrency)

    def is_model_available(self, model: str) -> bool:
        """v12：检查指定 model 是否可分配（Model Lockout 借鉴；v15 增加 disabled 判定）

        key 级可用 + model 级 cooldown 未到期 + model 未被 disabled。
        用于 _acquire 选 model 时跳过单 model 429 冻结或连续失败被降级的二元组，
        避免单 model 失败波及同 key 其他可用 tier 模型。

        注意：disabled model 的探针恢复不通过此方法判断，而是由 try_acquire_probe 专门处理。
        """
        if not self.is_available:
            return False
        if not model:
            return True  # 未指定 model 时只看 key 级
        # v15：disabled 的 model 不可分配（探针由 try_acquire_probe 单独放行）
        if model in self.model_disabled:
            return False
        cd = self.model_cooldowns.get(model)
        if cd is None:
            return True
        return time.time() >= cd

    def record_model_cooldown(self, model: str, cooldown_until: float) -> None:
        """v12：记录 model 级 cooldown（429 时调用）

        与 key.cooldown_until 独立——key 级保留作 fallback 信号，
        model 级实现精细化隔离。
        """
        if not model or cooldown_until <= 0:
            return
        # 取 max 避免被更短的新 cooldown 覆盖（已有更长 cooldown 时保留）
        existing = self.model_cooldowns.get(model, 0.0)
        self.model_cooldowns[model] = max(existing, cooldown_until)

    def model_cooldown_remaining(self, model: str) -> float:
        """v12：返回 model 剩余 cooldown 秒数（0 表示可用）"""
        if not model:
            return 0.0
        cd = self.model_cooldowns.get(model)
        if cd is None:
            return 0.0
        return max(0.0, cd - time.time())

    # ---- v15: model_disabled + 退避 + 探针恢复 ----

    def mark_model_disabled(self, model: str) -> None:
        """v15：标记 model 为 disabled（consecutive_fails 达阈值时调用）

        幂等：已 disabled 时不重复标记（保留原 disabled_at）。
        """
        if not model or model in self.model_disabled:
            return
        now = time.time()
        self.model_disabled[model] = {
            "disabled_at": now,
            "last_probe_at": now,  # 初始化为 disabled 时刻，从此开始计 probe_interval
            "probe_in_flight": False,
        }

    def try_acquire_probe(self, model: str, probe_interval_seconds: float, probe_max_concurrency: int) -> bool:
        """v15：尝试为 disabled model 获取探针资格

        条件：距 last_probe_at >= probe_interval_seconds 且当前 in-flight 探针数 < probe_max_concurrency。
        成功时标 probe_in_flight=True 并更新 last_probe_at，调用方应让该 model 被选中一次。
        失败时返回 False（仍在等待探针窗口或并发已满）。
        """
        if not model or model not in self.model_disabled:
            return False
        info = self.model_disabled[model]
        now = time.time()
        if info.get("probe_in_flight"):
            return False  # 已有探针在飞
        if now - info.get("last_probe_at", now) < probe_interval_seconds:
            return False  # 未到探针窗口
        # 全局并发检查（probe_max_concurrency 通常为 1，跨 model 统计 in-flight 数）
        in_flight_count = sum(
            1 for v in self.model_disabled.values() if v.get("probe_in_flight")
        )
        if in_flight_count >= probe_max_concurrency:
            return False
        info["probe_in_flight"] = True
        info["last_probe_at"] = now
        return True

    def resolve_probe(self, model: str, success: bool) -> None:
        """v15：结束探针，按结果处理 disabled 状态

        成功：移除 disabled 标记 + 清零 consecutive_fails。
        失败：保留 disabled，probe_in_flight 复位（last_probe_at 已在 try_acquire_probe 更新，等下个窗口）。
        """
        if not model or model not in self.model_disabled:
            return
        info = self.model_disabled[model]
        info["probe_in_flight"] = False
        if success:
            del self.model_disabled[model]
            stats = self.model_stats.get(model)
            if stats is not None:
                stats.consecutive_fails = 0

    def clear_model_disabled(self, model: str) -> None:
        """v15：手动清除 model 的 disabled 标记（/llm/pool/health-check 触发时调用）"""
        if not model:
            return
        self.model_disabled.pop(model, None)
        stats = self.model_stats.get(model)
        if stats is not None:
            stats.consecutive_fails = 0

    def model_disabled_info(self, model: str) -> dict | None:
        """v15：返回 model 的 disabled 元信息（无则 None），供 get_status 暴露"""
        if not model or model not in self.model_disabled:
            return None
        return dict(self.model_disabled[model])

    def has_any_available_model(self, exclude_disabled: bool = True) -> bool:
        """v15：key 是否还有至少一个可用 model（用于 _acquire 跳过无可用 model 的 key）

        exclude_disabled=True 时，disabled model 不计入可用。
        注意：此方法不考虑 tier 范围，仅做粗粒度判断；精细的 tier 过滤由调用方处理。
        """
        if not self.models:
            return False
        now = time.time()
        for m in self.models:
            if exclude_disabled and m in self.model_disabled:
                continue
            cd = self.model_cooldowns.get(m)
            if cd is not None and cd > now:
                continue
            return True
        return False

    def update_saturation(self, saturation: float) -> None:
        """v12：记录上游饱和信号（0.0=充裕/未知，1.0=耗尽）

        来源：解析上游响应头 x-ratelimit-remaining-* / anthropic-ratelimit-*-utilization。
        由 _call_openai / _call_anthropic 在 200 响应后调用。
        """
        try:
            s = float(saturation)
        except (TypeError, ValueError):
            return
        # 限制在 [0, 1] 范围内
        self.saturation = max(0.0, min(1.0, s))
        self.saturation_updated_at = time.time()

    def effective_saturation(self, ttl_seconds: float = 30.0) -> float:
        """v12：返回有效饱和度（含 TTL 检查）

        若 saturation_updated_at 距今超过 ttl_seconds，视为过期返回 0.0（未知）。
        否则返回 saturation。
        OmniRoute 借鉴：30s TTL 防止陈旧信号误导路由决策。
        """
        if self.saturation_updated_at <= 0:
            return 0.0
        if time.time() - self.saturation_updated_at > ttl_seconds:
            return 0.0
        return self.saturation

    @property
    def is_expired(self) -> bool:
        return self.stats.expired

    def has_tier(self, tier: int) -> bool:
        """检查此 key 是否含至少一个指定 tier 的 model（v8，向后兼容）"""
        return self.has_tier_in_range(tier, tier)

    def has_tier_in_range(self, min_tier: int, max_tier: int) -> bool:
        """检查此 key 是否含至少一个 tier 在 [min, max] 范围内的 model"""
        return any(min_tier <= t <= max_tier for t in self.model_tiers.values())

    def get_model_for_tier(self, tier: int) -> str:
        """获取此 key 中匹配 tier 的第一个 model 名（向后兼容）"""
        return self.get_best_model_in_range(tier, tier)

    def get_best_model_in_range(self, min_tier: int, max_tier: int,
                                exclude: set[str] | None = None) -> str:
        """获取此 key 中 tier 在范围内且最低的 model 名（优先更低 tier 省 cost）

        exclude: 排除的 model 名集合（本次调用周期内已失败过的 model，
                 触发 tier 升级 fallback，避免反复重试同一个坏 model）
        """
        candidates = [(t, name) for name, t in self.model_tiers.items()
                      if min_tier <= t <= max_tier and (not exclude or name not in exclude)]
        if not candidates:
            return ""
        candidates.sort(key=lambda x: x[0])  # 按 tier 升序
        return candidates[0][1]

    def use_case_eligible(self, use_case: str) -> bool:
        """检查此 key 是否可用于指定 use_case

        - 未知 use_case 放行（向后兼容）
        - sensitive use_case + privacy_warning 非空 → 拒绝
          （除非 config [llm.privacy] allow_privacy_warning_for_sensitive=true 放行）
        - allowed_uses 非空且不含 use_case → 拒绝

        v14: 读取 allow_privacy_warning_for_sensitive 开关，与 resolve_keys() 行为统一。
        """
        # v12: USE_CASE_REGISTRY 已迁入本模块（types.py），无需 lazy import key_store
        uc = USE_CASE_REGISTRY.get(use_case)
        if uc is None:
            return True
        if uc.sensitive and self.privacy_warning:
            # v14: 读隐私放行开关，与 resolve_keys() 一致
            if not _get_allow_privacy_warning():
                return False
        return not (self.allowed_uses and use_case not in self.allowed_uses)

    def record_model_stats(self, model: str, success: bool = False,
                           rate_limited: bool = False, expired: bool = False,
                           tokens: int = 0, error: str = "",
                           duration_ms: float | None = None) -> None:
        """更新 per-model 统计（与 key.stats 聚合独立）

        若 model 不在 model_stats 字典中（理论上不应发生），自动创建条目。
        v13 新增 duration_ms：传入时同步记录到 latency_samples 用于 p50/p95/p99 计算。
        """
        if not model:
            return
        ms = self.model_stats.get(model)
        if ms is None:
            ms = KeyStats()
            self.model_stats[model] = ms
        ms.last_used = time.time()
        if success:
            ms.ok += 1
            ms.total_tokens += tokens
        elif rate_limited:
            ms.rate_limited += 1
        elif expired:
            ms.expired = True
            ms.last_error = "expired"
        else:
            ms.fail += 1
            if error:
                ms.last_error = error[:200]
        # v13: 延迟样本记录（成功/失败均记录，便于观察尾延迟）
        if duration_ms is not None:
            ms.record_latency(duration_ms)

    def get_display_name(self, model_name: str) -> str:
        """获取 model 的展示名（v11 新增）

        展示名用于 UI 显示；调用时仍用 model_name（连接名）。
        - 若 model_display_names 中有映射，返回映射值
        - 否则返回 model_name 本身（向后兼容）
        """
        if not model_name:
            return ""
        return self.model_display_names.get(model_name) or model_name


@dataclass
class ProviderPolicy:
    """provider 级别的调用与限流策略"""
    name: str
    retry_count: int = 4
    retry_base_delay: float = 2.0
    retry_max_delay: float = 30.0
    retry_jitter: float = 0.2
    rate_limit_cooldown_seconds: float = 5.0
    rate_limit_backoff_multiplier: float = 2.0
    rate_limit_max_cooldown: float = 300.0
    max_concurrency: int = 3
    key_files: list[str] = field(default_factory=list)


def _normalize_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []
