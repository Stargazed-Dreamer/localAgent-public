"""健康检查端点：通过 StatusProvider 模式聚合各模块状态"""

import asyncio
import json
import logging
import threading
import time

from fastapi import FastAPI

from lib.schema import BaseSchema
from server.config import (
    get_browser_config,
    get_llm_config,
    get_screen_config,
    get_vision_config,
)

logger = logging.getLogger("localagent.health")

# 回退默认值：唯一权威来源是 server/main.py 的 VERSION（main 显式传参覆盖此默认，
# 且 health→main 导入会循环依赖，故此处仅保持同步 + 注明来源）
VERSION = "0.48.0"


# ========== 模块错误注册表（last_error 追踪）==========

_module_errors: dict[str, dict] = {}
_module_errors_lock = threading.Lock()


def record_module_error(module: str, error: str, exc_type: str = ""):
    """记录模块错误（供各模块在初始化/运行失败时调用）。

    module: 模块名（如 "ocr", "llm_pool", "memory", "vision"）
    error: 错误描述
    exc_type: 异常类型名（可选）
    """
    with _module_errors_lock:
        _module_errors[module] = {
            "error": error[:500],
            "exc_type": exc_type,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def clear_module_error(module: str):
    """清除模块错误记录（模块恢复后调用）"""
    with _module_errors_lock:
        _module_errors.pop(module, None)


def get_module_errors() -> dict:
    """获取所有模块的错误记录（供 /health 返回）"""
    with _module_errors_lock:
        return dict(_module_errors)


class HealthResponse(BaseSchema):
    """健康检查API响应的数据模型。"""
    status: str  # 总体健康状态
    version: str  # 当前版本号
    uptime_seconds: float  # 连续运行时间（秒）
    ocr: dict  # OCR模块状态
    agent: dict  # 智能体模块状态
    apikey: dict  # API密钥服务状态
    browser: dict  # 浏览器模块状态
    exec: dict  # 执行器模块状态
    screen: dict  # 屏幕模块状态
    vision: dict  # 视觉模块状态
    memory: dict  # 内存模块状态
    mindforge: dict  # MindForge模块状态
    accounting: dict  # 统计计费模块状态
    docviewer: dict  # 文档查看器模块状态
    system: dict  # 系统状态
    mcp: dict  # MCP模块状态
    worker: dict = {}  # 已废弃，保留字段以兼容旧客户端（始终返回空）
    llm_pool: dict  # LLM池模块状态
    user_message: dict = {}  # 用户消息注入状态
    agent_guide: dict = {}  # Agent Guide 路由层状态
    todos: dict = {}  # 待办模块状态
    loops: dict = {}  # Loop 自动触发系统状态
    inbox: dict = {}  # 收件箱状态
    project_structure: dict = {}  # 项目结构映射 baseline 状态
    auto_shutdown: dict = {}  # 自动关机端点状态（trigger_count 等）
    model_manager: dict = {}  # Model Lifecycle Manager 状态（design §10，低频枚举）
    last_errors: dict = {}  # 各模块最近一次错误（模块名 → {error, exc_type, timestamp}）


# ========== 各模块状态提供器（从原 health() 函数提取） ==========

def _get_ocr_status() -> dict:
    """OCR 状态（P1-A：含 model_ready/cold_start/loading/failed/加载耗时/推理耗时/推理次数/最近错误）"""
    from server.ocr import models as ocr_models
    from server.vl.remote_vl import vl_status_fields
    vision_cfg = get_vision_config()
    vl_fields = vl_status_fields()
    return {
        "ocr_loaded": ocr_models.ocr_loaded,
        "keep_models": ocr_models.keep_models,
        "vl_model_enabled": vision_cfg.get("vl_enabled", False),
        "vl_provider": vl_fields["vl_provider"],
        "vl_model": vl_fields["vl_model"],
        "vl_available": vl_fields["vl_available"],
        "vl_usable_for_sensitive": vl_fields.get("vl_usable_for_sensitive", False),
        # P1-A 可观测性
        "model_ready": ocr_models.model_ready,
        "cold_start": ocr_models.cold_start,
        "loading": ocr_models.loading,
        "failed": ocr_models.failed,
        # GPU→CPU 降级可见性：当前实际（或最近决策的）推理设备
        "active_device": ocr_models.active_device,
        "load_elapsed_ms": ocr_models._load_elapsed_ms,
        "last_inference_ms": ocr_models._last_inference_ms,
        "inference_count": ocr_models._inference_count,
        "last_error": ocr_models._last_error,
        # design §10 兼容委托：附加 ModelLifecycleManager 状态摘要
        "mlm_state": _get_model_manager_status(),
    }


def _get_agent_status() -> dict:
    """Agent 状态（v8: tier 系统；v10: 区分 configured vs available）

    v10 修正：原实现用 `if not k.is_available: continue` 过滤 key，
    导致 key 在冷却中时所有 model 从展示中消失，造成"配置好的 model 不见了"的误导。
    现在分别统计：
    - tiers_configured：所有 key 配置的 model（无论 key 是否当前可用）
    - tiers_available：当前可用 key（未 expired、未 cooldown、未满并发）的 model
    """
    # v14: config.toml 不再存放 api_key，直接检查 keys.json
    llm_ok = False
    try:
        from server.llm_pool.key_store import resolve_key
        resolved = resolve_key("agent_chat")
        llm_ok = bool(resolved and resolved.api_key)
    except Exception:
        pass
    # 向后兼容：检查 config.toml 是否有残留的 [llm] api_key
    if not llm_ok:
        llm_cfg = get_llm_config()
        llm_ok = llm_cfg["api_key"] not in ("", "sk-your-api-key-here")
    # v10: 按 tier(1-5) 聚合 model，区分 configured vs available
    # v11: model 名优先使用 display_name（UI 展示用），缺失则 fallback 到 name（连接名）
    tiers: dict[str, str] = {}
    configured_models: dict[int, set[str]] = {}
    available_models: dict[int, set[str]] = {}
    try:
        from server.llm_pool import get_pool, is_initialized
        if is_initialized():
            pool = get_pool()
            for k in pool.keys:
                # configured：所有 key 的所有 model（即使 key 已 expired/cooldown）
                for model_name, t in k.model_tiers.items():
                    disp = k.get_display_name(model_name)
                    configured_models.setdefault(t, set()).add(disp)
                # available：仅当前可分配的 key
                if k.is_available:
                    for model_name, t in k.model_tiers.items():
                        disp = k.get_display_name(model_name)
                        available_models.setdefault(t, set()).add(disp)
            # 展示时：用 configured 显示，并标注 available 数量
            legacy_map = {"cheap": 2, "default": 3, "powerful": 5}
            for old_name, tier_num in legacy_map.items():
                cfg = configured_models.get(tier_num, set())
                avail = available_models.get(tier_num, set())
                if cfg:
                    if avail == cfg:
                        tiers[old_name] = ", ".join(sorted(cfg))
                    else:
                        tiers[old_name] = f"{', '.join(sorted(cfg))}  (可用: {len(avail)})"
                else:
                    tiers[old_name] = "(无配置)"
            # 同时展示 v8 tier 数字
            for t in sorted(configured_models.keys()):
                cfg = configured_models[t]
                avail = available_models.get(t, set())
                if avail == cfg:
                    tiers[f"tier_{t}"] = ", ".join(sorted(cfg))
                else:
                    tiers[f"tier_{t}"] = f"{', '.join(sorted(cfg))}  (可用: {len(avail)}/{len(cfg)})"
    except Exception as e:
        tiers["error"] = f"获取 tier 信息失败: {e}"
    return {
        "configured": llm_ok,
        "models": tiers,
    }


def _get_apikey_status() -> dict:
    """ApiKey 状态（通过公共 get_status() 接口，不直读私有 _test_history）"""
    from server.apikey import get_status as _apikey_status
    return _apikey_status()


def _get_browser_status() -> dict:
    """浏览器状态（含持久 session 数量，评估文档 P0）"""
    import urllib.request
    browser_cfg = get_browser_config()
    browser_ok = False
    tab_count = None
    page_count = None
    target_count = None
    try:
        port = browser_cfg["debug_port"]
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        browser_ok = True
        tabs_resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2)
        tabs = json.loads(tabs_resp.read().decode())
        page_count = len([t for t in tabs if t.get("type") == "page"])
        target_count = len(tabs)
        tab_count = page_count  # 向后兼容
    except Exception:
        pass
    # 持久 session 数（可能为 None：session manager 未初始化或循环外调用）
    sessions_count = 0
    try:
        # 同步获取（avoid asyncio），直接读 _manager 字段
        from server.browser.session import manager as _bs
        mgr = _bs._manager
        if mgr is not None:
            sessions_count = len(mgr._sessions)
    except Exception:
        pass
    return {
        "connected": browser_ok,
        "debug_port": browser_cfg["debug_port"],
        "tab_count": tab_count,
        "target_count": target_count,
        "page_count": page_count,
        "sessions_count": sessions_count,
        "session_idle_timeout_secs": browser_cfg.get("session_idle_timeout_secs"),
        "session_max_count": browser_cfg.get("session_max_count"),
    }


