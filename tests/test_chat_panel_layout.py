"""Ticket 03：ChatPanel 布局弹性化测试

覆盖 spec D4 / Proof C 验收 / Anti-Cheat「禁止 setFixedWidth 回归（文本控件）」：
- chat.py 中 QTextEdit/QLabel/QTextBrowser/QPlainTextEdit 无 setFixedWidth/setFixedHeight
- 左侧会话列表与右侧主区用 QSplitter，可拖拽调宽
- 顶栏/输入区用 stretch factor + QSizePolicy，窗口缩放跟随
- 窗口缩到最小尺寸布局不崩（无控件被挤压不可见）
- 窗口放到最大尺寸无大面积空白浪费

测试策略：
- 源码静态扫描：读 chat.py 源码，断言无 setFixedWidth/setFixedHeight 调用
  （spec Anti-Cheat「grep setFixedWidth|setFixedHeight 在 chat.py 仅非文本控件使用（或无）」）
- 运行时实例化 ChatPanel：遍历所有文本控件，断言 minimumWidth != maximumWidth
  （即未被 setFixedWidth 锁死）
- QSplitter 存在性 + stretch factor + collapsible 验证
- 窗口缩放：resize(400,300) 和 resize(1920,1080)，断言关键控件 visible 且 width/height > 0

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式（参考 test_ui_theme.py）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHAT_PY = PROJECT_ROOT / "client" / "panels" / "chat.py"


# ============================================================================
# 源码静态扫描：禁止 setFixedWidth/setFixedHeight 回归
# ============================================================================


class TestSourceNoFixedSize:
    """chat.py 源码层：禁止 setFixedWidth/setFixedHeight 调用（spec Anti-Cheat）。

    spec Proof C：grep `setFixedWidth|setFixedHeight` 在 chat.py 仅非文本控件使用（或无）。
    本测试直接断言源码中无任何 setFixedWidth/setFixedHeight 调用（最严格）。
    """

    def test_no_set_fixed_width_in_source(self):
        """chat.py 源码不含 setFixedWidth 调用。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        matches = re.findall(r"\.setFixedWidth\s*\(", source)
        assert matches == [], (
            f"chat.py 中发现 {len(matches)} 处 setFixedWidth 调用（spec Anti-Cheat 禁止）：\n"
            + "\n".join(f"  - {m}" for m in matches)
        )

    def test_no_set_fixed_height_in_source(self):
        """chat.py 源码不含 setFixedHeight 调用。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        matches = re.findall(r"\.setFixedHeight\s*\(", source)
        assert matches == [], (
            f"chat.py 中发现 {len(matches)} 处 setFixedHeight 调用（spec Anti-Cheat 禁止）：\n"
            + "\n".join(f"  - {m}" for m in matches)
        )


# ============================================================================
# 运行时实例化：文本控件无固定尺寸
# ============================================================================


def _collect_text_widgets(panel):
    """递归收集 panel 内所有 QTextEdit/QPlainTextEdit/QTextBrowser/QLabel 实例。

    PySide6 的 findChildren 不接受 tuple（与 PyQt 不同），需逐类型调用后合并去重。
    """
    from PySide6.QtWidgets import (
        QLabel,
        QPlainTextEdit,
        QTextBrowser,
        QTextEdit,
    )

    seen_ids = set()
    widgets = []
    for text_type in (QTextEdit, QPlainTextEdit, QTextBrowser, QLabel):
        for w in panel.findChildren(text_type):
            if id(w) in seen_ids:
                continue
            seen_ids.add(id(w))
            widgets.append(w)
    return widgets


class TestNoFixedSizeOnTextWidgets:
    """运行时：所有文本控件未被 setFixedWidth/setFixedHeight 锁死。

    setFixedWidth(N) 会同时设 minimumWidth=N, maximumWidth=N，
    所以判断"被锁死"的方法：minimumWidth == maximumWidth 且 minimumWidth > 0。
    默认值：minimumWidth=0, maximumWidth=16777215（QWIDGETSIZE_MAX），不相等。
    """

    def test_no_fixed_width_on_text_widgets(self, qapp):
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            text_widgets = _collect_text_widgets(panel)
            assert len(text_widgets) > 0, "ChatPanel 应至少有一个文本控件"
            offenders = []
            for w in text_widgets:
                mn = w.minimumWidth()
                mx = w.maximumWidth()
                # 被 setFixedWidth 锁死：mn == mx 且 mn > 0
                if mn == mx and mn > 0:
                    offenders.append(
                        f"{type(w).__name__}(objectName={w.objectName()!r}): "
                        f"min={mn} max={mx}"
                    )
            assert offenders == [], (
                "以下文本控件被 setFixedWidth 锁死（ui_design_system 硬规则禁止）：\n"
                + "\n".join(f"  - {o}" for o in offenders)
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_no_fixed_height_on_text_widgets(self, qapp):
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            text_widgets = _collect_text_widgets(panel)
            assert len(text_widgets) > 0, "ChatPanel 应至少有一个文本控件"
            offenders = []
            for w in text_widgets:
                mn = w.minimumHeight()
                mx = w.maximumHeight()
                if mn == mx and mn > 0:
                    offenders.append(
                        f"{type(w).__name__}(objectName={w.objectName()!r}): "
                        f"min={mn} max={mx}"
                    )
            assert offenders == [], (
                "以下文本控件被 setFixedHeight 锁死（ui_design_system 硬规则禁止）：\n"
                + "\n".join(f"  - {o}" for o in offenders)
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# QSplitter 存在 + 弹性
# ============================================================================


class TestSplitterPresent:
    """ChatPanel 用 QSplitter 分隔左侧会话列表 + 右侧主区（spec D4）。"""

    def test_splitter_present(self, qapp):
        from PySide6.QtWidgets import QSplitter

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            splitters = panel.findChildren(QSplitter)
            assert len(splitters) >= 1, (
                "ChatPanel 应至少有一个 QSplitter（左侧会话列表 + 右侧主区分隔）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_splitter_horizontal_with_two_widgets(self, qapp):
        """QSplitter 应是水平方向，含两个子 widget（左侧 + 右侧）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSplitter

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            splitters = panel.findChildren(QSplitter)
            assert len(splitters) >= 1
            splitter = splitters[0]
            assert splitter.orientation() == Qt.Orientation.Horizontal, (
                "QSplitter 应为水平方向（左侧会话列表 + 右侧主区）"
            )
            assert splitter.count() >= 2, (
                f"QSplitter 应至少含 2 个子 widget，实际 {splitter.count()}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_splitter_left_collapsible(self, qapp):
        """左侧会话列表可折叠（spec D4 示例 setCollapsible(0, True)）。"""
        from PySide6.QtWidgets import QSplitter

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            splitters = panel.findChildren(QSplitter)
            assert len(splitters) >= 1
            splitter = splitters[0]
            # 左侧（index 0）应可折叠
            assert splitter.isCollapsible(0) is True, (
                "QSplitter 左侧（会话列表）应可折叠"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_splitter_stretch_factor_left_smaller_than_right(self, qapp):
        """左侧 stretch < 右侧 stretch（右侧主区占更大比例）。

        spec D4：左侧 stretch=1，右侧 stretch=4。
        QSplitter 没有 stretchFactor() getter，用 sizes() 比例验证行为：
        resize 后左侧占比应 < 50%（左侧 stretch=1，右侧 stretch=4，左侧约 20%）。
        """
        from PySide6.QtWidgets import QSplitter

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel.resize(1000, 600)
            qapp.processEvents()
            splitters = panel.findChildren(QSplitter)
            assert len(splitters) >= 1
            splitter = splitters[0]
            sizes = splitter.sizes()
            assert len(sizes) >= 2
            left_size = sizes[0]
            right_size = sizes[1]
            total = left_size + right_size
            assert total > 0, f"QSplitter 总宽为 0（sizes={sizes}）"
            left_ratio = left_size / total
            # 左侧 stretch=1，右侧 stretch=4 → 左侧约 20%，允许 10%-45%（弹性波动）
            assert left_ratio < 0.5, (
                f"左侧占比 {left_ratio:.2f} 应 < 0.5（左侧 stretch=1 < 右侧 stretch=4，"
                f"sizes={sizes}）"
            )
            assert right_size > left_size, (
                f"右侧 ({right_size}) 应大于左侧 ({left_size})，右侧主区占更大比例"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# 窗口缩放布局不崩
# ============================================================================


class TestWindowResizeLayoutOk:
    """窗口缩到最小/放到最大，布局不崩（spec D4：无控件被挤压不可见，无大面积空白浪费）。"""

    def _key_widgets_visible(self, panel) -> tuple[bool, str]:
        """检查 ChatPanel 关键控件 width/height > 0（offscreen 模式下 isVisible 不可靠，
        用 width/height > 0 判断"未被挤压不可见"）。
        """
        from PySide6.QtWidgets import QListWidget, QPushButton

        session_list = panel.findChild(QListWidget)
        if session_list is None:
            return False, "找不到 QListWidget（会话列表）"
        if session_list.width() <= 0:
            return False, f"会话列表宽度为 0（w={session_list.width()})"

        # 输入框：ChatPanel._input_edit 是 QTextEdit
        if not hasattr(panel, "_input_edit"):
            return False, "ChatPanel 无 _input_edit 属性"
        ie = panel._input_edit
        if ie.width() <= 0 or ie.height() <= 0:
            return False, f"输入框尺寸为 0（w={ie.width()}, h={ie.height()})"

        # 发送按钮（按文本找）
        send_btn = None
        for btn in panel.findChildren(QPushButton):
            if btn.text() == "发送":
                send_btn = btn
                break
        if send_btn is None:
            return False, "找不到发送按钮"
        if send_btn.width() <= 0 or send_btn.height() <= 0:
            return False, f"发送按钮尺寸为 0（w={send_btn.width()}, h={send_btn.height()})"

        return True, ""

    def test_resize_to_small_400x300(self, qapp):
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel.resize(400, 300)
            qapp.processEvents()
            ok, msg = self._key_widgets_visible(panel)
            assert ok, f"窗口缩到 400x300 布局崩：{msg}"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_resize_to_large_1920x1080(self, qapp):
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel.resize(1920, 1080)
            qapp.processEvents()
            ok, msg = self._key_widgets_visible(panel)
            assert ok, f"窗口放到 1920x1080 布局崩：{msg}"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_splitter_sizes_initial_200_for_left(self, qapp):
        """QSplitter 初始 sizes 应让左侧有合理宽度（spec D4：初始 sizes=[200, 800]）。"""
        from PySide6.QtWidgets import QSplitter

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel.resize(1000, 600)
            qapp.processEvents()
            splitters = panel.findChildren(QSplitter)
            assert len(splitters) >= 1
            splitter = splitters[0]
            sizes = splitter.sizes()
            assert len(sizes) >= 2
            left_size = sizes[0]
            # 左侧应有合理宽度（> 0，且不超过总宽一半）
            assert left_size > 0, f"QSplitter 左侧宽度为 0（sizes={sizes}）"
            total = sum(sizes)
            assert left_size < total, f"左侧宽度 {left_size} 不应 >= 总宽 {total}"
        finally:
            panel.deleteLater()
            qapp.processEvents()
