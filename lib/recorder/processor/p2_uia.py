"""P2 UIA 结构化器（接口定义，不实现）

spec-l1.md 第 80-84 行：
    L0 已有 UIA 数据（KeyboardSensor 用 UIA ValuePattern 差值采集输入框文本）
    P2 接口定义：按需采集 UIA 快照的接口（uia_snapshots/*.json），后续需要时实现
    L1 SDD 只定义接口签名，不实现

L1 SDD 范围：仅定义接口签名 + Protocol，不实现具体采集逻辑。
后续 ticket（L2 或 L3）按需实现时，遵守本接口契约。

L0 现状（已实现）：
- lib/uia.py 的 take_focused_value_snapshot() 返回 {"value": str, "is_password": bool}
- KeyboardSensor 在 keystroke 块 flush 时调用，做 start/end value 差值得出输入文本
- 仅取焦点控件的 value，不采集完整 UIA 树

P2 目标（未来实现）：
- 按需采集完整 UIA 快照（控件树 + boundingBox + ControlType + Name + Value）
- 输出到录制包 uia_snapshots/{timestamp}_{seq}.json
- L4 agent 消费时按 block.supplements.uia_snapshot 引用快照文件
- 与 02-block-design.md supplements.uia_snapshot 字段对齐

调用时机（未来）：
- L1 不实时跑 P2（L1 不走 VL/UIA 等慢操作，07-agent-consumption.md 第一节）
- L4 agent 消费录制包时按需调用 P2 采集当前 UIA 状态（"看图说话"的"看 UIA"版本）
- 或 L3 编辑器用户手动点击"采集 UIA"按钮触发
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class UIASnapshotter(Protocol):
    """P2 UIA 结构化器接口契约（L1 SDD 只定义不实现）。

    实现方需遵守：
    1. 采集当前前台窗口的焦点控件（或指定 hwnd 的控件树）
    2. 输出结构化 JSON 快照到录制包 uia_snapshots/ 目录
    3. 返回快照相对路径（相对录制包根），供 block.supplements.uia_snapshot 引用

    实现参考：lib/uia.py 的 take_focused_value_snapshot() 已用 uiautomation 库
    取焦点控件的 ValuePattern，可在此基础上扩展为完整控件树采集。
    """

    def snapshot(
        self,
        package_root: Path,
        timestamp: float,
        *,
        hwnd: int | None = None,
        max_depth: int = 5,
    ) -> str | None:
        """采集 UIA 快照并写入 uia_snapshots/ 目录。

        Args:
            package_root: 录制包根目录（uia_snapshots/ 创建在此目录下）
            timestamp: 事件时间戳（秒，相对录制开始），用于命名快照文件
            hwnd: 目标窗口句柄，None 时取当前前台窗口
            max_depth: 控件树最大采集深度（避免无限递归），默认 5

        Returns:
            快照文件相对路径（如 "uia_snapshots/00001230_000001.json"），
            采集失败时返回 None（不抛异常，由调用方决定降级策略）

        Raises:
            不应抛异常（实现方内部 try/except 兜底，失败返回 None）

        快照 JSON 格式（建议）：
            {
              "timestamp": 1.23,
              "abs_timestamp": 1700000001.23,
              "hwnd": 12345,
              "title": "登录窗口",
              "focused_control": {
                "control_type": "Edit",
                "name": "用户名",
                "value": "admin",
                "bounding_box": {"x": 100, "y": 200, "w": 200, "h": 30},
                "is_password": false
              },
              "tree": [
                {
                  "control_type": "Window",
                  "name": "登录窗口",
                  "bounding_box": {...},
                  "children": [...]
                }
              ],
              "version": "0.1.0"
            }
        """
        ...


def run_p2(
    package_root: Path,
    timestamp: float,
    *,
    snapshotter: UIASnapshotter | None = None,
    hwnd: int | None = None,
) -> str | None:
    """P2 入口（L1 SDD 占位，未实现）。

    Args:
        package_root: 录制包根目录
        timestamp: 事件时间戳（秒）
        snapshotter: UIA 采集器实例，None 时返回 None（L1 不提供默认实现）
        hwnd: 目标窗口句柄

    Returns:
        快照相对路径，未实现时返回 None

    Raises:
        NotImplementedError: L1 SDD 只定义接口不实现，调用方应自行注入 snapshotter
    """
    if snapshotter is None:
        # L1 SDD 不提供默认实现（spec-l1.md Out of Scope：P2 UIA 实现）
        # 调用方需注入 snapshotter 实例，否则返回 None
        return None
    return snapshotter.snapshot(package_root, timestamp, hwnd=hwnd)