def _get_exec_status() -> dict:
    """Exec 状态（含终端会话）—— 通过公共 get_status() 接口，不直读私有 _terminals/_exec_history"""
    from server.exec import get_status as _exec_status
    return _exec_status()


def _get_screen_status() -> dict:
    """Screen 状态（含焦点安全配置——评估文档 P0）"""
    from server.screen import get_status as _screen_base_status
    from server.screen.focus import DEFAULT_PROTECTED_PROCESSES
    from server.screen.uia import get_snapshot_cache_size as _uia_cache_size
    from server.screen.uia import uia_available
    screen_cfg = get_screen_config()
    # 受保护进程列表：config 配置优先，否则用模块默认
    protected_processes = screen_cfg.get("protected_processes") or DEFAULT_PROTECTED_PROCESSES
    base = _screen_base_status()
    return {
        "capture_available": True,  # 如果能到这里说明基本可用
        "emergency_stopped": base["emergency_stopped"],
        "auto_skip_cache_size": base["auto_skip_cache_size"],
        "require_confirm_default": screen_cfg.get("require_confirm_by_default", True),
        "admin_privileges": base["admin_privileges"],
        # 评估文档 P0 焦点安全状态
        "focus_protection": {
            "enabled": screen_cfg.get("focus_protection_enabled", True),
            "allow_unfocused_input_default": screen_cfg.get("allow_unfocused_input_default", False),
            "protected_processes": protected_processes,
            "protected_processes_is_default": screen_cfg.get("protected_processes") is None,
        },
        # canonical window token 能力标记（UWP host/child 同族解析）
        "canonical_token": {
            "available": True,  # resolve_canonical_window 始终可用（非 Windows 时返回安全默认值）
            "description": "UWP/WinUI 同族 HWND 解析（GetAncestor GA_ROOTOWNER）",
        },
        # 评估文档 P0-5 UIA 语义层状态
        "uia": {
            "available": uia_available(),
            "snapshot_cache_size": _uia_cache_size(),
            "description": "UI Automation accessibility tree + 语义动作（invoke/toggle/set_value/...）",
        },
        # 评估文档 P1-B 窗口生命周期 API 状态
        "window_lifecycle": {
            "available": True,
            "endpoints": [
                "screen_app_list", "screen_app_launch", "screen_app_wait",
                "screen_window_resolve",
                "screen_window_minimize", "screen_window_restore",
                "screen_window_raise", "screen_window_close",
            ],
            "token_verification": "canonical_hwnd + pid + process_create_time（进程重启后 token 失效）",
            "description": "app_list/launch/wait + window_resolve（多匹配候选）+ 窗口操作 + token 失效检测",
        },
        # 接管确认（takeover confirm）：顶栏未显示时 agent 键鼠操作前的用户确认
        "takeover_confirm": {
            "enabled": screen_cfg.get("takeover_confirm_enabled", True),
            "timeout_seconds": screen_cfg.get("takeover_confirm_timeout_seconds", 30),
            "timeout_action": screen_cfg.get("takeover_timeout_action", "cancel"),
            "protected_endpoints": [
                "screen_action", "screen_batch_actions",
                "screen_desktop_transaction", "screen_focus_window",
            ],
            "description": "顶栏 overlay 未显示时，agent 调键鼠端点前弹窗询问用户是否允许接管",
        },
        # 当前任务授权。health 字段名保留 takeover_persistent 以兼容旧客户端。
        "takeover_persistent": _get_persistent_status(),
    }


