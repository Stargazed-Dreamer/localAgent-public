"""SVG 图标加载与着色。

图标存放在 ``lib/ui/resources/icons/``，规范：24x24 viewBox、stroke=currentColor、
stroke-width 2、圆角端点（Lucide 风格自绘，清单见 docs/ui/icons.md）。

用法::

    from lib.ui import icon, tokens
    action.setIcon(icon("star", tokens.ICON_ACTIVE))
    label.setPixmap(icon_pixmap("image", tokens.TEXT_TERTIARY, 48))

颜色参数传 tokens 常量；新增图标只需把 .svg 放进 resources/icons/ 并在
docs/ui/icons.md 登记用途。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from lib.ui import tokens as T

_ICON_DIR = Path(__file__).resolve().parent / "resources" / "icons"
_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def available_icons() -> list[str]:
    """返回全部可用图标名（不含 .svg 后缀），按字母排序。"""
    return sorted(p.stem for p in _ICON_DIR.glob("*.svg"))


def icon_path(name: str) -> Path:
    p = _ICON_DIR / f"{name}.svg"
    if not p.is_file():
        raise KeyError(f"未知图标 {name!r}，可用：{', '.join(available_icons())}")
    return p


def render_svg(name: str, color: str, size: int, dpr: float = 1.0) -> QPixmap:
    """把图标渲染为 QPixmap（currentColor 替换为 color，按 dpr 超采样）。"""
    key = (name, color, size, dpr)
    if key in _cache:
        return _cache[key]
    svg = icon_path(name).read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError(f"图标 SVG 无效: {name}")
    pm = QPixmap(max(1, round(size * dpr)), max(1, round(size * dpr)))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, pm.width(), pm.height()))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    _cache[key] = pm
    return pm


def _default_dpr() -> float:
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0


def icon_pixmap(name: str, color: str = T.ICON_DEFAULT, size: int = T.ICON_SIZE_MD) -> QPixmap:
    """渲染图标为 QPixmap（自动按主屏 devicePixelRatio 超采样）。"""
    return render_svg(name, color, size, _default_dpr())


def icon(name: str, color: str = T.ICON_DEFAULT, size: int = T.ICON_SIZE_MD) -> QIcon:
    """渲染图标为 QIcon。"""
    return QIcon(icon_pixmap(name, color, size))
