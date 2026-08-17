"""details_dialog.py — 对象详情弹窗（ctrl+enter 触发，ESC 关闭）

显示文件/文件夹完整信息 + 操作按钮：
- [打开] os.startfile 文件 / 文件夹（文件夹 ticket 05 接管为导航）
- [在资源管理器中显示] subprocess 调 explorer /select
- [复制路径] 复制到剪贴板

文件夹详情额外显示：内部文件数 / 扩展名分布 / 嵌套深度
"""

import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QPushButton,
    QHBoxLayout, QApplication, QGroupBox,
)

from folder_info import describe_folder, format_ext_distribution
from type_descriptor import describe_file_type


def _human_readable_size(size: int) -> str:
    """字节数转人类可读（与 classifier_gui.human_readable_size 一致，独立复制避免循环依赖）"""
    if size < 0:
        return "未知"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _format_time(timestamp: float) -> str:
    """时间戳格式化（与 classifier_gui.format_time 一致）"""
    if not timestamp:
        return "未知"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return "未知"


class DetailsDialog(QDialog):
    """对象详情弹窗

    构造后调 exec() 模态显示，按 ESC 或关闭按钮退出。
    """

    def __init__(self, file_info: dict, current_category: str = "",
                 predicted_category: str = "", confidence: float = 0.0,
                 status: str = "", parent=None):
        """
        Args:
            file_info: 文件信息 dict（含 filename/path/size/modified/created/is_dir）
            current_category: 用户手动分配的分类
            predicted_category: LLM 预测分类
            confidence: 预测置信度（0-1）
            status: 状态文本（已处理/跳过/未处理）
        """
        super().__init__(parent)
        self._file_info = file_info
        self._path = file_info.get("path", "")
        self._is_dir = file_info.get("is_dir", False)

        self.setWindowTitle("对象详情")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        # 基本信息 group
        info_group = QGroupBox("基本信息")
        form = QFormLayout(info_group)
        form.setLabelAlignment(Qt.AlignRight)

        filename = file_info.get("filename", "")
        form.addRow("名称：", QLabel(filename))
        form.addRow("路径：", QLabel(self._path))
        form.addRow("类型：", QLabel(describe_file_type(filename, is_dir=self._is_dir)))
        form.addRow("大小：", QLabel(_human_readable_size(file_info.get("size", 0))))
        form.addRow("修改时间：", QLabel(_format_time(file_info.get("modified", 0))))
        form.addRow("创建时间：", QLabel(_format_time(file_info.get("created", 0))))
        form.addRow("当前分类：", QLabel(current_category or "（未分配）"))
        form.addRow("预测分类：", QLabel(predicted_category or "（未预测）"))
        form.addRow("置信度：", QLabel(f"{confidence * 100:.1f}%" if confidence else "未知"))
        form.addRow("状态：", QLabel(status or "未处理"))

        layout.addWidget(info_group)

        # 文件夹额外信息 group
        if self._is_dir:
            folder_group = QGroupBox("文件夹内容")
            folder_form = QFormLayout(folder_group)
            folder_form.setLabelAlignment(Qt.AlignRight)

            info = describe_folder(self._path)
            if info["error"]:
                folder_form.addRow("错误：", QLabel(info["error"]))
            else:
                folder_form.addRow("内部文件数：", QLabel(str(info["file_count"])))
                folder_form.addRow("扩展名分布：", QLabel(format_ext_distribution(info["ext_distribution"])))
                folder_form.addRow("嵌套深度：", QLabel(f"{info['max_depth']} 层"))
                folder_form.addRow("总大小：", QLabel(_human_readable_size(info["total_size"])))
                if info["truncated"]:
                    folder_form.addRow("提示：", QLabel("文件数过多，已截断统计"))

            layout.addWidget(folder_group)

        # 操作按钮行
        btn_row = QHBoxLayout()

        open_btn = QPushButton("打开")
        open_btn.clicked.connect(self._on_open)
        btn_row.addWidget(open_btn)

        explorer_btn = QPushButton("在资源管理器中显示")
        explorer_btn.clicked.connect(self._on_show_in_explorer)
        btn_row.addWidget(explorer_btn)

        copy_btn = QPushButton("复制路径")
        copy_btn.clicked.connect(self._on_copy_path)
        btn_row.addWidget(copy_btn)

        btn_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def keyPressEvent(self, event: QKeyEvent):
        """ESC 关闭弹窗（QDialog 默认 ESC 会 reject，这里显式确认行为）"""
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _on_open(self):
        """打开对象（文件用默认程序，文件夹在资源管理器打开）"""
        if not self._path or not os.path.exists(self._path):
            return
        try:
            os.startfile(self._path)
        except OSError:
            pass

    def _on_show_in_explorer(self):
        """在 Windows 资源管理器中选中该对象"""
        if not self._path or not os.path.exists(self._path):
            return
        try:
            # explorer /select,"path" 会在资源管理器中选中该文件/文件夹
            subprocess.Popen(["explorer", "/select,", self._path])
        except (OSError, subprocess.SubprocessError):
            pass

    def _on_copy_path(self):
        """复制完整路径到剪贴板"""
        cb = QApplication.clipboard()
        if cb:
            cb.setText(self._path)
