"""LocalAgent 客户端入口

启动方式：python -m client.main
"""

import sys
from typing import cast

from PySide6.QtWidgets import QApplication

from client.core.app import MainWindow
from lib.ui import apply_theme


def main():
    app = cast(QApplication, QApplication.instance() or QApplication(sys.argv))
    app.setApplicationName("LocalAgent Client")
    app.setOrganizationName("LocalAgent")
    app.setApplicationDisplayName("LocalAgent 客户端")

    # 全局深色工程主题（含 Fusion 风格 + 字体 + QSS）
    apply_theme(app)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
