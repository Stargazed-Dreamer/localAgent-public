"""配置管理 - 加载/保存 config.toml，支持多家大模型提供商

ADR-0027: load_config/save_config/CONFIG_PATH/缓存状态已迁移到 lib/config_reader.py。
此模块保留 server 专属逻辑（get_*_config() 配置读取器、schema 验证、update_config、
脱敏工具），通过 `from lib.config_reader import load_config` 复用底层读取机制。
client 不再依赖 server.config，直接从 lib.config_reader 读取配置。
"""

from pathlib import Path
from typing import Any

from pydantic import Field

# ADR-0027: load_config/save_config/CONFIG_PATH 已迁移到 lib/config_reader.py
# CONFIG_PATH 保留重导出用于向后兼容（# noqa: F401 抑制未使用告警）
from lib.config_reader import (
    CONFIG_PATH,  # noqa: F401
    load_config,
    save_config,
)
from lib.schema import BaseSchema

# 敏感字段列表（读取时脱敏）
SENSITIVE_KEYS = {"api_key", "secret", "password", "token"}


def _mask_value(value: str) -> str:
    """脱敏：只显示前4位和后4位，中间用****代替"""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _mask_config(config: dict) -> dict:
    """递归脱敏配置中的敏感字段"""
    masked = {}
    for key, value in config.items():
        if isinstance(value, dict):
            masked[key] = _mask_config(value)
        elif isinstance(value, str) and key.lower() in SENSITIVE_KEYS:
            masked[key] = _mask_value(value)
        else:
            masked[key] = value
    return masked


def get_config_masked() -> dict:
    """获取脱敏后的完整配置（用于API返回）"""
    return _mask_config(load_config())


def update_config(path: str, value: Any) -> dict:
    """更新配置中的指定字段

    path: 点分隔的路径，如 "llm.providers.mimo.api_key"
    value: 新值
    """
    config = load_config()
    keys = path.split(".")
    obj = config
    for key in keys[:-1]:
        if key not in obj:
            obj[key] = {}
        obj = obj[key]
    obj[keys[-1]] = value
    save_config(config)
    return _mask_config(config)


def get_llm_config(model_tier: str = "default") -> dict:
    """获取 LLM 配置（v14 精简版）

    v14: [llm.providers.*] 段已删除，base_url/api_key/model 统一由 keys.json 管理。
    此函数仅保留向后兼容：读取 config.toml 中可能残留的 [llm] base_url/api_key
    和 [llm.models.*] model 名。实际运行时 key 配置以 keys.json 为准。

    唯一调用方 health.py 用此函数检查 config.toml 是否有 api_key，
    若无则 fallback 到 keys.json 的 resolve_key() 检查。

    model_tier: "default" / "cheap" / "powerful"（向后兼容，实际不再使用 provider 引用）
    """
    config = load_config()
    llm = config.get("llm", {})

    base_url = llm.get("base_url", "")
    api_key = llm.get("api_key", "")

    models = llm.get("models", {})
    if model_tier in models and isinstance(models[model_tier], dict):
        tier_cfg = models[model_tier]
        model = tier_cfg.get("model", llm.get("model", ""))
        # tier 内直接覆盖（向后兼容方式3）
        base_url = tier_cfg.get("base_url", base_url)
        api_key = tier_cfg.get("api_key", api_key)
    else:
        model = llm.get("model", "")

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


def get_pool_defaults() -> dict:
    """获取全局默认 pool 策略（[llm.pool] 段）

    v14 新增：作为所有 key 的 fallback 策略。key 在 keys.json 中有 pool 段时用 key 级策略，
    否则用此全局默认。

    配置格式（config.toml）:
        [llm.pool]
        retry_count = 3
        retry_base_delay = 2.0
        retry_max_delay = 30.0
        retry_jitter = 0.2
        rate_limit_cooldown_seconds = 60.0
        rate_limit_backoff_multiplier = 2.0
        rate_limit_max_cooldown = 300.0
    """
    config = load_config()
    pool = config.get("llm", {}).get("pool", {})
    if not isinstance(pool, dict):
        pool = {}
    return {
        "retry_count": int(pool.get("retry_count", 4)),
        "retry_base_delay": float(pool.get("retry_base_delay", 2.0)),
        "retry_max_delay": float(pool.get("retry_max_delay", 30.0)),
        "retry_jitter": float(pool.get("retry_jitter", 0.2)),
        "rate_limit_cooldown_seconds": float(pool.get("rate_limit_cooldown_seconds", 5.0)),
        "rate_limit_backoff_multiplier": float(pool.get("rate_limit_backoff_multiplier", 2.0)),
        "rate_limit_max_cooldown": float(pool.get("rate_limit_max_cooldown", 300.0)),
    }


def get_llm_storage_config() -> dict:
    """获取 LLM 存储配置（keys_dir / stats_dir / unified_keys_file）

    配置格式（config.toml）:
        [llm.storage]
        keys_dir = "data/llm/keys"
        stats_dir = "data/llm/stats"
        unified_keys_file = "data/llm/keys.json"
    """
    config = load_config()
    storage = config.get("llm", {}).get("storage", {})
    if not isinstance(storage, dict):
        storage = {}
    return {
        "keys_dir": storage.get("keys_dir", "data/llm/keys"),
        "stats_dir": storage.get("stats_dir", "data/llm/stats"),
        "unified_keys_file": storage.get("unified_keys_file", "data/llm/keys.json"),
    }


