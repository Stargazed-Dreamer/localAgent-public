"""L0 传感器抽象协议

所有传感器（M1-M4）实现此协议。
传感器不直接写文件，而是通过 controller.emit_event 上报事件，
由 RecordingController 统一落盘（单一 seam，便于测试）。
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lib.recorder.controller import RecordingController


class Sensor(Protocol):
    """传感器协议：start() 启动监听，stop() 停止并释放资源。

    实现者应在 __init__ 时持有 controller 引用，
    在事件发生时调用 controller.emit_event(kind, payload)。
    """

    controller: "RecordingController"

    def start(self) -> None:
        """启动传感器监听。可能起后台线程。"""
        ...

    def stop(self) -> None:
        """停止监听并释放资源。应可重复调用（幂等）。"""
        ...
