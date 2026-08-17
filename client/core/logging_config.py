"""客户端日志系统

配置 root logger，输出到文件 (data/client.log) + 控制台（可选）。
支持 CLIENT_LOG_LEVEL 环境变量启动控制 + 运行时 set_log_level()。
按大小轮转（5MB × 3 备份）。

用法：
    from client.core.logging_config import get_logger
    logger = get_logger("services")
    logger.debug("polling backend...")
    logger.info("backend online")
    logger.warning("request failed: %s", err)

启动控制：
    CLIENT_LOG_LEVEL=DEBUG python -m client.main   # 启动时开 DEBUG
    CLIENT_LOG_CONSOLE=1 python -m client.main      # 同时输出到控制台

运行时控制：
    from client.core.logging_config import set_log_level
    set_log_level("DEBUG")   # 运行时切换级别
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LOG_FILE_PATH = Path(__file__).resolve().parents[2] / "data" / "client.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 3

_initialized = False


def _resolve_level(level_str: str | None) -> int:
    """将字符串/数字转为 logging 级别。默认 INFO。"""
    if not level_str:
        return logging.INFO
    level_str = level_str.upper().strip()
    # 数字
    try:
        return int(level_str)
    except ValueError:
        pass
    # 名称
    return getattr(logging, level_str, logging.INFO)


def setup_logging(level: str | None = None, console: bool | None = None) -> logging.Logger:
    """初始化 root logger。幂等——重复调用只更新级别。

    Args:
        level: 日志级别字符串 (DEBUG/INFO/WARNING/ERROR)，None 则读 CLIENT_LOG_LEVEL 环境变量
        console: 是否输出到控制台，None 则读 CLIENT_LOG_CONSOLE 环境变量
    """
    global _initialized

    if level is None:
        level = os.environ.get("CLIENT_LOG_LEVEL", "INFO")
    if console is None:
        console = os.environ.get("CLIENT_LOG_CONSOLE", "0") == "1"

    numeric_level = _resolve_level(level)
    root = logging.getLogger("client")
    root.setLevel(numeric_level)

    # 避免向 Python root logger 传播（防止 PySide6 内部日志混入）
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    if not _initialized:
        # 文件 handler（轮转）
        _LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE_PATH,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(numeric_level)
        root.addHandler(file_handler)

        if console:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(formatter)
            console_handler.setLevel(numeric_level)
            root.addHandler(console_handler)

        _initialized = True
        root.info("logging initialized: level=%s, file=%s, console=%s",
                  logging.getLevelName(numeric_level), _LOG_FILE_PATH, console)
    else:
        # 已初始化，只更新级别
        for h in root.handlers:
            h.setLevel(numeric_level)
        root.info("logging level changed to %s", logging.getLevelName(numeric_level))

    return root


def set_log_level(level: str) -> None:
    """运行时切换日志级别。"""
    setup_logging(level=level)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger。

    自动确保 logging 已初始化（若未手动调 setup_logging，用默认 INFO 级别）。
    name 建议：模块名（如 "services", "http_client", "panels.dashboard"）。
    """
    if not _initialized:
        setup_logging()
    return logging.getLogger(f"client.{name}")


def get_log_file_path() -> Path:
    """返回日志文件路径。"""
    return _LOG_FILE_PATH
