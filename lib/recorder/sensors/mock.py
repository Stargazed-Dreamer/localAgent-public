"""L0 测试用 Mock 传感器

同步发预设事件到 controller，用于：
- RecordingController 端到端测试（验证事件汇总、目录结构、meta.json）
- Ticket 01 的 vertical slice 闭环

真实传感器（Ticket 02-05）替换 mock 后即可端到端工作。

设计：
- start() 同步遍历预设事件列表，逐条调 controller.emit_event
- stop() 幂等，可重复调用
- 不起线程，简化测试时序
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.recorder.controller import RecordingController


class MockSensor:
    """Mock 传感器：start() 同步发完所有预设事件。

    Args:
        controller: RecordingController 引用（测试中可后注入）
        events: 预设事件列表，每项是 (kind, payload) 二元组
    """

    def __init__(
        self,
        controller: "RecordingController | None",
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.controller = controller
        self.events = list(events)
        self._stopped = False

    def start(self) -> None:
        """同步发完所有预设事件。"""
        if self.controller is None:
            raise RuntimeError("MockSensor.controller 未注入")
        for kind, payload in self.events:
            self.controller.emit_event(kind, payload)

    def stop(self) -> None:
        """幂等停止。"""
        self._stopped = True
