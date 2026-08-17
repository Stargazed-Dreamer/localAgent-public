"""Remote VL client - calls OpenAI-compatible vision-language APIs.

Replaces the old local Qwen2.5-VL subprocess bridge. Supports multiple
providers with automatic failover on rate-limit/error, mirroring the LLM
pool pattern but simplified (no per-project token stats).

v9 schema：key 来源统一走 key_store.resolve_keys(use_case)
  - 不再独立读取 keys.json（移除 _load_vl_keys_from_unified）
  - 不再从 config.toml [vision.vl_providers.*] 读 provider 配置
  - 每次 VL 调用：resolve_keys(use_case) → 候选 ResolvedKey 列表
  - 每个 ResolvedKey 含 base_url/api_key/model/vision_params（max_concurrency/timeout/rate_limit_cooldown）
  - 运行时状态（cooldown/active_count）按 key_id 索引，存在 _runtime_state

Config (config.toml, v9 后 VL provider 段已移除):

    [vision]
    vl_enabled = true
    vl_max_image_edge = 1280
    vl_jpeg_quality = 85
    vl_retry_backoffs = [2, 4, 8, 16, 32, 60]
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

logger = logging.getLogger("localagent.remote_vl")

# 默认值：可通过 config.toml [vision] 段覆盖
# 图片编码参数（全局，与 provider 无关）
_DEFAULT_MAX_IMAGE_EDGE = 1280
_DEFAULT_JPEG_QUALITY = 85
# 重试退避序列（全局，客户端策略）
_DEFAULT_RETRY_BACKOFFS = (2, 4, 8, 16, 32, 60)
# 限流冷却时间默认值（per-provider，实际从 ResolvedKey.vision_params 读取）
_DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
# 每次重试最长等待（冷却中的 provider 最多等这么久就尝试重新调用）。
_MAX_WAIT_PER_RETRY = 30.0


@dataclass
class ProviderRuntimeState:
    """按 key_id 索引的 VL provider 运行时状态（v9）

    v9 前 RemoteVLProvider 含 base_url/api_key/model 等静态字段 + 运行时状态。
    v9 后静态字段从 ResolvedKey 获取（每次 resolve_keys 调用），运行时状态独立存这里。
    """
    key_id: str
    cooldown_until: float = 0.0
    active_count: int = 0
    last_error: str = ""
    last_success: float = 0.0
    # 该 provider 上次调用结束时间（用于区分"并发 429" vs "额度耗尽 429"）
    # per-provider 追踪：fallback 到其他 provider 时不会互相干扰并发判定
    last_call_finished_ts: float = 0.0
    # 连续服务端错误次数（500/502/503/504）
    # 达阈值（_provider_exhausted_max_consecutive）时标记 provider exhausted，
    # 避免 ice 等不稳定 provider 503 持续失败时无限短冷却循环导致业务出错
    consecutive_server_errors: int = 0

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())


class RemoteVLClient:
    """Multi-provider remote VL client with automatic failover.

    v9: provider 列表由 key_store.resolve_keys(use_case) 动态返回，不再缓存。
    运行时状态（cooldown/active_count）按 key_id 索引，跨调用持久化。

    Exposes an interface compatible with the old QwenVLBridge so ocr.py /
    vision.py only need an import swap and minor field renames.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._lock = threading.Lock()
        # 运行时状态：key_id → ProviderRuntimeState（cooldown/active_count/last_error/last_success）
        self._runtime_state: dict[str, ProviderRuntimeState] = {}
        # per-key_id lock：序列化同 key_id 的并发调用（避免同 key 并发触发 429）
        self._per_key_lock: dict[str, threading.Lock] = {}
        # 429 日志目录：None=用项目根 data/activity/，测试可注入 tmp_path 避免污染生产文件
        self._log_dir: Path | None = None
        # VL 配额管理器覆盖（测试注入）：None=用全局 vl_quota 单例
        self._vl_quota_override: object | None = None
        # 全局编码参数（从 [vision] 段读取，默认值见模块顶部）
        self._max_image_edge: int = _DEFAULT_MAX_IMAGE_EDGE
        self._jpeg_quality: int = _DEFAULT_JPEG_QUALITY
        self._retry_backoffs: tuple = _DEFAULT_RETRY_BACKOFFS
        # 今日额度耗尽标志（聚合状态：所有 provider 都在冷却中时为 True）
        # 跨日时由 _check_daily_exhausted_reset 自动重置
        # 注意：单个 provider 耗尽不再标记全局，仅当所有候选 provider 都耗尽时才标记
        self._daily_exhausted: bool = False
        self._daily_exhausted_date: str = ""  # YYYYMMDD，用于跨日重置检测
        self._daily_exhausted_at: str = ""
        self._daily_exhausted_reason: str = ""
        # Provider 级额度耗尽追踪：key_id → {cooldown_until, reason, kind, source, consecutive_count, date}
        # 单个 provider 非并发 429 时标记，冷却该 provider（不阻止其他 provider）
        # consecutive_count 达 max_consecutive 时 cooldown_until 设为次日 00:00（当日不再试）
        self._provider_exhausted: dict[str, dict] = {}
        # Provider 级耗尽配置（从 [vision] 段读取）
        self._provider_exhausted_cooldown_seconds: int = 3600  # 单 provider 耗尽冷却时长（1 小时）
        self._provider_exhausted_max_consecutive: int = 3      # 连续耗尽上限，达此值当日不再试
        # 上次成功调用结束时间（用于区分"并发 429" vs "今日额度 429"）
        # 规则：非并发（距上次调用 >= 60s）导致的 429 = 今日额度到顶
        self._last_call_finished_ts: float = 0.0
        self._loaded: bool = False  # 首次 reload_config 标志

    # ---------- config loading ----------

    def reload_config(self) -> None:
        """(Re)load global VL config from config.toml. Safe to call repeatedly.

        v9: 不再加载 provider 列表（provider 由 resolve_keys 动态返回）。
        """
        from server.config import get_vision_config

        cfg = get_vision_config()
        self._enabled = bool(cfg.get("vl_enabled", False))
        # 全局编码参数
        self._max_image_edge = int(cfg.get("vl_max_image_edge", _DEFAULT_MAX_IMAGE_EDGE))
        self._jpeg_quality = int(cfg.get("vl_jpeg_quality", _DEFAULT_JPEG_QUALITY))
        self._retry_backoffs = tuple(cfg.get("vl_retry_backoffs", _DEFAULT_RETRY_BACKOFFS))
        # Provider 级耗尽配置（从 [vision] 段读取，可调）
        self._provider_exhausted_cooldown_seconds = int(cfg.get("vl_provider_exhausted_cooldown_seconds", 3600))
        self._provider_exhausted_max_consecutive = int(cfg.get("vl_provider_exhausted_max_consecutive", 3))
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.reload_config()
        self._check_daily_exhausted_reset()

    # ---------- daily exhausted management ----------

    def _check_daily_exhausted_reset(self) -> None:
        """跨日时自动重置 daily_exhausted 标志 + _provider_exhausted dict（00:00 后清空）"""
        today = time.strftime("%Y%m%d")
        # 检查全局 daily_exhausted 跨日
        if self._daily_exhausted and self._daily_exhausted_date and self._daily_exhausted_date != today:
            logger.info(
                "VL daily_exhausted 跨日重置: %s → %s (前日原因: %s)",
                self._daily_exhausted_date, today, self._daily_exhausted_reason,
            )
            self._daily_exhausted = False
            self._daily_exhausted_date = ""
            self._daily_exhausted_at = ""
            self._daily_exhausted_reason = ""
        # 检查 _provider_exhausted 跨日：清空所有 date != today 的条目
        if self._provider_exhausted:
            stale_keys = [k for k, v in self._provider_exhausted.items() if v.get("date") != today]
            if stale_keys:
                logger.info(
                    "VL _provider_exhausted 跨日重置：清空 %d 个 provider 的耗尽状态: %s",
                    len(stale_keys), stale_keys,
                )
                for k in stale_keys:
                    del self._provider_exhausted[k]

    def is_daily_exhausted(self) -> bool:
        """今日是否已耗尽（ModelScope 非并发 429 触发）"""
        self._ensure_loaded()
        return self._daily_exhausted

    def _mark_daily_exhausted(self, reason: str, kind: str = "upstream_overload",
                              source: str | None = None) -> None:
        """标记今日 VL 已耗尽（当日不再调用 provider）

        仅在所有候选 provider 都耗尽时调用（聚合状态），单个 provider 耗尽用 _mark_provider_exhausted。

        Args:
            reason: 人类可读原因（含 HTTP body 摘要）
            kind: 耗尽类型，见 vl_quota.mark_daily_exhausted 文档：
                - "quota_exhausted"：配额真用完（body 明示 quota exceeded / daily limit）
                - "upstream_overload"：上游临时过载（body 提示 rate_limited / concurrency）
                默认 "upstream_overload"，因为 ModelScope 多数 429 都是临时过载而非真耗尽
            source: 429 来源 provider（如 "ModelScope"），用于 vl_quota 来源细分
        """
        self._daily_exhausted = True
        self._daily_exhausted_date = time.strftime("%Y%m%d")
        self._daily_exhausted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._daily_exhausted_reason = reason[:300]
        logger.warning("VL 今日额度已耗尽 (kind=%s, source=%s): %s", kind, source, reason)
        # 同步给 VL 配额管理器（让 /health 和监控面板可见）
        try:
            if self._vl_quota_override is not None:
                self._vl_quota_override.mark_daily_exhausted(reason, kind=kind, source=source)
            else:
                from server.activity_tracker.vl_quota import vl_quota
                vl_quota.mark_daily_exhausted(reason, kind=kind, source=source)
        except Exception:
            pass  # 配额管理器不可用时不影响主流程

    def _mark_provider_exhausted(self, key_id: str, reason: str,
                                 kind: str = "upstream_overload",
                                 source: str | None = None) -> None:
        """标记单个 provider 额度耗尽（不阻止其他 provider）

        provider 级耗尽追踪：累加 consecutive_count，达 max_consecutive 时 cooldown_until
        设为次日 00:00（当日不再试该 provider），否则 cooldown = now + cooldown_seconds。

        Args:
            key_id: provider 的 key_id
            reason: 429 错误消息（截断 300 字符）
            kind: "quota_exhausted" | "upstream_overload"
            source: provider 主域名（如 "modelscope"）
        """
        today = time.strftime("%Y%m%d")
        existing = self._provider_exhausted.get(key_id, {})
        # 跨日时 consecutive_count 重置（date 不一致）
        consecutive = 0 if existing.get("date") != today else existing.get("consecutive_count", 0)
        consecutive += 1
        now = time.time()
        if consecutive >= self._provider_exhausted_max_consecutive:
            # 达止损上限：cooldown 到次日 00:00（当日不再试）
            import datetime as _dt
            tomorrow_midnight = (_dt.datetime.now() + _dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cooldown_until = tomorrow_midnight.timestamp()
            logger.warning(
                "VL provider '%s' 连续 %d 次额度耗尽，当日不再试（cooldown 到次日 00:00）: %s",
                key_id, consecutive, reason[:200],
            )
        else:
            cooldown_until = now + self._provider_exhausted_cooldown_seconds
            logger.warning(
                "VL provider '%s' 额度耗尽 (%d/%d)，冷却 %ds: %s",
                key_id, consecutive, self._provider_exhausted_max_consecutive,
                self._provider_exhausted_cooldown_seconds, reason[:200],
            )
        self._provider_exhausted[key_id] = {
            "cooldown_until": cooldown_until,
            "reason": reason[:300],
            "kind": kind,
            "source": source or "unknown",
            "consecutive_count": consecutive,
            "date": today,
        }

    def _is_provider_exhausted(self, key_id: str) -> bool:
        """provider 是否在额度耗尽冷却中（cooldown_until > now）"""
        pex = self._provider_exhausted.get(key_id)
        if not pex:
            return False
        return time.time() < pex["cooldown_until"]

    def _log_429(self, key_id: str, base_url: str, model: str, error_msg: str, is_concurrent: bool) -> None:
        """持久化 429 日志到 vl_429_log.jsonl（供 smart_limit 分析）

        路径解析优先级：
            1. self._log_dir（测试注入 tmp_path 时用此目录）
            2. 项目根 data/activity/vl_429_log.jsonl
        """
        try:
            if self._log_dir is not None:
                log_path = self._log_dir / "vl_429_log.jsonl"
            else:
                log_path = Path("data/activity/vl_429_log.jsonl")
                if not log_path.is_absolute():
                    log_path = Path(__file__).resolve().parent.parent.parent / log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "key_id": key_id,
                "base_url": base_url,
                "model": model,
                "is_concurrent": is_concurrent,
                "error": error_msg[:300],
                "daily_exhausted": not is_concurrent,  # 非并发 429 = 今日额度耗尽
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("写 vl_429_log.jsonl 失败: %s", e)

    # ---------- compat properties (mirror old QwenVLBridge surface) ----------

    @property
    def enabled(self) -> bool:
        self._ensure_loaded()
        return self._enabled

    @property
    def running(self) -> bool:
        """Compat alias: a remote API is 'running' as long as it's configured."""
        return self.enabled

    @property
    def model_loaded(self) -> bool:
        """Compat alias: treat 'available' (any provider not in cooldown) as loaded."""
        return self.available

    @property
    def available(self) -> bool:
        self._ensure_loaded()
        if not self._enabled:
            return False
        # 检查是否有任何 VL key 可用（不在冷却中 + 并发未满）
        return self._pick_provider(use_case="vl_ocr") is not None

    # ---------- runtime state management ----------

    def _get_runtime_state(self, key_id: str) -> ProviderRuntimeState:
        """获取或创建 key_id 的运行时状态（线程安全）"""
        with self._lock:
            if key_id not in self._runtime_state:
                self._runtime_state[key_id] = ProviderRuntimeState(key_id=key_id)
            return self._runtime_state[key_id]

    def _get_per_key_lock(self, key_id: str) -> threading.Lock:
        """获取或创建 key_id 的 per-key lock"""
        with self._lock:
            if key_id not in self._per_key_lock:
                self._per_key_lock[key_id] = threading.Lock()
            return self._per_key_lock[key_id]

    def _mark_cooldown(self, state: ProviderRuntimeState, seconds: float, error: str) -> None:
        state.cooldown_until = max(state.cooldown_until, time.time() + seconds)
        state.last_error = error
        logger.warning("VL key '%s' cooled down %.0fs: %s", state.key_id, seconds, error)

    def _mark_success(self, state: ProviderRuntimeState) -> None:
        state.last_success = time.time()
        state.last_error = ""
        state.consecutive_server_errors = 0  # 成功时清零连续服务端错误计数

    # ---------- provider selection ----------

    def _pick_provider(self, use_case: str | None = None) -> tuple | None:
        """Pick the next available (ResolvedKey, ProviderRuntimeState) pair.

        v9: 通过 key_store.resolve_keys(use_case) 获取候选 key 列表，
        按 tier 升序（resolve_keys 已排序）遍历，返回第一个不在冷却中且并发未满的。

        Args:
            use_case: VL use_case（vl_ocr/vl_vision/vl_activity_tracker）
                      resolve_keys 会按 use_case.sensitive 过滤 privacy_warning，
                      按 use_case.default_tier 范围过滤 tier，按 allowed_uses 过滤白名单

        Returns:
            (ResolvedKey, ProviderRuntimeState) 或 None（无可用 key）
        """
        from server.llm_pool.key_store import resolve_keys

        self._ensure_loaded()
        if not self._enabled:
            return None
        if not use_case:
            use_case = "vl_ocr"  # 默认 use_case

        candidates = resolve_keys(use_case)
        if not candidates:
            return None

        for rk in candidates:
            state = self._get_runtime_state(rk.key_id)
            # 检查限流 cooldown
            if state.in_cooldown:
                continue
            # 检查 provider 级额度耗尽冷却（provider 级 fallback 改造）
            if self._is_provider_exhausted(rk.key_id):
                continue
            # 检查 active_count < max_concurrency
            max_conc = rk.vision_params.get("max_concurrency", 1)
            if state.active_count >= max_conc:
                continue
            return (rk, state)
        return None

    def _all_vl_candidates(self) -> list:
        """获取所有 VL 候选 key（用于 status / cooldown 聚合，不过滤 cooldown/active_count）

        使用 vl_ocr use_case（resolve_keys 会过滤 privacy_warning，但 status 想显示全部）。
        为显示全部，直接从 key_store.load_keys() 过滤 vl scope。
        """
        from server.llm_pool.key_store import load_keys
        records = load_keys()
        candidates = []
        for rec in records:
            if not rec.enabled:
                continue
            if rec.status.get("works") is False:
                continue
            # 检查是否含 vl scope model
            vl_models = [m for m in rec.models
                         if m.get("enabled", True) and "vl" in m.get("scope", [])]
            if not vl_models:
                continue
            # 构造类似 ResolvedKey 的简化视图（用于 status 显示）
            from server.llm_pool.key_store import _to_resolved_key
            # 选第一个 vl model（按 tier 升序）
            vl_models.sort(key=lambda m: int(m.get("tier", 3)))
            chosen_model = vl_models[0]["name"]
            rk = _to_resolved_key(rec, chosen_model)
            candidates.append(rk)
        return candidates

    # ---------- image encoding ----------

    def _encode_image(self, image: Image.Image) -> str:
        """PIL Image -> JPEG base64 data URL, resized if too large."""
        img = image.convert("RGB")
        w, h = img.size
        longest = max(w, h)
        if longest > self._max_image_edge:
            scale = self._max_image_edge / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._jpeg_quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    # ---------- core API call ----------

    def _call_chat(self, rk, messages: list[dict], timeout: int | None = None) -> dict:
        """Call the provider's chat completions endpoint. Returns parsed JSON or raises.

        Args:
            rk: ResolvedKey（含 base_url/api_key/model/vision_params）
            messages: chat messages
            timeout: 覆盖 vision_params.timeout 的可选超时
        """
        import json as _json
        import urllib.request

        url = f"{rk.base_url}/chat/completions"
        body = _json.dumps({
            "model": rk.model,
            "messages": messages,
            "stream": False,
            "max_tokens": 16384,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {rk.api_key}",
            },
            method="POST",
        )
        t_out = timeout or rk.vision_params.get("timeout", 60)
        with urllib.request.urlopen(req, timeout=t_out) as resp:
            payload = resp.read().decode("utf-8")
        return _json.loads(payload)

    def _call_with_failover(self, image: Image.Image, text_prompt: str,
                            timeout: int | None = None,
                            use_case: str | None = None) -> dict:
        """Send image+text to a VL provider, failover across providers + retries.

        Returns {"status":"ok","answer":...} on success or {"status":"error","detail":...}.
        """
        self._ensure_loaded()
        if not self._enabled:
            return {"status": "error", "detail": "远程 VL 未启用 (vl_enabled=false)"}
        # 注意：不再在开头检查 _daily_exhausted（provider 级 fallback 改造）
        # 单个 provider 耗尽只冷却该 provider，_pick_provider 会跳过 exhausted 的 provider
        # 仅当所有候选 provider 都耗尽时才标记全局 _daily_exhausted（见函数末尾）

        data_url = self._encode_image(image)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]

        attempted: set[str] = set()
        last_detail = "无可用 provider"
        backoffs = self._retry_backoffs
        max_attempts = len(backoffs)
        for attempt_idx, backoff in enumerate(backoffs):
            picked = self._pick_provider(use_case=use_case)
            if picked is None:
                # 所有 provider 不可用（冷却中或并发已满）：等待后重试，不直接返回错误。
                # 用户要求：失败后不返回错误，而是指数退避重试指定次数再报错或成功。
                next_available = self._next_available_time()
                wait = next_available - time.time()
                if wait > 0:
                    # 有 provider 在冷却：等到它解冻（单次最多等 _MAX_WAIT_PER_RETRY）
                    actual_wait = min(wait, _MAX_WAIT_PER_RETRY)
                    logger.info(
                        "所有 VL provider 冷却中，等待 %.1fs 后重试 (%d/%d)",
                        actual_wait, attempt_idx + 1, max_attempts,
                    )
                    time.sleep(actual_wait)
                else:
                    # 没冷却但都不可用（并发已满）：用 backoff 等待
                    actual_wait = min(backoff, _MAX_WAIT_PER_RETRY)
                    logger.info(
                        "VL provider 并发已满，等待 %.1fs 后重试 (%d/%d)",
                        actual_wait, attempt_idx + 1, max_attempts,
                    )
                    time.sleep(actual_wait)
                continue  # 总是重试，不返回错误
            rk, state = picked
            attempted.add(rk.key_id)

            plock = self._get_per_key_lock(rk.key_id)
            timeout_cfg = rk.vision_params.get("timeout", 60)
            acquired = plock.acquire(timeout=timeout_cfg + 5)
            if not acquired:
                last_detail = f"key {rk.key_id} 并发等待超时"
                continue
            state.active_count += 1
            try:
                try:
                    result = self._call_chat(rk, messages, timeout=timeout)
                except Exception as e:
                    msg = str(e)[:300]
                    # Distinguish rate-limit / server errors from network errors.
                    is_rate_limit = "429" in msg or "Too Many Requests" in msg.lower() or "rate" in msg.lower()
                    is_server_error = any(code in msg for code in ("500", "502", "503", "504"))
                    if is_rate_limit:
                        # 区分"并发 429" vs "今日额度耗尽 429"（per-provider 追踪）
                        # 规则：该 provider 距上次调用结束 < 60s 仍触发 429 = 并发限流
                        #       该 provider 距上次调用结束 >= 60s 触发 429 = 额度到顶（非并发）
                        # 例外：last_call_finished_ts==0（该 provider 从未完成过调用）时默认按并发处理，
                        #       因为从未成功调用过就不可能已耗尽今日额度
                        # per-provider 追踪：fallback 到其他 provider 时不会互相干扰并发判定
                        now_ts = time.time()
                        if state.last_call_finished_ts == 0.0:
                            is_concurrent = True
                        else:
                            is_concurrent = (now_ts - state.last_call_finished_ts) < 60.0
                        cooldown_seconds = rk.vision_params.get("rate_limit_cooldown", _DEFAULT_RATE_LIMIT_COOLDOWN)
                        if is_concurrent:
                            # 并发 429：常规冷却，后续仍可重试
                            self._mark_cooldown(state, cooldown_seconds, msg)
                            self._log_429(rk.key_id, rk.base_url, rk.model, msg, is_concurrent=True)
                            last_detail = f"[concurrent 429] {msg}"
                            logger.warning(
                                "VL key '%s' 并发 429 (%d/%d): %s",
                                rk.key_id, attempt_idx + 1, max_attempts, msg,
                            )
                            if backoff < backoffs[-1]:
                                time.sleep(min(backoff, _MAX_WAIT_PER_RETRY))
                            continue
                        # 非并发 429：该 provider 额度耗尽，标记 provider 级冷却
                        # 不再标记全局 _daily_exhausted，继续尝试其他 provider（fallback）
                        # 推断 kind：body 含 "quota" / "exceeded" / "daily" 视为真耗尽，否则视为上游过载
                        msg_lower = msg.lower()
                        if any(kw in msg_lower for kw in ("quota", "exceeded", "daily", "limit reached")):
                            kind = "quota_exhausted"
                        else:
                            kind = "upstream_overload"
                        # 推断 source：从 base_url 提取主域名（如 api.modelscope.cn → modelscope）
                        source = rk.base_url.split("//")[-1].split("/")[0].split(":")[0] if rk.base_url else "unknown"
                        self._mark_provider_exhausted(rk.key_id, msg, kind=kind, source=source)
                        self._log_429(rk.key_id, rk.base_url, rk.model, msg, is_concurrent=False)
                        last_detail = f"[provider exhausted] {rk.key_id}: {msg}"
                        logger.warning(
                            "VL key '%s' 非并发 429（额度耗尽），fallback 到其他 provider (%d/%d): %s",
                            rk.key_id, attempt_idx + 1, max_attempts, msg,
                        )
                        # 不 return，continue 尝试下一个 provider（_pick_provider 会跳过已耗尽的）
                        continue
                    if is_server_error:
                        # 服务端错误（500/502/503/504）：累加连续错误计数
                        # 达阈值时标记 provider exhausted，触发聚合 daily_exhausted
                        # 避免 ice 等不稳定 provider（~80% 可用率）503 持续失败时
                        # 无限短冷却循环导致业务出错
                        state.consecutive_server_errors += 1
                        # 连续服务端错误达阈值 → 标记 provider exhausted（kind="upstream_overload"）
                        # 让聚合检查 all_exhausted 能通过，触发 daily_exhausted → loop_actions 走 OCR 兜底
                        if state.consecutive_server_errors >= self._provider_exhausted_max_consecutive:
                            source = rk.base_url.split("//")[-1].split("/")[0].split(":")[0] if rk.base_url else "unknown"
                            self._mark_provider_exhausted(
                                rk.key_id, msg,
                                kind="upstream_overload",
                                source=source,
                            )
                            last_detail = f"[provider exhausted after {state.consecutive_server_errors} server errors] {rk.key_id}: {msg}"
                            logger.warning(
                                "VL key '%s' 连续 %d 次服务端错误，标记 provider exhausted (%d/%d): %s",
                                rk.key_id, state.consecutive_server_errors,
                                attempt_idx + 1, max_attempts, msg,
                            )
                            continue  # 尝试下一个 provider（_pick_provider 会跳过已耗尽的）
                        # 未达阈值：不 mark_cooldown，让 _pick_provider 仍可选到该 provider，
                        # 由 backoff 控制重试节奏，使 consecutive_server_errors 能在同一轮快速累加
                        state.last_error = msg
                        last_detail = msg
                        logger.warning(
                            "VL key '%s' 调用失败 (连续服务端错误 %d/%d) (%d/%d): %s",
                            rk.key_id, state.consecutive_server_errors,
                            self._provider_exhausted_max_consecutive,
                            attempt_idx + 1, max_attempts, msg,
                        )
                        if backoff < backoffs[-1]:
                            time.sleep(min(backoff, _MAX_WAIT_PER_RETRY))
                        continue
                    # 其他网络/解析错误
                    state.last_error = msg
                    last_detail = msg
                    logger.warning(
                        "VL key '%s' 调用失败 (%d/%d): %s",
                        rk.key_id, attempt_idx + 1, max_attempts, msg,
                    )
                    if backoff < backoffs[-1]:
                        time.sleep(min(backoff, _MAX_WAIT_PER_RETRY))
                    continue
                # Parse answer.
                choices = result.get("choices") or []
                if not choices:
                    # provider 返回空 choices：可能是模型格式问题、prompt 触发安全过滤或 provider bug，
                    # 不一定限流。仅记 last_error，立即尝试下一 provider，不冷却。
                    last_detail = f"key {rk.key_id} 返回无 choices: {str(result)[:200]}"
                    state.last_error = last_detail
                    self._mark_success(state)  # 调用本身成功，避免冷却下一轮
                    continue
                content = choices[0].get("message", {}).get("content", "")
                if isinstance(content, list):
                    # Some VL APIs return content as list of parts.
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                self._mark_success(state)
                return {"status": "ok", "answer": content or ""}
            finally:
                state.active_count -= 1
                plock.release()
                # 记录该 provider 本次调用结束时间（per-provider，用于下次 429 时区分"并发 vs 额度耗尽"）
                # 不论成功/失败/异常，都更新（已发出的 HTTP 请求即视为"已调用"）
                state.last_call_finished_ts = time.time()
                # 保留全局 _last_call_finished_ts 向后兼容（status 等可能引用）
                self._last_call_finished_ts = time.time()

        # 所有重试用完仍未成功：检查是否所有候选 provider 都在额度耗尽冷却中
        # 若是 → 标记全局 _daily_exhausted（聚合状态）+ 同步 vl_quota + 返回 daily_exhausted error
        # 若否（如并发限流/网络错误）→ 返回普通错误
        all_candidates = self._all_vl_candidates()
        if all_candidates:
            all_exhausted = all(
                self._is_provider_exhausted(rk.key_id) for rk in all_candidates
            )
            if all_exhausted and not self._daily_exhausted:
                exhausted_reasons = "; ".join(
                    f"{rk.key_id}: {self._provider_exhausted[rk.key_id]['reason'][:100]}"
                    for rk in all_candidates
                    if rk.key_id in self._provider_exhausted
                )
                self._mark_daily_exhausted(
                    f"所有 VL provider 额度耗尽: {exhausted_reasons}",
                    kind="quota_exhausted",
                )
                return {
                    "status": "error",
                    "detail": f"所有 VL provider 额度已耗尽: {exhausted_reasons}",
                    "daily_exhausted": True,
                }
        return {
            "status": "error",
            "detail": f"所有重试失败（{max_attempts} 次指数退避）: {last_detail}",
        }

    def _next_available_time(self) -> float:
        """所有 VL 候选 key 的最早解冻时间（用于 _call_with_failover 等待决策）

        综合考虑两种冷却：ProviderRuntimeState.cooldown_until（限流冷却）
        和 _provider_exhausted[key_id].cooldown_until（额度耗尽冷却）
        """
        candidates = self._all_vl_candidates()
        if not candidates:
            return 0.0
        times = []
        for rk in candidates:
            # 限流冷却
            times.append(self._get_runtime_state(rk.key_id).cooldown_until)
            # 额度耗尽冷却（若存在）
            pex = self._provider_exhausted.get(rk.key_id)
            if pex:
                times.append(pex["cooldown_until"])
        return min(times) if times else 0.0

    # ---------- public API (compatible with old QwenVLBridge) ----------

    def ocr(self, image: Image.Image, prompt: str | None = None,
            timeout: int = 300, use_case: str = "vl_ocr") -> dict:
        """Document OCR via VL. Returns {'status':'ok','markdown':...,'text':...}."""
        text_prompt = prompt or "请完整、准确地识别图中的文字，尽量保留原有顺序和版面结构。"
        result = self._call_with_failover(image, text_prompt, timeout=timeout, use_case=use_case)
        if result.get("status") != "ok":
            return result
        answer = result.get("answer", "")
        return {"status": "ok", "markdown": answer, "text": answer}

    def understand(
        self,
        image: Image.Image,
        question: str,
        bbox: list[float] | None = None,
        timeout: int = 300,
        use_case: str = "vl_vision",
    ) -> dict:
        """Image understanding via VL. Returns {'status':'ok','answer':...}.

        Note: Qwen3-VL 内部使用 0-1000 归一化坐标。如果问题涉及坐标,
        返回的坐标是归一化的(0-1000),调用者需自行反归一化:
        pixel_x = norm_x * image.width / 1000
        pixel_y = norm_y * image.height / 1000
        如需直接获取像素坐标,请使用 locate() 方法。
        """
        if bbox and len(bbox) == 4:
            bbox_text = f"关注区域坐标为[{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}]（归一化或像素坐标）。"
        else:
            bbox_text = ""
        text_prompt = f"请直接回答用户问题，简洁准确。{bbox_text} 用户问题：{question}"
        return self._call_with_failover(image, text_prompt, timeout=timeout, use_case=use_case)

    def locate(
        self,
        image: Image.Image,
        target: str,
        timeout: int = 300,
        use_case: str = "vl_vision",
    ) -> dict:
        """定位图片中目标元素的位置,返回像素坐标(已反归一化)。

        Qwen3-VL 内部使用 0-1000 归一化坐标,本方法自动反归一化为像素坐标。

        参数:
            image: PIL Image
            target: 目标描述(如"下载标签"、"为不完整的文件添加扩展名.!qB 复选框")

        返回:
            成功: {
                "status": "ok",
                "found": True,
                "x": int, "y": int,  # 像素坐标(基于原图尺寸)
                "nx": float, "ny": float,  # 归一化坐标(0-1000)
                "description": str,  # VL 的简短描述
            }
            未找到: {"status": "ok", "found": False, "description": str}
            错误: {"status": "error", "detail": str}
        """
        import re as _re

        w, h = image.size
        prompt = (
            f"请在图片中查找「{target}」。"
            f"如果找到,返回其中心的归一化坐标(0-1000,0=左/上,1000=右/下)。"
            f"如果找不到,found 设为 false。"
            f"严格按以下JSON格式返回,不要其他内容:\n"
            f'{{"found": true, "x": 0-1000, "y": 0-1000, "desc": "简短描述"}}'
        )
        result = self._call_with_failover(image, prompt, timeout=timeout, use_case=use_case)
        if result.get("status") != "ok":
            return result
        answer = result.get("answer", "").strip()

        # 用正则提取字段——VL 是语言模型，返回的 JSON 可能格式不严格
        # （如 "x": 928, 950 缺少 "y":，或用单引号、多余文本等）
        found_match = _re.search(r'"found"\s*:\s*(true|false)', answer, _re.IGNORECASE)
        found = bool(found_match and found_match.group(1).lower() == 'true')

        if not found:
            desc_match = _re.search(r'"desc"\s*:\s*"([^"]*)"', answer)
            return {
                "status": "ok",
                "found": False,
                "description": desc_match.group(1) if desc_match else f"未找到（VL回答: {answer[:200]}）",
            }

        # 提取 x
        x_match = _re.search(r'"x"\s*:\s*([\d.]+)', answer)
        if not x_match:
            return {
                "status": "ok",
                "found": False,
                "description": f"无法提取x坐标: {answer[:200]}",
            }
        nx = float(x_match.group(1))

        # 提取 y——优先 "y": 数字；否则尝试 "x": 数字, 数字 的第二个数
        y_match = _re.search(r'"y"\s*:\s*([\d.]+)', answer)
        if y_match:
            ny = float(y_match.group(1))
        else:
            xy_match = _re.search(r'"x"\s*:\s*([\d.]+)\s*,\s*([\d.]+)', answer)
            if xy_match:
                ny = float(xy_match.group(2))
            else:
                return {
                    "status": "ok",
                    "found": False,
                    "description": f"无法提取y坐标: {answer[:200]}",
                }

        # 反归一化: 0-1000 → 像素坐标
        px = int(round(nx * w / 1000))
        py = int(round(ny * h / 1000))
        # 钳制到图片范围内
        px = max(0, min(w - 1, px))
        py = max(0, min(h - 1, py))

        # 提取 desc（与 not found 分支对称，从 answer 中正则提取）
        desc_match = _re.search(r'"desc"\s*:\s*"([^"]*)"', answer)
        description = desc_match.group(1) if desc_match else ""

        return {
            "status": "ok",
            "found": True,
            "x": px,
            "y": py,
            "nx": nx,
            "ny": ny,
            "image_size": [w, h],
            "description": description,
        }

    def status(self) -> dict:
        """Return a status dict for /health and /ocr/status / /vision/status.

        v9: provider 信息从 key_store.load_keys() 过滤 vl scope + _runtime_state 组合。
        不再有 default_provider 概念（use_case 驱动选 key）。
        """
        self._ensure_loaded()
        # 获取所有 VL 候选 key（不过滤 cooldown/active_count，用于展示）
        candidates = self._all_vl_candidates()
        providers_info = []
        for rk in candidates:
            state = self._get_runtime_state(rk.key_id)
            max_conc = rk.vision_params.get("max_concurrency", 1)
            available = (not state.in_cooldown) and state.active_count < max_conc
            providers_info.append({
                "key_id": rk.key_id,
                "label": rk.label,
                "model": rk.model,
                "base_url": rk.base_url,
                "available": available,
                "cooldown_remaining": round(state.cooldown_remaining, 1),
                "active_count": state.active_count,
                "max_concurrency": max_conc,
                "last_error": state.last_error,
                "privacy_warning": rk.privacy_warning,
            })

        # 计算 sensitive use_case 的真实可用性（考虑 privacy_warning 过滤）
        # vl_available 只反映"有 provider 不在冷却中"，但 sensitive use_case 还会被
        # privacy_warning 过滤；vl_usable_for_sensitive 反映过滤后的真实可用性。
        allow_privacy_warning = False
        try:
            from server.config import get_privacy_config
            allow_privacy_warning = get_privacy_config().get(
                "allow_privacy_warning_for_sensitive", False
            )
        except Exception:
            pass
        sensitive_usable = False
        if self._enabled and candidates:
            # 用 _pick_provider 检查（它会走 resolve_keys 的 sensitive 过滤）
            sensitive_usable = self._pick_provider(use_case="vl_ocr") is not None
        # 构建 provider_exhausted 状态 dict（每个耗尽 provider 的详细信息）
        now = time.time()
        provider_exhausted = {}
        for key_id, pex in self._provider_exhausted.items():
            provider_exhausted[key_id] = {
                "cooldown_until": pex["cooldown_until"],
                "cooldown_remaining": round(max(0.0, pex["cooldown_until"] - now), 1),
                "reason": pex["reason"],
                "kind": pex["kind"],
                "source": pex["source"],
                "consecutive_count": pex["consecutive_count"],
            }
        return {
            "enabled": self._enabled,
            "available": self.available,
            "providers": providers_info,
            "vl_usable_for_sensitive": sensitive_usable,
            "allow_privacy_warning_for_sensitive": allow_privacy_warning,
            "daily_exhausted": self._daily_exhausted,
            "daily_exhausted_at": self._daily_exhausted_at,
            "daily_exhausted_reason": self._daily_exhausted_reason,
            "provider_exhausted": provider_exhausted,
        }