def _get_persistent_status() -> dict:
    """当前任务授权状态（兼容旧 takeover_persistent health 路径）。

    T17：授权状态改从 SessionManager 获取（不再反向 import gui_client 拿授权）。
    overlay_visible 仍从 gui_client 拿（GUI 渲染状态，与会话管理层分离）。
    """
    screen_cfg = get_screen_config()
    configured_timeout = int(
        screen_cfg.get("takeover_idle_timeout_seconds", 300)
    )
    try:
        from server.screen.session import get_session_manager
        status = get_session_manager().status()
    except Exception as e:
        return {
            "enabled": False,
            "mode": "no_permission",
            "max_duration_seconds": None,
            "source": "",
            "last_input_time": 0.0,
            "overlay_visible": False,
            "error": str(e),
            "description": "SessionManager 不可用，当前任务授权状态未知",
        }

    mode = status.get("mode", "no_permission")
    max_duration = status.get("max_duration_seconds")
    if mode == "watchdog":
        description = "看门狗模式：持续监控免空闲撤销，到时长上限自动降级到普通模式，危险操作仍拦截"
    elif mode == "normal":
        description = "当前任务内普通控制免重复确认，空闲两阶段超时自动撤销"
    else:
        description = "无权限：副作用端点返回 403，需先调 screen_request_control 请求授权"

    # overlay_visible 仍需从 overlay_client 拿（GUI 渲染状态）
    overlay_visible = False
    try:
        from server.overlay_client import overlay_client
        overlay_visible = bool(overlay_client.overlay_visible)
    except Exception:
        pass

    return {
        "configured": screen_cfg.get("takeover_persistent_enabled", True),
        "enabled": status["active"],
        **status,
        "mode": mode,
        "max_duration_seconds": max_duration,
        "idle_timeout_seconds": (
            status.get("idle_timeout_seconds") or configured_timeout
        ),
        "configured_idle_timeout_seconds": configured_timeout,
        "configured_watchdog_max_duration_seconds": screen_cfg.get(
            "takeover_watchdog_max_duration_seconds", 36000
        ),
        "last_input_time": status.get("last_activity_at", 0.0),
        "overlay_visible": overlay_visible,
        "description": description,
    }