def get_llm_quota_tracking_config() -> dict:
    """获取 LLM 多维配额跟踪配置（v13，OmniRoute 多维配额借鉴）

    配置格式（config.toml）:
        [llm.quota_tracking]
        enabled = true
        dimensions = ["requests/hour", "tokens/hour", "tokens/day"]

    dimensions 字符串格式：<metric>/<unit>
    - metric ∈ {"requests", "tokens"}
    - unit ∈ {"minute", "5min", "hour", "6h", "12h", "day", "week", "month"}
    """
    config = load_config()
    qt = config.get("llm", {}).get("quota_tracking", {})
    if not isinstance(qt, dict):
        qt = {}
    enabled = bool(qt.get("enabled", True))
    raw_dims = qt.get("dimensions", ["requests/hour", "tokens/hour", "tokens/day"])
    if not isinstance(raw_dims, (list, tuple)):
        raw_dims = ["requests/hour", "tokens/hour", "tokens/day"]
    # 规范化 + 过滤非法格式
    from server.llm_pool.types import parse_quota_dimension
    parsed: list[tuple[str, int]] = []
    for d in raw_dims:
        if not isinstance(d, str):
            continue
        parsed_dim = parse_quota_dimension(d)
        if parsed_dim is not None:
            parsed.append((d.strip().lower(), parsed_dim[1]))
    return {
        "enabled": enabled,
        "dimensions": parsed,  # list[(dim_str, window_seconds)]
    }


def get_llm_circuit_breaker_config() -> dict:
    """获取 LLM Provider 级 Circuit Breaker 配置（v13，OmniRoute 三态熔断借鉴）

    配置格式（config.toml）:
        [llm.circuit_breaker]
        enabled = true
        fail_threshold = 5         # 连续 N 次失败后 OPEN
        open_seconds = 60          # OPEN 持续时间（秒），过后转 HALF_OPEN
        half_open_probe_count = 1  # HALF_OPEN 允许的探针请求数
        success_threshold = 2      # HALF_OPEN 连续成功 N 次后转 CLOSED
    """
    config = load_config()
    cb = config.get("llm", {}).get("circuit_breaker", {})
    if not isinstance(cb, dict):
        cb = {}
    return {
        "enabled": bool(cb.get("enabled", True)),
        "fail_threshold": int(cb.get("fail_threshold", 5)),
        "open_seconds": float(cb.get("open_seconds", 60.0)),
        "half_open_probe_count": int(cb.get("half_open_probe_count", 1)),
        "success_threshold": int(cb.get("success_threshold", 2)),
    }


def get_model_health_config() -> dict:
    """获取 LLM Model 级健康度配置（v15，model_disabled + 退避 + 探针恢复）

    配置格式（config.toml）:
        [llm.model_health]
        enabled = true                       # 总开关
        consecutive_fail_threshold = 5       # 连续失败 N 次后标记 model_disabled
        probe_interval_seconds = 300         # disabled 后探针间隔（秒）
        probe_max_concurrency = 1            # 探针并发上限（通常 1）
        cooldown_base_timeout = 5            # timeout 错误的首次退避秒数
        cooldown_base_http_5xx = 10          # HTTP 5xx 错误的首次退避秒数
        cooldown_base_http_other = 10        # 其他非 200 HTTP 的首次退避秒数
        cooldown_base_empty_content = 15     # 200 但空响应的首次退避秒数
        cooldown_base_exception = 20         # 其他异常的首次退避秒数
        cooldown_max = 60                    # 退避封顶秒数（指数递增后封顶）

    退避策略：cooldown = min(base * 2^attempt, cooldown_max)
    429 / 401 / 402 不计 consecutive_fails（已有专门机制或归 key 级）。
    """
    config = load_config()
    mh = config.get("llm", {}).get("model_health", {})
    if not isinstance(mh, dict):
        mh = {}
    return {
        "enabled": bool(mh.get("enabled", True)),
        "consecutive_fail_threshold": int(mh.get("consecutive_fail_threshold", 5)),
        "probe_interval_seconds": float(mh.get("probe_interval_seconds", 300.0)),
        "probe_max_concurrency": int(mh.get("probe_max_concurrency", 1)),
        "cooldown_base_timeout": float(mh.get("cooldown_base_timeout", 5.0)),
        "cooldown_base_http_5xx": float(mh.get("cooldown_base_http_5xx", 10.0)),
        "cooldown_base_http_other": float(mh.get("cooldown_base_http_other", 10.0)),
        "cooldown_base_empty_content": float(mh.get("cooldown_base_empty_content", 15.0)),
        "cooldown_base_exception": float(mh.get("cooldown_base_exception", 20.0)),
        "cooldown_max": float(mh.get("cooldown_max", 60.0)),
    }


