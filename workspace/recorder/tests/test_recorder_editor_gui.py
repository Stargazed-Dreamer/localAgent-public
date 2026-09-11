"""Ticket 26 单测：EditorWindow GUI（用 QTest 测试交互）。

测试覆盖：
- EditorController：操作生成 annotation
- BlockDelegate：状态叠加绘制
- TimelineView：单击选中 / Ctrl+多选 / Shift+范围选
- ChapterList：章节列表点击跳转
- DetailPanel：详情预览
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib.recorder.editor.annotation import Action, Annotation
from lib.recorder.editor.store import AnnotationStore
from workspace.recorder.tools.editor.widgets.chapter_list import ChapterList
from workspace.recorder.tools.editor.widgets.detail_panel import DetailPanel
from workspace.recorder.tools.editor.widgets.timeline_view import TimelineView


def _make_timeline() -> dict:
    return {
        "recording_id": "test_rec",
        "duration_seconds": 10.0,
        "block_count": 4,
        "tracks": ["operation", "image", "text", "chapter"],
        "blocks": [
            {"id": "c001", "category": "text", "type": "chapter", "timestamp": 0.0, "duration": 0.0,
             "primary": {"name": "登录"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
            {"id": "b001", "category": "operation", "type": "mouse_click", "timestamp": 1.2, "duration": 0.05,
             "primary": {"x": 100, "y": 200}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
            {"id": "b002", "category": "operation", "type": "keyboard_input", "timestamp": 1.8, "duration": 0.3,
             "primary": {"text": "admin"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
            {"id": "f001", "category": "image", "type": "screenshot", "timestamp": 1.5, "duration": 0.0,
             "primary": {"frame_path": "frames/001.png"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        ],
    }


# ===== ChapterList =====


class _FakeInsertBlockDialog:
    """T8 后 _on_insert 使用 InsertBlockDialog 替代 QInputDialog.getItem。

    测试中用此类 monkeypatch ``InsertBlockDialog``，构造时传入期望返回的 key
    （"note"/"chapter"/"image"/"text"），``exec()`` 直接返回 Accepted。
    """

    def __init__(self, key: str) -> None:
        self._key = key

    def exec(self):  # noqa: D401 - QDialog API
        from PySide6.QtWidgets import QDialog
        return QDialog.DialogCode.Accepted

    def selected_key(self) -> str:
        return self._key


def _patch_insert_dialog(monkeypatch, key: str) -> None:
    """Patch InsertBlockDialog to return the given key without UI."""
    from workspace.recorder.tools.editor.widgets import insert_block_dialog

    monkeypatch.setattr(
        insert_block_dialog,
        "InsertBlockDialog",
        lambda position_hint, parent=None: _FakeInsertBlockDialog(key),
    )


def test_chapter_list_builds_from_timeline(qapp) -> None:
    timeline = _make_timeline()
    widget = ChapterList(timeline)
    assert widget.count() == 1  # 只有 1 个 chapter 块


def test_chapter_list_click_emits_signal(qapp) -> None:
    timeline = _make_timeline()
    widget = ChapterList(timeline)
    received: list[str] = []
    widget.block_selected.connect(lambda bid: received.append(bid))
    widget.setCurrentRow(0)
    assert received == ["c001"]


def test_chapter_list_labels_empty_name(qapp) -> None:
    """上游章节名为空时显示明确占位，避免左栏出现空行。"""
    timeline = _make_timeline()
    timeline["blocks"][0]["primary"]["name"] = ""

    widget = ChapterList(timeline)

    assert widget.item(0).text() == "未命名章节 [0.0s]"


# ===== TimelineView =====

def test_timeline_view_row_count(qapp) -> None:
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    assert widget._model.rowCount() == 4


def test_timeline_view_selected_block_id(qapp) -> None:
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    # 选中第 1 行（b001）
    index = widget._model.index(1)
    widget.setCurrentIndex(index)
    assert widget.selected_block_id() == "b001"


def test_timeline_view_current_selection_emits_signal(qapp) -> None:
    """键盘、章节跳转和 UIA 改变当前项时都应刷新块详情。"""
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    received: list[str] = []
    widget.block_selected.connect(received.append)

    widget.setCurrentIndex(widget._model.index(2))

    assert received == ["b002"]


def test_timeline_view_selection_only_change_updates_current(qapp) -> None:
    """UIA/读屏只改变 selection 时，详情所用 current 也应跟随。"""
    from PySide6.QtCore import QItemSelectionModel

    widget = TimelineView(_make_timeline(), AnnotationStore())
    widget.select_first_block()
    received: list[str] = []
    widget.block_selected.connect(received.append)

    widget.selectionModel().select(
        widget.model().index(2),
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )

    assert widget.selected_block_id() == "b002"
    assert received == ["b002"]


def test_timeline_view_exposes_accessible_row_text(qapp) -> None:
    """自绘时间轴行也应向 UIA/读屏暴露可理解的名称。"""
    from PySide6.QtCore import Qt

    widget = TimelineView(_make_timeline(), AnnotationStore())

    accessible_text = widget.model().index(1).data(Qt.ItemDataRole.AccessibleTextRole)
    assert accessible_text == "b001，1.2s，点击 (100, 200)"


def test_timeline_view_multi_select(qapp) -> None:
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    # Ctrl+多选第 1 和第 2 行
    from PySide6.QtCore import QItemSelectionModel

    widget.selectionModel().select(widget._model.index(1), QItemSelectionModel.SelectionFlag.Select)
    widget.selectionModel().select(widget._model.index(2), QItemSelectionModel.SelectionFlag.Select)
    ids = widget.selected_block_ids()
    assert set(ids) == {"b001", "b002"}


def test_timeline_view_refresh_preserves_multi_selection(qapp) -> None:
    """annotation 刷新不能丢掉合并操作所需的 Ctrl 多选。"""
    from PySide6.QtCore import QItemSelectionModel

    widget = TimelineView(_make_timeline(), AnnotationStore())
    selection_model = widget.selectionModel()
    selection_model.select(
        widget.model().index(1),
        QItemSelectionModel.SelectionFlag.Select,
    )
    selection_model.select(
        widget.model().index(2),
        QItemSelectionModel.SelectionFlag.Select,
    )
    selection_model.setCurrentIndex(
        widget.model().index(2),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )

    widget.refresh()

    assert widget.selected_block_ids() == ["b002", "b001"]


def test_timeline_view_trimmed_block_remains_selectable_for_restore(qapp) -> None:
    """软裁剪后保留时间轴行，用户仍可选中并恢复。"""
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    assert widget._model.rowCount() == 4

    # trim b002
    store.add(Annotation(id="a001", target_block_id="b002", action=Action.TRIM))
    widget.scroll_to_block("b002")
    widget.refresh()
    assert widget._model.rowCount() == 4
    assert widget.selected_block_id() == "b002"

    # restore b002
    store.add(Annotation(id="a002", target_block_id="b002", action=Action.RESTORE))
    widget.refresh()
    assert widget._model.rowCount() == 4
    assert widget.selected_block_id() == "b002"


def test_timeline_view_scroll_to_block(qapp) -> None:
    timeline = _make_timeline()
    store = AnnotationStore()
    widget = TimelineView(timeline, store)
    widget.scroll_to_block("b002")
    # blocks 顺序：c001(0) b001(1) b002(2) f001(3)
    assert widget.currentIndex().row() == 2  # b002 在第 3 行


# ===== DetailPanel =====

def test_detail_panel_show_block(qapp, tmp_path: Path) -> None:
    panel = DetailPanel()
    block = {
        "id": "b001",
        "type": "mouse_click",
        "timestamp": 1.234,
        "duration": 0.056,
        "primary": {"x": 100, "y": 200, "frame_path": ""},
    }
    panel.show_block(block, tmp_path)
    assert panel.id_label.text() == "b001"
    assert panel.type_label.text() == "mouse_click"
    assert "1.234" in panel.ts_label.text()


def test_detail_panel_no_frame(qapp, tmp_path: Path) -> None:
    """无截图块显示 EmptyState 占位（02-editor §5.1）。"""
    panel = DetailPanel()
    block = {"id": "b001", "type": "keyboard_input", "timestamp": 1.0, "duration": 0.1,
             "primary": {"text": "hello"}}
    panel.show_block(block, tmp_path)
    # 无截图时帧计数标签显示"本块无截图"，frame_label 显示 EmptyState 图标
    assert panel._frame_count_label.text() == "本块无截图"
    assert panel.frame_label.pixmap() is not None
    assert panel.frame_label.pixmap().isNull() is False
    # keyboard_input 非可编辑类型，text_edit 只读但显示文本
    assert panel.text_edit.toPlainText() == "hello"
    assert panel.text_edit.isReadOnly() is True


def test_detail_panel_shows_operation_evidence_and_fields(qapp, tmp_path: Path) -> None:
    """操作块显示 supplements 中的截图；未知字段进原始数据折叠区（02-editor §5.2）。"""
    from PySide6.QtGui import QImage

    frame_path = tmp_path / "frames" / "001.png"
    frame_path.parent.mkdir()
    assert QImage(32, 24, QImage.Format.Format_RGB32).save(str(frame_path))

    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200, "button": "left"},
            "supplements": {"before_frame": "frames/001.png"},
        },
        tmp_path,
    )

    # 截图预览
    assert panel.frame_label.pixmap() is not None
    assert panel.frame_label.pixmap().isNull() is False
    # mouse_click 无 text/name，内容区为空（不再显示裸 JSON）
    assert panel.text_edit.toPlainText() == ""
    # 未知字段（x/y/button）进入原始数据折叠区
    assert panel._raw_toggle.isHidden() is False
    raw_text = panel._raw_edit.toPlainText()
    assert '"x": 100' in raw_text
    assert '"button": "left"' in raw_text
    # 默认收起
    assert panel._raw_edit.isHidden() is True
    assert panel._raw_toggle.isChecked() is False


def test_detail_panel_stt_block_content_editable(qapp, tmp_path: Path) -> None:
    """STT 转写/用户注释/参考文本类块的内容区可编辑（02-editor §5.2）。"""
    panel = DetailPanel()
    for block_type in ("stt_transcript", "user_note", "reference_text"):
        panel.show_block(
            {
                "id": "b001",
                "type": block_type,
                "timestamp": 1.0,
                "duration": 0.1,
                "primary": {"text": "示例文本"},
            },
            tmp_path,
        )
        assert panel.text_edit.isReadOnly() is False
        assert panel.text_edit.toPlainText() == "示例文本"


def test_detail_panel_chips_reflect_block_status(qapp, tmp_path: Path) -> None:
    """状态 chips 应反映块的 marked_* 状态。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200},
            "status": {
                "marked_key": True,
                "marked_anomaly": False,
                "marked_automatable": True,
                "is_trimmed": False,
            },
        },
        tmp_path,
    )
    assert panel._chips["key"].isChecked() is True
    assert panel._chips["anomaly"].isChecked() is False
    assert panel._chips["automatable"].isChecked() is True
    # 非 trimmed 块 chips 可用
    for btn in panel._chips.values():
        assert btn.isEnabled() is True


