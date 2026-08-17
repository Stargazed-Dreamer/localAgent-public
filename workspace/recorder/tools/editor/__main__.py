"""L3 编辑器 CLI 入口。

启动：
    # 位置参数（CLI 直接调用）
    python -m workspace.recorder.tools.editor <package_path>

    # --package-path 选项（工具面板 / 脚本调用）
    python -m workspace.recorder.tools.editor --package-path <package_path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from workspace.recorder.tools.editor.editor_app import run_editor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workspace.recorder.tools.editor",
        description="L3 编辑器：查看时间轴 + 标注/移除块/音频调参 + 标记就绪",
    )
    parser.add_argument(
        "package_path",
        nargs="?",
        help="录制包目录路径（如 workspace/recorder/recordings/rec_20260723_143000）",
    )
    parser.add_argument(
        "--package-path",
        dest="package_path_opt",
        help="录制包目录路径（与位置参数二选一，供工具面板传参用）",
    )
    args = parser.parse_args(argv)

    path_str = args.package_path_opt or args.package_path
    if not path_str:
        parser.print_help(sys.stderr)
        return 1

    package_path = Path(path_str).resolve()
    if not package_path.exists():
        print(f"[error] 录制包目录不存在：{package_path}", file=sys.stderr)
        return 1
    return run_editor(package_path)


if __name__ == "__main__":
    raise SystemExit(main())
