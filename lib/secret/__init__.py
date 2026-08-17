"""lib/secret — 密钥统一管理中转层.

所有代码读取密钥文件路径或非 LLM 密钥时必须经过此模块。
禁止在代码中硬编码 "data/llm/keys.json" 或 "data/secret/secrets.toml" 等路径。

公开 API:
    路径常量:
        get_project_root() -> Path
        get_llm_keys_path() -> Path
        get_secrets_toml_path() -> Path
        get_config_path() -> Path

    密钥读取（非 LLM）:
        get_secret(key, default=None) -> str | None
        get_secret_or_raise(key) -> str

    异常:
        SecretNotFoundError
        SecretsFileNotFoundError

    测试辅助:
        clear_cache()

设计原则:
    - LLM keys 仍由 server/llm_pool/key_store.py 管理，通过 get_llm_keys_path() 获取路径
    - 非 LLM 密钥（tushare_token / github_token / example_token 等）从
      data/secret/secrets.toml 读取
    - 所有路径常量集中在此模块，路径变化时只需改一处
"""

from lib.secret.paths import (
    get_config_path,
    get_llm_keys_path,
    get_project_root,
    get_secrets_toml_path,
)
from lib.secret.reader import (
    SecretNotFoundError,
    SecretsFileNotFoundError,
    clear_cache,
    get_secret,
    get_secret_or_raise,
)

__all__ = [
    # 路径常量
    "get_project_root",
    "get_llm_keys_path",
    "get_secrets_toml_path",
    "get_config_path",
    # 密钥读取
    "get_secret",
    "get_secret_or_raise",
    # 异常
    "SecretNotFoundError",
    "SecretsFileNotFoundError",
    # 测试辅助
    "clear_cache",
]