def test_detail_panel_chips_disabled_for_trimmed_block(qapp, tmp_path: Path) -> None:
    """已移除块的 chips 应禁用（无标记意义）。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200},
            "status": {
                "marked_key": False,
                "marked_anomaly": False,
                "marked_automatable": False,
                "is_trimmed": True,
            },
        },
        tmp_path,
    )
    for btn in panel._chips.values():
        assert btn.isEnabled() is False


def test_detail_panel_chip_toggled_emits_signal(qapp, tmp_path: Path) -> None:
    """chip 点击应发出 chip_toggled 信号。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200},
            "status": {
                "marked_key": False,
                "marked_anomaly": False,
                "marked_automatable": False,
                "is_trimmed": False,
            },
        },
        tmp_path,
    )
    received: list[tuple[str, bool]] = []
    panel.chip_toggled.connect(lambda name, checked: received.append((name, checked)))

    # 用户点击 key chip → checked=True
    panel._chips["key"].setChecked(True)
    assert received == [("key", True)]

    # 再点击 → checked=False
    panel._chips["key"].setChecked(False)
    assert received == [("key", True), ("key", False)]


def test_detail_panel_sync_chips_does_not_reemit(qapp, tmp_path: Path) -> None:
    """外部 sync_chips 同步状态时不应重发 chip_toggled（避免循环）。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200},
            "status": {
                "marked_key": False,
                "marked_anomaly": False,
                "marked_automatable": False,
                "is_trimmed": False,
            },
        },
        tmp_path,
    )
    received: list[tuple[str, bool]] = []
    panel.chip_toggled.connect(lambda name, checked: received.append((name, checked)))

    panel.sync_chips({"marked_key": True, "marked_anomaly": True, "marked_automatable": False})
    assert received == []  # 不应发出信号
    assert panel._chips["key"].isChecked() is True
    assert panel._chips["anomaly"].isChecked() is True


def test_detail_panel_frame_click_emits_viewer_request(qapp, tmp_path: Path) -> None:
    """截图区点击应发出 open_viewer_requested 信号（02-editor §5.1）。"""
    from PySide6.QtGui import QImage

    frame_path = tmp_path / "frames" / "001.png"
    frame_path.parent.mkdir()
    assert QImage(32, 24, QImage.Format.Format_RGB32).save(str(frame_path))

    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "screenshot",
            "timestamp": 1.0,
            "duration": 0.0,
            "primary": {"frame_path": "frames/001.png"},
        },
        tmp_path,
    )
    received: list[tuple[list, int]] = []
    panel.open_viewer_requested.connect(
        lambda paths, idx: received.append((list(paths), idx))
    )

    panel._on_frame_click()
    assert len(received) == 1
    paths, idx = received[0]
    assert len(paths) == 1
    assert paths[0].name == "001.png"
    assert idx == 0


def test_detail_panel_frame_navigation(qapp, tmp_path: Path) -> None:
    """多帧块的前后帧导航（02-editor §5.1）。"""
    from PySide6.QtGui import QImage

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for i in range(3):
        assert QImage(32, 24, QImage.Format.Format_RGB32).save(
            str(frames_dir / f"frame_{i:03d}.png")
        )

    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "screenshot",
            "timestamp": 1.0,
            "duration": 0.0,
            "primary": {"frame_path": "frames/frame_000.png"},
            "supplements": {
                "before_frame": "frames/frame_001.png",
                "after_frame": "frames/frame_002.png",
            },
        },
        tmp_path,
    )

    # 初始：第 1/3 帧
    assert panel._frame_index == 0
    assert "1/3" in panel._frame_count_label.text()
    assert panel._prev_frame_btn.isEnabled() is False
    assert panel._next_frame_btn.isEnabled() is True

    # 下一帧
    panel._on_next_frame()
    assert panel._frame_index == 1
    assert "2/3" in panel._frame_count_label.text()
    assert panel._prev_frame_btn.isEnabled() is True
    assert panel._next_frame_btn.isEnabled() is True

    # 下一帧
    panel._on_next_frame()
    assert panel._frame_index == 2
    assert "3/3" in panel._frame_count_label.text()
    assert panel._next_frame_btn.isEnabled() is False

    # 上一帧
    panel._on_prev_frame()
    assert panel._frame_index == 1


def test_detail_panel_raw_data_toggle(qapp, tmp_path: Path) -> None:
    """原始数据折叠区展开/收起（02-editor §5.2）。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200, "button": "left"},
        },
        tmp_path,
    )

    # 默认收起
    assert panel._raw_toggle.isHidden() is False
    assert panel._raw_edit.isHidden() is True
    assert panel._raw_toggle.isChecked() is False

    # 展开
    panel._raw_toggle.setChecked(True)
    assert panel._raw_edit.isHidden() is False
    assert "▾" in panel._raw_toggle.text()

    # 收起
    panel._raw_toggle.setChecked(False)
    assert panel._raw_edit.isHidden() is True
    assert "▸" in panel._raw_toggle.text()