def _get_vision_status() -> dict:
    """Vision 状态（远程 VL）

    含 provider 级额度耗尽 fallback 状态：
    - providers: 完整 provider 列表（含 cooldown/active_count/last_error）
    - provider_exhausted: 单 provider 耗尽状态（cooldown_until/reason/kind/source/consecutive_count）
    - daily_exhausted: 聚合状态（所有 provider 都耗尽时为 True）
    """
    from server.vl.remote_vl import vl_status_fields
    vision_cfg = get_vision_config()
    vl_fields = vl_status_fields()
    return {
        "vl_model_enabled": vision_cfg.get("vl_enabled", False),
        "vl_provider": vl_fields["vl_provider"],
        "vl_model": vl_fields["vl_model"],
        "vl_available": vl_fields["vl_available"],
        "vl_usable_for_sensitive": vl_fields.get("vl_usable_for_sensitive", False),
        "allow_privacy_warning_for_sensitive": vl_fields.get("allow_privacy_warning_for_sensitive", False),
        # Provider 级 fallback 状态（provider 级额度耗尽改进）
        "providers": vl_fields.get("providers", []),
        "provider_exhausted": vl_fields.get("provider_exhausted", {}),
        "daily_exhausted": vl_fields.get("daily_exhausted", False),
        "daily_exhausted_at": vl_fields.get("daily_exhausted_at"),
        "daily_exhausted_reason": vl_fields.get("daily_exhausted_reason"),
    }


