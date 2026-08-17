"""L3 编辑器启动逻辑：检查 timeline.json + 加载 + 启动 GUI。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from lib.recorder.editor.store import AnnotationStore
from lib.recorder.timeline import build_timeline

logger = logging.getLogger(__name__)


def load_timeline(package_path: Path) -> dict:
    """加载 timeline.json，不存在则调 build_timeline 生成。"""
    timeline_path = package_path / "timeline.json"
    if not timeline_path.exists():
        logger.info("timeline.json 不存在，自动构建...")
        build_timeline(package_path)
    with timeline_path.open(encoding="utf-8") as f:
        return json.load(f)


def run_editor(package_path: Path) -> int:
    """启动编辑器 GUI。

    Returns: 进程退出码。
    """
    try:
        from PySide6.QtWidgets import QApplication

        from lib.ui import apply_theme
        from workspace.recorder.tools.editor.editor_window import EditorWindow

        timeline = load_timeline(package_path)
        store = AnnotationStore()
        store.load(package_path / "annotations.json")

        app = QApplication.instance() or QApplication([])
        apply_theme(app)
        window = EditorWindow(package_path=package_path, timeline=timeline, store=store)
        window.show()
        return app.exec()
    except Exception:
        logger.exception("编辑器启动失败")
        return 1
