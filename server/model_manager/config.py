"""Model Lifecycle Manager — 配置读取（design §9）

读 config.toml `[model_manager]` 段。schema 验证在 T5 接入
server/config.py 的 _CONFIG_SCHEMAS（本模块先提供默认值齐全的读取器）。
"""

from __future__ import annotations

from server.model_manager.monitor import ResourceMonitorConfig


def get_model_manager_config() -> dict:
    """获取模型存活管理器配置（全部默认值，缺段时整体回退为启用+默认参数）

    配置格式（config.toml）:
        [model_manager]
        enabled = true
        poll_interval_sec = 5.0
        reload_fail_cooldown_sec = 30      # per-model 冷却默认值

        [model_manager.gpu]
        enabled = true
        high_watermark = 0.90
        high_sustain = 3
        critical_watermark = 0.97
        low_watermark = 0.70
        low_sustain = 6
        min_loaded_seconds = 60
        unload_timeout_sec = 15

        [model_manager.cpu]
        enabled = false                    # RAM 主动策略默认关闭（design D2）

        [model_manager.models.<id>]       # 每模型覆盖
        evictable = false
        priority = 50
        reload_fail_cooldown_sec = 30
    """
    from lib.config_reader import load_config

    config = load_config()
    mm = config.get("model_manager", {})
    if not isinstance(mm, dict):
        mm = {}

    poll = float(mm.get("poll_interval_sec", 5.0))
    cooldown_default = float(mm.get("reload_fail_cooldown_sec", 30))

    def _res_section(name: str) -> dict:
        sec = mm.get(name, {})
        return sec if isinstance(sec, dict) else {}

    gpu_sec = _res_section("gpu")
    cpu_sec = _res_section("cpu")

    def _res_cfg(name: str, sec: dict, *, enabled_default: bool) -> ResourceMonitorConfig:
        return ResourceMonitorConfig(
            resource=name,
            enabled=bool(sec.get("enabled", enabled_default)),
            poll_interval_sec=poll,
            high_watermark=float(sec.get("high_watermark", 0.90)),
            high_sustain=int(sec.get("high_sustain", 3)),
            critical_watermark=float(sec.get("critical_watermark", 0.97)),
            low_watermark=float(sec.get("low_watermark", 0.70)),
            low_sustain=int(sec.get("low_sustain", 6)),
        )

    def _min_loaded(sec: dict) -> float:
        return float(sec.get("min_loaded_seconds", 60))

    def _unload_timeout(sec: dict) -> float:
        return float(sec.get("unload_timeout_sec", 15))

    models_sec = mm.get("models", {})
    if not isinstance(models_sec, dict):
        models_sec = {}

    return {
        "enabled": bool(mm.get("enabled", True)),
        "poll_interval_sec": poll,
        "reload_fail_cooldown_sec": cooldown_default,
        "gpu": _res_cfg("gpu", gpu_sec, enabled_default=True),
        "cpu": _res_cfg("cpu", cpu_sec, enabled_default=False),
        "gpu_min_loaded_seconds": _min_loaded(gpu_sec),
        "cpu_min_loaded_seconds": _min_loaded(cpu_sec),
        "gpu_unload_timeout_sec": _unload_timeout(gpu_sec),
        "cpu_unload_timeout_sec": _unload_timeout(cpu_sec),
        "models": {
            k: (v if isinstance(v, dict) else {})
            for k, v in models_sec.items()
        },
    }


def get_model_override(cfg: dict, model_id: str, key: str, default):
    """读取 [model_manager.models.<id>] 的单字段覆盖"""
    return cfg.get("models", {}).get(model_id, {}).get(key, default)


def set_model_override(cfg: dict, model_id: str, key: str, value) -> None:
    """运行时写入 per-model 覆盖（design §9 keep_models 写回链）。

    修改传入的 cfg dict（manager 单例持有），下次 get_model_override 读取立即生效。
    不持久化到 config.toml（运行时覆写，进程重启回退到配置文件值）。
    """
    models = cfg.setdefault("models", {})
    overrides = models.setdefault(model_id, {})
    overrides[key] = value


def is_restore_preload(cfg: dict, model_id: str, default: bool = True) -> bool:
    """读取 [model_manager.models.<id>].restore_preload（design §9）

    默认 True：与 OCR/MindForge 原默认 keep_models=True 行为对齐（D4 向后兼容）。
    """
    return bool(get_model_override(cfg, model_id, "restore_preload", default))


def get_model_cooldown(cfg: dict, model_id: str) -> float:
    """per-model 重载失败冷却（默认取全局，design §7）"""
    return float(get_model_override(
        cfg, model_id, "reload_fail_cooldown_sec", cfg["reload_fail_cooldown_sec"]
    ))


def is_model_evictable(cfg: dict, model_id: str, driver_default: bool) -> bool:
    """配置覆盖优先，未配置用驱动默认值（embedding 默认钉住，design §5.2）"""
    return bool(get_model_override(cfg, model_id, "evictable", driver_default))
