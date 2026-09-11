"""_qt_widget_reaper 守卫：测试创建的 Qt widget 必须在 teardown 后被收割。

背景：测试进程无运行事件循环，deleteLater() 的 DeferredDelete 永不执行，
widget 全程滞留 → client_ui 合跑段错误（temp/sdd/qt-segfault-conftest/spec-v2.md）。
本文件用两个测试验证 reaper 的差集收割语义：

- 第一个测试创建带标记的顶层 widget（模拟"只创建不清理"的泄漏测试）
- 第二个测试断言该 widget 已从 allWidgets() 消失

依赖 pytest 按文件内定义顺序执行（同文件内测试保证先后）。
"""

from __future__ import annotations

_probe_widget = None


def test_reaper_leaves_a_marker_widget(qapp):
    """创建一个未显式清理的顶层 widget（reaper 应在 teardown 收割它）。"""
    global _probe_widget
    from PySide6.QtWidgets import QLabel, QWidget

    _probe_widget = QWidget()
    _probe_widget.setObjectName("reaper_probe_widget")
    lay = QLabel("probe", _probe_widget)
    lay.setObjectName("reaper_probe_child")
    _probe_widget.resize(10, 10)


def test_reaper_harvested_marker_widget(qapp):
    """上一个测试的 widget 已被 _qt_widget_reaper 强制销毁。"""
    from PySide6.QtWidgets import QApplication

    assert _probe_widget is not None, "marker 测试未运行（顺序依赖被破坏）"
    alive = [
        w for w in QApplication.allWidgets()
        if w.objectName() in ("reaper_probe_widget", "reaper_probe_child")
    ]
    assert alive == [], (
        f"_qt_widget_reaper 未收割上一测试的 widget: {alive!r}——"
        "滞留 widget 累积是 client_ui 合跑段错误的根因，勿移除该守卫"
    )
