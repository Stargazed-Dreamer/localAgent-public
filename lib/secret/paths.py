"""密钥文件路径常量。

所有需要密钥文件路径的代码都必须从 lib/secret 导入对应函数，
禁止在代码中硬编码 "data/llm/keys.json" 等路径。

设计：
- 项目根通过 lib/secret/paths.py 自身位置定位（parents[2] = localAgent/）
- get_llm_keys_path() 优先读 config.toml [llm_storage].unified_keys_file，回退默认 data/llm/keys.json
- get_secrets_toml_path() 返回 data/secret/secrets.toml（非 LLM 密钥统一存储）
- get_config_path() 返回 config.toml

与 server/llm_pool/key_store.py 的 _get_unified_keys_path 行为保持一致，
避免双源解析导致路径漂移。
"""

from __future__ import annotations

from pathlib import Path

# 项目根：lib/secret/paths.py → parents[2] = localAgent/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 模块级默认值（config 读取失败时使用）。基于 __file__ 的绝对路径，不依赖 cwd。
_DEFAULT_KEYS_FILE = _PROJECT_ROOT / "data" / "llm" / "keys.json"
_DEFAULT_SECRETS_TOML = _PROJECT_ROOT / "data" / "secret" / "secrets.toml"
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.toml"


def get_project_root() -> Path:
    """返回项目根目录。"""
    return _PROJECT_ROOT


def get_llm_keys_path() -> Path:
    """返回 LLM keys.json 路径。

    优先读 config.toml [llm_storage].unified_keys_file，
    回退到默认 data/llm/keys.json。

    与 server/llm_pool/key_store.py 的 _get_unified_keys_path 行为一致：
    相对路径相对于项目根解析；绝对路径原样返回。
    """
    try:
        import tomllib

        config_path = _DEFAULT_CONFIG_PATH
        if not config_path.exists():
            return _DEFAULT_KEYS_FILE
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        storage = config.get("llm_storage", {})
        if not isinstance(storage, dict):
            return _DEFAULT_KEYS_FILE
        path_str = storage.get("unified_keys_file", "")
        if not path_str or not isinstance(path_str, str):
            return _DEFAULT_KEYS_FILE
        p = Path(path_str)
        if p.is_absolute():
            return p
        return _PROJECT_ROOT / p
    except Exception:
        return _DEFAULT_KEYS_FILE


def get_secrets_toml_path() -> Path:
    """返回 data/secret/secrets.toml 路径（非 LLM 密钥统一存储）。"""
    return _DEFAULT_SECRETS_TOML


def get_config_path() -> Path:
    """返回 config.toml 路径。"""
    return _DEFAULT_CONFIG_PATH
