"""T5 单测：ScreenshotViewer 缩放/平移/前后帧导航。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QColor, QImage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace.recorder.tools.editor.widgets.screenshot_viewer import (
    MAX_ZOOM,
    MIN_ZOOM,
    ScreenshotViewer,
)


def _make_frames(tmp_path: Path, count: int = 3) -> list[Path]:
    """生成 count 张测试 PNG（不同颜色以区分）。"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    paths = []
    colors = ["#FF0000", "#00FF00", "#0000FF"]
    for i in range(count):
        img = QImage(64, 48, QImage.Format.Format_RGB32)
        img.fill(QColor(colors[i % len(colors)]))
        p = frames_dir / f"frame_{i:03d}.png"
        assert img.save(str(p))
        paths.append(p)
    return paths


def test_viewer_constructs_with_empty_frames(qapp) -> None:
    v = ScreenshotViewer([], 0)
    assert v.current_index() == 0
    assert v.current_frame_path() is None


def test_viewer_initial_index_clamped(qapp, tmp_path: Path) -> None:
    """初始索引越界时夹到合法区间。"""
    frames = _make_frames(tmp_path, 3)
    v = ScreenshotViewer(frames, current_index=99)
    assert v.current_index() == 2
    v2 = ScreenshotViewer(frames, current_index=-5)
    assert v2.current_index() == 0


def test_viewer_prev_next_navigation(qapp, tmp_path: Path) -> None:
    """←/→ 按钮与 _prev/_next 帧导航。"""
    frames = _make_frames(tmp_path, 3)
    v = ScreenshotViewer(frames, current_index=1)
    assert v.current_index() == 1
    assert v.current_frame_path() == frames[1]

    # 上一帧
    v._prev_frame()
    assert v.current_index() == 0
    # 再上一帧（已在首位，不应越界）
    v._prev_frame()
    assert v.current_index() == 0

    # 下一帧
    v._next_frame()
    v._next_frame()
    assert v.current_index() == 2
    # 再下一帧（已在末位，不应越界）
    v._next_frame()
    assert v.current_index() == 2


def test_viewer_nav_buttons_enabled_state(qapp, tmp_path: Path) -> None:
    """前后帧按钮的启用/禁用应随索引变化。"""
    frames = _make_frames(tmp_path, 3)
    v = ScreenshotViewer(frames, current_index=0)
    assert v._prev_btn.isEnabled() is False
    assert v._next_btn.isEnabled() is True

    v._next_frame()
    assert v._prev_btn.isEnabled() is True
    assert v._next_btn.isEnabled() is True

    v._next_frame()
    assert v._prev_btn.isEnabled() is True
    assert v._next_btn.isEnabled() is False


def test_viewer_frame_changed_signal(qapp, tmp_path: Path) -> None:
    """帧切换应发出 frame_changed 信号。"""
    frames = _make_frames(tmp_path, 3)
    v = ScreenshotViewer(frames, current_index=0)
    received: list[int] = []
    v.frame_changed.connect(received.append)

    v._next_frame()
    v._next_frame()
    assert received == [1, 2]


def test_viewer_zoom_limits(qapp, tmp_path: Path) -> None:
    """缩放应在 10%-800% 范围内，超出边界不再变化。"""
    frames = _make_frames(tmp_path, 1)
    v = ScreenshotViewer(frames, current_index=0)
    # 切到 100% 以获得确定的起始缩放
    v._set_100_percent()
    assert abs(v._view.transform().m11() - 1.0) < 1e-3

    # 持续放大直到上限
    for _ in range(50):
        v._zoom_in()
    assert v._view.transform().m11() <= MAX_ZOOM + 1e-3

    # 重置后持续缩小直到下限
    v._set_100_percent()
    for _ in range(50):
        v._zoom_out()
    assert v._view.transform().m11() >= MIN_ZOOM - 1e-3


def test_viewer_toggle_fit_switches_modes(qapp, tmp_path: Path) -> None:
    """双击/按钮在适应窗口与 100% 间切换。"""
    frames = _make_frames(tmp_path, 1)
    v = ScreenshotViewer(frames, current_index=0)
    # 初始为适应模式
    assert v._fit_mode is True

    # 切换到 100%
    v._toggle_fit()
    assert v._fit_mode is False
    assert abs(v._view.transform().m11() - 1.0) < 1e-3

    # 切换回适应
    v._toggle_fit()
    assert v._fit_mode is True


def test_viewer_title_shows_n_of_m(qapp, tmp_path: Path) -> None:
    """标题应显示「文件名 · 第 N/M 帧」格式。"""
    frames = _make_frames(tmp_path, 3)
    v = ScreenshotViewer(frames, current_index=1)
    title = v._title_label.text()
    assert "第 2/3 帧" in title
    assert "frame_001.png" in title


def test_viewer_handles_missing_frame_file(qapp, tmp_path: Path) -> None:
    """帧文件被删除后查看器不应崩溃，标题显示加载失败。"""
    frames = _make_frames(tmp_path, 1)
    v = ScreenshotViewer(frames, current_index=0)
    # 删除文件后重新加载
    frames[0].unlink()
    v._load_current_frame()
    assert "加载失败" in v._title_label.text()