def test_detail_panel_raw_data_hidden_when_no_extras(qapp, tmp_path: Path) -> None:
    """无未知字段的块不显示原始数据折叠区。"""
    panel = DetailPanel()
    panel.show_block(
        {
            "id": "b001",
            "type": "mouse_click",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"x": 100, "y": 200, "text": "点击"},
        },
        tmp_path,
    )
    # x/y 不在 _KNOWN_PRIMARY，会进原始数据区
    # 但若只移除所有未知字段则不显示
    panel.show_block(
        {
            "id": "b001",
            "type": "user_note",
            "timestamp": 1.0,
            "duration": 0.1,
            "primary": {"text": "纯文本注释"},
        },
        tmp_path,
    )
    assert panel._raw_toggle.isHidden() is True
    assert panel._raw_edit.isHidden() is True


# ===== BlockDelegate 绘制（间接验证：状态叠加不崩）=====

def test_block_delegate_state_overlay(qapp) -> None:
    """验证状态叠加逻辑：多状态共存时 annotation apply 正确（绘制在 GUI 中验证）。"""
    from workspace.recorder.tools.editor.widgets.timeline_view import TimelineModel

    timeline = _make_timeline()
    store = AnnotationStore()
    # 添加多种状态到 b001
    store.add(Annotation(id="a001", target_block_id="b001", action=Action.MARK_KEY))
    store.add(Annotation(id="a002", target_block_id="b001", action=Action.MARK_ANOMALY))
    store.add(Annotation(id="a003", target_block_id="b001", action=Action.MARK_AUTOMATABLE))

    model = TimelineModel(timeline, store)
    # b001 在第 2 行（c001=0, b001=1）
    b001 = model.block_at(1)
    assert b001["status"]["marked_key"] is True
    assert b001["status"]["marked_anomaly"] is True
    assert b001["status"]["marked_automatable"] is True