def _get_memory_status() -> dict:
    """记忆状态（三层记忆系统）"""
    try:
        from server.memory.manager import get_memory_manager
        mem_mgr = get_memory_manager()
        mem_stats = mem_mgr.get_status()
        clear_module_error("memory")
        return {
            "available": True,
            "messages": mem_stats.get("messages", 0),
            "facts": mem_stats.get("facts", 0),
            "summaries": mem_stats.get("summaries", 0),
            "embedding_ready": mem_stats.get("embedding_ready", False),
            "db_size_mb": mem_stats.get("db_size_mb", 0),
            "maintainer": mem_stats.get("maintainer", {}),
        }
    except Exception as e:
        record_module_error("memory", str(e), type(e).__name__)
        return {
            "available": False,
        }


def _get_todos_status() -> dict:
    """待办模块状态"""
    from server.todos.router import get_status as _todos_status
    return _todos_status()


def _get_mindforge_status() -> dict:
    """MindForge 状态—— 通过公共 get_status() 接口，不直读私有 _resolve_mindforge_dir"""
    from server.mindforge import get_status as _mf_status
    return _mf_status()


def _get_component_health_status() -> dict[str, dict]:
    """动态加载所有声明了 [health_check] 的组件健康检查

    通过 manifest [health_check] 段发现组件的健康检查入口，
    动态调用组件的 get_status() 函数。
    删除 workspace/<module>/ 后 manifest 消失，健康检查自动跳过。

    返回 {component_name: status_dict}，单个组件健康检查失败时
    返回 {"error": "..."}，不影响其他组件和整个 /health 端点。
    """
    import importlib

    from server.component_manifest import get_components_by_capability

    result = {}
    for m in get_components_by_capability("health_check"):
        if m.health_check is None:
            continue
        try:
            # 动态导入组件的健康检查模块
            # manifest file 字段形如 "health.py"，去掉 .py 后缀作为模块名
            module_name = f"workspace.{m.name}.{m.health_check.file.replace('.py', '')}"
            mod = importlib.import_module(module_name)
            func = getattr(mod, m.health_check.function, None)
            if func and callable(func):
                result[m.name] = func()
        except Exception as e:
            result[m.name] = {"error": str(e)}
    return result


def _get_docviewer_status() -> dict:
    """DocViewer 状态"""
    from server.docviewer import SUPPORTED_EXTENSIONS
    docviewer_available = True
    for mod_name in ["fitz", "docx", "openpyxl", "pptx"]:
        try:
            __import__(mod_name)
        except ImportError:
            docviewer_available = False
            break
    return {
        "available": docviewer_available,
        "supported_formats": list(SUPPORTED_EXTENSIONS.keys()),
    }


def _get_system_status() -> dict:
    """System 状态（防休眠开关）"""
    from server.system import keep_awake
    return keep_awake.status()


