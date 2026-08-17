"""审批面板路由中间件（lib 纯逻辑层）。

职责：
- heartbeat：跟踪面板进程在线状态（心跳 + 阈值判断）
- store：审批队列内存存储（ApprovalItem + 线程安全 CRUD）
- router：路由决策（面板在线→入队等待，离线→fallback）+ async 等待用户决策

不依赖 server / Qt / FastAPI，可在纯 Python 环境下测试。
server 端调用此包实现 /approvals/* 端点。
"""
from lib.approval_router import heartbeat, router, store
from lib.approval_router.store import ApprovalItem

__all__ = [
    "ApprovalItem",
    "heartbeat",
    "router",
    "store",
]