# ===== EditorController（通过 EditorWindow 测试）=====

def test_editor_window_add_annotation(qapp, tmp_path: Path) -> None:
    """EditorWindow 工具栏操作生成 annotation。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    timeline = _make_timeline()
    store = AnnotationStore()
    window = EditorWindow(package_path=tmp_path, timeline=timeline, store=store)

    # 选中 b001
    index = window.timeline_view._model.index(1)
    window.timeline_view.setCurrentIndex(index)

    # 点击标关键
    window._on_mark_key_toggle(True)

    assert len(store.annotations) == 1
    assert store.annotations[0].action == Action.MARK_KEY
    assert store.annotations[0].target_block_id == "b001"


def test_editor_window_initial_selection_populates_detail(qapp, tmp_path: Path) -> None:
    """窗口打开时首个时间轴块应同步到详情面板。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )

    assert window.timeline_view.selected_block_id() == "c001"
    assert window.detail_panel.id_label.text() == "c001"


def test_editor_window_annotation_refreshes_chapters_and_detail(qapp, tmp_path: Path) -> None:
    """annotation 生效后三区域应展示同一份编辑视图。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )

    window._add_annotation(Action.RENAME_CHAPTER, "c001", {"new_name": "账号登录"})

    assert window.chapter_list.item(0).text() == "账号登录 [0.0s]"
    assert window.detail_panel.text_edit.toPlainText() == "账号登录"


def test_editor_window_remove_block_toggle_emits_trim_then_untrim(qapp, tmp_path: Path) -> None:
    """移除块 toggle：未移除→点击→TRIM 标注；已移除→点击→UNTRIM 标注（02-editor §2.2）。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    # 选中 b001
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(1))

    # 初始：未移除，动作 unchecked，文本=移除块
    assert window._actions["remove_block"].isChecked() is False
    assert window._actions["remove_block"].text() == "移除块"

    # 点击 toggle（目标态=checked=True）→ 追加 TRIM
    window._on_remove_block_toggle(True)
    assert window.store.active_annotations[-1].action == Action.TRIM
    # 刷新后块状态 is_trimmed=True → 动作 checked，文本=恢复块，图标=eye
    assert window._actions["remove_block"].isChecked() is True
    assert window._actions["remove_block"].text() == "恢复块"

    # 再点击 toggle（目标态=checked=False）→ 追加 UNTRIM
    window._on_remove_block_toggle(False)
    assert window.store.active_annotations[-1].action == Action.UNTRIM
    # 刷新后块状态 is_trimmed=False → 动作 unchecked，文本=移除块，图标=eye-off
    assert window._actions["remove_block"].isChecked() is False
    assert window._actions["remove_block"].text() == "移除块"


