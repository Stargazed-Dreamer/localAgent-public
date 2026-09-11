"""HTTP 客户端单例

封装对 server 的调用。所有方法同步阻塞（timeout=HTTP_TIMEOUT_S），
适合在 QThread 中调用；主线程调用应保持单次快速请求，长任务请走 QThread。

403 审批拦截：post/put/delete 收到 403 + approval_id 时，弹 ConfirmDialog
让用户确认，确认后调 /command-guard/request-approval 获 token，重试原请求
加 X-Approval-Token 头。仅主线程弹窗（后台线程返回 None，避免 Qt 崩溃）。
"""

import threading
import time
from typing import Optional, cast

import requests

from client.core.constants import FAKE_PROXY_URL, HTTP_TIMEOUT_S, SERVER_URL
from client.core.logging_config import get_logger

logger = get_logger("http_client")


# 审批请求超时（/command-guard/request-approval 会 spawn PySide6 子进程弹窗，
# 用户可能需要时间思考，给 120s）
_APPROVAL_TIMEOUT_S = 120.0


class HttpClient:
    """server HTTP 客户端单例"""
    _instance: Optional["HttpClient"] = None
    _instance_lock = threading.Lock()

    def __init__(self, base_url: str = SERVER_URL):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        # 本客户端只访问 127.0.0.1（后端 8766 / fake proxy 9999），
        # 禁止继承系统/环境代理（HTTP_PROXY 等）：外部加速器关闭或换端口时，
        # 走代理的本地探测会静默 ConnectionRefused，表现为服务"拉不起来"。
        self._session.trust_env = False
        # 最近一次请求的 HTTP 状态码（None=网络异常/未发请求）。
        # 供 HttpWorker.status_code 信号透传，让面板区分 404/409/超时等
        self.last_status_code: int | None = None

    @classmethod
    def instance(cls) -> "HttpClient":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（测试用）"""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance._session.close()
                except Exception:
                    pass
            cls._instance = None

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _handle_approval_response(self, resp, method: str, path: str) -> str | None:
        """处理 403 审批响应。

        返回 approval_token（应重试）或 None（不重试/取消/非审批 403）。

        T08 接通：主线程直接弹窗；worker 线程经 ApprovalBridge 把弹窗调度到
        主线程执行（信号跨线程 QueuedConnection），修复此前后台线程 403 静默
        返回 None 导致 loop 面板等 HttpWorker 调用误报"已触发"的问题。
        """
        if resp.status_code != 403:
            return None
        try:
            body = resp.json()
        except Exception:
            return None
        if not isinstance(body, dict) or "approval_id" not in body:
            return None

        logger.info("403 approval required: %s %s approval_id=%s", method, path, body.get("approval_id", ""))

        # Qt 未安装（纯 CLI 场景）：无法弹窗
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            return None
        app = QApplication.instance()
        if app is None:
            return None

        # 主线程：直接弹窗；worker 线程：经 ApprovalBridge 桥接到主线程
        if threading.current_thread() is threading.main_thread():
            return self._run_approval_dialog(body, method, path)

        from client.core.http_worker import get_approval_bridge

        bridge = get_approval_bridge(self)
        # bridge 必须亲和主线程（moveToThread），否则其槽仍在 worker 线程执行，
        # ConfirmDialog 会在非 GUI 线程创建导致 Qt 崩溃
        if bridge.thread() is not threading.main_thread():
            app_thread = app.thread()
            if app_thread is not None:
                bridge.moveToThread(app_thread)
        token = bridge.request_approval(body, method, path)
        if token:
            logger.info("403 approval: approved via bridge, token issued")
        else:
            logger.info("403 approval: denied/cancelled/timeout via bridge")
        return token

    def _run_approval_dialog(self, body: dict, method: str, path: str) -> str | None:
        """在主线程执行审批对话框 + request-approval，返回 token 或 None。

        主线程直调和 ApprovalBridge._run_dialog 共用此实现，避免两份逻辑漂移。
        """
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        # QApplication.instance() 桩标注为 QCoreApplication | None，
        # activeWindow 仅在 QApplication 上存在；运行时单例必为 QApplication
        parent = cast(QApplication, app).activeWindow() if app else None
        approval_id = body.get("approval_id", "")
        message = body.get("message", f"{method} {path} 需要 user 审批")
        detail_parts = [f"approval_id: {approval_id}"]
        if body.get("safety"):
            detail_parts.append(f"safety: {body['safety']}")

        from client.widgets.confirm_dialog import ConfirmDialog
        confirmed = ConfirmDialog.confirm(
            parent,
            title=f"审批请求 — {method} {path}",
            message=message,
            risk_level="warning",
            detail="\n".join(detail_parts),
        )
        if not confirmed:
            logger.info("403 approval: user cancelled")
            return None

        # 调 /command-guard/request-approval（server 会 spawn PySide6 子进程正式审批）
        reason = f"GUI 用户确认执行 {method} {path}"
        try:
            approval_resp = self._session.post(
                self._build_url("/command-guard/request-approval"),
                json={"approval_id": approval_id, "agent_reason": reason},
                timeout=_APPROVAL_TIMEOUT_S,
            )
            if approval_resp.status_code >= 400:
                logger.warning("403 approval: request-approval failed status=%d", approval_resp.status_code)
                return None
            approval_data = approval_resp.json()
        except Exception as e:
            logger.warning("403 approval: request-approval error: %s", e)
            return None

        if not approval_data.get("approved"):
            logger.info("403 approval: denied by user")
            return None
        logger.info("403 approval: approved, token issued")
        return approval_data.get("approval_token", "") or None

    def get(self, path: str, params: dict | None = None,
            base_url: str | None = None, timeout: float | None = None) -> dict | None:
        """GET 请求，失败返回 None"""
        url = (base_url or self.base_url) + path if base_url else self._build_url(path)
        t0 = time.perf_counter()
        try:
            resp = self._session.get(url, params=params, timeout=timeout or HTTP_TIMEOUT_S)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.last_status_code = resp.status_code
            if resp.status_code < 400:
                logger.debug("GET %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
                return resp.json() if resp.content else {}
            logger.warning("GET %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
            return None
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("GET %s → error: %s (%.0fms)", path, e, elapsed_ms)
            return None

    def post(self, path: str, json: dict | None = None,
             base_url: str | None = None, timeout: float | None = None) -> dict | None:
        url = (base_url or self.base_url) + path if base_url else self._build_url(path)
        t0 = time.perf_counter()
        try:
            resp = self._session.post(url, json=json, timeout=timeout or HTTP_TIMEOUT_S)
            self.last_status_code = resp.status_code
            # 403 审批重试（仅一次）
            token = self._handle_approval_response(resp, "POST", path)
            if token:
                resp = self._session.post(
                    url, json=json, timeout=timeout or HTTP_TIMEOUT_S,
                    headers={"X-Approval-Token": token},
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code < 400:
                logger.debug("POST %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
                return resp.json() if resp.content else {}
            logger.warning("POST %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
            return None
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.last_status_code = None
            logger.warning("POST %s → error: %s (%.0fms)", path, e, elapsed_ms)
            return None

    def put(self, path: str, json: dict | None = None,
            timeout: float | None = None) -> dict | None:
        url = self._build_url(path)
        t0 = time.perf_counter()
        try:
            resp = self._session.put(url, json=json, timeout=timeout or HTTP_TIMEOUT_S)
            token = self._handle_approval_response(resp, "PUT", path)
            if token:
                resp = self._session.put(
                    url, json=json, timeout=timeout or HTTP_TIMEOUT_S,
                    headers={"X-Approval-Token": token},
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code < 400:
                logger.debug("PUT %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
                return resp.json() if resp.content else {}
            logger.warning("PUT %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
            return None
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("PUT %s → error: %s (%.0fms)", path, e, elapsed_ms)
            return None

    def patch(self, path: str, json: dict | None = None,
              timeout: float | None = None) -> dict | None:
        url = self._build_url(path)
        t0 = time.perf_counter()
        try:
            resp = self._session.patch(url, json=json, timeout=timeout or HTTP_TIMEOUT_S)
            token = self._handle_approval_response(resp, "PATCH", path)
            if token:
                resp = self._session.patch(
                    url, json=json, timeout=timeout or HTTP_TIMEOUT_S,
                    headers={"X-Approval-Token": token},
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code < 400:
                logger.debug("PATCH %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
                return resp.json() if resp.content else {}
            logger.warning("PATCH %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
            return None
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("PATCH %s → error: %s (%.0fms)", path, e, elapsed_ms)
            return None

    def delete(self, path: str, json: dict | None = None,
               timeout: float | None = None) -> dict | None:
        url = self._build_url(path)
        t0 = time.perf_counter()
        try:
            resp = self._session.delete(url, json=json, timeout=timeout or HTTP_TIMEOUT_S)
            token = self._handle_approval_response(resp, "DELETE", path)
            if token:
                resp = self._session.delete(
                    url, json=json, timeout=timeout or HTTP_TIMEOUT_S,
                    headers={"X-Approval-Token": token},
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code < 400:
                logger.debug("DELETE %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
                return resp.json() if resp.content else {}
            logger.warning("DELETE %s → %d (%.0fms)", path, resp.status_code, elapsed_ms)
            return None
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("DELETE %s → error: %s (%.0fms)", path, e, elapsed_ms)
            return None

    def is_reachable(self) -> bool:
        """探测 server /health 是否可达"""
        try:
            resp = self._session.get(
                self._build_url("/health"), timeout=HTTP_TIMEOUT_S
            )
            return resp.status_code == 200
        except Exception:
            return False

    # —— Fake Proxy 探测 ——

    def fake_proxy_stats(self) -> dict | None:
        """获取 Fake Proxy 统计，未启动返回 None"""
        try:
            resp = self._session.get(
                FAKE_PROXY_URL + "/api/stats", timeout=HTTP_TIMEOUT_S
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def fake_proxy_shutdown(self) -> bool:
        """关闭 Fake Proxy"""
        try:
            resp = self._session.post(
                FAKE_PROXY_URL + "/shutdown", timeout=HTTP_TIMEOUT_S
            )
            return resp.status_code < 400
        except Exception:
            return False

    def fake_proxy_is_reachable(self) -> bool:
        try:
            resp = self._session.get(
                FAKE_PROXY_URL + "/api/stats", timeout=HTTP_TIMEOUT_S
            )
            return resp.status_code == 200
        except Exception:
            return False