def get_llm_compression_config() -> dict:
    """获取 LLM 消息压缩配置（v14，OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）

    配置格式（config.toml）:
        [llm.compression]
        enabled = false                # 总开关（默认关闭，按需启用）
        mode = "standard"              # off | lite | standard | aggressive
        min_length = 2000              # 仅对估算 token 数 >= min_length 的 content 应用压缩
    """
    config = load_config()
    comp = config.get("llm", {}).get("compression", {})
    if not isinstance(comp, dict):
        comp = {}
    enabled = bool(comp.get("enabled", False))
    mode = str(comp.get("mode", "standard")).lower().strip()
    if mode not in ("off", "lite", "standard", "aggressive"):
        mode = "standard"
    if not enabled:
        mode = "off"
    min_length = int(comp.get("min_length", 2000))
    if min_length < 100:
        min_length = 100  # 防过小阈值导致短消息也被压缩
    return {
        "enabled": enabled,
        "mode": mode,
        "min_length": min_length,
    }


def get_llm_routing_config() -> dict:
    """获取 LLM 路由策略配置（v14，OmniRoute Phase 3.3 Auto 评分路由借鉴）

    配置格式（config.toml）:
        [llm.routing]
        strategy = "round_robin"      # round_robin | auto
        mode_pack = "balanced"        # balanced | fast | quality | cheap | offline
                                      # （非 balanced 暂未实现差异化权重，保留接口）

    ADR-0001 决策文档：docs/adr/0001-auto-scoring-routing.md
    """
    config = load_config()
    routing = config.get("llm", {}).get("routing", {})
    if not isinstance(routing, dict):
        routing = {}
    strategy = str(routing.get("strategy", "round_robin")).lower().strip()
    if strategy not in ("round_robin", "auto"):
        strategy = "round_robin"  # 非法值兜底为默认 round_robin
    mode_pack = str(routing.get("mode_pack", "balanced")).lower().strip()
    if mode_pack not in ("balanced", "fast", "quality", "cheap", "offline"):
        mode_pack = "balanced"
    return {
        "strategy": strategy,
        "mode_pack": mode_pack,
    }


def get_privacy_config() -> dict:
    """获取 LLM 隐私配置

    配置格式（config.toml）:
        [llm.privacy]
        # 当所有 key 都带 privacy_warning（如全免费 key）时，sensitive use_case 会全部失效。
        # 设为 true 可放行 sensitive+privacy_warning 的 key，由用户自担风险。
        # 默认 false（保持安全过滤）。vl_usable_for_sensitive 状态字段会反映此开关效果。
        allow_privacy_warning_for_sensitive = false

    注：sensitive_uses 已迁移到 server/llm_pool/key_store.py 的 USE_CASE_REGISTRY（程序真源），
    不再支持通过 config.toml 配置。
    """
    config = load_config()
    privacy = config.get("llm", {}).get("privacy", {})
    if not isinstance(privacy, dict):
        privacy = {}
    return {
        "allow_privacy_warning_for_sensitive": bool(
            privacy.get("allow_privacy_warning_for_sensitive", False)
        ),
    }


def get_browser_config() -> dict:
    """获取调试浏览器配置（通用，支持任何 Chromium 内核浏览器）

    配置格式（config.toml）:
        [browser]
        debug_port = 9222
        user_data_dir = "<project_root>\\chrome_debug"
        feedback_vl_enabled = true
        session_idle_timeout_secs = 900   # 持久 session idle 超时（15min，留足 agent 多步操作余量）
        session_max_count = 20            # 持久 session 数量上限（防内存泄漏）

    向后兼容：若未配置 [browser] 段，会回退读取旧的 [chrome] 段。
    """
    config = load_config()
    # 优先读 [browser]，未配置则回退到旧 [chrome] 段（向后兼容）
    browser = config.get("browser", {})
    if not browser and "chrome" in config:
        browser = config.get("chrome", {})
    return {
        "debug_port": browser.get("debug_port", 9222),
        "user_data_dir": browser.get("user_data_dir", ""),
        # 浏览器原子操作 VL 反馈闭环总开关（agent 传 verify_prompt 时是否真的调 VL）
        "feedback_vl_enabled": browser.get("feedback_vl_enabled", True),
        # 持久 session 配置（评估文档 P0 第 1 项）
        "session_idle_timeout_secs": int(browser.get("session_idle_timeout_secs", 900)),
        "session_max_count": int(browser.get("session_max_count", 20)),
        # 浏览器选择器调用统计 DB 路径；留空则用 data/browser_stats.db
        "stats_db_path": browser.get("stats_db_path", ""),
    }


# 向后兼容别名（旧调用方过渡期使用，新代码请直接用 get_browser_config）
get_chrome_config = get_browser_config


def get_server_config() -> dict:
    """获取服务器配置"""
    config = load_config()
    server = config.get("server", {})
    return {
        "host": server.get("host", "127.0.0.1"),
        "port": server.get("port", 8766),
        # v16: exec_python 内联等待秒数。子进程在 N 秒内完成则直接返回完整结果
        # (status=done)，否则返回 terminal_id (status=running) 让 agent 调 exec_inspect。
        # 0=禁用，回退 v15 立即返回行为。默认 10s 覆盖 ~80% 短任务。
        "exec_python_inline_wait_secs": server.get("exec_python_inline_wait_secs", 10),
    }


