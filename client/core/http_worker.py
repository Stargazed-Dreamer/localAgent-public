"""T08：可复用的 HTTP worker QThread + 主线程审批桥接器。

设计依据：
- 7 个 panel 各自内联定义 RefreshThread/DetailThread 等 QThread 子类（9 个重复样板）
- HttpClient._handle_approval_response 仅主线程弹窗，后台线程 403 静默返回 None
- 迁移 mutation slot 到 QThread 后，403 审批链必须跨线程工作

本模块提供两个组件：

1. ``ApprovalBridge``：主线程审批桥接器（QObject 单例）
   - worker 线程通过 ``request_approval()`` 发射信号到主线程弹 ConfirmDialog
   - 主线程处理完后通过 ``threading.Event`` 通知 worker 线程
   - HttpClient._handle_approval_response 改为调用此桥接器，实现跨线程审批

2. ``HttpWorker``：一次性 HTTP 调用 QThread
   - ``__init__`` 存参数（method/path/json/timeout）
   - ``run()`` 调 ``HttpClient.<method>()``，结果存 ``self.result``
   - ``done`` / ``failed`` 信号回主线程
   - 用法：``worker = HttpWorker("post", "/path", json={...}); worker.done.connect(handle); worker.start()``
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

logger = logging.getLogger("http_worker")


# ─── 主线程审批桥接器 ────────────────────────────────────────────


class ApprovalBridge(QObject):
    """主线程审批桥接器（单例）。

    worker 线程通过 ``request_approval()`` 发射信号到主线程弹 ConfirmDialog，
    主线程处理完后通过 ``threading.Event`` 通知 worker 线程。

    线程安全：``request_approval`` 可从任何线程调用，阻塞直到主线程处理完或超时。
    """

    _request = Signal(object)  # payload tuple: (body, method, path, result_dict, event)

    def __init__(self, http_client: Any) -> None:
        super().__init__()
        self._http = http_client
        # AutoConnection：跨线程发射时自动变 QueuedConnection
        self._request.connect(self._on_request)

    def request_approval(
        self, body: dict, method: str, path: str, *, timeout: float = 130.0
    ) -> str | None:
        """从任何线程调用，阻塞直到主线程处理完。返回 approval_token 或 None。

        Args:
            body: 403 响应体（含 approval_id/message/safety）
            method: HTTP 方法
            path: 请求路径
            timeout: 等待主线程处理的最长时间（秒）
        """
        result: dict[str, Any] = {"token": None}
        event = threading.Event()
        # 发射信号到主线程（跨线程自动 QueuedConnection）
        self._request.emit((body, method, path, result, event))
        if not event.wait(timeout=timeout):
            logger.warning("ApprovalBridge: 等待主线程审批超时 (%.0fs)", timeout)
        return result["token"]

    @Slot(object)
    def _on_request(self, payload: tuple) -> None:
        """主线程槽：弹 ConfirmDialog + 调 request-approval，结果写回 result dict。"""
        body, method, path, result, event = payload
        try:
            result["token"] = self._run_dialog(body, method, path)
        except Exception as e:
            logger.warning("ApprovalBridge: 审批对话框异常: %s", e)
            result["token"] = None
        finally:
            event.set()

    def _run_dialog(self, body: dict, method: str, path: str) -> str | None:
        """在主线程执行审批对话框 + request-approval，返回 token 或 None。

        委托 HttpClient._run_approval_dialog 共享实现（主线程直调路径与
        bridge 路径共用一份逻辑，避免两处漂移）。
        """
        return self._http._run_approval_dialog(body, method, path)


# ─── 模块级 ApprovalBridge 单例 ──────────────────────────────────

_approval_bridge: ApprovalBridge | None = None
_approval_bridge_lock = threading.Lock()


def get_approval_bridge(http_client: Any) -> ApprovalBridge:
    """获取 ApprovalBridge 单例（主线程首次调用时创建）。

    后续调用（含 worker 线程）复用同一实例。
    注意：QObject 亲和其创建线程。若单例首次在 worker 线程被调用时创建，
    调用方（HttpClient._handle_approval_response）负责在弹窗前把 bridge
    moveToThread 到主线程，保证 _on_request 槽经 QueuedConnection 在主线程执行。
    """
    global _approval_bridge
    with _approval_bridge_lock:
        if _approval_bridge is None:
            _approval_bridge = ApprovalBridge(http_client)
        return _approval_bridge


# ─── 一次性 HTTP 调用 QThread ────────────────────────────────────


class HttpWorker(QThread):
    """一次性 HTTP 调用 worker 线程。

    用法::

        worker = HttpWorker("post", "/apikey/keys", json={...}, timeout=15)
        worker.done.connect(self._on_add_done)
        worker.failed.connect(self._on_add_failed)
        worker.start()  # 非阻塞，UI 不冻结

    信号：
        done(dict | None): HTTP 成功（status < 400），传回 JSON 响应（或 None）
        failed(str): HTTP 异常或调用出错，传回错误描述

    注：403 审批通过 ApprovalBridge 自动跨线程处理，调用方无需关心。
    """

    done = Signal(object)  # dict | None
    failed = Signal(str)  # error message
    # HTTP 状态码（status < 400 为 None）。用于区分"网络错误 None"和
    # "HTTP 错误码 None"（如 loop run 的 409 task_busy 需要面板感知）
    status_code = Signal(object)  # int | None

    def __init__(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
        http_client: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._method = method.lower().strip()
        self._path = path
        self._json = json
        self._params = params
        self._timeout = timeout
        self._base_url = base_url
        # http_client：None 时运行时用 HttpClient.instance()（生产），
        # 传入时直接用该实例（测试用 FakeHttp mock）。
        self._http_client = http_client
        self.result: dict | None = None
        self.error: str | None = None

    def run(self) -> None:
        """在 worker 线程执行 HTTP 调用。"""
        try:
            if self._http_client is not None:
                http = self._http_client
            else:
                from client.core.http_client import HttpClient

                http = HttpClient.instance()
            kwargs: dict[str, Any] = {}
            if self._json is not None:
                kwargs["json"] = self._json
            if self._params is not None:
                kwargs["params"] = self._params
            if self._timeout is not None:
                kwargs["timeout"] = self._timeout
            if self._base_url is not None:
                kwargs["base_url"] = self._base_url

            # 8-5（2026-09-13 code review）修复：last_status_code 是 HttpClient 单例的
            # 共享字段，ServiceManager 线程 / BadgeThread / 其他 HttpWorker 并发请求
            # 会在"本 worker 调用完成 → 信号 emit"之间覆盖它。改为调用后立即把状态码
            # 读入局部变量，emit 传该局部变量，把竞态窗口压到调用返回后的相邻几条
            # 字节码（不再跨 emit 排队延迟取值）。
            _status: int | None = None
            method = self._method
            if method == "get":
                self.result = http.get(self._path, **kwargs)
                _status = getattr(http, "last_status_code", None)
            elif method == "post":
                self.result = http.post(self._path, **kwargs)
                _status = getattr(http, "last_status_code", None)
            elif method == "put":
                self.result = http.put(self._path, **kwargs)
                _status = getattr(http, "last_status_code", None)
            elif method == "patch":
                self.result = http.patch(self._path, **kwargs)
                _status = getattr(http, "last_status_code", None)
            elif method == "delete":
                self.result = http.delete(self._path, **kwargs)
                _status = getattr(http, "last_status_code", None)
            else:
                self.error = f"unsupported method: {method}"
                self.failed.emit(self.error)
                return

            # HttpClient 返回 None 表示 HTTP 失败（status >= 400 或异常）
            # 但无法区分"404 返回 None"和"网络错误返回 None"
            # 这里统一用 done 信号传回 result（可能是 None）
            # _status：本次调用刚读出的 HTTP 状态码（供面板区分错误类型）
            self.status_code.emit(_status)
            self.done.emit(self.result)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            logger.warning("HttpWorker %s %s failed: %s", self._method, self._path, self.error)
            self.failed.emit(self.error)
