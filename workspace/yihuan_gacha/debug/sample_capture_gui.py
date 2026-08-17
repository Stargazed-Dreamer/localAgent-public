r"""异环抽卡样本采集工具

PySide6 GUI，点一下按钮就截取异环游戏窗口，保存为 PNG。
用于采集 dice 识别测试样本。

用法：
    .venv\Scripts\python.exe tools\yihuan_gacha\sample_capture_gui.py

工作流：
1. 打开异环游戏，进入抽卡记录页面
2. 在本 GUI 上点"截图"按钮 → 自动截取异环窗口保存
3. 翻页/滚动到下一个典型样本 → 再点"截图"
4. 采集完毕后，缩略图列表里可以右键"在文件夹中显示"
"""
import sys
import time
import base64
import requests
from pathlib import Path
from datetime import datetime
from collections import deque

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QImage, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QScrollArea, QGridLayout,
    QFileDialog, QMessageBox, QFrame, QSizePolicy, QCheckBox,
    QDialog, QListWidget, QDialogButtonBox, QListWidgetItem
)

API = "http://127.0.0.1:8766"
# 注意：游戏窗口实际标题是 "异环  "（带两个尾随空格），见 yihuan_gacha.py 的 WINDOW
# 后端 _find_window 先精确匹配再包含匹配；GUI 标题不能含"异环"两字，否则会被误匹配
WINDOW_TITLE_DEFAULT = "异环"
OUTPUT_DIR = Path("workspace/yihuan_gacha/test_samples")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class CaptureWorker(QThread):
    """异步截图线程：先 focus 激活窗口，等渲染，再截图"""
    finished_ok = Signal(str, int, int)   # path, width, height
    failed = Signal(str)
    status_update = Signal(str)

    def __init__(self, window_title: str, save_path: Path, hwnd: int = None,
                 focus_first: bool = True, force_fullscreen_crop: bool = False):
        super().__init__()
        self.window_title = window_title
        self.save_path = save_path
        self.hwnd = hwnd
        self.focus_first = focus_first
        self.force_fullscreen_crop = force_fullscreen_crop

    def run(self):
        try:
            # Step 1: focus 激活窗口（避免截到 GUI 自己）
            if self.focus_first:
                self.status_update.emit("正在激活游戏窗口...")
                try:
                    focus_payload = {}
                    if self.hwnd:
                        focus_payload["hwnd"] = self.hwnd
                    else:
                        focus_payload["window_title"] = self.window_title
                    focus_resp = requests.post(
                        f"{API}/screen/focus-window",
                        json=focus_payload, timeout=10,
                    )
                    if focus_resp.status_code != 200:
                        # focus 失败不致命，继续截图
                        self.status_update.emit(f"focus 警告: {focus_resp.text[:100]}")
                except Exception as e:
                    self.status_update.emit(f"focus 异常(继续): {e}")
                # 等游戏渲染
                import time as _t
                _t.sleep(0.6)

            # Step 2: 截图
            self.status_update.emit("正在截图...")
            capture_payload = {
                "mode": "window",
                "format": "base64",
                "force_fullscreen_crop": self.force_fullscreen_crop,
            }
            if self.hwnd:
                capture_payload["hwnd"] = self.hwnd
            else:
                capture_payload["window_title"] = self.window_title

            resp = requests.post(
                f"{API}/screen/capture",
                json=capture_payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            img_b64 = data.get("image")
            if not img_b64:
                self.failed.emit("截图返回空 image 字段")
                return

            img_bytes = base64.b64decode(img_b64)
            self.save_path.write_bytes(img_bytes)
            captured_title = data.get("window_title", "")
            self.finished_ok.emit(
                str(self.save_path),
                int(data.get("width", 0)),
                int(data.get("height", 0)),
            )
            if captured_title:
                self.status_update.emit(f"已截窗口: '{captured_title}'")
        except requests.exceptions.ConnectionError:
            self.failed.emit(f"无法连接后端 {API}，请确认后端已启动（start.bat）")
        except requests.exceptions.HTTPError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", "")
            except Exception:
                pass
            self.failed.emit(f"HTTP {e.response.status_code}: {detail or str(e)}")
        except Exception as e:
            self.failed.emit(f"截图异常: {type(e).__name__}: {e}")


class WindowPickerDialog(QDialog):
    """列出所有窗口，让用户选一个，返回 hwnd"""

    def __init__(self, parent=None, keyword: str = ""):
        super().__init__(parent)
        self.setWindowTitle("选择游戏窗口")
        self.resize(600, 500)
        self.selected_hwnd = None
        self.selected_title = None

        layout = QVBoxLayout(self)
        hint = QLabel(f"关键词: '{keyword}'  （双击或选中后点确定）\n注意：标题含尾随空格是正常的（如 '异环  '）")
        hint.setStyleSheet("color: #666; padding: 4px;")
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._accept_item)
        layout.addWidget(self.list_widget)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._load(keyword)

    def _load(self, keyword: str):
        try:
            resp = requests.get(f"{API}/screen/windows", timeout=10)
            resp.raise_for_status()
            windows = resp.json().get("windows", [])
        except Exception as e:
            QMessageBox.critical(self, "失败", f"获取窗口列表失败: {e}")
            return

        # 过滤：标题非空 + 关键词匹配（不区分大小写）
        kw = (keyword or "").strip().lower()
        candidates = []
        for w in windows:
            title = w.get("title", "")
            if not title.strip():
                continue
            if w.get("is_minimized"):
                continue
            if kw and kw not in title.lower():
                continue
            candidates.append(w)

        # 把游戏窗口排前面（标题更短的优先，"异环  " 比 "异环XXX" 短）
        candidates.sort(key=lambda w: (len(w.get("title", "")), w.get("z_order", 999)))

        for w in candidates:
            title = w.get("title", "")
            hwnd = w.get("hwnd", 0)
            pid = w.get("pid", 0)
            proc = w.get("process_name", "")
            bw = w.get("width", 0)
            bh = w.get("height", 0)
            # 标题里的尾随空格用 □ 表示，方便看出
            title_display = title.replace(" ", "□")
            text = f"hwnd={hwnd}  '{title_display}'  {bw}x{bh}  pid={pid}  {proc}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, (hwnd, title))
            self.list_widget.addItem(item)

        if not self.list_widget.count():
            QMessageBox.information(self, "无匹配", f"没有标题含 '{keyword}' 的窗口")

    def _accept_item(self, item):
        self.selected_hwnd, self.selected_title = item.data(Qt.UserRole)
        self.accept()

    def accept(self):
        cur = self.list_widget.currentItem()
        if cur:
            self.selected_hwnd, self.selected_title = cur.data(Qt.UserRole)
        super().accept()