def get_logging_config() -> dict:
    """获取日志配置

    配置格式（config.toml）:
        [logging]
        file_enabled = true                  # 是否写入文件日志（默认 true）
        file_path = "data/logs/server.log"   # 日志文件路径（相对项目根）
        max_bytes = 10485760                 # 单文件最大字节数（10MB）
        backup_count = 10                    # 保留的备份文件数
        file_level = "INFO"                  # 文件日志级别
        console_level = "INFO"               # 控制台日志级别
    """
    config = load_config()
    log = config.get("logging", {})
    if not isinstance(log, dict):
        log = {}
    return {
        "file_enabled": bool(log.get("file_enabled", True)),
        "file_path": log.get("file_path", "data/logs/server.log"),
        "max_bytes": int(log.get("max_bytes", 10 * 1024 * 1024)),
        "backup_count": int(log.get("backup_count", 10)),
        "file_level": str(log.get("file_level", "INFO")).upper(),
        "console_level": str(log.get("console_level", "INFO")).upper(),
    }


def get_screen_config() -> dict:
    """获取屏幕控制配置"""
    config = load_config()
    screen = config.get("screen", {})
    return {
        "default_mode": screen.get("default_mode", "window"),
        "require_confirm_by_default": screen.get("require_confirm_by_default", True),
        "emergency_hotkey": screen.get("emergency_hotkey", "<ctrl>+`"),
        "emergency_cooldown_seconds": screen.get("emergency_cooldown_seconds", 10),
        "danger_keywords_block": screen.get("danger_keywords_block", []),
        "danger_keywords_confirm": screen.get("danger_keywords_confirm", []),
        # execute_action VL 反馈闭环总开关（agent 传 verify_prompt 时是否真的调 VL）
        "feedback_vl_enabled": screen.get("feedback_vl_enabled", True),
        "confirm_timeout_seconds": screen.get("confirm_timeout_seconds", 180),
        # 覆盖层自动隐藏：agent N 秒无操作后自动收起"Agent操作中"提示（0=禁用）
        "overlay_auto_hide_seconds": screen.get("overlay_auto_hide_seconds", 30),
        # 评估文档 P0：焦点安全开关（关闭时跳过 verify_focus_for_input / STALE_COORDINATES / canonical 解析）
        "focus_protection_enabled": screen.get("focus_protection_enabled", True),
        # 受保护进程名列表（agent 宿主窗口，前台是这个且目标不是它时阻断键盘动作）
        # None / 缺失时使用 server.screen.focus.DEFAULT_PROTECTED_PROCESSES
        "protected_processes": screen.get("protected_processes", None),
        # 键盘动作未验证焦点时是否放行（默认 False 强校验；调用方可显式覆盖）
        "allow_unfocused_input_default": screen.get("allow_unfocused_input_default", False),
        # 接管确认（takeover confirm）：顶栏未显示时，键鼠/focus 等争夺输入的操作前弹窗询问用户
        "takeover_confirm_enabled": screen.get("takeover_confirm_enabled", True),
        # 坐标点击 UIA 融合总闸（ZCode computer-use 风格）：click 默认先 UIA hit-test，
        # 命中可点击元素直接后台 invoke（不抢焦点），未命中回退原始键鼠。
        # false 时 strategy 参数被忽略、全部走原始键鼠（旧行为）
        "coordinate_uia_fusion": screen.get("coordinate_uia_fusion", True),
        # 弹窗超时秒数（默认 30s，与 overlay_auto_hide_seconds 对称）
        "takeover_confirm_timeout_seconds": screen.get("takeover_confirm_timeout_seconds", 30),
        # 超时行为：cancel（超时取消，默认）| proceed（兼容旧配置，不推荐）
        "takeover_timeout_action": screen.get("takeover_timeout_action", "cancel"),
        # 持久授权模式（takeover-persistent）：开启后覆盖层常驻、键鼠端点跳过接管确认弹窗
        "takeover_persistent_enabled": screen.get("takeover_persistent_enabled", True),
        # 当前任务授权空闲超时。旧 warning 配置名作为兼容回退。
        "takeover_idle_timeout_seconds": screen.get(
            "takeover_idle_timeout_seconds",
            screen.get("takeover_idle_warning_seconds", 300),
        ),
        "takeover_idle_warning_seconds": screen.get(
            "takeover_idle_warning_seconds",
            screen.get("takeover_idle_timeout_seconds", 300),
        ),
        # 看门狗模式（mode="watchdog"）硬上限秒数，到期自动撤销授权。默认 10h=36000s。
        # watchdog 模式不因空闲撤销，仅由硬上限/紧急停止/用户主动撤销。
        "takeover_watchdog_max_duration_seconds": screen.get(
            "takeover_watchdog_max_duration_seconds", 36000
        ),
        # ===== 会话管理层（SessionManager）时间策略 =====
        # normal 模式正常阶段（浅蓝横条），默认 600 秒（10 分钟）。超过进入告警阶段。
        "idle_warning_seconds": int(screen.get("idle_warning_seconds", 600)),
        # normal 模式告警阶段（黄色横条），默认 1200 秒（20 分钟）。超过降级到无权限。
        "idle_grace_seconds": int(screen.get("idle_grace_seconds", 1200)),
        # watchdog 默认时长（小时），用户未在弹窗中指定时使用，默认 10。
        "watchdog_default_hours": int(screen.get("watchdog_default_hours", 10)),
        # watchdog 最小时长（小时），弹窗输入框下限，默认 1。
        "watchdog_min_hours": int(screen.get("watchdog_min_hours", 1)),
        # watchdog 最大时长（小时），弹窗输入框上限，默认 999。
        "watchdog_max_hours": int(screen.get("watchdog_max_hours", 999)),
        # SessionManager worker 线程检查间隔（秒），默认 5.0。仅用于过期检查，不影响 1 秒倒计时刷新。
        "session_check_interval_seconds": float(
            screen.get("session_check_interval_seconds", 5.0)
        ),
    }


