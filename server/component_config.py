"""组件配置 Schema 加载器 - 运行时合并组件配置

读取 manifest 声明的 config_schema 入口，加载组件配置 schema 文件，
运行时合并到主 config 对象。

设计原则：
- 配置项以 [component_name] 为 namespace 合并，避免与主 config 冲突
- 删除 workspace/<module>/ 后 manifest 消失，配置自动清除
- manifest enabled=false 时配置仍合并（供其他组件引用），但 4 个插入点不激活
- 不修改主 config.example.toml，组件配置完全由 manifest 管理

用法：
    from server.component_config import load_component_configs, get_merged_config

    # 仅获取组件配置
    component_configs = load_component_configs()

    # 获取主配置 + 组件配置的合并配置
    merged = get_merged_config()
"""

from __future__ import annotations

import logging
import threading

import toml

from server.component_manifest import get_component, load_manifests

logger = logging.getLogger(__name__)

_configs_cache: dict[str, dict] | None = None
_cache_lock = threading.Lock()


def _load_component_config(component_name: str, schema_file: str) -> dict:
    """加载单个组件的 config_schema.toml

    返回以 component_name 为 key 的配置 dict，如 {"test_component": {...}}
    失败时返回空 dict。
    """
    m = get_component(component_name)
    if m is None:
        return {}

    schema_path = m.workspace_dir / schema_file
    if not schema_path.exists():
        logger.warning("组件 %s config_schema 文件不存在: %s", component_name, schema_path)
        return {}

    try:
        data = toml.load(schema_path)
    except Exception as e:
        logger.warning("组件 %s config_schema 解析失败 %s: %s", component_name, schema_path, e)
        return {}

    # 配置项以 component_name 为 namespace 合并
    # 如果 schema 文件已含 [component_name] section，直接用；否则包装一层
    if component_name in data and isinstance(data[component_name], dict):
        return data
    return {component_name: data}


def load_component_configs() -> dict[str, dict]:
    """加载所有组件配置 schema，返回 {component_name: config_dict}

    启动时调用一次，后续调用返回缓存。
    删除 workspace/<module>/ 后需调 reset_cache() 重扫。
    """
    global _configs_cache
    if _configs_cache is not None:
        return _configs_cache
    with _cache_lock:
        if _configs_cache is not None:
            return _configs_cache
        result: dict[str, dict] = {}
        try:
            manifests = load_manifests()
        except Exception as e:
            logger.warning("manifest 加载失败，跳过组件配置合并: %s", e)
            return result
        for name, m in manifests.items():
            if m.config_schema is None:
                continue
            cfg = _load_component_config(name, m.config_schema.file)
            if cfg:
                result.update(cfg)
        _configs_cache = result
    return _configs_cache


def get_component_config(name: str) -> dict | None:
    """按名查询组件配置"""
    return load_component_configs().get(name)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，override 优先（同名 key 取 override 的值）。

    - 同名 key 且双方都是 dict：递归合并
    - 同名 key 但只有一方是 dict：override 整体替换（保留 override 类型）
    - 其他：override 替换 base 的值
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_merged_config() -> dict:
    """获取主配置 + 组件配置的合并配置

    合并策略（重要）：
    - 组件 config_schema.toml 提供默认值（删除 workspace/<module>/ 后自动消失）
    - 主 config.toml 用户实际配置优先，深度覆盖组件默认值
    - 主 config.toml 中已有的 [component_name] section 不被组件 schema 覆盖
    - 主 config.toml 中未设置的组件配置项，由 schema 提供默认值

    合并顺序：component_configs（默认） → 主 config（覆盖）
    """
    from server.config import load_config
    component_configs = load_component_configs()
    main_config = load_config()
    # 组件配置作为默认，主配置覆盖
    merged: dict = {}
    # 先放主配置的所有非组件段
    for k, v in main_config.items():
        merged[k] = v
    # 再处理组件段：组件 schema 提供默认值，主配置中的同名段深度覆盖
    for comp_name, comp_cfg in component_configs.items():
        if comp_name in main_config and isinstance(main_config[comp_name], dict) and isinstance(comp_cfg, dict):
            merged[comp_name] = _deep_merge(comp_cfg, main_config[comp_name])
        else:
            # 主配置未设置此组件，用 schema 默认值
            merged[comp_name] = comp_cfg
    return merged


def reset_cache() -> None:
    """重置组件配置缓存（测试用，或强制重新扫描）"""
    global _configs_cache
    with _cache_lock:
        _configs_cache = None
