"""面板基类 + 元数据定义

所有面板继承 PanelBase，设置 PANEL_META 类属性即可被 PanelRegistry 自动发现。
未来 brain v3 落地时新增对话/Loop/Goal/Hook 面板，只需在 client/panels/ 加一个文件。
"""

from dataclasses import dataclass
from typing import ClassVar

from PySide6.QtWidgets import QWidget


@dataclass
class PanelMeta:
    """面板元数据"""
    id: str                                  # "dashboard" / "tools" / "chat"（未来）等
    title: str                               # 显示名："概览" / "工具" / "对话"
    icon: str                                # SVG 图标名（lib/ui/icons），未命中则按 emoji 回退
    order: int                               # 显示顺序，数字越小越靠前
    category: str = "main"                   # "main" | "monitor" | "advanced"（导航分组）
    requires_backend: bool = True            # 是否需要 server 在线才能工作
    requires_agent: bool = False             # 是否需要 agent 核心模块（未来用，本期不用）


class PanelBase(QWidget):
    """所有面板的抽象基类。

    子类必须设置 PANEL_META 类属性，定义面板元信息。
    钩子方法 on_show / on_hide / on_refresh / on_backend_status_change 子类按需重写。
    """
    PANEL_META: ClassVar[PanelMeta | None] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_loading = False
        # 活跃 HttpWorker 引用集合，防止被 GC（asyncio.create_task 教训同理）
        # worker.finished 时自动从集合移除
        self._http_workers: set = set()

    def _make_worker(self, method: str, path: str, **kwargs):
        """创建 HttpWorker，绑定 self._http（测试可 mock panel._http）。

        - 生产：worker 在 QThread 中异步执行 HTTP，不阻塞 UI
        - 测试：FakeHttp.put/get 同步返回，worker.run() 仍在线程中执行，
          测试可 worker.wait(timeout) 等待完成

        worker 引用存入 self._http_workers，finished 时自动清理。
        """
        from client.core.http_worker import HttpWorker
        http_client = getattr(self, "_http", None)
        worker = HttpWorker(method, path, http_client=http_client, **kwargs)
        self._http_workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._http_workers.discard(w))
        return worker

    # —— loading 状态 ——

    @classmethod
    def meta(cls) -> PanelMeta:
        if cls.PANEL_META is None:
            raise NotImplementedError(f"{cls.__name__} 必须定义 PANEL_META")
        return cls.PANEL_META

    # —— loading 状态 ——

    def _set_loading(self, loading: bool) -> None:
        """子类调用此方法标记加载状态，触发 on_loading_changed 钩子"""
        if self._is_loading != loading:
            self._is_loading = loading
            self.on_loading_changed(loading)

    def on_loading_changed(self, loading: bool) -> None:
        """加载状态变化时调用（子类按需重写，如更新刷新按钮文本）"""

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    # —— 钩子方法，子类按需重写 ——

    def on_show(self) -> None:
        """切换到此面板时调用"""

    def on_hide(self) -> None:
        """离开此面板时调用"""

    def on_refresh(self) -> None:
        """用户点刷新按钮或自动刷新周期触发"""

    def on_backend_status_change(self, online: bool) -> None:
        """后端在线状态变化时调用"""