def get_command_guard_config() -> dict:
    """Return destructive command guard settings."""
    guard = load_config().get("command_guard", {})
    # approval_level: HTTP/MCP 端点审批严格性（strict/moderate/loose/none）。
    # 与 enabled 独立——enabled 只控制 dcg 二进制预检查；
    # approval_level 控制 HTTP 中间件和 MCP 网关的端点审批拦截。
    level = guard.get("approval_level", "strict")
    if level not in ("strict", "moderate", "loose", "none"):
        level = "strict"
    return {
        "enabled": guard.get("enabled", True),
        "approval_level": level,
        "dcg_path": guard.get("dcg_path", ""),
        "fail_closed": guard.get("fail_closed", True),
        "timeout_seconds": guard.get("timeout_seconds", 3.0),
        "approval_ttl_seconds": guard.get("approval_ttl_seconds", 300),
        "token_ttl_seconds": guard.get("token_ttl_seconds", 120),
        "gui_timeout_seconds": guard.get("gui_timeout_seconds", 180),
        "llm_review_enabled": guard.get("llm_review_enabled", True),
        "llm_review_timeout": guard.get("llm_review_timeout", 5),
        "llm_review_endpoints": guard.get(
            "llm_review_endpoints",
            ["exec_python", "exec_cmd", "exec_terminal_spawn", "exec_apply_patch"],
        ),
        # 详细审查日志：开启时记录 code/cmd/agent_reason/token 签发与丢弃等全量细节
        # 到 data/approval_audit.jsonl，用于排查审批流程问题。默认关闭。
        "detailed_audit_log": guard.get("detailed_audit_log", False),
    }


def get_vision_config() -> dict:
    """获取视觉AI配置

    config.toml [vision] 段只保留全局参数（编码/重试/开关）；VL provider 配置
    统一存 keys.json 的 key.vision 段（按 base_url 匹配），由 use_case 驱动选 key。

    保留字段：
        [vision]
        vl_enabled = true
        vl_max_image_edge = 1280
        vl_jpeg_quality = 85
        vl_retry_backoffs = [2, 4, 8, 16, 32, 60]

    provider 级额度耗尽追踪（server/vl/remote_vl.py 消费）：
        vl_provider_exhausted_cooldown_seconds = 3600  # 单 provider 耗尽冷却时长（秒）
        vl_provider_exhausted_max_consecutive = 3      # 连续耗尽上限，达此值当日不再试
    """
    config = load_config()
    vision = config.get("vision", {})
    return {
        "vl_enabled": vision.get("vl_enabled", False),
        "vl_max_image_edge": int(vision.get("vl_max_image_edge", 1280)),
        "vl_jpeg_quality": int(vision.get("vl_jpeg_quality", 85)),
        "vl_retry_backoffs": tuple(vision.get("vl_retry_backoffs", (2, 4, 8, 16, 32, 60))),
        # Provider 级额度耗尽追踪（server/vl/remote_vl.py 消费）
        "vl_provider_exhausted_cooldown_seconds": int(vision.get("vl_provider_exhausted_cooldown_seconds", 3600)),
        "vl_provider_exhausted_max_consecutive": int(vision.get("vl_provider_exhausted_max_consecutive", 3)),
    }


def get_ocr_config() -> dict:
    """获取OCR配置

    截图 OCR 默认关闭 PaddleOCR 的 UVDoc 去畸变，使检测框保持原始图片坐标。
    保留可选仿射修正，供替换检测器经校准确认存在系统误差时使用：
    actual = (raw + ratio*dim) / k

    更新模型时先跑三档 bbox 回归并做直接 detector / 完整 pipeline A/B；
    禁止用非恒等参数补偿重新启用的 UVDoc。

    配置格式（config.toml）:
        [ocr]
        kx = 1.0
        ky = 1.0
        bx_ratio = 0.0
        by_ratio = 0.0
        preload_on_startup = false  # P1-A：启动后预热 PaddleOCR（阻塞 ~10s 加载）

    全部为默认值（k=1.0, ratio=0.0）时等价于不修正。
    """
    config = load_config()
    ocr = config.get("ocr", {})
    return {
        "kx": float(ocr.get("kx", 1.0)),
        "ky": float(ocr.get("ky", 1.0)),
        "bx_ratio": float(ocr.get("bx_ratio", 0.0)),
        "by_ratio": float(ocr.get("by_ratio", 0.0)),
        "preload_on_startup": bool(ocr.get("preload_on_startup", False)),
    }


