"""LocalAgent 测试共享 fixtures 和环境配置

- sys.path 设置：确保项目根目录可导入
- torch 在 PaddlePaddle 之前加载，避免 Windows DLL 冲突（可选，无 torch 时跳过 GPU 测试）
- 共享 fixtures：app, client, small_image_base64, small_ui_image_base64
- mock_overlay_client（autouse）：避免测试中弹出真实的 PySide6 确认窗口

已知环境噪声（遇到直接忽略，勿当 bug 修）：
- pytest 全量测试通过后（退出码=0），atexit 阶段可能抛 `PermissionError: [WinError 5]`
  on `C:\\Users\\<user>\\AppData\\Local\\Temp\\pytest-of-<user>\\pytest-current`
- 根因：Windows 死符号链接 + pytest `cleanup_dead_symlinks` 行为，非项目代码 bug
- 详见 docs/dev-workflow.md "已知环境噪声：pytest atexit PermissionError（Windows，可忽略）" 章节
"""


# PyTorch 必须在 PaddlePaddle 之前加载，避免 Windows DLL 冲突
# 改为 try-import：无 torch 环境仍可跑非 GPU 测试（标注 @pytest.mark.gpu 的会跳过）
try:
    import torch  # noqa: F401
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

# paddleocr + cv2 完整性检查（修复教训：cv2 能 import 但内容为空时 IMREAD_COLOR 不存在，
# 导致 paddleocr 运行时崩溃。只检查 torch 不够，必须验证 paddleocr 真正可用）
HAS_PADDLE_OCR = False
if HAS_TORCH:
    try:
        import cv2
        # cv2 模块可能存在但内容为空（DLL 加载失败），必须检查关键属性
        _ = cv2.IMREAD_COLOR
        import paddleocr  # noqa: F401
        HAS_PADDLE_OCR = True
    except (ImportError, AttributeError, Exception):
        HAS_PADDLE_OCR = False

import base64  # noqa: E402
import io  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

if TYPE_CHECKING:
    # 仅用于 _BackendSession.__init__ 的类型注解；运行时由 real_client fixture 局部 import
    import requests

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def pytest_configure(config):
    """注册自定义 marker"""
    config.addinivalue_line("markers", "gpu: 标记需要 torch/PaddlePaddle/GPU 的测试")
    config.addinivalue_line(
        "markers",
        "browser: requires real debug browser on CDP :9222 + backend on :8766",
    )
    config.addinivalue_line(
        "markers",
        "real_backend: 需要真实后端运行在 127.0.0.1:8766（TestClient 线程模型与 UIA COM 不兼容）",
    )
    config.addinivalue_line(
        "markers",
        "network: 需要真实公网访问（example.com/bilibili.com 等）",
    )
    config.addinivalue_line(
        "markers",
        "chaos: 混沌/失败注入测试，可能影响系统稳定性",
    )


def pytest_collection_modifyitems(config, items):
    """无 torch 或 paddleocr/cv2 不可用时自动跳过 @pytest.mark.gpu 测试

    教训（2026-08-03）：原实现只检查 HAS_TORCH，但 paddleocr 依赖的 cv2 可能是空壳模块
    （cv2.__file__ is None, dir(cv2) 为空，IMREAD_COLOR 不存在），导致 OCR 测试运行时崩溃。
    必须同时验证 paddleocr + cv2 关键属性可用。
    """
    if HAS_TORCH and HAS_PADDLE_OCR:
        return
    if not HAS_TORCH:
        reason = "无 torch，跳过 GPU 依赖测试"
    else:
        reason = "paddleocr/cv2 不可用（cv2 可能是空壳模块），跳过 OCR 依赖测试"
    skip_gpu = pytest.mark.skip(reason=reason)
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip_gpu)