def test_editor_window_mark_toggles_emit_unmark_when_unchecking(qapp, tmp_path: Path) -> None:
    """标关键 toggle：未标→点击→MARK_KEY；已标→点击→UNMARK_KEY（02-editor §2.2 标记幂等）。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(1))

    # 点击 mark_key（目标态=True）→ MARK_KEY
    window._on_mark_key_toggle(True)
    assert window.store.active_annotations[-1].action == Action.MARK_KEY
    assert window._actions["mark_key"].isChecked() is True

    # 再点击（目标态=False）→ UNMARK_KEY
    window._on_mark_key_toggle(False)
    assert window.store.active_annotations[-1].action == Action.UNMARK_KEY
    assert window._actions["mark_key"].isChecked() is False

    # 标异常、标可自动化同理
    window._on_mark_anomaly_toggle(True)
    assert window.store.active_annotations[-1].action == Action.MARK_ANOMALY
    window._on_mark_anomaly_toggle(False)
    assert window.store.active_annotations[-1].action == Action.UNMARK_ANOMALY

    window._on_mark_automatable_toggle(True)
    assert window.store.active_annotations[-1].action == Action.MARK_AUTOMATABLE
    window._on_mark_automatable_toggle(False)
    assert window.store.active_annotations[-1].action == Action.UNMARK_AUTOMATABLE


def test_editor_window_sync_checkable_states_from_block_status(qapp, tmp_path: Path) -> None:
    """选中块变化时 checkable 动作的 checked 态应同步块状态（旧包加载回显）。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    # 给 b001 加上多状态
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(1))
    window._on_mark_key_toggle(True)
    window._on_mark_anomaly_toggle(True)
    window._on_remove_block_toggle(True)

    # 切换到 b002（无状态），动作应全部 unchecked
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(2))
    assert window._actions["mark_key"].isChecked() is False
    assert window._actions["mark_anomaly"].isChecked() is False
    assert window._actions["remove_block"].isChecked() is False
    assert window._actions["remove_block"].text() == "移除块"

    # 切回 b001，动作应回显 b001 的状态
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(1))
    assert window._actions["mark_key"].isChecked() is True
    assert window._actions["mark_anomaly"].isChecked() is True
    assert window._actions["remove_block"].isChecked() is True
    assert window._actions["remove_block"].text() == "恢复块"