class ThumbnailWidget(QFrame):
    """单张缩略图卡片"""
    double_clicked = Signal(str)  # path

    def __init__(self, path: str, pixmap: QPixmap, index: int):
        super().__init__()
        self.path = path
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(1)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        lbl = QLabel()
        lbl.setPixmap(pixmap)
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        info = QLabel(f"#{index:03d}  {Path(path).name}")
        info.setStyleSheet("font-size: 10px; color: #666;")
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        # 右键菜单：在文件夹中显示
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        act_reveal = QAction("在文件夹中显示", self)
        act_reveal.triggered.connect(self._reveal)
        self.addAction(act_reveal)
        act_open = QAction("用默认程序打开", self)
        act_open.triggered.connect(self._open)
        self.addAction(act_open)
        act_delete = QAction("删除此图", self)
        act_delete.triggered.connect(self._delete)
        self.addAction(act_delete)

    def _reveal(self):
        # Windows: 在资源管理器中选中文件
        import subprocess
        subprocess.run(["explorer", "/select,", self.path], shell=False)

    def _open(self):
        import os
        os.startfile(self.path)

    def _delete(self):
        try:
            Path(self.path).unlink()
            self.setParent(None)
            self.deleteLater()
        except Exception as e:
            QMessageBox.warning(self, "删除失败", str(e))

    def mouseDoubleClickEvent(self, e):
        self.double_clicked.emit(self.path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # 标题不能含"异环"两字，否则后端 _find_window 包含匹配会误中本 GUI
        self.setWindowTitle("抽卡样本采集器")
        self.resize(1100, 760)

        # 选中的窗口（优先用 hwnd，避免标题歧义）
        self.selected_hwnd = None
        self.selected_title = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # ===== 顶部控制区 =====
        top = QHBoxLayout()
        top.setSpacing(8)

        top.addWidget(QLabel("窗口标题:"))
        self.title_edit = QLineEdit(WINDOW_TITLE_DEFAULT)
        self.title_edit.setMinimumWidth(120)
        top.addWidget(self.title_edit)

        self.pick_btn = QPushButton("🎯 选择窗口")
        self.pick_btn.setToolTip("列出所有含关键词的窗口，选一个避免歧义（推荐）")
        self.pick_btn.clicked.connect(self._on_pick_window)
        top.addWidget(self.pick_btn)

        self.win_label = QLabel("未选择窗口（用标题匹配）")
        self.win_label.setStyleSheet("color: #666; padding: 0 6px;")
        top.addWidget(self.win_label)

        top.addStretch(1)

        self.capture_btn = QPushButton("📸  截图并保存")
        self.capture_btn.setMinimumHeight(44)
        self.capture_btn.setStyleSheet(
            "QPushButton { background-color: #2196F3; color: white; "
            "font-size: 14px; font-weight: bold; padding: 0 24px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #1976D2; }"
            "QPushButton:pressed { background-color: #0D47A1; }"
            "QPushButton:disabled { background-color: #9E9E9E; }"
        )
        self.capture_btn.clicked.connect(self._on_capture)
        top.addWidget(self.capture_btn)

        self.refresh_btn = QPushButton("🔄 刷新列表")
        self.refresh_btn.clicked.connect(self._refresh_list)
        top.addWidget(self.refresh_btn)

        self.clear_btn = QPushButton("🗑 清空目录")
        self.clear_btn.clicked.connect(self._clear_dir)
        top.addWidget(self.clear_btn)

        top.addStretch(1)
        self.count_label = QLabel("已采集 0 张")
        self.count_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #2196F3;")
        top.addWidget(self.count_label)

        outer.addLayout(top)

        # ===== 选项区 =====
        opt = QHBoxLayout()
        self.focus_chk = QCheckBox("截图前 focus 激活窗口（推荐，避免截到 GUI）")
        self.focus_chk.setChecked(True)
        opt.addWidget(self.focus_chk)

        self.force_fs_chk = QCheckBox("force_fullscreen_crop（DirectX 全屏游戏用，PrintWindow 假成功时勾上）")
        self.force_fs_chk.setChecked(False)
        opt.addWidget(self.force_fs_chk)

        opt.addStretch(1)
        outer.addLayout(opt)

        # ===== 提示区 =====
        hint = QLabel(
            "工作流：1) 点\"选择窗口\"选游戏窗口（避免歧义）；2) 游戏进入抽卡记录页；3) 点\"截图\"或按空格键；4) 翻到下一条 → 再截图。\n"
            "建议采集：5/6点骰子、不同星级、集点赠礼、沉眠地、各种道具（迷迭/失纬棋子）各2-3张。"
        )
        hint.setStyleSheet(
            "QLabel { background-color: #FFF8E1; color: #5D4037; "
            "padding: 8px; border-radius: 4px; font-size: 12px; }"
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # ===== 缩略图滚动区 =====
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: 1px solid #ddd; border-radius: 4px; }")

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_layout.setSpacing(8)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_container)
        outer.addWidget(self.scroll, 1)

        # ===== 状态栏 =====
        self.status = self.statusBar()
        self.status.showMessage("就绪")

        # ===== 状态 =====
        self.worker = None
        self.thumbnails = deque(maxlen=200)  # 仅保留最近 200 个 widget 引用
        self._next_index = 1

        self._refresh_list()

    # ---------- 操作 ----------

    def _on_pick_window(self):
        keyword = self.title_edit.text().strip()
        dlg = WindowPickerDialog(self, keyword)
        if dlg.exec() == QDialog.Accepted and dlg.selected_hwnd:
            self.selected_hwnd = dlg.selected_hwnd
            self.selected_title = dlg.selected_title
            # 显示选中的窗口（尾随空格用 □ 显示）
            title_disp = (self.selected_title or "").replace(" ", "□")
            self.win_label.setText(f"hwnd={self.selected_hwnd}  '{title_disp}'")
            self.win_label.setStyleSheet("color: #1B5E20; padding: 0 6px; font-weight: bold;")
            self.status.showMessage(f"已选窗口 hwnd={self.selected_hwnd}", 4000)

    def _on_capture(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "请稍候", "上一次截图还没完成")
            return

        title = self.title_edit.text().strip()
        if not self.selected_hwnd and not title:
            QMessageBox.warning(self, "参数缺失", "请填写窗口标题或点\"选择窗口\"")
            return

        # 生成文件名: sample_YYYYMMDD_HHMMSS_nnn.png
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        idx = self._next_index
        self._next_index += 1
        save_path = OUTPUT_DIR / f"sample_{ts}_{idx:03d}.png"

        self.capture_btn.setEnabled(False)
        self.status.showMessage(f"正在截图... (hwnd={self.selected_hwnd or 'N/A'}, title='{title}')")
        self.worker = CaptureWorker(
            window_title=title,
            save_path=save_path,
            hwnd=self.selected_hwnd,
            focus_first=self.focus_chk.isChecked(),
            force_fullscreen_crop=self.force_fs_chk.isChecked(),
        )
        self.worker.finished_ok.connect(self._on_capture_ok)
        self.worker.failed.connect(self._on_capture_fail)
        self.worker.status_update.connect(self._on_status_update)
        self.worker.start()

    def _on_status_update(self, msg: str):
        self.status.showMessage(msg, 3000)

    def _on_capture_ok(self, path: str, w: int, h: int):
        self.capture_btn.setEnabled(True)
        self.status.showMessage(f"已保存: {Path(path).name}  ({w}x{h})", 5000)
        self._add_thumbnail(path)
        self._update_count()

    def _on_capture_fail(self, err: str):
        self.capture_btn.setEnabled(True)
        self.status.showMessage(f"截图失败: {err}", 8000)
        QMessageBox.critical(self, "截图失败", err)

    # ---------- 缩略图列表 ----------

    def _refresh_list(self):
        # 清空
        while self.grid_layout.count():
            it = self.grid_layout.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.thumbnails.clear()

        # 重新加载目录
        files = sorted(OUTPUT_DIR.glob("sample_*.png"))
        if not files:
            self._next_index = 1
        else:
            # 解析最大序号
            max_idx = 0
            for f in files:
                # sample_YYYYMMDD_HHMMSS_nnn.png
                stem = f.stem
                try:
                    idx = int(stem.rsplit("_", 1)[-1])
                    if idx > max_idx:
                        max_idx = idx
                except ValueError:
                    pass
            self._next_index = max_idx + 1

        for f in files:
            self._add_thumbnail(str(f))

        self._update_count()

    def _add_thumbnail(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            return
        thumb = pm.scaled(
            200, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        idx = len(self.thumbnails) + 1
        w = ThumbnailWidget(path, thumb, idx)
        w.double_clicked.connect(self._on_thumb_double_click)
        # 追加到网格（每行5个）
        row = (self.grid_layout.count()) // 5
        col = (self.grid_layout.count()) % 5
        self.grid_layout.addWidget(w, row, col)
        self.thumbnails.append(w)

    def _on_thumb_double_click(self, path: str):
        # 双击用默认程序打开原图
        import os
        os.startfile(path)

    def _update_count(self):
        n = len(list(OUTPUT_DIR.glob("sample_*.png")))
        self.count_label.setText(f"已采集 {n} 张")

    def _clear_dir(self):
        files = list(OUTPUT_DIR.glob("sample_*.png"))
        if not files:
            QMessageBox.information(self, "清空目录", "目录已为空")
            return
        ret = QMessageBox.question(
            self, "清空目录",
            f"确认删除 {len(files)} 张样本图片？\n路径: {OUTPUT_DIR}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if ret != QMessageBox.Yes:
            return
        for f in files:
            try:
                f.unlink()
            except Exception:
                pass
        self._refresh_list()
        self.status.showMessage(f"已清空 {len(files)} 张样本", 3000)

    # ---------- 快捷键 ----------

    def keyPressEvent(self, e):
        # F5 或 空格 = 截图（快捷键，比鼠标点更快）
        if e.key() in (Qt.Key_F5, Qt.Key_Space):
            self._on_capture()
        elif e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Yihuan Sample Capture")
    w = MainWindow()
    w.show()
    # 首次提示
    QTimer = None
    try:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, lambda: w.status.showMessage(
            "提示: 按空格键或 F5 可快速截图（无需点按钮）", 5000
        ))
    except Exception:
        pass
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
