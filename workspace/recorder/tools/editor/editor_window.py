"""L3 编辑器主窗口：三区域布局 + 工具栏 + 快捷键。

布局（02-editor.md §0）：
- 左侧章节列表（~150px）
- 中间时间轴（自适应，4 列横向布局）
- 下方详情预览（折叠区）

工具栏（02-editor.md §2）：5 个 checkable 动作（移除块/标关键/标异常/标可自动化/
就绪），点击按目标状态追加 MARK/UNMARK 或 TRIM/UNTRIM 标注；选中块变化时由
_sync_mark_buttons 同步 checked 态 + 图标 + statusTip。

视觉规范：docs/ui/ + temp/sdd/recorder-gui-redesign/02-editor.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from lib.recorder.editor.annotation import Action, Annotation, next_inserted_block_id
from lib.recorder.editor.store import AnnotationStore
from lib.ui import icon, tokens
from lib.ui.theme import set_text_role
from workspace.recorder.tools.editor.widgets.chapter_list import ChapterList
from workspace.recorder.tools.editor.widgets.detail_panel import DetailPanel
from workspace.recorder.tools.editor.widgets.timeline_view import TimelineView, _block_summary

logger = logging.getLogger(__name__)


class EditorWindow(QMainWindow):
    """L3 编辑器主窗口。"""

    finalize_requested = Signal()

    def __init__(
        self,
        package_path: Path,
        timeline: dict[str, Any],
        store: AnnotationStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.package_path = package_path
        self.timeline = timeline
        self.store = store
        self.setWindowTitle(f"录制编辑器 - {package_path.name}")

        self._build_ui()
        self._build_toolbar()
        self._build_shortcuts()
        self._connect_signals()
        self._update_action_enabled_states()
        self._update_status_bar()

    def _build_ui(self) -> None:
        """三区域布局：左章节 + 中时间轴 + 下详情。"""
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 上半：左章节列表 + 中时间轴
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.chapter_list = ChapterList(self.timeline)
        self.timeline_view = TimelineView(self.timeline, self.store)

        timeline_panel = QWidget()
        timeline_layout = QVBoxLayout(timeline_panel)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        header = QFrame()
        header.setFixedHeight(28)
        header.setObjectName("TimelineHeader")
        header_layout = QGridLayout(header)
        header_layout.setContentsMargins(12, 0, 24, 0)
        header_layout.setHorizontalSpacing(8)
        for column, text in enumerate(("时间", "事件 / 窗口", "证据", "内容")):
            label = QLabel(text)
            set_text_role(label, "secondary")
            header_layout.addWidget(label, 0, column)
        header_layout.setColumnMinimumWidth(0, 78)
        header_layout.setColumnStretch(1, 3)
        header_layout.setColumnMinimumWidth(2, 72)
        header_layout.setColumnStretch(3, 2)
        timeline_layout.addWidget(header)
        timeline_layout.addWidget(self.timeline_view, stretch=1)

        top_splitter.addWidget(self.chapter_list)
        top_splitter.addWidget(timeline_panel)
        top_splitter.setSizes([190, 1010])

        # 下半：详情预览
        self.detail_panel = DetailPanel()

        splitter.addWidget(top_splitter)
        splitter.addWidget(self.detail_panel)
        splitter.setSizes([600, 200])

        self.setCentralWidget(splitter)
        self.resize(1200, 800)

    def _build_toolbar(self) -> None:
        """工具栏（02-editor.md §2.1）：编辑 | 标记(checkable) | 插入 | 历史 | 音频 | 就绪。

        5 个 checkable 动作（移除块/标关键/标异常/标可自动化/就绪）点击时由 triggered(bool)
        读目标状态追加 MARK/UNMARK 或 TRIM/UNTRIM 标注；选中块变化时由
        ``_sync_mark_buttons`` 同步 checked 态 + 图标 + 文本（不触发 triggered）。
        """
        toolbar = QToolBar("编辑工具", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(toolbar)

        self._actions: dict[str, QAction] = {}

        def add_action(
            name: str,
            text: str,
            slot,
            shortcut: str = "",
            icon_name: str | None = None,
            icon_color: str = tokens.ICON_DEFAULT,
            checkable: bool = False,
        ) -> None:
            act = QAction(text, self)
            if icon_name:
                act.setIcon(icon(icon_name, icon_color))
            if checkable:
                act.setCheckable(True)
                act.triggered.connect(lambda checked: slot(checked))
            else:
                act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
                act.setToolTip(f"{text} ({shortcut})")
            toolbar.addAction(act)
            self._actions[name] = act

        # 编辑组：移除块（toggle，原 trim/restore 合并）
        add_action("remove_block", "移除块", self._on_remove_block_toggle, "Delete",
                   "eye-off", tokens.ICON_DANGER, checkable=True)
        add_action("merge", "合并到前块", self._on_merge, "M", "merge")
        add_action("split", "拆分块", self._on_split, "S", "scissors")
        toolbar.addSeparator()
        # 标记组：3 个 checkable
        add_action("mark_key", "标关键", self._on_mark_key_toggle, "K", "star",
                   tokens.BLOCK_KEY, checkable=True)
        add_action("mark_anomaly", "标异常", self._on_mark_anomaly_toggle, "X", "alert-triangle",
                   tokens.ICON_DANGER, checkable=True)
        add_action("mark_automatable", "标可自动化", self._on_mark_automatable_toggle, "A", "bot",
                   tokens.ICON_ACTIVE, checkable=True)
        # 插入
        add_action("insert", "插入块", self._on_insert, "I", "plus")
        toolbar.addSeparator()
        # 历史
        add_action("undo", "撤销", self._on_undo, "Ctrl+Z", "undo")
        add_action("redo", "重做", self._on_redo, "Ctrl+Shift+Z", "redo")
        toolbar.addSeparator()
        # 音频 + 就绪
        add_action("audio", "音频调参", self._on_audio_panel, "", "sliders")
        add_action("finalize", "标记就绪", self._on_finalize_toggle, "F", "check-circle",
                   tokens.SUCCESS, checkable=True)

    def _build_shortcuts(self) -> None:
        """额外快捷键（不显示在工具栏，窗口级全局快捷键）。

        F1 → 快捷键帮助对话框（02-editor §9）。
        """
        help_action = QAction("快捷键帮助", self)
        help_action.setShortcut(QKeySequence("F1"))
        help_action.setToolTip("快捷键帮助 (F1)")
        help_action.setStatusTip("显示快捷键帮助")
        help_action.triggered.connect(self._on_show_shortcuts_help)
        self.addAction(help_action)

    def _on_show_shortcuts_help(self) -> None:
        """F1 → 弹出快捷键帮助对话框（02-editor §9）。"""
        from workspace.recorder.tools.editor.widgets.shortcuts_help_dialog import ShortcutsHelpDialog

        dialog = ShortcutsHelpDialog(self)
        dialog.exec()

    def _connect_signals(self) -> None:
        """连接信号。"""
        self.chapter_list.block_selected.connect(self._on_chapter_selected)
        self.chapter_list.block_double_clicked.connect(self._on_block_double_clicked)
        self.timeline_view.block_selected.connect(self._on_block_selected)
        self.timeline_view.block_double_clicked.connect(self._on_block_double_clicked)
        self.timeline_view.selection_changed.connect(self._update_action_enabled_states)
        # 详情面板：截图点击开查看器 + 状态 chips 与工具栏双向同步（02-editor §5）
        self.detail_panel.open_viewer_requested.connect(self._on_open_viewer)
        self.detail_panel.chip_toggled.connect(self._on_chip_toggled)
        self.timeline_view.select_first_block()

    # ===== 工具栏槽函数 =====

    def _selected_block_id(self) -> str | None:
        """当前选中的块 ID。"""
        return self.timeline_view.selected_block_id()

    def _add_annotation(self, action: Action, target: str, payload: dict | None = None) -> None:
        """添加 annotation + 刷新视图 + 自动保存。"""
        ann = Annotation(id="", target_block_id=target, action=action, payload=payload or {})
        self.store.add(ann)
        self.store.save(self.package_path / "annotations.json")
        self._refresh_views()

    def _refresh_views(self) -> None:
        """让时间轴、章节和详情使用同一份 annotation 编辑视图。"""
        current_id = self._selected_block_id()
        self.timeline_view.refresh()
        self.chapter_list.refresh({"blocks": self.timeline_view.blocks()})
        if current_id:
            self._on_block_selected(current_id)
        self._update_status_bar()

    # ----- checkable toggle 槽（triggered(bool) 传入目标状态）-----

    def _on_remove_block_toggle(self, checked: bool) -> None:
        """移除块/恢复块 toggle。checked=True → TRIM；False → UNTRIM。"""
        target = self._selected_block_id()
        if not target:
            return
        self._add_annotation(Action.TRIM if checked else Action.UNTRIM, target)

    def _on_mark_key_toggle(self, checked: bool) -> None:
        target = self._selected_block_id()
        if not target:
            return
        self._add_annotation(Action.MARK_KEY if checked else Action.UNMARK_KEY, target)

    def _on_mark_anomaly_toggle(self, checked: bool) -> None:
        target = self._selected_block_id()
        if not target:
            return
        self._add_annotation(Action.MARK_ANOMALY if checked else Action.UNMARK_ANOMALY, target)

    def _on_mark_automatable_toggle(self, checked: bool) -> None:
        target = self._selected_block_id()
        if not target:
            return
        self._add_annotation(Action.MARK_AUTOMATABLE if checked else Action.UNMARK_AUTOMATABLE, target)

    def _on_finalize_toggle(self, checked: bool) -> None:
        """就绪 toggle。checked=True → finalize；False → unfinalize（均需确认）。"""
        if checked:
            reply = QMessageBox.question(
                self, "标记就绪",
                "标记此录制包为已就绪？L4 agent 将能消费此录制包。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.store.finalize()
                self.store.save(self.package_path / "annotations.json")
                self.finalize_requested.emit()
            else:
                # 用户取消：回退 checked 态（不触发 triggered）
                self._actions["finalize"].blockSignals(True)
                self._actions["finalize"].setChecked(False)
                self._actions["finalize"].blockSignals(False)
        else:
            reply = QMessageBox.question(
                self, "取消就绪",
                "取消就绪标记？此录制包将不再可被 L4 agent 消费。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.store.unfinalize()
                self.store.save(self.package_path / "annotations.json")
            else:
                self._actions["finalize"].blockSignals(True)
                self._actions["finalize"].setChecked(True)
                self._actions["finalize"].blockSignals(False)
        self._update_action_enabled_states()
        self._update_status_bar()

    # ----- 非 checkable 槽 -----

    def _on_merge(self) -> None:
        targets = self.timeline_view.selected_block_ids()
        if len(targets) >= 2:
            main = targets[0]
            merged = targets[1:]
            self._add_annotation(Action.MERGE_BLOCKS, main, {"merged_block_ids": merged})

    def _on_split(self) -> None:
        target = self._selected_block_id()
        if target:
            # 简化：在块中间时间戳拆分
            from lib.recorder.editor.merge import merge_timeline_annotations

            merged = merge_timeline_annotations(self.package_path)
            block = next((b for b in merged["blocks"] if b["id"] == target), None)
            if block:
                split_ts = block["timestamp"] + block.get("duration", 0) / 2
                self._add_annotation(Action.SPLIT_BLOCK, target, {"split_timestamp": split_ts})

    def _on_insert(self) -> None:
        """插入块（02-editor §7：自定义 InsertBlockDialog 替换 QInputDialog）。

        流程：
        1. 弹出 InsertBlockDialog 选择类型 + 显示位置提示
        2. 根据类型走文本输入/文件选择子流程
        3. 追加 INSERT_* 标注到选中块之后
        """
        from PySide6.QtWidgets import QFileDialog, QInputDialog

        from workspace.recorder.tools.editor.widgets.insert_block_dialog import InsertBlockDialog

        target = self._selected_block_id()
        if not target:
            QMessageBox.warning(self, "提示", "请先选中一个块作为父块")
            return

        # 构造位置提示（02-editor §7）
        block = self.timeline_view.block_by_id(target)
        if block:
            ts = block.get("timestamp", 0.0)
            position_hint = f"将插入到选中块（{ts:.1f}s {_block_summary(block)}）之后"
        else:
            position_hint = "追加到时间轴末尾"

        dialog = InsertBlockDialog(position_hint, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choice_key = dialog.selected_key()
        if choice_key is None:
            return

        if choice_key == "note":
            text, ok = QInputDialog.getText(self, "插入注释", "注释内容：")
            if ok and text.strip():
                self._insert_user_block(Action.INSERT_NOTE, target, {"text": text.strip()})
        elif choice_key == "chapter":
            name, ok = QInputDialog.getText(self, "插入章节", "章节名：")
            if ok and name.strip():
                self._insert_user_block(Action.INSERT_CHAPTER, target, {"name": name.strip()})
        elif choice_key == "image":
            image_path, _ = QFileDialog.getOpenFileName(
                self,
                "插入参考图片",
                str(self.package_path),
                "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)",
            )
            if image_path:
                self._insert_user_block(
                    Action.INSERT_IMAGE,
                    target,
                    {"image_path": str(Path(image_path).resolve())},
                )
        elif choice_key == "text":
            text_path, _ = QFileDialog.getOpenFileName(
                self,
                "插入文件文本",
                str(self.package_path),
                "文本 (*.txt *.md *.json *.csv *.log);;所有文件 (*)",
            )
            if not text_path:
                return
            source = Path(text_path)
            try:
                text = self._read_text_file(source)
            except ValueError as exc:
                QMessageBox.warning(self, "文件过大", str(exc))
                return
            except OSError as exc:
                QMessageBox.warning(self, "读取失败", f"无法读取文件：{exc}")
                return
            self._insert_user_block(
                Action.INSERT_TEXT,
                target,
                {
                    "text": text,
                    "source_file": str(source.resolve()),
                },
            )

    def _insert_user_block(
        self,
        action: Action,
        target_block_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Add one inserted block with an ID unique in the current edited view."""
        block_ids = [str(block["id"]) for block in self.timeline_view.blocks()]
        inserted_id = next_inserted_block_id(block_ids)
        self._add_annotation(
            action,
            target_block_id,
            {**payload, "position": "after", "inserted_block_id": inserted_id},
        )

    @staticmethod
    def _read_text_file(source: Path) -> str:
        """Read a small text attachment using the recorder's supported encodings."""
        if source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("请选择不超过 2 MB 的文本文件。")
        try:
            return source.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return source.read_text(encoding="gb18030", errors="replace")

    def _on_undo(self) -> None:
        if self.store.undo():
            self.store.save(self.package_path / "annotations.json")
            self._refresh_views()

    def _on_redo(self) -> None:
        if self.store.redo():
            self.store.save(self.package_path / "annotations.json")
            self._refresh_views()

    def _on_audio_panel(self) -> None:
        """打开音频调参面板（D049）。"""
        from workspace.recorder.tools.editor.audio_panel import AudioPanel

        panel = AudioPanel(self.package_path, self)
        panel.exec()
        # 关闭后重新加载 timeline（可能 STT 重跑后 transcript 变了）
        if panel.result() == QDialog.DialogCode.Accepted:
            from workspace.recorder.tools.editor.editor_app import load_timeline

            self.timeline = load_timeline(self.package_path)
            self.store.load(self.package_path / "annotations.json")
            self.timeline_view.set_timeline(self.timeline)
            self._refresh_views()

    # ===== 信号槽 =====

    def _on_chapter_selected(self, block_id: str) -> None:
        """章节列表点击 → 时间轴跳转。"""
        self.timeline_view.scroll_to_block(block_id)

    def _on_block_selected(self, block_id: str) -> None:
        """时间轴块选中 → 详情预览更新 + 同步 checkable 动作状态。"""
        block = self.timeline_view.block_by_id(block_id)
        if block:
            self.detail_panel.show_block(block, self.package_path)
        self._update_action_enabled_states()

    def _on_open_viewer(self, frame_paths: list, current_index: int) -> None:
        """详情面板截图点击 → 打开截图查看器（02-editor §6）。"""
        from workspace.recorder.tools.editor.widgets.screenshot_viewer import ScreenshotViewer

        if not frame_paths:
            return
        viewer = ScreenshotViewer(frame_paths, current_index, self)
        viewer.exec()

    def _on_chip_toggled(self, chip_name: str, checked: bool) -> None:
        """详情面板状态 chip 点击 → 追加 MARK/UNMARK 标注（02-editor §5.2 双向同步）。

        与工具栏 toggle 槽一致：checked=True → MARK_*；False → UNMARK_*。
        """
        target = self._selected_block_id()
        if not target:
            return
        action_map = {
            "key": (Action.MARK_KEY, Action.UNMARK_KEY),
            "anomaly": (Action.MARK_ANOMALY, Action.UNMARK_ANOMALY),
            "automatable": (Action.MARK_AUTOMATABLE, Action.UNMARK_AUTOMATABLE),
        }
        if chip_name not in action_map:
            return
        mark_action, unmark_action = action_map[chip_name]
        self._add_annotation(mark_action if checked else unmark_action, target)

    def _update_action_enabled_states(self) -> None:
        """根据当前选择与块状态启用/禁用工具栏动作 + 同步 checkable 态。

        - 无选中块：编辑/标记/插入组禁用，statusTip 说明原因（02-editor §2.2）
        - 选中已移除块：合并/拆分/插入/标记禁用（移除块本身仍可用，用于恢复）
        - 选中正常块：所有动作可用
        - 就绪动作始终可用（包级状态，与块选择无关）
        """
        block_id = self._selected_block_id()
        block = self.timeline_view.block_by_id(block_id) if block_id else None
        is_trimmed = bool(block and block.get("status", {}).get("is_trimmed", False))
        has_block = block is not None
        multi_selected = len(self.timeline_view.selected_block_ids()) >= 2

        # 移除块 toggle：选中块时始终可用（无论 trimmed 与否，用于双向切换）
        self._actions["remove_block"].setEnabled(has_block)
        # 合并/拆分/插入：多选/未 trim/有 duration 才可用
        self._actions["merge"].setEnabled(multi_selected)
        self._actions["split"].setEnabled(
            has_block and not is_trimmed and block.get("duration", 0.0) > 0
        )
        # 标记组：未 trim 的块才能标记（已移除块无标记意义）
        for name in ("mark_key", "mark_anomaly", "mark_automatable", "insert"):
            self._actions[name].setEnabled(has_block and not is_trimmed)

        # statusTip 反馈禁用原因
        if not has_block:
            for name in ("remove_block", "merge", "split", "mark_key",
                         "mark_anomaly", "mark_automatable", "insert"):
                self._actions[name].setStatusTip("请先在时间轴选择一个块")
        else:
            self._actions["remove_block"].setStatusTip(
                "恢复此块到最终视图" if is_trimmed else "从最终视图移除（可恢复）"
            )
            self._actions["merge"].setStatusTip(
                "与上一个同类型块合并" if multi_selected else "选择两个以上块以合并"
            )
            self._actions["split"].setStatusTip(
                "按时间点拆分为两块" if (not is_trimmed and block.get("duration", 0.0) > 0)
                else "此块不可拆分（已移除或时长为 0）"
            )
            for name, label in (("mark_key", "标记/取消关键步骤"),
                                ("mark_anomaly", "标记/取消异常"),
                                ("mark_automatable", "标记/取消可自动化"),
                                ("insert", "在选中块后插入新块")):
                self._actions[name].setStatusTip(
                    label if not is_trimmed else "已移除块不可执行此操作"
                )

        # 同步 checkable 态 + 图标/文本（不触发 triggered）
        self._sync_checkable_states(block)

        # 就绪动作：包级状态，与块选择无关
        self._actions["finalize"].setStatusTip(
            "标记本包可进入消费" if not self.store.finalized else "取消就绪标记"
        )

    def _sync_checkable_states(self, block: dict[str, Any] | None) -> None:
        """按当前块状态同步 4 个块级 checkable 动作的 checked/图标/文本。

        用 blockSignals 避免 setChecked 触发 triggered 回调（02-editor §2.2）。
        finalize 由 _sync_finalize_state 单独处理。
        """
        status = block.get("status", {}) if block else {}
        # 4 个块级 checkable：移除块/标关键/标异常/标可自动化
        target_states = {
            "remove_block": bool(status.get("is_trimmed", False)),
            "mark_key": bool(status.get("marked_key", False)),
            "mark_anomaly": bool(status.get("marked_anomaly", False)),
            "mark_automatable": bool(status.get("marked_automatable", False)),
        }
        for name, target in target_states.items():
            act = self._actions[name]
            act.blockSignals(True)
            act.setChecked(target)
            act.blockSignals(False)

        # 移除块动作的文本/图标随状态切换（02-editor §3）
        is_trimmed = target_states["remove_block"]
        self._actions["remove_block"].setText("恢复块" if is_trimmed else "移除块")
        self._actions["remove_block"].setIcon(
            icon("eye" if is_trimmed else "eye-off", tokens.ICON_DANGER)
        )
        self._actions["remove_block"].setToolTip(
            f"{'恢复块' if is_trimmed else '移除块'} (Delete)"
        )

        # finalize checked 态由 store.finalized 驱动
        self._sync_finalize_state()

    def _sync_finalize_state(self) -> None:
        """就绪动作 checked 态由 store.finalized 驱动（与块选择无关）。"""
        act = self._actions["finalize"]
        act.blockSignals(True)
        act.setChecked(self.store.finalized)
        act.blockSignals(False)
        act.setText("已就绪" if self.store.finalized else "标记就绪")
        act.setIcon(
            icon("check-circle", tokens.SUCCESS if self.store.finalized else tokens.ICON_DEFAULT)
        )

    def _update_status_bar(self) -> None:
        """显示编辑统计和录制包状态（02-editor §8 状态栏）。"""
        blocks = self.timeline_view.blocks()
        trimmed = sum(bool(b.get("status", {}).get("is_trimmed", False)) for b in blocks)
        edits = len(self.store.active_annotations)
        duration = self.timeline.get("duration_seconds", 0.0)
        ready = "已就绪" if self.store.finalized else "未就绪"
        self.statusBar().showMessage(
            f"共 {len(blocks)} 块  ·  已移除 {trimmed}  |  {edits} 项编辑  |  {duration:.1f}s  |  {ready}"
        )

    def _on_block_double_clicked(self, block_id: str) -> None:
        """双击章节直接重命名；其他块打开插入菜单。"""
        block = self.timeline_view.block_by_id(block_id)
        if block and block.get("type") == "chapter":
            from PySide6.QtWidgets import QInputDialog

            current_name = str(block.get("primary", {}).get("name", ""))
            name, ok = QInputDialog.getText(
                self,
                "重命名章节",
                "章节名：",
                text=current_name,
            )
            normalized = name.strip()
            if ok and normalized and normalized != current_name:
                self._add_annotation(
                    Action.RENAME_CHAPTER,
                    block_id,
                    {"new_name": normalized},
                )
            return
        self._on_insert()