def test_editor_window_status_bar_uses_yiyichu_not_caijian(qapp, tmp_path: Path) -> None:
    """状态栏文案应使用「已移除」而非「裁剪」（02-editor §3，T4 验收点）。"""
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    window._update_status_bar()
    assert "已移除" in window.statusBar().currentMessage()
    assert "裁剪" not in window.statusBar().currentMessage()


def test_editor_window_finalize_toggle_checks_state_and_persists(qapp, tmp_path: Path) -> None:
    """就绪 toggle：checking 触发 finalize + 持久化；unchecking 触发 unfinalize。"""
    from PySide6.QtWidgets import QMessageBox

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    timeline = _make_timeline()
    store = AnnotationStore()
    window = EditorWindow(package_path=tmp_path, timeline=timeline, store=store)

    # mock 确认对话框返回 Yes
    QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    # checking → finalize
    window._on_finalize_toggle(True)
    assert store.finalized is True
    assert window._actions["finalize"].isChecked() is True
    assert window._actions["finalize"].text() == "已就绪"
    assert (tmp_path / "annotations.json").exists()

    # unchecking → unfinalize
    window._on_finalize_toggle(False)
    assert store.finalized is False
    assert window._actions["finalize"].isChecked() is False
    assert window._actions["finalize"].text() == "标记就绪"


def test_editor_window_finalize_cancel_reverts_checked(qapp, tmp_path: Path) -> None:
    """用户在就绪确认对话框点取消时，checked 态应回退（不修改 store）。"""
    from PySide6.QtWidgets import QMessageBox

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    timeline = _make_timeline()
    store = AnnotationStore()
    window = EditorWindow(package_path=tmp_path, timeline=timeline, store=store)

    # mock 确认对话框返回 No
    QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No)

    window._on_finalize_toggle(True)
    assert store.finalized is False
    assert window._actions["finalize"].isChecked() is False


