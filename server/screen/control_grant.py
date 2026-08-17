"""Deprecated re-export shim for backward compatibility.

.. deprecated::
    本模块已弃用，新代码请使用 `server.screen.session.SessionManager`。
    本文件仅保留 `ControlGrantManager` 别名以兼容历史文档/外部脚本引用，
    实际实现已迁移至 `server.screen.session.manager.SessionManager`。

注意：
- 旧 `ControlGrantManager` 的 API（grant(idle_timeout_seconds=)/revoke/
  touch_successful_control/is_watchdog_mode/expire_if_idle 等）与新
  `SessionManager` API（grant(mode=)/release/extend/transition/can_operate
  /can_shutdown 等）**不兼容**。
- 本 shim 仅 re-export 类别名，**不提供旧 API 兼容包装**。
- 若有外部脚本仍用旧 API，需迁移到新 API（参考 tests/test_session_manager.py）。
"""

from __future__ import annotations

from server.screen.session.manager import SessionManager as ControlGrantManager
from server.screen.session.state import SessionState as ControlGrant

__all__ = ["ControlGrant", "ControlGrantManager"]