def get_mindforge_config() -> dict:
    """获取 MindForge 集成配置"""
    config = load_config()
    mf = config.get("mindforge", {})
    return {
        "enabled": mf.get("enabled", False),
        "path": mf.get("path", ""),  # 用户必须在 config.toml 配置 MindForge 项目路径
        "python_executable": mf.get("python_executable", ""),
    }


def get_loops_config() -> dict:
    """获取 Loop 自动触发系统配置

    配置格式（config.toml）:
        [loops]
        enabled = true
        timezone = "Asia/Shanghai"

        [loops.activity_tracker]   # 每个任务一个子段，段名即 task_id
        enabled = true
        collect_windows_interval = 60
        ...

        [loops.download_watcher]
        enabled = true
        scan_interval = 300
        ...
    """
    config = load_config()
    loops = config.get("loops", {})
    if not isinstance(loops, dict):
        return {"enabled": False, "timezone": "Asia/Shanghai", "tasks": {}}
    # 顶层字段
    top_fields = {"enabled", "timezone"}
    tasks = {k: v for k, v in loops.items() if k not in top_fields and isinstance(v, dict)}
    return {
        "enabled": loops.get("enabled", True),
        "timezone": loops.get("timezone", "Asia/Shanghai"),
        "tasks": tasks,
    }


def get_inbox_config() -> dict:
    """获取收件箱配置

    配置格式（config.toml）:
        [inbox]
        db_path = "data/inbox.db"
        auto_cleanup_days = 10
    """
    config = load_config()
    inbox = config.get("inbox", {})
    if not isinstance(inbox, dict):
        inbox = {}
    return {
        "db_path": inbox.get("db_path", "data/inbox.db"),
        "auto_cleanup_days": int(inbox.get("auto_cleanup_days", 10)),
    }


def get_cleanup_config() -> dict:
    """获取数据存储清理机制配置

    配置格式（config.toml）:
        [cleanup]
        # browser_stats 聚合归档清理
        browser_stats_details_retention_days = 30
        # approvals 日志按大小轮转
        approvals_log_max_bytes = 10485760
        approvals_log_backup_count = 5
        approval_audit_log_max_bytes = 10485760
        approval_audit_log_backup_count = 5
        # storage_growth 告警阈值
        memory_facts_warn_rows = 5000
        todos_archived_warn_count = 1000
        db_file_warn_size_mb = 50
    """
    config = load_config()
    cleanup = config.get("cleanup", {})
    if not isinstance(cleanup, dict):
        cleanup = {}
    return {
        "browser_stats_details_retention_days": int(cleanup.get("browser_stats_details_retention_days", 30)),
        "approvals_log_max_bytes": int(cleanup.get("approvals_log_max_bytes", 10485760)),
        "approvals_log_backup_count": int(cleanup.get("approvals_log_backup_count", 5)),
        "approval_audit_log_max_bytes": int(cleanup.get("approval_audit_log_max_bytes", 10485760)),
        "approval_audit_log_backup_count": int(cleanup.get("approval_audit_log_backup_count", 5)),
        "memory_facts_warn_rows": int(cleanup.get("memory_facts_warn_rows", 5000)),
        "todos_archived_warn_count": int(cleanup.get("todos_archived_warn_count", 1000)),
        "db_file_warn_size_mb": int(cleanup.get("db_file_warn_size_mb", 50)),
    }


