"""LocalAgent 共享 UI 主题基础设施。

全项目 GUI（client / recorder / 未来所有 PySide6 界面）统一使用本模块，
设计规范见 docs/ui/ 目录（style-guide.md / components.md / icons.md /
patterns.md / agent-cookbook.md）。

用法::

    from lib.ui import apply_theme, icon
    from lib.ui import tokens

    app = QApplication(sys.argv)
    apply_theme(app)                       # 一套深色工程主题
    btn.setIcon(icon("star", tokens.ICON_ACTIVE))
"""

from lib.ui import tokens  # noqa: F401
from lib.ui.controls import icon_button  # noqa: F401
from lib.ui.icons import available_icons, icon, icon_pixmap  # noqa: F401
from lib.ui.theme import apply_theme, build_qss  # noqa: F401