def _get_mcp_status(app) -> dict:
    """MCP 架构状态—— advanced 部分通过公共 get_status() 接口，不直读私有 _advanced_registry"""
    from server.advanced import get_status as _advanced_status
    from server.mcp_whitelist import DIRECT_TOOLS
    from server.templates import TEMPLATES
    direct_tools_count = len(DIRECT_TOOLS)
    adv_status = _advanced_status()
    return {
        "direct_tools_count": direct_tools_count,
        "direct_tools_limit": 50,
        "gateway_tools_count": adv_status["gateway_tools_count"],
        "template_count": len(TEMPLATES),
        "gateway_categories": adv_status["gateway_categories"],
        "template_categories": sorted({t["category"] for t in TEMPLATES}),
        "image_content_patch": True,
    }


def _get_llm_pool_status() -> dict:
    """LLM 并发池状态"""
    llm_pool_status: dict[str, object] = {"initialized": False}
    # v6-lite-chat-fix T02：/llm/pool/models 端点已注册（含 grouped_by_tier/default_tier/default_model）
    # 此字段始终为 True，因为端点在 FastAPI app 启动时就注册了（不依赖池初始化）
    llm_pool_status["models_endpoint"] = {
        "available": True,
        "path": "/llm/pool/models",
        "supports_grouped_by_tier": True,
    }
    try:
        # 统一 keys.json（v4 真源）
        # 路径常量从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
        from lib.secret import get_llm_keys_path
        from server.llm_pool import (
            _get_status_dict,
            _load_keys_records,
            get_pool,
            is_initialized,
            should_run_health_check,
        )
        _keys_file = get_llm_keys_path()
        keys_files = [_keys_file]
        seen = set()
        all_keys_data = []
        needs_check_any = False
        last_check_latest = ""
        for kf in keys_files:
            if str(kf) in seen or not kf.exists():
                continue
            seen.add(str(kf))
            needs_check_any = needs_check_any or should_run_health_check()
            try:
                kf_data, _wrapper = _load_keys_records(kf)
            except Exception:
                continue  # 非 JSON 文件（如纯文本 key），跳过健康统计
            all_keys_data.extend(kf_data)
            if kf_data:
                # 找该文件中所有记录的最晚 last_health_check（不一定所有记录都被检查过）
                for rec in kf_data:
                    lc = _get_status_dict(rec).get("last_health_check", "")
                    if lc and lc > last_check_latest:
                        last_check_latest = lc
        if all_keys_data:
            llm_pool_status["needs_health_check"] = needs_check_any
            llm_pool_status["total_keys"] = len(all_keys_data)
            llm_pool_status["active_keys"] = sum(
                1 for d in all_keys_data if _get_status_dict(d).get("works", True))
            llm_pool_status["last_health_check"] = last_check_latest
        if is_initialized():
            pool = get_pool()
            status = pool.get_status()
            # 统计带隐私警告的 key 数量
            privacy_warning_count = sum(1 for k in status.get("keys", []) if k.get("privacy_warning"))
            # v10：免费/paid 分布（基于 is_free 推断，比 privacy_warning 更准确）
            free_keys_count = status.get("free_keys_count", 0)
            paid_keys_count = status.get("paid_keys_count", 0)
            # v10：近期调用历史总数（环形缓冲，反映后端运行时累计活动）
            recent_calls_count = status.get("recent_calls_count", 0)
            # v13：Provider 级 Circuit Breaker 聚合统计
            # get_breakers_snapshot() 返回 dict（含 enabled/breakers 等顶层字段），
            # 真正的熔断器在 breakers_snap["breakers"]，是 {base_url: snapshot_dict}。
            # 必须取 .values() 遍历 snapshot dict，否则遍历 dict 会得到 base_url 字符串，
            # 调 .get("state") 会报 'str' object has no attribute 'get'。
            breakers_snap = pool.get_breakers_snapshot() if hasattr(pool, "get_breakers_snapshot") else {}
            breaker_list = list((breakers_snap.get("breakers") or {}).values()) if isinstance(breakers_snap, dict) else []
            breaker_open = sum(1 for b in breaker_list if b.get("state") == "OPEN")
            breaker_half_open = sum(1 for b in breaker_list if b.get("state") == "HALF_OPEN")
            breaker_total = len(breaker_list)
            llm_pool_status = {
                "initialized": True,
                "version": "v14",  # v14：schema 版本标识（RTK/Caveman 压缩 + Auto 评分路由）
                "total_keys": status["total_keys"],
                "active_keys": status["active_keys"],
                "current_active": status["current_active"],
                "total_max_concurrency": status["total_max_concurrency"],
                # v12 新增：去重后真实并发上限（同 pool_key 取 max）
                "total_max_concurrency_deduped": status.get("total_max_concurrency_deduped", status["total_max_concurrency"]),
                "dedup_ratio": status.get("dedup_ratio", 1.0),
                "pool_keys_count": len(status.get("pool_keys", [])),
                "total_tokens_consumed": status["total_tokens_consumed"],
                "needs_health_check": llm_pool_status.get("needs_health_check", False),
                "last_health_check": llm_pool_status.get("last_health_check", ""),
                "privacy_warning_keys": privacy_warning_count,
                # v10 新增
                "free_keys": free_keys_count,
                "paid_keys": paid_keys_count,
                "recent_calls_count": recent_calls_count,
                # v12 新增：LKGP 会话粘性摘要
                "session_stickiness_count": status.get("session_stickiness_count", 0),
                "session_stickiness_ttl_seconds": status.get("session_stickiness_ttl_seconds", 0),
                # v13 新增：多维配额跟踪
                "quota_tracking_enabled": status.get("quota_tracking_enabled", False),
                "quota_tracking_dimensions": status.get("quota_tracking_dimensions", []),
                # v13 新增：Provider 级 Circuit Breaker
                "circuit_breaker_enabled": status.get("circuit_breaker_enabled", False),
                "circuit_breaker_open_count": breaker_open,
                "circuit_breaker_half_open_count": breaker_half_open,
                "circuit_breaker_total_providers": breaker_total,
                # v14 新增：消息压缩（OmniRoute RTK + Caveman 借鉴）
                "compression_mode": status.get("compression_mode", "off"),
                "compression_min_length": status.get("compression_min_length", 2000),
                "compression_total": status.get("compression_total", 0),
                "compression_saved_chars": status.get("compression_saved_chars", 0),
                # v14 新增：路由策略（OmniRoute Auto 评分路由借鉴，ADR-0001）
                "routing_strategy": status.get("routing_strategy", "round_robin"),
                "routing_mode_pack": status.get("routing_mode_pack", "balanced"),
                "routing_last_scores_count": len(status.get("routing_last_scores", {})),
                "total_models": sum(len(k.get("models", [])) for k in status.get("keys", [])),
                "free_models": sum(
                    1 for k in status.get("keys", []) for m in k.get("models", []) if m.get("is_free")
                ),
                # v15 新增：Model 级健康度（model_disabled + 退避 + 探针恢复）
                "model_health_enabled": status.get("model_health_enabled", True),
                "model_health_disabled_count": status.get("model_health_disabled_count", 0),
                "model_health_threshold": status.get("model_health_threshold", 5),
                "model_health_probe_interval": status.get("model_health_probe_interval", 300.0),
                # v6-lite-chat-fix T02：/llm/pool/models 端点状态（含 grouped_by_tier）
                "models_endpoint": {
                    "available": True,
                    "path": "/llm/pool/models",
                    "supports_grouped_by_tier": True,
                },
            }
        clear_module_error("llm_pool")
    except Exception as e:
        record_module_error("llm_pool", str(e), type(e).__name__)
    return llm_pool_status