def get_models_config() -> dict:
    """获取统一模型存储配置

    所有 AI 模型集中存储在 external_dir 下按子目录分类。
    个人部署设为 <data_drive>:\\ai_models；发布时留空则各模块回退到项目内 weights/ 目录。

    配置格式（config.toml）:
        [models]
        external_dir = "<data_drive>:\\ai_models"   # 个人部署；发布时留空
        paddle_device = "gpu"   # PaddleOCR 推理设备: gpu（默认）| cpu | auto
        # GPU→CPU 条件降级（GPU 压力拒绝态且条件达标时降到 CPU 继续服务）
        gpu_fallback_min_avail_gb = 2.0   # 降级所需最小可用内存（GB）
        gpu_fallback_max_cpu_pct = 70.0   # 降级允许的最大 CPU 占用（%）
        gpu_fallback_hold_sec = 300.0     # 降级后最短保持时间（秒，迟滞防抖）

    子目录映射（external_dir 非空时）:
        paddleocr/      - PaddleOCR 模型
        embeddings/     - 嵌入模型 (bge-small-zh-v1.5 等)
        faster_whisper/ - Whisper STT 模型
        cosyvoice/      - CosyVoice TTS 模型
        huggingface/    - HF 缓存 (同时由环境变量 HF_HOME 指向)
        modelscope/     - ModelScope 缓存 (同时由环境变量 MODELSCOPE_CACHE 指向)

    回退路径（external_dir 为空时）:
        weights/paddlex      - PaddleOCR
        weights/embeddings   - 嵌入模型
    """
    config = load_config()
    models_cfg = config.get("models", {})
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    external_dir = str(models_cfg.get("external_dir", "")).strip()
    # PaddleOCR 推理设备：gpu（默认）| cpu | auto（自动检测后回落 cpu）
    # 历史：2026-08-26 曾因「GPU 首次推理挂死」把默认值降为 cpu；该结论已于 2026-08-31
    # 实机复测推翻（三档各 3 轮共 9 次全部正常返回，零挂死），故默认值改回 gpu。
    # 完整经过见 server/ocr.py 的 _detect_paddle_device() 注释。
    paddle_device = str(models_cfg.get("paddle_device", "gpu")).strip().lower() or "gpu"

    # GPU→CPU 条件降级（2026-08-31）：GPU 压力拒绝态时若主机内存/CPU 达标，
    # OCR 降级到 CPU 继续服务而非直接不可用。阈值依据 2026-08-31 游戏满载实测
    # （可用内存最低 0.46GB 不可降级 / 常态 14.5GB 充裕；CPU 峰值 47.7%）
    gpu_fallback_min_avail_gb = float(models_cfg.get("gpu_fallback_min_avail_gb", 2.0))
    gpu_fallback_max_cpu_pct = float(models_cfg.get("gpu_fallback_max_cpu_pct", 70.0))
    gpu_fallback_hold_sec = float(models_cfg.get("gpu_fallback_hold_sec", 300.0))

    project_weights = Path(__file__).resolve().parent.parent / "weights"

    if external_dir:
        base = Path(external_dir)
        return {
            "external_dir": external_dir,
            "paddle_device": paddle_device,
            "gpu_fallback_min_avail_gb": gpu_fallback_min_avail_gb,
            "gpu_fallback_max_cpu_pct": gpu_fallback_max_cpu_pct,
            "gpu_fallback_hold_sec": gpu_fallback_hold_sec,
            "paddleocr_dir": str(base / "paddleocr"),
            "embeddings_dir": str(base / "embeddings"),
            "faster_whisper_dir": str(base / "faster_whisper"),
            "cosyvoice_dir": str(base / "cosyvoice"),
        }
    else:
        # 发布模式：回退到项目内 weights/ 目录
        return {
            "external_dir": "",
            "paddle_device": paddle_device,
            "gpu_fallback_min_avail_gb": gpu_fallback_min_avail_gb,
            "gpu_fallback_max_cpu_pct": gpu_fallback_max_cpu_pct,
            "gpu_fallback_hold_sec": gpu_fallback_hold_sec,
            "paddleocr_dir": str(project_weights / "paddlex"),
            "embeddings_dir": str(project_weights / "embeddings"),
            "faster_whisper_dir": str(project_weights / "faster_whisper"),
            "cosyvoice_dir": str(project_weights / "cosyvoice"),
        }


# ==================== 配置 Schema 验证（P2-18）====================


class ServerConfigSchema(BaseSchema):
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)


class LoggingConfigSchema(BaseSchema):
    file_enabled: bool = True
    file_path: str = "data/logs/server.log"
    max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    backup_count: int = Field(default=10, ge=0)
    file_level: str = "INFO"
    console_level: str = "INFO"


class BrowserConfigSchema(BaseSchema):
    """调试浏览器配置 Schema（通用，支持 Chromium 内核浏览器）"""
    debug_port: int = Field(default=9222, ge=1, le=65535)
    user_data_dir: str = ""
    stats_db_path: str = ""
    feedback_vl_enabled: bool = True
    session_idle_timeout_secs: int = Field(default=900, ge=60)
    session_max_count: int = Field(default=20, ge=1)


# 向后兼容别名
ChromeConfigSchema = BrowserConfigSchema


class VisionConfigSchema(BaseSchema):
    """视觉 AI 配置 Schema（v9 schema，[vision] 段）

    覆盖 get_vision_config() 返回的所有字段，启动时 validate_config() 可发现配置错误。
    """
    vl_enabled: bool = False
    vl_max_image_edge: int = Field(default=1280, ge=64, le=4096)
    vl_jpeg_quality: int = Field(default=85, ge=1, le=100)
    vl_retry_backoffs: tuple = (2, 4, 8, 16, 32, 60)
    # Provider 级额度耗尽追踪（server/vl/remote_vl.py 消费）
    vl_provider_exhausted_cooldown_seconds: int = Field(default=3600, ge=60)
    vl_provider_exhausted_max_consecutive: int = Field(default=3, ge=1)


class ScreenConfigSchema(BaseSchema):
    default_mode: str = "window"
    require_confirm_by_default: bool = True
    emergency_hotkey: str = "<ctrl>+`"
    emergency_cooldown_seconds: int = Field(default=10, ge=0)
    danger_keywords_block: list[str] = Field(default_factory=list)
    danger_keywords_confirm: list[str] = Field(default_factory=list)
    confirm_timeout_seconds: int = Field(default=180, gt=0)
    # 评估文档 P0：焦点安全
    focus_protection_enabled: bool = True
    protected_processes: list[str] | None = None
    allow_unfocused_input_default: bool = False
    # 接管确认：顶栏未显示时键鼠/focus 操作前弹窗询问
    takeover_confirm_enabled: bool = True
    takeover_confirm_timeout_seconds: int = Field(default=30, gt=0)
    takeover_timeout_action: str = "cancel"
    # 持久授权模式（takeover-persistent）：开启后覆盖层常驻、键鼠端点跳过接管确认弹窗
    takeover_persistent_enabled: bool = True
    takeover_idle_timeout_seconds: int = Field(default=300, gt=0)
    # 旧配置名，保留兼容读取。
    takeover_idle_warning_seconds: int = Field(default=300, gt=0)