def test_editor_window_repeated_inserts_use_unique_block_ids(qapp, tmp_path: Path, monkeypatch) -> None:
    """连续插入内容不能复用 u001，避免 merge 后出现重复块 ID。"""
    from PySide6.QtWidgets import QInputDialog

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    _patch_insert_dialog(monkeypatch, "note")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *args, **kwargs: ("补充说明", True)))

    window._on_insert()
    window._on_insert()

    inserted_ids = [
        ann.payload["inserted_block_id"]
        for ann in window.store.active_annotations
        if ann.action == Action.INSERT_NOTE
    ]
    assert inserted_ids == ["u001", "u002"]


def test_editor_window_double_click_chapter_renames_it(qapp, tmp_path: Path, monkeypatch) -> None:
    """双击章节块应进入重命名，而不是打开通用插入菜单。"""
    from PySide6.QtWidgets import QInputDialog

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    window = EditorWindow(
        package_path=tmp_path,
        timeline=_make_timeline(),
        store=AnnotationStore(),
    )
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *args, **kwargs: ("", False)))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *args, **kwargs: ("登录流程", True)))

    window._on_block_double_clicked("c001")

    assert window.chapter_list.item(0).text() == "登录流程 [0.0s]"
    assert window.store.active_annotations[-1].action == Action.RENAME_CHAPTER


def test_editor_window_inserts_reference_image(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QFileDialog

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    image_path = tmp_path / "reference.png"
    assert QImage(24, 16, QImage.Format.Format_RGB32).save(str(image_path))
    window = EditorWindow(tmp_path, _make_timeline(), AnnotationStore())
    _patch_insert_dialog(monkeypatch, "image")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(image_path), "")),
    )

    window._on_insert()

    inserted = window.timeline_view.block_by_id("u001")
    assert inserted is not None
    assert inserted["type"] == "reference_image"
    assert inserted["primary"]["frame_path"] == str(image_path.resolve())


def test_editor_window_inserts_file_text(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    text_path = tmp_path / "reference.md"
    text_path.write_text("# 参考内容\n\n正文", encoding="utf-8")
    window = EditorWindow(tmp_path, _make_timeline(), AnnotationStore())
    _patch_insert_dialog(monkeypatch, "text")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(text_path), "")),
    )

    window._on_insert()

    inserted = window.timeline_view.block_by_id("u001")
    assert inserted is not None
    assert inserted["type"] == "reference_text"
    assert inserted["primary"]["text"] == "# 参考内容\n\n正文"


def test_editor_window_handles_missing_text_file(qapp, tmp_path: Path, monkeypatch) -> None:
    """文件选择后若被移除，编辑器应提示读取失败而不是抛出异常。"""
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    missing_path = tmp_path / "missing.md"
    warnings: list[str] = []
    window = EditorWindow(tmp_path, _make_timeline(), AnnotationStore())
    _patch_insert_dialog(monkeypatch, "text")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(missing_path), "")),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: warnings.append(str(args[2]))),
    )

    window._on_insert()

    assert warnings and warnings[0].startswith("无法读取文件：")
    assert window.store.active_annotations == []


def test_editor_window_undo_redo(qapp, tmp_path: Path) -> None:
    from workspace.recorder.tools.editor.editor_window import EditorWindow

    timeline = _make_timeline()
    store = AnnotationStore()
    window = EditorWindow(package_path=tmp_path, timeline=timeline, store=store)

    # 选中 b001 + 标关键
    window.timeline_view.setCurrentIndex(window.timeline_view._model.index(1))
    window._on_mark_key_toggle(True)
    assert len(store.active_annotations) == 1

    # undo
    window._on_undo()
    assert len(store.active_annotations) == 0

    # redo
    window._on_redo()
    assert len(store.active_annotations) == 1


def test_editor_window_finalize_legacy(qapp, tmp_path: Path) -> None:
    """旧 _on_finalize 已被 _on_finalize_toggle 取代，保留兼容性 smoke 测试。"""
    from PySide6.QtWidgets import QMessageBox

    from workspace.recorder.tools.editor.editor_window import EditorWindow

    timeline = _make_timeline()
    store = AnnotationStore()
    window = EditorWindow(package_path=tmp_path, timeline=timeline, store=store)

    # mock 确认对话框返回 Yes，通过 toggle 触发
    QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window._on_finalize_toggle(True)

    assert store.finalized is True
    # annotations.json 已保存
    assert (tmp_path / "annotations.json").exists()
