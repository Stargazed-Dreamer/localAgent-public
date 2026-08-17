"""lib/secret 单元测试。

覆盖：
- paths.py：路径常量解析（默认路径 + config.toml 覆盖路径 + 项目根定位）
- reader.py：get_secret / get_secret_or_raise / mtime 缓存 / 异常路径
- 迁移脚本的幂等性（独立测试模块 test_migrate_secrets.py，若需要）

测试模式参考 tests/test_config.py / tests/test_llm_pool.py。
"""

from __future__ import annotations

import time

import pytest

import lib.secret
from lib.secret import (
    SecretNotFoundError,
    SecretsFileNotFoundError,
    clear_cache,
    get_config_path,
    get_llm_keys_path,
    get_project_root,
    get_secret,
    get_secret_or_raise,
    get_secrets_toml_path,
)
from lib.secret import paths as paths_module
from lib.secret import reader as reader_module

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个测试前后清空 reader 缓存，避免测试间污染。"""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def tmp_secrets_toml(tmp_path, monkeypatch):
    """创建临时 secrets.toml，monkeypatch 让 get_secrets_toml_path 指向它。"""
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(
        '[tokens]\n'
        'tushare_token = "test_tushare_123"\n'
        'github_token = "ghp_test_456"\n'
        'example_token = "ghp_pat_789"\n'
        'empty_token = ""\n'
        '[other]\n'
        'foo = "bar"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        paths_module, "get_secrets_toml_path", lambda: secrets_path, raising=True
    )
    # reader 模块中 _load_secrets 直接调 get_secrets_toml_path（局部 import），
    # 需要 patch reader 模块中引用的函数（reader.py 顶部 from lib.secret.paths import get_secrets_toml_path）
    monkeypatch.setattr(
        reader_module, "get_secrets_toml_path", lambda: secrets_path, raising=True
    )
    return secrets_path


# =====================================================================
# paths.py 测试
# =====================================================================


class TestPaths:
    """路径常量解析测试。"""

    def test_get_project_root_returns_localagent_root(self):
        """项目根应该是 localAgent/ 目录。"""
        root = get_project_root()
        assert root.is_dir()
        # 项目根应含 server/ client/ lib/ 等标志性目录
        assert (root / "server").is_dir()
        assert (root / "lib").is_dir()
        assert (root / "config.toml").is_file() or (root / "config.example.toml").is_file()

    def test_get_llm_keys_path_default_when_no_config(self, tmp_path, monkeypatch):
        """config.toml 不存在时回退到默认 data/llm/keys.json。"""
        # 临时项目根，无 config.toml
        fake_root = tmp_path / "fake_project"
        fake_root.mkdir()
        # 模拟 _PROJECT_ROOT 指向 fake_root
        monkeypatch.setattr(paths_module, "_PROJECT_ROOT", fake_root)
        monkeypatch.setattr(paths_module, "_DEFAULT_KEYS_FILE", fake_root / "data" / "llm" / "keys.json")
        monkeypatch.setattr(paths_module, "_DEFAULT_CONFIG_PATH", fake_root / "config.toml")
        path = get_llm_keys_path()
        assert path == fake_root / "data" / "llm" / "keys.json"

    def test_get_llm_keys_path_reads_config_override(self, tmp_path, monkeypatch):
        """config.toml [llm_storage].unified_keys_file 应覆盖默认路径。"""
        fake_root = tmp_path / "fake_project"
        fake_root.mkdir()
        custom_keys_path = "custom/path/keys.json"
        config_content = (
            "[llm_storage]\n"
            f'unified_keys_file = "{custom_keys_path}"\n'
        )
        (fake_root / "config.toml").write_text(config_content, encoding="utf-8")
        monkeypatch.setattr(paths_module, "_PROJECT_ROOT", fake_root)
        monkeypatch.setattr(paths_module, "_DEFAULT_CONFIG_PATH", fake_root / "config.toml")
        path = get_llm_keys_path()
        assert path == fake_root / custom_keys_path

    def test_get_llm_keys_path_handles_absolute_override(self, tmp_path, monkeypatch):
        """config.toml 中绝对路径应原样返回。"""
        fake_root = tmp_path / "fake_project"
        fake_root.mkdir()
        abs_keys_path = tmp_path / "absolute" / "keys.json"
        # TOML basic string 中反斜杠需转义
        abs_str = str(abs_keys_path).replace("\\", "\\\\")
        config_content = (
            "[llm_storage]\n"
            f'unified_keys_file = "{abs_str}"\n'
        )
        (fake_root / "config.toml").write_text(config_content, encoding="utf-8")
        monkeypatch.setattr(paths_module, "_PROJECT_ROOT", fake_root)
        monkeypatch.setattr(paths_module, "_DEFAULT_CONFIG_PATH", fake_root / "config.toml")
        path = get_llm_keys_path()
        assert path == abs_keys_path

    def test_get_llm_keys_path_falls_back_when_llm_storage_missing(self, tmp_path, monkeypatch):
        """config.toml 无 [llm_storage] 段时回退默认。"""
        fake_root = tmp_path / "fake_project"
        fake_root.mkdir()
        (fake_root / "config.toml").write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
        default_path = fake_root / "data" / "llm" / "keys.json"
        monkeypatch.setattr(paths_module, "_PROJECT_ROOT", fake_root)
        monkeypatch.setattr(paths_module, "_DEFAULT_KEYS_FILE", default_path)
        monkeypatch.setattr(paths_module, "_DEFAULT_CONFIG_PATH", fake_root / "config.toml")
        path = get_llm_keys_path()
        assert path == default_path

    def test_get_secrets_toml_path_returns_data_secret_secrets_toml(self):
        """get_secrets_toml_path 返回 data/secret/secrets.toml。"""
        path = get_secrets_toml_path()
        assert path.name == "secrets.toml"
        assert path.parent.name == "secret"
        assert path.parent.parent.name == "data"

    def test_get_config_path_returns_project_config_toml(self):
        """get_config_path 返回项目根的 config.toml。"""
        path = get_config_path()
        assert path.name == "config.toml"


# =====================================================================
# reader.py 测试
# =====================================================================


class TestReaderGetSecret:
    """get_secret() 测试。"""

    def test_get_secret_returns_value_for_existing_key(self, tmp_secrets_toml):
        """存在的 key 返回对应值。"""
        assert get_secret("tushare_token") == "test_tushare_123"
        assert get_secret("github_token") == "ghp_test_456"
        assert get_secret("example_token") == "ghp_pat_789"

    def test_get_secret_returns_default_for_missing_key(self, tmp_secrets_toml):
        """不存在的 key 返回 default。"""
        assert get_secret("nonexistent_key") is None
        assert get_secret("nonexistent_key", "fallback") == "fallback"

    def test_get_secret_returns_default_when_file_missing(self, tmp_path, monkeypatch):
        """secrets.toml 文件不存在时返回 default（不抛出）。"""
        missing_path = tmp_path / "nonexistent.toml"
        monkeypatch.setattr(paths_module, "get_secrets_toml_path", lambda: missing_path)
        monkeypatch.setattr(reader_module, "get_secrets_toml_path", lambda: missing_path)
        assert get_secret("any_key") is None
        assert get_secret("any_key", "default_val") == "default_val"

    def test_get_secret_handles_empty_value(self, tmp_secrets_toml):
        """空字符串值通过 get_secret 返回空字符串（不视为 missing）。"""
        # empty_token = "" 在 toml 中存在但值为空
        result = get_secret("empty_token", default="should_not_use")
        assert result == ""

    def test_get_secret_returns_none_when_tokens_section_missing(self, tmp_path, monkeypatch):
        """secrets.toml 无 [tokens] 段时返回 default。"""
        path = tmp_path / "no_tokens.toml"
        path.write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
        monkeypatch.setattr(paths_module, "get_secrets_toml_path", lambda: path)
        monkeypatch.setattr(reader_module, "get_secrets_toml_path", lambda: path)
        assert get_secret("any_key") is None


class TestReaderGetSecretOrRaise:
    """get_secret_or_raise() 测试。"""

    def test_get_secret_or_raise_returns_value_for_existing_key(self, tmp_secrets_toml):
        """存在的 key 返回对应值。"""
        assert get_secret_or_raise("tushare_token") == "test_tushare_123"

    def test_get_secret_or_raise_raises_when_file_missing(self, tmp_path, monkeypatch):
        """secrets.toml 不存在时抛 SecretsFileNotFoundError。"""
        missing_path = tmp_path / "nonexistent.toml"
        monkeypatch.setattr(paths_module, "get_secrets_toml_path", lambda: missing_path)
        monkeypatch.setattr(reader_module, "get_secrets_toml_path", lambda: missing_path)
        with pytest.raises(SecretsFileNotFoundError):
            get_secret_or_raise("any_key")

    def test_get_secret_or_raise_raises_when_key_missing(self, tmp_secrets_toml):
        """key 不存在时抛 SecretNotFoundError。"""
        with pytest.raises(SecretNotFoundError):
            get_secret_or_raise("nonexistent_key")

    def test_get_secret_or_raise_raises_when_value_empty(self, tmp_secrets_toml):
        """值为空字符串时抛 SecretNotFoundError。"""
        with pytest.raises(SecretNotFoundError):
            get_secret_or_raise("empty_token")


class TestReaderCache:
    """mtime 缓存测试。"""

    def test_cache_returns_same_value_within_mtime(self, tmp_secrets_toml):
        """同一 mtime 下多次读取用缓存。"""
        # 第一次读取加载缓存
        v1 = get_secret("tushare_token")
        assert v1 == "test_tushare_123"
        # 检查缓存已填充
        assert reader_module._cache
        assert reader_module._cache_mtime > 0
        # 第二次读取应命中缓存
        v2 = get_secret("tushare_token")
        assert v2 == v2

    def test_cache_reloads_when_mtime_changes(self, tmp_secrets_toml):
        """文件 mtime 变化后自动重载。"""
        # 第一次读取
        v1 = get_secret("tushare_token")
        assert v1 == "test_tushare_123"
        old_mtime = reader_module._cache_mtime

        # 修改文件（写入新值）
        time.sleep(0.05)  # 确保 mtime 变化
        tmp_secrets_toml.write_text(
            '[tokens]\ntushare_token = "updated_value"\n',
            encoding="utf-8",
        )

        # 第二次读取应重载
        v2 = get_secret("tushare_token")
        assert v2 == "updated_value"
        assert reader_module._cache_mtime > old_mtime

    def test_clear_cache_resets_state(self, tmp_secrets_toml):
        """clear_cache 后缓存为空。"""
        get_secret("tushare_token")
        assert reader_module._cache
        clear_cache()
        assert not reader_module._cache
        assert reader_module._cache_mtime == -1.0


class TestReaderEdgeCases:
    """边界情况测试。"""

    def test_invalid_toml_raises_error(self, tmp_path, monkeypatch):
        """secrets.toml 内容非法时，tomllib 抛出 TOMLDecodeError。"""
        path = tmp_path / "invalid.toml"
        path.write_text("this is not = valid = toml", encoding="utf-8")
        monkeypatch.setattr(paths_module, "get_secrets_toml_path", lambda: path)
        monkeypatch.setattr(reader_module, "get_secrets_toml_path", lambda: path)
        import tomllib
        with pytest.raises(tomllib.TOMLDecodeError):
            get_secret("any_key")

    def test_secret_inheritance(self):
        """SecretNotFoundError 是 KeyError 子类，SecretsFileNotFoundError 是 FileNotFoundError 子类。"""
        assert issubclass(SecretNotFoundError, KeyError)
        assert issubclass(SecretsFileNotFoundError, FileNotFoundError)


# =====================================================================
# 公开 API 导出测试
# =====================================================================


class TestPublicAPI:
    """__init__.py 导出测试。"""

    def test_all_exported_names_importable(self):
        """__all__ 中所有名字都可从 lib.secret 顶层导入。"""
        for name in lib.secret.__all__:
            assert hasattr(lib.secret, name), f"{name} not exported"

    def test_no_private_leak(self):
        """__all__ 不应包含下划线开头的私有名。"""
        for name in lib.secret.__all__:
            assert not name.startswith("_"), f"{name} should be private"