class CommandGuardConfigSchema(BaseSchema):
    enabled: bool = True
    dcg_path: str = ""
    fail_closed: bool = True
    timeout_seconds: float = Field(default=3.0, gt=0)
    approval_ttl_seconds: int = Field(default=300, gt=0)
    token_ttl_seconds: int = Field(default=120, gt=0)
    gui_timeout_seconds: int = Field(default=180, gt=0)
    # 以下字段此前缺失导致启动 extra_forbidden 告警；实际已被运行时消费，现已补回：
    approval_level: str = "strict"
    llm_review_enabled: bool = True
    llm_review_timeout: float = Field(default=5.0, gt=0)
    llm_review_endpoints: list[str] = Field(
        default_factory=lambda: [
            "exec_python",
            "exec_cmd",
            "exec_terminal_spawn",
            "exec_apply_patch",
        ]
    )
    detailed_audit_log: bool = False


class OcrConfigSchema(BaseSchema):
    """[ocr] 段 schema（T5 顺手补 preload_on_startup 字段，与 get_ocr_config() 对齐）"""
    kx: float = 1.0
    ky: float = 1.0
    bx_ratio: float = 0.0
    by_ratio: float = 0.0
    # P1-A：启动后后台预热 PaddleOCR（不阻塞 startup）。get_ocr_config() 早已消费此字段，
    # 此前 schema 未声明导致 validate_config() 静默忽略类型错误。
    preload_on_startup: bool = False


class InboxConfigSchema(BaseSchema):
    db_path: str = "data/inbox.db"
    auto_cleanup_days: int = Field(default=10, ge=0)


class ModelManagerResourceSchema(BaseSchema):
    """[model_manager.gpu] / [model_manager.cpu] 子段 schema（design §9）

    enabled=True 表示该资源压力监控启用；min_loaded_seconds/unload_timeout_sec
    为顶层字段（_min_loaded/_unload_timeout 在 config.py 中按子段读取，
    schema 中也声明以便 validate_config 早发现错误）。
    """
    enabled: bool | None = None  # None=继承默认（gpu=True / cpu=False，由 get_model_manager_config 兜底）
    poll_interval_sec: float = Field(default=5.0, gt=0)
    high_watermark: float = Field(default=0.90, ge=0.0, le=1.0)
    high_sustain: int = Field(default=3, ge=1)
    critical_watermark: float = Field(default=0.97, ge=0.0, le=1.0)
    low_watermark: float = Field(default=0.70, ge=0.0, le=1.0)
    low_sustain: int = Field(default=6, ge=1)
    min_loaded_seconds: float = Field(default=60.0, ge=0.0)
    unload_timeout_sec: float = Field(default=15.0, ge=0.0)


class ModelManagerConfigSchema(BaseSchema):
    """[model_manager] 段 schema（design §9，T5 接入）

    覆盖 get_model_manager_config() 读取的顶层字段 + gpu/cpu 子段。
    [model_manager.models.<id>] 动态子段由 get_model_manager_config() 运行时兜底
    （schema 不做静态校验，避免新增模型 id 时需同步改 schema）。
    """
    enabled: bool = True
    poll_interval_sec: float = Field(default=5.0, gt=0)
    reload_fail_cooldown_sec: float = Field(default=30.0, gt=0)
    gpu: ModelManagerResourceSchema = Field(default_factory=ModelManagerResourceSchema)
    cpu: ModelManagerResourceSchema = Field(default_factory=ModelManagerResourceSchema)


_CONFIG_SCHEMAS: dict[str, type[BaseSchema]] = {
    "server": ServerConfigSchema,
    "logging": LoggingConfigSchema,
    "browser": BrowserConfigSchema,
    "chrome": BrowserConfigSchema,  # 向后兼容：[chrome] 段也走同一 schema
    "vision": VisionConfigSchema,
    "screen": ScreenConfigSchema,
    "command_guard": CommandGuardConfigSchema,
    "ocr": OcrConfigSchema,
    "inbox": InboxConfigSchema,
    "model_manager": ModelManagerConfigSchema,
}


def validate_config() -> list[str]:
    """验证 config.toml 各段的类型和范围，返回告警列表。

    不修改配置内容——get_*_config() 函数已处理默认值。
    此函数仅在启动时调用，用于尽早发现配置错误。
    """
    config = load_config()
    if not config:
        return []

    warnings: list[str] = []
    for section_name, schema_cls in _CONFIG_SCHEMAS.items():
        section = config.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            warnings.append(f"[{section_name}] 应为 table（字典），实际为 {type(section).__name__}")
            continue
        try:
            schema_cls(**section)
        except Exception as e:
            for line in str(e).split("\n"):
                line = line.strip()
                if line:
                    warnings.append(f"[{section_name}] {line}")
    return warnings
