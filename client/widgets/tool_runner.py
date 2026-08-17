"""工具启动器核心组件

从 tools_launcher.py 迁移而来，提供：
- ScriptRunner：subprocess 执行命令 + 实时读输出（QThread）
- QueueManager：任务队列串行执行（QThread）
- ToolDetailWidget：单个工具的选项表单 + 运行/停止/加入队列 + 输出区
- CategoryPageWidget：分类页面，Tab 展示分类下所有工具
- ToolLauncherWindow：独立的工具启动器主窗口（兼容旧 launcher.bat）
- main：旧版入口，被 tools_launcher.py shim 调用
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.constants import PROJECT_ROOT, TOOLS_MANIFEST_PATH
from lib.ui import tokens
from lib.ui.theme import set_text_role


def load_manifest() -> dict:
    """加载工具清单"""
    if not TOOLS_MANIFEST_PATH.exists():
        return {"categories": [], "tools": []}
    text = TOOLS_MANIFEST_PATH.read_text(encoding="utf-8-sig")
    return json.loads(text.lstrip("\ufeff"))


def get_category_map(manifest: dict) -> dict:
    """获取 category_id -> category_info 映射"""
    return {c["id"]: c for c in manifest.get("categories", [])}


def filter_tools_by_user_facing(tools: list, show_all: bool) -> list:
    """根据 user_facing 字段过滤工具

    show_all=True 时返回全部；否则只返回 user_facing=true 的工具
    （user_facing 字段缺失时默认按 usage 字段推断：user/both=true，agent=false）
    """
    if show_all:
        return list(tools)
    result = []
    for tool in tools:
        if "user_facing" in tool:
            if tool["user_facing"]:
                result.append(tool)
        else:
            # 兼容期：未填 user_facing 字段时按 usage 推断
            usage = tool.get("usage", "both")
            if usage in ("user", "both"):
                result.append(tool)
    return result


def filter_categories(manifest: dict, show_all: bool) -> list:
    """过滤分类：仅保留至少有 1 个可见工具的分类（show_all=True 时返回全部）"""
    if show_all:
        return list(manifest.get("categories", []))
    visible_tools_by_cat = {}
    for tool in filter_tools_by_user_facing(manifest.get("tools", []), show_all=False):
        cat_id = tool.get("category", "other")
        visible_tools_by_cat[cat_id] = visible_tools_by_cat.get(cat_id, 0) + 1
    return [c for c in manifest.get("categories", [])
            if visible_tools_by_cat.get(c["id"], 0) > 0]


# ===== ScriptRunner =====

class ScriptRunner(QThread):
    """脚本运行线程，实时读取输出"""
    output_signal = Signal(str)
    finished_signal = Signal(int, str)

    def __init__(self, command: list[str], cwd: Path):
        """T03: command 改为参数列表，shell=False 防注入。"""
        super().__init__()
        self.command = command
        self.cwd = cwd
        self.process = None
        self._stopped = False

    def run(self):
        try:
            child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                shell=False,  # T03: 参数列表模式，防命令注入
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
            )
            for line in self.process.stdout:
                if self._stopped:
                    break
                self.output_signal.emit(line.rstrip("\n"))
            self.process.wait()
            code = self.process.returncode
            self.finished_signal.emit(code, "完成" if code == 0 else f"退出码 {code}")
        except Exception as e:
            self.output_signal.emit(f"[启动失败] {e}")
            self.finished_signal.emit(-1, str(e))

    def stop(self):
        """停止脚本（终止整个子进程树）

        T03: shell=False 后不再有 cmd.exe 包装器，但子进程可能仍有子进程
        （如 python 启动的子进程），仍用 taskkill /F /T /PID 杀整个进程树。
        """
        self._stopped = True
        if self.process:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass


# ===== QueueManager =====

class QueueManager(QThread):
    """任务队列管理器，顺序执行多个任务"""
    task_started = Signal(str)            # tool_id
    task_output = Signal(str, str)         # tool_id, line
    task_finished = Signal(str, int, str) # tool_id, code, msg
    queue_changed = Signal()
    queue_finished = Signal()

    def __init__(self):
        super().__init__()
        self.tasks = []  # [(tool_id, tool_name, command[list[str]]), ...]
        self.process = None
        self._running = False
        self._stopped = False

    def add_task(self, tool_id: str, tool_name: str, command: list[str]):
        """T03: command 改为参数列表，配合 shell=False 防注入。"""
        self.tasks.append((tool_id, tool_name, command))
        self.queue_changed.emit()

    def clear(self):
        if not self._running:
            self.tasks.clear()
            self.queue_changed.emit()

    def get_status(self) -> dict:
        running = self._running
        total = len(self.tasks)
        current = self.tasks[0] if running and self.tasks else None
        pending = total - (1 if running else 0)
        return {"running": running, "current": current, "pending": pending, "total": total}

    def start_queue(self):
        if not self._running and self.tasks:
            self._running = True
            self._stopped = False
            self.start()

    def stop_queue(self):
        self._stopped = True
        if self.process:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass

    def run(self):
        while self.tasks and not self._stopped:
            tool_id, tool_name, command = self.tasks[0]
            self.task_started.emit(tool_id)

            try:
                child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
                self.process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    shell=False,  # T03: 参数列表模式，防命令注入
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                )
                for line in self.process.stdout:
                    if self._stopped:
                        break
                    self.task_output.emit(tool_id, line.rstrip("\n"))
                self.process.wait()
                code = self.process.returncode
                msg = "完成" if code == 0 else f"退出码 {code}"
            except Exception as e:
                self.task_output.emit(tool_id, f"[启动失败] {e}")
                code = -1
                msg = str(e)

            self.task_finished.emit(tool_id, code, msg)

            if not self._stopped:
                self.tasks.pop(0)
                self.queue_changed.emit()

            self.process = None

        self._running = False
        self.queue_finished.emit()


# ===== ToolDetailWidget =====

class ToolDetailWidget(QWidget):
    """单个工具的详情面板（选项 + 运行按钮 + 输出）"""

    run_requested = Signal(str, str)  # (tool_name, command)

    def __init__(self, tool: dict, category_name: str, on_enqueue=None):
        super().__init__()
        self.tool = tool
        self.tool_id = tool.get("id", tool["name"])
        self.category_name = category_name
        self.on_enqueue = on_enqueue
        self.option_widgets = {}  # option_name -> widget
        self.runner = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # 标题
        title_label = QLabel(f"{self.tool['name']}")
        set_text_role(title_label, "title")
        layout.addWidget(title_label)

        # 分类标签 + 使用方式
        usage_text = {"agent": "Agent调用", "user": "用户手动", "both": "两者皆可"}.get(
            self.tool.get("usage", ""), ""
        )
        meta_label = QLabel(f"分类: {self.category_name}  |  使用: {usage_text}")
        set_text_role(meta_label, "secondary")
        layout.addWidget(meta_label)

        # 描述
        desc_label = QLabel(self.tool.get("description", ""))
        desc_label.setWordWrap(True)
        set_text_role(desc_label, "secondary")
        layout.addWidget(desc_label)

        # 需求提示
        hints = []
        if self.tool.get("requires_browser"):
            hints.append("需要调试 Chrome（端口 9222）")
        if self.tool.get("requires_admin"):
            hints.append("需要管理员权限")
        if self.tool.get("notes"):
            hints.append(f"{self.tool['notes']}")
        if hints:
            hint_label = QLabel("\n".join(hints))
            hint_label.setWordWrap(True)
            set_text_role(hint_label, "warning")
            layout.addWidget(hint_label)

        # 选项区
        options = self.tool.get("options", [])
        if options:
            options_group = QGroupBox("选项")
            options_layout = QVBoxLayout(options_group)
            options_layout.setSpacing(6)

            for opt in options:
                # 兼容字符串格式（如 "--copy-user-data  说明..."），转为 dict
                if isinstance(opt, str):
                    parts = opt.split(maxsplit=1)
                    opt = {
                        "name": parts[0] if parts else opt,
                        "description": parts[1] if len(parts) > 1 else "",
                        "default": False,
                        "optional": True,
                    }
                opt_name = opt["name"]
                opt_desc = opt.get("description", "")
                opt_default = opt.get("default")
                opt_choices = opt.get("choices")
                opt_optional = opt.get("optional", False)

                row = QHBoxLayout()
                enable_cb = QCheckBox(opt_name)
                enable_cb.setToolTip(opt_desc)
                enable_cb.setChecked(False)
                row.addWidget(enable_cb)

                if opt_choices:
                    value_widget = QComboBox()
                    for c in opt_choices:
                        value_widget.addItem(str(c))
                    if opt_default in opt_choices:
                        value_widget.setCurrentText(str(opt_default))
                elif isinstance(opt_default, bool):
                    value_widget = QCheckBox("启用")
                    value_widget.setChecked(opt_default)
                elif isinstance(opt_default, int) and not isinstance(opt_default, bool):
                    value_widget = QSpinBox()
                    value_widget.setRange(0, 999999)
                    value_widget.setValue(opt_default)
                else:
                    value_widget = QLineEdit()
                    if opt_default is not None and opt_default is not False:
                        value_widget.setText(str(opt_default))
                    if opt_optional:
                        value_widget.setPlaceholderText(opt_desc)

                value_widget.setToolTip(opt_desc)
                row.addWidget(value_widget, 1)

                if opt_desc and not opt_optional:
                    desc_small = QLabel(opt_desc)
                    set_text_role(desc_small, "caption")
                    row.addWidget(desc_small)

                options_layout.addLayout(row)
                self.option_widgets[opt_name] = (enable_cb, value_widget, opt)

            layout.addWidget(options_group)

        # 按钮区
        btn_layout = QHBoxLayout()
        self.run_btn = QPushButton("运行")
        self.run_btn.clicked.connect(self._on_run)
        btn_layout.addWidget(self.run_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)

        self.enqueue_btn = QPushButton("加入队列")
        self.enqueue_btn.clicked.connect(self._on_enqueue)
        btn_layout.addWidget(self.enqueue_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 输出区
        output_label = QLabel("输出:")
        set_text_role(output_label, "secondary")
        layout.addWidget(output_label)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setMinimumHeight(200)
        layout.addWidget(self.output_text, 1)

    def _build_command(self) -> list[str]:
        """T03: 返回参数列表而非命令字符串，配合 shell=False 防注入。"""
        import shlex
        cmd = shlex.split(self.tool["command"])
        for opt_name, (enable_cb, value_widget, _opt) in self.option_widgets.items():
            if not enable_cb.isChecked():
                continue
            if isinstance(value_widget, QCheckBox):
                if value_widget.isChecked():
                    cmd.append(opt_name)
            elif isinstance(value_widget, QComboBox):
                val = value_widget.currentText()
                if val:
                    cmd.extend([opt_name, val])
            elif isinstance(value_widget, QSpinBox):
                val = value_widget.value()
                cmd.extend([opt_name, str(val)])
            else:
                val = value_widget.text().strip()
                if val:
                    cmd.extend([opt_name, val])
        return cmd

    def _on_run(self):
        cmd = self._build_command()
        self.output_text.clear()
        self._append_output(f"$ {' '.join(cmd)}\n", color=tokens.INFO_TEXT)
        self._append_output(f"工作目录: {PROJECT_ROOT}\n", color=tokens.TEXT_TERTIARY)
        self._append_output("-" * 60 + "\n", color=tokens.TEXT_TERTIARY)

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.runner = ScriptRunner(cmd, PROJECT_ROOT)
        self.runner.output_signal.connect(self._on_output)
        self.runner.finished_signal.connect(self._on_finished)
        self.runner.start()

    def _on_stop(self):
        if self.runner and self.runner.isRunning():
            reply = QMessageBox.question(
                self, "确认停止", "确定要停止正在运行的脚本吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.runner.stop()
                self._append_output("\n[用户停止了脚本]\n", color=tokens.WARNING_TEXT)

    def _on_output(self, line: str):
        self._append_output(line + "\n")

    def _on_finished(self, code: int, msg: str):
        color = tokens.SUCCESS_TEXT if code == 0 else tokens.DANGER_TEXT
        self._append_output("-" * 60 + "\n", color=tokens.TEXT_TERTIARY)
        self._append_output(f"[{msg}]\n", color=color)
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.runner = None

    def _on_enqueue(self):
        cmd = self._build_command()
        if self.on_enqueue:
            self.on_enqueue(self.tool_id, self.tool["name"], cmd)
        self._append_output(f"[已加入队列] {cmd}\n", color=tokens.WARNING_TEXT)

    def on_queue_started(self, command: list[str]):
        self.output_text.clear()
        self._append_output(f"$ {command}\n", color=tokens.INFO_TEXT)
        self._append_output(f"工作目录: {PROJECT_ROOT}\n", color=tokens.TEXT_TERTIARY)
        self._append_output("-" * 60 + "\n", color=tokens.TEXT_TERTIARY)
        self._append_output("[队列任务开始]\n", color=tokens.WARNING_TEXT)
        self.run_btn.setEnabled(False)
        self.enqueue_btn.setEnabled(False)

    def on_queue_output(self, line: str):
        self._append_output(line + "\n")

    def on_queue_finished(self, code: int, msg: str):
        color = tokens.SUCCESS_TEXT if code == 0 else tokens.DANGER_TEXT
        self._append_output("-" * 60 + "\n", color=tokens.TEXT_TERTIARY)
        self._append_output(f"[队列任务 {msg}]\n", color=color)
        self.run_btn.setEnabled(True)
        self.enqueue_btn.setEnabled(True)

    def _append_output(self, text: str, color: str = tokens.TEXT_SECONDARY):
        cursor = self.output_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.output_text.setTextCursor(cursor)
        self.output_text.ensureCursorVisible()


# ===== CategoryPageWidget =====

class CategoryPageWidget(QWidget):
    """分类页面：展示该分类下的所有工具（Tab 布局）"""

    def __init__(self, tools: list, category_name: str, on_enqueue=None):
        super().__init__()
        self.tools = tools
        self.category_name = category_name
        self.detail_widgets = []
        self._build_ui(on_enqueue)

    def _build_ui(self, on_enqueue=None):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        if not self.tools:
            label = QLabel("此分类下暂无工具")
            label.setAlignment(Qt.AlignCenter)
            set_text_role(label, "title")
            layout.addWidget(label)
            return

        tabs = QTabWidget()
        for tool in self.tools:
            detail = ToolDetailWidget(tool, self.category_name, on_enqueue)
            self.detail_widgets.append(detail)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(detail)
            tabs.addTab(scroll, tool["name"])
        layout.addWidget(tabs)

    def get_tabs(self) -> QTabWidget:
        return self.findChild(QTabWidget)


# ===== 旧版工具启动器主窗口（用于 shim 兼容） =====

class ToolLauncherWindow(QMainWindow):
    """独立的工具启动器主窗口（旧版 launcher.bat 入口）

    保留此窗口让旧 launcher.bat 仍能启动独立的工具启动器，
    客户端主窗口的 Tools 面板会复用 ToolDetailWidget/CategoryPageWidget/QueueManager。
    """

    def __init__(self, show_all: bool = False):
        super().__init__()
        self.show_all = show_all
        self.manifest = load_manifest()
        self.category_map = get_category_map(self.manifest)
        self.tool_widgets = {}
        self.queue_manager = QueueManager()
        self._build_ui()
        self._load_tools()
        self._connect_queue_signals()

    def _build_ui(self):
        self.setWindowTitle("LocalAgent 工具启动器")
        self.resize(1100, 750)

        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 左侧分类列表（弹性：min/max 替代 setFixedWidth，窗口缩放时更鲁棒）
        sidebar_frame = QFrame()
        sidebar_frame.setMinimumWidth(180)
        sidebar_frame.setMaximumWidth(240)
        sidebar_layout = QVBoxLayout(sidebar_frame)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        title_label = QLabel("工具箱")
        set_text_role(title_label, "heading")
        sidebar_layout.addWidget(title_label)

        self.category_list = QListWidget()
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        sidebar_layout.addWidget(self.category_list, 1)

        visible_count = len(filter_tools_by_user_facing(
            self.manifest.get("tools", []), show_all=self.show_all))
        total_count = len(self.manifest.get("tools", []))
        status_text = f"显示 {visible_count}/{total_count} 个工具"
        status_label = QLabel(status_text)
        set_text_role(status_label, "caption")
        sidebar_layout.addWidget(status_label)

        main_layout.addWidget(sidebar_frame)

        # 右侧内容区
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        outer_layout.addLayout(main_layout, 1)

        # 底部队列栏
        self._build_queue_bar(outer_layout)

        # 加载分类
        self.category_pages = {}
        for cat in filter_categories(self.manifest, show_all=self.show_all):
            item = QListWidgetItem(f"{cat['icon']}  {cat['name']}")
            item.setSizeHint(QSize(180, 40))
            self.category_list.addItem(item)

        if self.category_list.count() > 0:
            self.category_list.setCurrentRow(0)

    def _build_queue_bar(self, parent_layout):
        bar = QFrame()
        bar.setFixedHeight(50)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        queue_label = QLabel("队列:")
        set_text_role(queue_label, "secondary")
        layout.addWidget(queue_label)
        self.queue_status_label = QLabel("0 个任务")
        self.queue_status_label.setMinimumWidth(200)
        set_text_role(self.queue_status_label, "secondary")
        layout.addWidget(self.queue_status_label)

        layout.addStretch()

        self.queue_run_btn = QPushButton("执行队列")
        self.queue_run_btn.setEnabled(False)
        self.queue_run_btn.clicked.connect(self._on_run_queue)
        layout.addWidget(self.queue_run_btn)

        self.queue_stop_btn = QPushButton("停止")
        self.queue_stop_btn.setEnabled(False)
        self.queue_stop_btn.clicked.connect(self._on_stop_queue)
        layout.addWidget(self.queue_stop_btn)

        self.queue_clear_btn = QPushButton("清空")
        self.queue_clear_btn.setEnabled(False)
        self.queue_clear_btn.clicked.connect(self._on_clear_queue)
        layout.addWidget(self.queue_clear_btn)

        parent_layout.addWidget(bar)

    def _load_tools(self):
        tools = filter_tools_by_user_facing(
            self.manifest.get("tools", []), show_all=self.show_all)
        cat_tools = {}
        for tool in tools:
            cat_id = tool.get("category", "other")
            cat_tools.setdefault(cat_id, []).append(tool)

        for cat in filter_categories(self.manifest, show_all=self.show_all):
            cat_id = cat["id"]
            cat_name = cat["name"]
            page = CategoryPageWidget(cat_tools.get(cat_id, []), cat_name, self._on_enqueue_task)
            self.category_pages[cat_id] = page
            self.stack.addWidget(page)
            for detail in page.detail_widgets:
                self.tool_widgets[detail.tool_id] = detail

    def _on_enqueue_task(self, tool_id: str, tool_name: str, command: str):
        self.queue_manager.add_task(tool_id, tool_name, command)

    def _connect_queue_signals(self):
        self.queue_manager.task_started.connect(self._on_queue_task_started)
        self.queue_manager.task_output.connect(self._on_queue_task_output)
        self.queue_manager.task_finished.connect(self._on_queue_task_finished)
        self.queue_manager.queue_changed.connect(self._on_queue_changed)
        self.queue_manager.queue_finished.connect(self._on_queue_finished)
        self._on_queue_changed()

    def _switch_to_tool(self, tool_id: str):
        visible_cats = filter_categories(self.manifest, show_all=self.show_all)
        for cat_idx, cat in enumerate(visible_cats):
            cat_id = cat["id"]
            page = self.category_pages.get(cat_id)
            if not page:
                continue
            tabs = page.get_tabs()
            if tabs:
                for i in range(tabs.count()):
                    tab_page = tabs.widget(i)
                    detail = tab_page.widget() if hasattr(tab_page, "widget") else tab_page
                    if isinstance(detail, ToolDetailWidget) and detail.tool_id == tool_id:
                        self.category_list.setCurrentRow(cat_idx)
                        tabs.setCurrentIndex(i)
                        return

    def _on_queue_task_started(self, tool_id: str):
        widget = self.tool_widgets.get(tool_id)
        if widget:
            self._switch_to_tool(tool_id)
            widget.on_queue_started(widget._build_command())

    def _on_queue_task_output(self, tool_id: str, line: str):
        widget = self.tool_widgets.get(tool_id)
        if widget:
            widget.on_queue_output(line)

    def _on_queue_task_finished(self, tool_id: str, code: int, msg: str):
        widget = self.tool_widgets.get(tool_id)
        if widget:
            widget.on_queue_finished(code, msg)

    def _on_queue_changed(self):
        status = self.queue_manager.get_status()
        if status["running"] and status["current"]:
            self.queue_status_label.setText(
                f"执行中: {status['current'][1]} | 待执行: {status['pending']}"
            )
        else:
            self.queue_status_label.setText(f"{status['total']} 个任务")
        self.queue_run_btn.setEnabled(not status["running"] and status["total"] > 0)
        self.queue_stop_btn.setEnabled(status["running"])
        self.queue_clear_btn.setEnabled(not status["running"] and status["total"] > 0)

    def _on_queue_finished(self):
        self._on_queue_changed()

    def _on_run_queue(self):
        self.queue_manager.start_queue()

    def _on_stop_queue(self):
        self.queue_manager.stop_queue()

    def _on_clear_queue(self):
        self.queue_manager.clear()

    def _on_category_changed(self, row: int):
        visible_cats = filter_categories(self.manifest, show_all=self.show_all)
        if 0 <= row < len(visible_cats):
            cat_id = visible_cats[row]["id"]
            page = self.category_pages.get(cat_id)
            if page:
                self.stack.setCurrentWidget(page)

    def closeEvent(self, event):
        running_runners = []
        for page in self.category_pages.values():
            tabs = page.get_tabs()
            if tabs:
                for i in range(tabs.count()):
                    tab_page = tabs.widget(i)
                    detail = tab_page.widget() if hasattr(tab_page, "widget") else tab_page
                    if isinstance(detail, ToolDetailWidget) and detail.runner and detail.runner.isRunning():
                        running_runners.append((detail.tool["name"], detail.runner))

        queue_running = self.queue_manager._running

        if running_runners or queue_running:
            names = [name for name, _ in running_runners]
            if queue_running:
                names.append("任务队列")
            reply = QMessageBox.question(
                self, "确认退出",
                f"以下脚本正在运行中：\n  - {chr(10).join(names)}\n\n"
                "关闭 GUI 将终止所有运行中的脚本。\n确定要关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                for _, runner in running_runners:
                    runner.stop()
                if queue_running:
                    self.queue_manager.stop_queue()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main():
    """旧版工具启动器入口（被 tools_launcher.py shim 调用）"""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("LocalAgent 工具启动器")

    window = ToolLauncherWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