def pytest_runtest_teardown(item, nextitem):
    """每个测试结束后清理 Qt 状态，防止 QApplication 跨测试累积状态导致 access violation。

    教训（2026-08-03，watchdog-mode E2E 全量回归诊断）：14 个 Qt 测试文件通过
    `QApplication.instance() or QApplication([])` 共享进程级 QApplication 实例，
    累积状态不可控，全量测试时随机崩溃（exit code 3221225477 = STATUS_ACCESS_VIOLATION），
    JUnit XML 无 failure/error 记录（崩溃发生在 pytest 汇总/进程退出阶段）。
    典型表现：单点测试正确，全量测试 fail 后无法排查。

    本 hook 在每个测试结束后清理（F：从 7 个文件的 _cleanup_qt_state autouse fixture
    提取增强，统一全局生效）：
    1. closeAllWindows：关闭所有 top-level 窗口（释放 widget 资源）
    2. sendPostedEvents(None, 0) x3 循环：处理已 post 的事件（包括 deleteLater 的
       QDeferredDeleteEvent）。3 次循环确保延迟删除事件被充分处理（原 7 文件的做法）
    3. gc.collect()：强制回收 Python 对象，释放 Qt 对象引用（原 7 文件的做法）

    设计选择：用 sendPostedEvents 而非 processEvents。processEvents 进入事件循环可能
    触发 offscreen 平台 + WindowStaysOnTopHint/Dialog flag 组合的 access violation
    （参考 test_monitoring_task_authorization.py 的 _mock_qt_offscreen_crashes 注释）。
    sendPostedEvents 只处理已 post 的事件，不等待新事件，不进入事件循环，更安全。

    不调用 app.quit()，避免破坏共享实例（其他测试还要复用）。

    方案对比：
    - pytest-forked：子进程隔离，最彻底，但 Windows 兼容性差（os.fork 模拟）+ 维护停滞
      （2022 年 v1.6 后无更新）+ 子进程崩溃无法捕获堆栈（加剧"无法排查"）→ 不引入
    - 改 module-scoped qapp 为 function-scoped：破坏现有测试，工作量大
    - 本 hook（方案 C）：无新依赖，最小侵入，覆盖跨文件状态累积
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return

    app = QApplication.instance()
    if app is None:
        return

    try:
        app.closeAllWindows()
    except Exception:
        # 普通异常兜底（access violation 是 Windows fatal exception，try/except 抓不住，
        # 但 closeAllWindows 通常不进入事件循环，触发概率低）
        pass

    try:
        from PySide6.QtCore import QCoreApplication
        # 3 次循环处理所有 receiver 的所有事件类型（包括 QDeferredDeleteEvent）
        # 原 7 个文件的 _cleanup_qt_state 用 3 次循环，此处对齐
        for _ in range(3):
            try:
                QCoreApplication.sendPostedEvents(None, 0)
            except Exception:
                break
    except Exception:
        pass

    try:
        import gc
        gc.collect()
    except Exception:
        pass


# ========== Fixtures ==========

@pytest.fixture(autouse=True)
def mock_overlay_client(monkeypatch):
    """自动 mock OverlayClient，避免测试中弹出真实的 PySide6 窗口。

    confirm_action 直接返回"已确认"，show_overlay/hide_overlay 更新状态但不操作 Qt。
    测试仍会走完整的确认代码路径（截图、安全检查、danger_level 判断），
    只是跳过最后的 GUI 弹窗等待环节。

    会话管理层（SessionManager）使用真实逻辑，不 mock 被测对象——
    这样 takeover-persistent 的状态流转测试能验证真实状态机。
    routes.py 直接调 SessionManager.grant/release/status，mock 仅负责 overlay 渲染 + 弹窗。

    不再 mock _ensure_takeover_approved：让真实函数跑，MockGUIClient.ensure_takeover_approved
    返回 skipped 兜底（真实函数收到 skipped → 返回 (True, "")），行为与原 mock 一致。
    真实函数的 persistent_mode 短路（T2 加入）也能被测到。
    """
    # 禁用 agent_guide 向量化语义匹配（避免 xdist 多进程同时加载模型导致 worker crash）
    # 测试只验证关键词匹配逻辑，向量化补强在生产环境生效
    try:
        import server.agent_guide_embedder as _age
        monkeypatch.setattr(_age, "EMBEDDING_ENABLED", False)
    except ImportError:
        pass

    # 每个 test 重置 SessionManager 单例，确保授权状态不跨 test 泄漏
    from server.screen.session import Mode as SessionMode
    from server.screen.session.manager import reset_session_manager
    reset_session_manager()
    # T22 后：副作用端点检查 SessionManager.can_operate()，无授权返回 403。
    # gate 函数被 mock 为总返回 True（不调 grant），所以这里默认 grant normal 授权，
    # 让大多数副作用端点测试无需逐个改签名即可通过。
    # 需要测"无权限 403"的测试显式调 reset_session_manager() 撤销此默认授权。
    from server.screen.session import get_session_manager
    get_session_manager().grant(
        mode=SessionMode.NORMAL,
        task_description="test default",
        source="test",
    )

    class MockGUIClient:
        def __init__(self):
            self.overlay_visible = False
            self.last_overlay_message = ""
            self._started = True
            self._overlay_auto_hide_seconds = 30

        # --- SessionManager 委托（真实逻辑，不 mock）---
        def _session(self):
            from server.screen.session import get_session_manager
            return get_session_manager()

        # --- GUI 弹窗 mock（跳过 Qt）---
        def confirm_action(self, **kwargs):
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        def confirm_start(self, **kwargs):
            return {"status": "confirmed", "task_authorization": False}

        def ensure_takeover_approved(self, *args, **kwargs):
            requested_mode = kwargs.get("requested_mode", "normal")
            if kwargs.get("force_prompt"):
                # 模拟用户勾选 watchdog 复选框（与 requested_mode 对齐，便于测试 watchdog 路径）
                # 真实行为：用户主动勾选才授权 watchdog；测试默认模拟用户同意请求的模式
                return {
                    "status": "confirmed",
                    "reason": "",
                    "task_authorization": True,
                    "requested_mode": requested_mode,
                    "authorized_mode": requested_mode,
                }
            return {
                "status": "skipped",
                "reason": "",
                "task_authorization": self._session().is_active(),
                "requested_mode": requested_mode,
                "authorized_mode": "watchdog" if self._session().status()["mode"] == "watchdog" else "normal",
            }

        def show_overlay(self, message="Agent操作中，请尽量不要使用电脑",
                         position="top", persistent=False):
            self.overlay_visible = True
            self.last_overlay_message = message
            # 注意：show_overlay 不再自动 grant；授权由 routes.py 调 SessionManager.grant 执行

        def hide_overlay(self, *args, **kwargs):
            self.overlay_visible = False
            self.last_overlay_message = ""
            # 注意：hide_overlay 不再自动 release；撤销由 routes.py 调 SessionManager.release 执行

        def poke_overlay(self, *args, **kwargs):
            pass

        def set_overlay_auto_hide_seconds(self, seconds):
            self._overlay_auto_hide_seconds = max(0, int(seconds))

        def start(self):
            pass

        def shutdown(self):
            self._session().shutdown()

    import server.overlay_client
    monkeypatch.setattr(server.overlay_client, "overlay_client", MockGUIClient())

    # 保存真实 _ensure_takeover_approved 供 persistent_mode 短路测试使用（T2）
    try:
        from server.screen import routes as _screen_routes
        if not hasattr(_screen_routes, "_ensure_takeover_approved_orig"):
            _screen_routes._ensure_takeover_approved_orig = _screen_routes._ensure_takeover_approved
        # mock 为总 True（保持原有行为：测试环境无真实 GUI 子进程，fail-closed 会
        # 阻断所有键鼠端点测试；FakeGui 也没 ensure_takeover_approved 方法）
        # persistent_mode 短路逻辑在真实函数里，T2 用 _ensure_takeover_approved_orig 测
        monkeypatch.setattr(_screen_routes, "_ensure_takeover_approved",
                            lambda screen_cfg, task_description: (True, ""))
    except Exception:
        pass


@pytest.fixture(scope="session")
def app():
    """创建 FastAPI 应用（仅一次）"""
    from server.main import app
    return app


@pytest.fixture(scope="session")
def client(app):
    """创建 TestClient（仅一次）"""
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def small_image_base64():
    """生成一张小尺寸带文字的测试图片（200x100，base64编码）"""
    img = Image.new("RGB", (200, 100), (255, 255, 255))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "Hello Test", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ========== 共享 Qt/DB fixtures（F：从 41 个测试文件提取，减少重复定义）==========
# 提取顺序：tmp_db_path（低风险）→ qapp（中风险）→ _cleanup_qt_state（增强现有 hook）
# 调研结论详见 temp/sdd/test-optimization/report.md "F 调研结论"


# QT_QPA_PLATFORM=offscreen：避免测试弹真实窗口（20 个 qapp 文件原本各自 setdefault，
# 提取到 conftest.py 统一设置。setdefault 不覆盖已设置的值，对显式设 offscreen 的文件无影响）
import os as _os  # noqa: E402

_os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def tmp_db_path(tmp_path) -> str:
    """每个测试独立的 SQLite 文件路径（从 18 个测试文件提取的共享 fixture）。

    返回 tmp_path / "test_tmp.db"。原 18 个文件各自用不同文件名
    （headless_test.db / agent_test.db / b2_token_test.db 等），但文件名不在任何
    断言硬编码（经 Grep 确认只出现在 fixture 定义中），统一为中性名无副作用。
    schema 初始化由下游 store fixture 或测试函数内 EventStore(db_path=...).init() 完成。
    """
    return str(tmp_path / "test_tmp.db")


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication（从 23 个测试文件提取的共享 fixture）。

    scope=module 与 22 个原文件一致；test_ui_theme.py 原 scope=session+autouse，
    保留其局部覆盖（视觉渲染需 session 级）。test_client_visual_regressions.py
    需 apply_theme，保留其局部覆盖。

    QT_QPA_PLATFORM=offscreen 在 conftest.py 顶部 setdefault，避免弹真实窗口。
    """
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def small_ui_image_base64():
    """生成一张模拟UI的小尺寸测试图片（400x300，base64编码）
    比1920x1080小14倍，大幅降低内存消耗
    """
    img = Image.new("RGB", (400, 300), (240, 240, 240))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    # 模拟标题栏
    draw.rectangle([10, 10, 390, 40], fill=(0, 120, 215))
    draw.text((20, 15), "Test Window", fill=(255, 255, 255))
    # 模拟按钮
    draw.rectangle([150, 120, 250, 150], fill=(0, 120, 215))
    draw.text((170, 125), "Click Me", fill=(255, 255, 255))
    # 模拟输入框
    draw.rectangle([50, 180, 350, 210], fill=(255, 255, 255), outline=(200, 200, 200))
    draw.text((60, 185), "Type here...", fill=(150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ========== Browser E2E fixtures ==========

@pytest.fixture(scope="session")
def browser_fixture_server():
    """session-scoped 静态 HTTP 服务器，serve tests/fixtures/browser/ 目录。

    用 ThreadingHTTPServer + 随机端口，yield base_url，teardown 关闭。
    所有 browser E2E 测试复用同一服务器实例。
    """
    import http.server
    import socketserver
    import threading

    fixture_dir = PROJECT_ROOT / "tests" / "fixtures" / "browser"
    if not fixture_dir.exists():
        pytest.skip(f"browser fixture dir not found: {fixture_dir}")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(fixture_dir), **kwargs)
        def log_message(self, *args, **kwargs):
            pass  # 静默，避免污染 pytest 输出

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def real_browser():
    """function-scoped 真实浏览器 fixture。

    检查 CDP :9222 可连 + 后端 :8766 可连。可连 yield requests.Session 指向后端，
    不可连 pytest.skip。@pytest.mark.browser 标记的测试使用此 fixture。
    """
    import requests

    # 检查 CDP :9222 可连（调试浏览器）
    try:
        r = requests.get("http://127.0.0.1:9222/json/version", timeout=2)
        if r.status_code != 200:
            pytest.skip("debug browser on CDP :9222 not reachable (non-200)")
    except requests.RequestException as e:
        pytest.skip(f"debug browser on CDP :9222 not running: {e}")

    # 检查后端 :8766 可连
    sess = requests.Session()
    try:
        r = sess.get("http://127.0.0.1:8766/health", timeout=3)
        if r.status_code != 200:
            pytest.skip(f"backend on :8766 not healthy (status={r.status_code})")
    except requests.RequestException as e:
        pytest.skip(f"backend on :8766 not running: {e}")

    yield sess


class _BackendSession:
    """包装 requests.Session，自动补全 base_url，让测试用例可以用相对路径
    （如 `real_client.post("/screen/uia/snapshot", ...)`），与 TestClient 行为一致。

    设计动机：测试用例 `real_client.post("/screen/...")` 用相对路径，但
    requests.Session 不支持相对路径（抛 MissingSchema）。本类在 request 入口
    拦截 URL，若为相对路径则补全 base_url 前缀，无需改测试用例。

    用包装类而非 monkey-patch Session.request，因为后者会破坏 Session 内部
    其他调用路径（如 session.rebuild_auth 等）。
    """

    def __init__(self, base_url: str, session: "requests.Session"):
        self._base_url = base_url.rstrip("/")
        self._session = session

    def _full_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = "/" + url
        return self._base_url + url

    def request(self, method, url, *args, **kwargs):
        return self._session.request(method, self._full_url(url), *args, **kwargs)

    def get(self, url, *args, **kwargs):
        return self._session.get(self._full_url(url), *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._session.post(self._full_url(url), *args, **kwargs)

    def put(self, url, *args, **kwargs):
        return self._session.put(self._full_url(url), *args, **kwargs)

    def delete(self, url, *args, **kwargs):
        return self._session.delete(self._full_url(url), *args, **kwargs)

    def patch(self, url, *args, **kwargs):
        return self._session.patch(self._full_url(url), *args, **kwargs)

    def head(self, url, *args, **kwargs):
        return self._session.head(self._full_url(url), *args, **kwargs)

    def options(self, url, *args, **kwargs):
        return self._session.options(self._full_url(url), *args, **kwargs)

    def close(self):
        self._session.close()


@pytest.fixture
def real_client():
    """function-scoped 真实后端 fixture。

    检查后端 :8766 可连。可连 yield _BackendSession（包装 requests.Session，
    支持相对路径，与 TestClient 行为一致），不可连 pytest.skip。
    @pytest.mark.real_backend 标记的测试使用此 fixture。

    为什么需要 real_client？
      Starlette TestClient 把同步端点放到 anyio 线程池执行，UIA 的
      ControlFromHandle 在该工作线程中会触发 Windows fatal exception
      (access violation)——COM 单元线程模型与 TestClient 线程池不兼容。
      real_client 连真实后端（独立进程，主线程 COM 已正确初始化），无此问题。

    使用方式：
      1. 另开终端启动后端：uv run python -m server.main
      2. 通过 POST /screen/control/request 让用户在弹窗中授权 watchdog 模式
         （real_client 测试不经过 mock_overlay_client，需后端 session 真实激活）
      3. 再跑：uv run python -m pytest tests/test_e2e_computer_use.py -v -m real_backend

    注意：real_client 测试不经过 mock_overlay_client（autouse fixture 只 mock
    TestClient 路径），所以后端 session 必须真实激活——测试进程内的
    SessionManager.grant() 不会跨进程生效。需通过 HTTP 端点授权
    （POST /screen/control/request 会弹真实 PySide6 窗口，用户需手动点击允许）。
    """
    import requests

    base_url = "http://127.0.0.1:8766"
    sess = requests.Session()
    try:
        r = sess.get(f"{base_url}/health", timeout=3)
        if r.status_code != 200:
            pytest.skip(f"backend on :8766 not healthy (status={r.status_code})")
    except requests.RequestException as e:
        pytest.skip(f"backend on :8766 not running: {e}. 启动后端: uv run python -m server.main")

    yield _BackendSession(base_url, sess)
    sess.close()
