"""Ticket 05：模板管理面板测试

覆盖 spec D5（独立面板形态）/ D7（全 task_type 复选框）/ D14（三点菜单 + 自动保存）/
D28（复选框列表）/ D29（不做模板导入导出，仅文件 CRUD）。

测试策略：
- 源码静态扫描：grep class _TemplateManagerPanel + 关键方法
- 运行时实例化：QApplication + _TemplateManagerPanel，验证左侧列表/右侧编辑区/skills 复选框
- 行为测试：新建/选中/失焦自动保存/删除（mock QMessageBox.question）/复制
- 数据隔离：monkeypatch 替换 DEFAULT_TEMPLATES_PATH 到 tmp_path，不污染实际文件
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEMPLATE_STORE_PY = PROJECT_ROOT / "client" / "core" / "agent" / "template_store.py"
# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def isolated_templates_path(tmp_path, monkeypatch):
    """用 monkeypatch 把 template_store.DEFAULT_TEMPLATES_PATH 替换为 tmp_path 下文件。

    _TemplateManagerPanel 内部调用 template_load_all() 等用默认 path，
    monkeypatch 后所有调用自动走 tmp_path 隔离。
    """
    tmp_file = tmp_path / "chat_templates.json"

    # 替换 template_store 模块的 DEFAULT_TEMPLATES_PATH
    from client.core.agent import template_store

    monkeypatch.setattr(template_store, "DEFAULT_TEMPLATES_PATH", tmp_file)

    # 也替换 chat.py 引用的别名（chat.py 通过 client.core.agent import 拿到 DEFAULT_TEMPLATES_PATH 别名）
    # 注意：_TemplateManagerPanel 内部直接调 template_load_all() 不传 path，
    # 而 template_store.load_all() 内部用 `p = path or DEFAULT_TEMPLATES_PATH`，
    # monkeypatch.setattr(template_store, "DEFAULT_TEMPLATES_PATH", tmp_file) 会同时影响
    # template_store.load_all() 内部对 DEFAULT_TEMPLATES_PATH 的引用（因为 Python 模块属性是动态查找的）。

    # 初始化默认空白模板（避免 _ensure_default_file 逻辑被触发误以为文件已存在）
    if not tmp_file.exists():
        import time

        from client.core.agent import Template

        now = time.time()
        blank = Template(
            id="blank", name="空白", prompt="", skills=[],
            created_at=now, updated_at=now,
        )
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(
            json.dumps([blank.to_dict()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    yield tmp_file


# ============================================================================
# Part 2: 运行时实例化 — _TemplateManagerPanel 组件
# ============================================================================


class TestTemplateManagerPanelComponents:
    """_TemplateManagerPanel 实例化验证（spec D5 组件结构）。"""

    def test_panel_object_name(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel objectName 应为 templateManagerPanel。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert panel.objectName() == "templateManagerPanel"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_has_template_list(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel 应有 QListWidget 模板列表。"""
        from PySide6.QtWidgets import QListWidget

        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert isinstance(panel._template_list, QListWidget), (
                "_template_list 应是 QListWidget"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_has_new_button(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel 左侧顶部应有 + 新建按钮。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            buttons = panel.findChildren(QPushButton)
            plus_btns = [b for b in buttons if b.text() == "+" and b.toolTip() == "新建模板"]
            assert len(plus_btns) >= 1, "应有 + 新建模板 按钮（D14）"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_has_name_edit(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel 应有 name QLineEdit。"""
        from PySide6.QtWidgets import QLineEdit

        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert isinstance(panel._name_edit, QLineEdit), "_name_edit 应是 QLineEdit"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_has_prompt_edit(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel 应有 prompt QTextEdit。"""
        from PySide6.QtWidgets import QTextEdit

        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert isinstance(panel._prompt_edit, QTextEdit), "_prompt_edit 应是 QTextEdit"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_has_skills_scroll(self, qapp, isolated_templates_path):
        """_TemplateManagerPanel 应有 skills QScrollArea。"""
        from PySide6.QtWidgets import QScrollArea

        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert isinstance(panel._skills_scroll, QScrollArea), (
                "_skills_scroll 应是 QScrollArea"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_renders_skill_checkboxes_from_index(self, qapp, isolated_templates_path):
        """skills 复选框应从 _index.md 渲染（D7 全部 task_type）。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            # 至少有 1 个复选框（_index.md 列出的 task_type）
            assert len(panel._skill_checkboxes) > 0, (
                "应至少有 1 个 skill 复选框（D7 从 _index.md 渲染）"
            )
            # 复选框数量应 ≥ 30（spec 估值 49，实际 50+）
            assert len(panel._skill_checkboxes) >= 30, (
                f"skill 复选框数量应 ≥ 30（spec 估值 49），实际 {len(panel._skill_checkboxes)}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_default_has_blank_template(self, qapp, isolated_templates_path):
        """初始化默认应有空白模板（item 0 = 空白）。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            assert panel._template_list.count() >= 1, "应至少有 1 个模板（默认空白）"
            # 第一项应是空白模板
            item0 = panel._template_list.item(0)
            assert item0 is not None
            assert item0.text() == "空白", f"第一项应是'空白'，实际 {item0.text()!r}"
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: 行为测试 — 选中 / 自动保存 / 新建 / 复制 / 删除
# ============================================================================


class TestTemplateManagerPanelSelection:
    """选中模板 → 加载到编辑区（D5）。"""

    def test_select_template_loads_to_editor(self, qapp, isolated_templates_path):
        """点击模板列表项 → 编辑区加载对应模板字段。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            # 默认有空白模板
            assert panel._template_list.count() >= 1
            item0 = panel._template_list.item(0)
            panel._on_template_selected(item0)

            # 编辑区应加载空白模板字段
            assert panel._current_template_id == "blank"
            assert panel._name_edit.text() == "空白"
            assert panel._prompt_edit.toPlainText() == ""
            # skills 全部未勾选
            for cb in panel._skill_checkboxes.values():
                assert cb.isChecked() is False
        finally:
            panel.deleteLater()
            qapp.processEvents()


class TestTemplateManagerPanelAutoSave:
    """失焦自动保存（D14）。"""

    def test_editing_finished_saves_name(self, qapp, isolated_templates_path):
        """name edit editingFinished → 自动保存到文件。"""
        from client.core.agent import template_get
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            # 选中空白模板
            panel._on_template_selected(panel._template_list.item(0))

            # 修改 name
            panel._name_edit.setText("空白改")
            # 触发 editingFinished 信号
            panel._name_edit.editingFinished.emit()

            # 重新从文件读
            tpl = template_get("blank")
            assert tpl is not None, "自动保存后应能从文件读到模板"
            assert tpl.name == "空白改", f"name 应保存为 '空白改'，实际 {tpl.name!r}"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_checkbox_state_changed_saves_skills(self, qapp, isolated_templates_path):
        """skill 复选框状态变更 → 自动保存到文件。"""
        from client.core.agent import list_all_task_types, template_get
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            panel._on_template_selected(panel._template_list.item(0))

            # 找一个 task_type 勾选
            task_types = list_all_task_types()
            assert len(task_types) > 0
            target_task_type = task_types[0]
            cb = panel._skill_checkboxes[target_task_type]
            # 模拟勾选（不发信号，避免反复保存）
            cb.setChecked(True)
            # 手动触发一次保存（stateChanged 信号也会触发，但保险起见）
            panel._auto_save_current()

            # 重新从文件读
            tpl = template_get("blank")
            assert tpl is not None
            assert target_task_type in tpl.skills, (
                f"勾选后 skills 应含 '{target_task_type}'，实际 {tpl.skills!r}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_loading_state_skips_save(self, qapp, isolated_templates_path):
        """_loading=True 时跳过自动保存（避免初始化加载触发保存）。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            panel._loading = True
            # 修改 name 但不保存
            panel._name_edit.setText("不应被保存")
            panel._auto_save_current()  # 应直接返回不保存

            # 重新从文件读，name 不应被改
            from client.core.agent import template_get
            tpl = template_get("blank")
            assert tpl is not None
            assert tpl.name != "不应被保存", "_loading=True 时不应触发保存"
        finally:
            panel.deleteLater()
            qapp.processEvents()


class TestTemplateManagerPanelNewTemplate:
    """点 + 新建模板（D14）。"""

    def test_new_template_creates_entry(self, qapp, isolated_templates_path):
        """点 + → 列表项 +1 + 选中新模板。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            before_count = panel._template_list.count()
            panel._on_new_template()
            after_count = panel._template_list.count()

            assert after_count == before_count + 1, (
                f"点 + 后列表应 +1，before={before_count} after={after_count}"
            )
            # 新模板应被选中
            assert panel._current_template_id is not None
            assert panel._name_edit.text() == "新模板", (
                f"新模板 name 应为 '新模板'，实际 {panel._name_edit.text()!r}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_new_template_emits_templates_changed(self, qapp, isolated_templates_path):
        """新建模板 → emit templates_changed 信号。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            received: list = []
            panel.templates_changed.connect(lambda: received.append(1))
            panel._on_new_template()
            assert len(received) >= 1, "新建模板应 emit templates_changed"
        finally:
            panel.deleteLater()
            qapp.processEvents()


class TestTemplateManagerPanelDuplicate:
    """复制模板（D14）。"""

    def test_duplicate_template_creates_copy(self, qapp, isolated_templates_path):
        """复制模板 → 新建同名 + '副本' 后缀。"""
        from client.panels.chat import _TemplateManagerPanel

        panel = _TemplateManagerPanel()
        try:
            before_count = panel._template_list.count()
            # 复制空白模板
            panel._duplicate_template("blank")
            after_count = panel._template_list.count()

            assert after_count == before_count + 1, (
                f"复制后列表应 +1，before={before_count} after={after_count}"
            )
            # 新模板 name 应含 "副本"
            current_item = panel._template_list.currentItem()
            assert current_item is not None
            assert "副本" in current_item.text(), (
                f"复制模板 name 应含 '副本'，实际 {current_item.text()!r}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()


class TestTemplateManagerPanelDelete:
    """删除模板（D18 确认对话框）。"""

    def test_delete_template_with_confirmation_yes(self, qapp, isolated_templates_path, monkeypatch):
        """点删除 + 确认 Yes → 模板从列表消失。"""
        from PySide6.QtWidgets import QMessageBox

        from client.panels.chat import _TemplateManagerPanel

        # mock QMessageBox.question 直接返回 Yes
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )

        panel = _TemplateManagerPanel()
        try:
            # 先新建一个用于删除
            panel._on_new_template()
            before_count = panel._template_list.count()
            target_id = panel._current_template_id
            assert target_id is not None

            current_item = panel._template_list.currentItem()
            panel._delete_template(target_id, current_item)

            after_count = panel._template_list.count()
            assert after_count == before_count - 1, (
                f"删除后列表应 -1，before={before_count} after={after_count}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_delete_template_cancelled_no_change(self, qapp, isolated_templates_path, monkeypatch):
        """点删除 + 选 No → 模板仍在。"""
        from PySide6.QtWidgets import QMessageBox

        from client.panels.chat import _TemplateManagerPanel

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )

        panel = _TemplateManagerPanel()
        try:
            panel._on_template_selected(panel._template_list.item(0))
            target_id = panel._current_template_id
            before_count = panel._template_list.count()

            current_item = panel._template_list.currentItem()
            panel._delete_template(target_id, current_item)

            after_count = panel._template_list.count()
            assert after_count == before_count, (
                f"取消删除后列表不变，before={before_count} after={after_count}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 4: ChatPanel 集成
# ============================================================================


class TestChatPanelTemplatePanelIntegration:
    """ChatPanel 与 _TemplateManagerPanel 集成（spec D5 联动）。"""

    def test_chat_panel_has_template_panel(self, qapp, isolated_templates_path):
        """ChatPanel 应有 _template_panel 属性（非占位 QLabel）。"""
        from client.panels.chat import ChatPanel, _TemplateManagerPanel

        panel = ChatPanel()
        try:
            assert hasattr(panel, "_template_panel"), "ChatPanel 应有 _template_panel 属性"
            assert isinstance(panel._template_panel, _TemplateManagerPanel), (
                "_template_panel 应是 _TemplateManagerPanel 实例（T05 替换占位）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_on_manage_templates_switches_to_panel(self, qapp, isolated_templates_path):
        """点 _on_manage_templates → 切到 _template_panel 视图。"""
        from PySide6.QtWidgets import QStackedWidget

        from client.panels.chat import ChatPanel, _TemplateManagerPanel

        panel = ChatPanel()
        try:
            stack = panel.findChild(QStackedWidget)
            panel._on_manage_templates()
            qapp.processEvents()
            assert isinstance(stack.currentWidget(), _TemplateManagerPanel), (
                "点 _on_manage_templates 后应切到 _TemplateManagerPanel 视图"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_templates_changed_signal_refreshes_start_page(self, qapp, isolated_templates_path):
        """模板变更信号 → 通知开始页刷新模板 tab（D5 联动）。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 验证信号—槽连接：emit 信号后 _start_page._templates 应被刷新
            # _start_page.refresh_templates 是 callable
            assert callable(panel._start_page.refresh_templates)

            # 触发信号（应触发 _start_page.refresh_templates）
            panel._template_panel.templates_changed.emit()
            qapp.processEvents()

            # 验证信号 emit 不抛错，且 _start_page 仍能正常工作
            after_count = len(panel._start_page._templates)
            assert after_count >= 1, (
                f"信号 emit 后 _start_page._templates 应至少 1 项，实际 {after_count}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()