def _get_user_message_status() -> dict:
    """用户消息注入状态"""
    from server.user_message import get_pending
    return {
        "pending_count": len(get_pending()),
        "pending_messages": [m["text"][:100] for m in get_pending()],
    }


def _get_agent_guide_status() -> dict:
    """Agent Guide 状态"""
    from server.agent_guide import get_status as _agent_guide_status
    return _agent_guide_status()


def _get_loops_status() -> dict:
    """Loop 自动触发系统状态"""
    from server.activity_tracker.loop_manager import get_status as _loop_status
    return _loop_status()


def _get_inbox_status() -> dict:
    """收件箱状态"""
    from server.inbox import get_status as _inbox_status
    return _inbox_status()


def _get_project_structure_status() -> dict:
    """项目结构映射 baseline 状态"""
    from server.project_structure import get_status as _ps_status
    return _ps_status()


def _get_auto_shutdown_status() -> dict:
    """自动关机端点状态"""
    from server.auto_shutdown import get_status as _as_status
    return _as_status()


def _get_model_manager_status() -> dict:
    """Model Lifecycle Manager 状态（design §10 低频枚举）

    不放高频计数器（避免 client 指纹漂移，design §10/§5.8）。
    失败时返回最小 stub（不阻塞 /health）。
    """
    try:
        from server.model_manager import get_model_manager
        return get_model_manager().health_summary()
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


