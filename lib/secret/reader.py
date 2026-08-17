"""非 LLM 密钥读取层。

从 data/secret/secrets.toml 读取 token 类密钥（tushare_token / github_token /
example_token 等）。LLM keys 仍由 server/llm_pool/key_store.py 管理，通过
lib.secret.get_llm_keys_path() 获取路径。

设计：
- mtime 缓存：文件变化时自动重载（与 server/config.py load_config 模式一致）
- 线程安全：单一 RLock 保护缓存
- 错误处理：
    get_secret() 返回 default（文件不存在或 key 不存在都不抛出）
    get_secret_or_raise() 抛出明确异常（用于必须存在密钥的场景）

secrets.toml schema:
    [tokens]
    tushare_token = "xxx"
    github_token = "yyy"
    example_token = "zzz"
"""

from __future__ import annotations

import os
import threading
import tomllib

from lib.secret.paths import get_secrets_toml_path


class SecretNotFoundError(KeyError):
    """请求的 secret 不存在或为空时抛出。"""


class SecretsFileNotFoundError(FileNotFoundError):
    """secrets.toml 文件不存在时抛出（get_secret_or_raise 场景）。"""


# 模块级缓存：避免每次 get_secret 都重读文件
_cache: dict = {}
_cache_mtime: float = -1.0
_cache_lock = threading.RLock()


def _load_secrets() -> dict:
    """加载 secrets.toml，带 mtime 缓存。

    与 server/config.py load_config 模式一致：
    - 文件不存在返回空 dict（而非抛出，方便 get_secret 返回 default）
    - mtime 未变则用缓存
    - 拿到锁后双检
    """
    global _cache, _cache_mtime
    path = get_secrets_toml_path()
    if not path.exists():
        return {}
    current_mtime = os.path.getmtime(path)
    if current_mtime == _cache_mtime and _cache:
        return _cache
    with _cache_lock:
        # 双检：拿到锁后再确认一次
        current_mtime = os.path.getmtime(path)
        if current_mtime == _cache_mtime and _cache:
            return _cache
        with open(path, "rb") as f:
            data = tomllib.load(f)
        _cache = data
        _cache_mtime = current_mtime
        return _cache


def get_secret(key: str, default: str | None = None) -> str | None:
    """从 secrets.toml [tokens] 段读取密钥。

    Args:
        key: 密钥名（如 "tushare_token" / "github_token" / "example_token"）
        default: key 不存在时的返回值（默认 None）

    Returns:
        密钥值；key 不存在或文件不存在时返回 default。

    Note:
        secrets.toml 文件不存在时也返回 default（不抛出），
        方便首次部署未运行迁移脚本的场景。
    """
    data = _load_secrets()
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        return default
    return tokens.get(key, default)


def get_secret_or_raise(key: str) -> str:
    """从 secrets.toml [tokens] 段读取密钥，不存在时抛出。

    Args:
        key: 密钥名

    Returns:
        密钥值（非空字符串）

    Raises:
        SecretsFileNotFoundError: secrets.toml 文件不存在
        SecretNotFoundError: key 不存在或值为空
    """
    path = get_secrets_toml_path()
    if not path.exists():
        raise SecretsFileNotFoundError(
            f"secrets.toml not found at {path}. "
            "Run `uv run python tools/migrate_secrets.py` to create it."
        )
    data = _load_secrets()
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict) or key not in tokens:
        raise SecretNotFoundError(
            f"Secret '{key}' not found in [tokens] section of {path}"
        )
    value = tokens[key]
    if not isinstance(value, str) or not value.strip():
        raise SecretNotFoundError(
            f"Secret '{key}' is empty or non-string in {path}"
        )
    return value


def clear_cache() -> None:
    """清除内存缓存（测试用）。"""
    global _cache, _cache_mtime
    with _cache_lock:
        _cache = {}
        _cache_mtime = -1.0
