#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户消息注入 GUI - 向后端发送补充指令

在 agent 工作期间，用户可以通过此界面发送补充指令。
指令暂存在后端，当 agent 调用非工作端点时自动返回。

用法: uv run python tools/user_message_gui.py
"""

import sys
import requests
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QListWidget, QListWidgetItem,
    QFrame, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QEvent
from PySide6.QtGui import QFont, QColor, QKeyEvent

BACKEND_URL = "http://127.0.0.1:8766"


class MessageFetcher(QThread):
    """后台线程：定期获取待发送消息列表"""
    messages_updated = Signal(list)
    error_occurred = Signal(str)

    def __init__(self, interval_ms=2000):
        super().__init__()
        self._interval = interval_ms
        self._running = True

    def run(self):
        while self._running:
            try:
                r = requests.get(f"{BACKEND_URL}/user/message", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    self.messages_updated.emit(data.get("pending", []))
            except requests.exceptions.ConnectionError:
                self.error_occurred.emit("后端不可达")
            except Exception as e:
                self.error_occurred.emit(str(e))
            self.msleep(self._interval)

    def stop(self):
        self._running = False


class UserMessageGUI(QWidget):
    """用户消息注入主窗口"""

    def __init__(self):
        super().__init__()
        self._fetcher = None
        self._init_ui()
        self._start_fetcher()

    def _init_ui(self):
        self.setWindowTitle("用户补充指令 - LocalAgent")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |  # 总在最前
            Qt.WindowType.Window  # 普通窗口（可最小化）
        )
        self.setMinimumWidth(450)
        self.setMinimumHeight(350)
        self.resize(500, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ---- 标题 ----
        title = QLabel("用户补充指令")
        title.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #333;")
        layout.addWidget(title)

        hint = QLabel(
            "输入补充指令后点击发送，agent 下次调用端点时会自动收到。\n"
            "工作端点（LLM池/mcp标准端点）被排除，不会收到指令。"
        )
        hint.setFont(QFont("Microsoft YaHei", 9))
        hint.setStyleSheet("color: #666; margin-bottom: 4px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- 输入区 ----
        input_frame = QFrame()
        input_frame.setStyleSheet("""
            QFrame { background-color: #f5f5f5; border-radius: 6px; padding: 4px; }
        """)
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(8, 8, 8, 8)

        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("输入补充指令...")
        self.text_input.setMaximumHeight(80)
        self.text_input.setFont(QFont("Microsoft YaHei", 10))
        input_layout.addWidget(self.text_input)

        btn_layout = QHBoxLayout()
        self.send_btn = QPushButton("发送指令")
        self.send_btn.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        self.send_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50; color: white;
                border: none; padding: 6px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #45a049; }
            QPushButton:pressed { background-color: #3d8b40; }
            QPushButton:disabled { background-color: #ccc; }
        """)
        self.send_btn.clicked.connect(self._send_message)

        self.clear_btn = QPushButton("清空输入")
        self.clear_btn.setFont(QFont("Microsoft YaHei", 9))
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #e0e0e0; border: none;
                padding: 6px 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #d0d0d0; }
        """)
        self.clear_btn.clicked.connect(self.text_input.clear)

        btn_layout.addWidget(self.send_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_frame)

        # ---- 待发送消息列表 ----
        list_label = QLabel("待发送消息（agent 尚未接收）")
        list_label.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        list_label.setStyleSheet("color: #333; margin-top: 4px;")
        layout.addWidget(list_label)

        self.msg_list = QListWidget()
        self.msg_list.setFont(QFont("Microsoft YaHei", 9))
        self.msg_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #ddd; border-radius: 4px;
                background-color: #fafafa;
            }
            QListWidget::item {
                padding: 6px 8px; border-bottom: 1px solid #eee;
            }
        """)
        layout.addWidget(self.msg_list)

        # ---- 底部操作 ----
        bottom_layout = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.status_label.setFont(QFont("Microsoft YaHei", 8))
        self.status_label.setStyleSheet("color: #999;")

        self.cancel_all_btn = QPushButton("全部取消")
        self.cancel_all_btn.setFont(QFont("Microsoft YaHei", 9))
        self.cancel_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336; color: white;
                border: none; padding: 4px 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #d32f2f; }
            QPushButton:disabled { background-color: #ccc; }
        """)
        self.cancel_all_btn.clicked.connect(self._cancel_all)

        bottom_layout.addWidget(self.status_label)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.cancel_all_btn)
        layout.addLayout(bottom_layout)

        # ---- 快捷键 ----
        # Ctrl+Enter 发送
        self.text_input.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.text_input and event.type() == QEvent.Type.KeyPress:
            key_event = QKeyEvent(event)
            if key_event.key() == Qt.Key.Key_Return and (key_event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._send_message()
                return True
        return super().eventFilter(obj, event)

    def _start_fetcher(self):
        self._fetcher = MessageFetcher(interval_ms=2000)
        self._fetcher.messages_updated.connect(self._update_list)
        self._fetcher.error_occurred.connect(self._on_error)
        self._fetcher.start()

    def _update_list(self, pending):
        self.msg_list.clear()
        for msg in pending:
            item_text = f"#{msg['id']}: {msg['text'][:80]}"
            if len(msg['text']) > 80:
                item_text += "..."
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, msg['id'])

            # 添加取消按钮
            cancel_widget = QPushButton("取消")
            cancel_widget.setFixedSize(50, 24)
            cancel_widget.setStyleSheet("""
                QPushButton {
                    background-color: #ff9800; color: white;
                    border: none; border-radius: 3px; font-size: 11px;
                }
                QPushButton:hover { background-color: #e68a00; }
            """)
            msg_id = msg['id']
            cancel_widget.clicked.connect(lambda checked, mid=msg_id: self._cancel_one(mid))

            item.setSizeHint(cancel_widget.sizeHint())
            self.msg_list.addItem(item)
            self.msg_list.setItemWidget(item, cancel_widget)

        count = len(pending)
        self.status_label.setText(f"待发送: {count} 条" if count > 0 else "就绪")
        self.cancel_all_btn.setEnabled(count > 0)

    def _on_error(self, error):
        self.status_label.setText(f"错误: {error}")
        self.status_label.setStyleSheet("color: #f44336;")

    def _send_message(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请输入消息内容")
            return

        try:
            r = requests.post(
                f"{BACKEND_URL}/user/message",
                json={"text": text},
                timeout=5,
            )
            if r.status_code == 200:
                self.text_input.clear()
                self.status_label.setText("指令已发送，等待 agent 接收")
                self.status_label.setStyleSheet("color: #4CAF50;")
                QTimer.singleShot(3000, lambda: self.status_label.setStyleSheet("color: #999;"))
            else:
                QMessageBox.warning(self, "发送失败", f"HTTP {r.status_code}: {r.text[:200]}")
        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "连接失败", f"无法连接后端: {BACKEND_URL}\n请确认后端已启动。")
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _cancel_one(self, msg_id):
        try:
            r = requests.delete(f"{BACKEND_URL}/user/message/{msg_id}", timeout=3)
            if r.status_code == 200:
                self.status_label.setText(f"已取消 #{msg_id}")
        except Exception:
            pass

    def _cancel_all(self):
        try:
            r = requests.delete(f"{BACKEND_URL}/user/message", timeout=3)
            if r.status_code == 200:
                self.status_label.setText("已全部取消")
        except Exception:
            pass

    def closeEvent(self, event):
        if self._fetcher:
            self._fetcher.stop()
            self._fetcher.wait(2000)
        event.accept()


def main():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName("LocalAgent 用户消息")

    window = UserMessageGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
