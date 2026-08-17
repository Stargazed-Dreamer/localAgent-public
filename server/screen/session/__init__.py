"""Session management layer for Computer Use.

三层架构中的会话管理层（Layer 2），负责：
- 权限模式管理（无权限 / 普通执行 / 看门狗）
- 授权生命周期（grant / release / extend / transition）
- 时间策略（idle_warning / idle_grace / watchdog_max_duration）
- 状态变更事件总线（GUI / health 订阅）

依赖方向：
- Layer 1（执行层/路由层）→ 本层（检查权限）
- Layer 3（GUI 覆盖层）→ 本层（订阅事件渲染横条）
- Layer 0（health）→ 本层（查询状态）

不依赖任何上层模块（不 import gui_process / core.health）。
"""

from server.screen.session.events import EventBus, SessionEvent
from server.screen.session.manager import SessionManager, get_session_manager, reset_session_manager
from server.screen.session.modes import Mode
from server.screen.session.policy import TimePolicy
from server.screen.session.state import SessionState

__all__ = [
    "EventBus",
    "Mode",
    "SessionEvent",
    "SessionManager",
    "SessionState",
    "TimePolicy",
    "get_session_manager",
    "reset_session_manager",
]