# ========== TTL 缓存 ==========

_status_cache: dict = {"data": None, "ts": 0.0}
_status_cache_ttl = 5.0  # 5 秒缓存
_status_cache_lock = threading.Lock()


def invalidate_health_cache() -> None:
    """Invalidate cached health data after externally visible state changes."""
    with _status_cache_lock:
        _status_cache["data"] = None
        _status_cache["ts"] = 0.0


def register_health_routes(app: FastAPI, version: str = VERSION):
    """注册 /health 端点"""

    @app.get("/health", response_model=HealthResponse)
    async def health():
        """全局健康检查 - 汇总所有模块状态"""
        # 5 秒 TTL 缓存，避免频繁调用
        now = time.time()
        with _status_cache_lock:
            if _status_cache["data"] is not None and (now - _status_cache["ts"]) < _status_cache_ttl:
                return _status_cache["data"]

        from server.core.lifecycle import get_start_time
        _st = get_start_time()
        uptime = time.perf_counter() - _st if _st else 0

        # 各模块状态（直接调用函数，避免循环 import）
        # 组件健康状态通过 manifest [health_check] 段动态发现，
        # accounting 字段从动态结果中取值以保持 HealthResponse 向后兼容
        _component_health = _get_component_health_status()
        # browser 状态内含同步 urllib×2（各 timeout=2），包 to_thread 避免阻塞事件循环
        browser_status = await asyncio.to_thread(_get_browser_status)
        result = HealthResponse(
            status="ok",
            version=version,
            uptime_seconds=round(uptime, 1),
            ocr=_get_ocr_status(),
            agent=_get_agent_status(),
            apikey=_get_apikey_status(),
            browser=browser_status,
            exec=_get_exec_status(),
            screen=_get_screen_status(),
            vision=_get_vision_status(),
            memory=_get_memory_status(),
            todos=_get_todos_status(),
            mindforge=_get_mindforge_status(),
            accounting=_component_health.get("accounting", {"review_data_available": False, "item_count": 0}),
            docviewer=_get_docviewer_status(),
            system=_get_system_status(),
            mcp=_get_mcp_status(app),
            llm_pool=_get_llm_pool_status(),
            user_message=_get_user_message_status(),
            agent_guide=_get_agent_guide_status(),
            loops=_get_loops_status(),
            inbox=_get_inbox_status(),
            project_structure=_get_project_structure_status(),
            auto_shutdown=_get_auto_shutdown_status(),
            model_manager=_get_model_manager_status(),
            last_errors=get_module_errors(),
        )

        with _status_cache_lock:
            _status_cache["data"] = result
            _status_cache["ts"] = now

        return result