# Global singleton (mirrors old qwen_vl_bridge singleton pattern).
remote_vl = RemoteVLClient()


def vl_status_fields() -> dict:
    """返回 VL 状态的公共字段（供 ocr_status / vision_status 共用，避免重复 extract 逻辑）。

    v9: 不再有 default_provider 概念。vl_provider 改为展示第一个可用 provider 的 label，
    vl_model 改为对应 model。若无可用 provider，vl_provider/vl_model 为空字符串。

    返回 dict 含: vl_provider, vl_model, vl_available,
                  vl_usable_for_sensitive, allow_privacy_warning_for_sensitive,
                  providers (完整 provider 列表含 cooldown/active_count),
                  provider_exhausted (provider 级额度耗尽状态),
                  daily_exhausted / daily_exhausted_at / daily_exhausted_reason (聚合状态)
    """
    remote_vl.reload_config()
    vstatus = remote_vl.status()
    # 选第一个可用 provider 展示（v9 后无 default_provider，按 providers 顺序取第一个 available）
    vl_provider = ""
    vl_model = ""
    for p in vstatus.get("providers", []):
        if p.get("available"):
            vl_provider = p.get("label") or p.get("key_id", "")
            vl_model = p.get("model", "")
            break
    return {
        "vl_provider": vl_provider,
        "vl_model": vl_model,
        "vl_available": vstatus.get("available", False),
        "vl_usable_for_sensitive": vstatus.get("vl_usable_for_sensitive", False),
        "allow_privacy_warning_for_sensitive": vstatus.get("allow_privacy_warning_for_sensitive", False),
        # Provider 级状态（provider 级 fallback 改进）：透传给 /health vision 字段
        "providers": vstatus.get("providers", []),
        "provider_exhausted": vstatus.get("provider_exhausted", {}),
        "daily_exhausted": vstatus.get("daily_exhausted", False),
        "daily_exhausted_at": vstatus.get("daily_exhausted_at"),
        "daily_exhausted_reason": vstatus.get("daily_exhausted_reason"),
    }
