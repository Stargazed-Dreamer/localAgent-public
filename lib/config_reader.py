"""配置读取共享层 - lib 层 config.toml 读取机制（ADR-0027）

提取自 server/config.py。client 和 server 都从此模块读取 config.toml，
消除 client→server 的直接依赖（ADR-0027: Client-Server 模块依赖边界）。

公开 API:
    load_config() -> dict   带 mtime 检查的内存缓存配置读取
    save_config(config)     写入 config.toml 并刷新缓存

server 专属逻辑（get_*_config() 配置读取器、schema 验证、update_config、
脱敏工具）保留在 server/config.py，通过 `from lib.config_reader import load_config`
复用底层读取机制。client 只需 load_config，不依赖任何 server 模块。

设计原则:
    - lib 是共享底层层，不 import server/ 或 client/（避免循环依赖）
    - 单一真源：config.toml 的缓存状态集中在此模块，server.config.save_config
      通过此模块的 save_config 刷新缓存，避免双源缓存漂移
    - 缓存策略：mtime 检查 + 双检锁（与原 server/config.py 行为一致）
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import toml

# 项目根：lib/config_reader.py → parents[1] = localAgent/
# 与 lib/secret/paths.py 的 _PROJECT_ROOT 计算方式一致（parents[N]）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# config.toml 路径（原 server/config.py 中 CONFIG_PATH 的等价计算）
CONFIG_PATH = _PROJECT_ROOT / "config.toml"

# 配置缓存：避免每次调用 load_config() 都 toml.load()
# 模块级状态，server.config 通过导入复用此缓存（单一真源）
_config_cache: dict = {}
_config_mtime: float = -1.0
_config_lock = threading.Lock()


def load_config() -> dict:
    """加载 config.toml 配置（带 mtime 检查的内存缓存）"""
    global _config_cache, _config_mtime
    if not CONFIG_PATH.exists():
        return {}
    current_mtime = os.path.getmtime(CONFIG_PATH)
    if current_mtime == _config_mtime and _config_cache:
        return _config_cache
    with _config_lock:
        # 双检：拿到锁后再确认一次
        current_mtime = os.path.getmtime(CONFIG_PATH)
        if current_mtime == _config_mtime and _config_cache:
            return _config_cache
        _config_cache = toml.load(str(CONFIG_PATH))
        _config_mtime = current_mtime
        return _config_cache


def save_config(config: dict):
    """保存配置到 config.toml（并刷新内存缓存）"""
    global _config_cache, _config_mtime
    CONFIG_PATH.write_text(toml.dumps(config), encoding="utf-8")
    with _config_lock:
        _config_cache = dict(config)
        _config_mtime = os.path.getmtime(CONFIG_PATH)
