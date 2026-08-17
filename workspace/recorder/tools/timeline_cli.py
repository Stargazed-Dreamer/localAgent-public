"""L2 时间轴层 CLI 入口（spec-l2.md Ticket 22）

用法：
    # 默认：构建 timeline.json + 初始化 annotations.json + 打印预览
    python -m workspace.recorder.tools.timeline_cli <package_path>

    # 只预览不重新构建（要求 timeline.json 已存在）
    python -m workspace.recorder.tools.timeline_cli <package_path> --preview-only

    # 自定义章节切分空闲阈值（默认 2.0s）
    python -m workspace.recorder.tools.timeline_cli <package_path> --idle-threshold 3.0

设计：
- 默认流程：build_timeline → init_annotations → format_preview
- --preview-only：跳过 build_timeline，直接 format_preview（适合查看已有 timeline.json）
- 预览输出到 stdout，便于管道处理和自动化测试
- 错误输出到 stderr，不污染 stdout 的预览内容
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lib.recorder.timeline import build_timeline, init_annotations
from lib.recorder.timeline.preview import format_preview


def build_arg_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="workspace.recorder.tools.timeline_cli",
        description="L2 时间轴层 CLI：构建 timeline.json + 初始化 annotations.json + 预览",
    )
    parser.add_argument(
        "package_path",
        nargs="?",
        type=str,
        help="录制包根目录路径（含 meta.json + blocks.json + keyframes.json + transcript.json）",
    )
    parser.add_argument(
        "--package-path",
        dest="package_path_opt",
        type=str,
        help="录制包根目录路径（与位置参数二选一，供工具面板传参用）",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只预览不重新构建 timeline.json（要求 timeline.json 已存在）",
    )
    parser.add_argument(
        "--idle-threshold",
        type=float,
        default=2.0,
        help="章节切分空闲阈值（秒），默认 2.0（连续空闲超过此值切一个新章节）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Args:
        argv: 命令行参数，None 时用 sys.argv[1:]

    Returns:
        退出码：0 成功 / 1 失败
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    path_str = args.package_path_opt or args.package_path
    if not path_str:
        parser.print_help(sys.stderr)
        return 1

    package_path = Path(path_str)
    if not package_path.exists():
        print(
            f"[error] 录制包目录不存在：{package_path}",
            file=sys.stderr,
        )
        return 1
    if not package_path.is_dir():
        print(
            f"[error] 路径不是目录：{package_path}",
            file=sys.stderr,
        )
        return 1

    if args.preview_only:
        # 只预览模式：要求 timeline.json 已存在
        timeline_path = package_path / "timeline.json"
        if not timeline_path.exists():
            print(
                f"[error] timeline.json 不存在：{timeline_path}",
                file=sys.stderr,
            )
            print(
                "[hint] 请去掉 --preview-only 先构建 timeline.json",
                file=sys.stderr,
            )
            return 1
    else:
        # 默认模式：构建 timeline.json + 初始化 annotations.json
        options = {"idle_threshold": args.idle_threshold}
        result = build_timeline(package_path, options=options)

        if result.errors:
            print("[warn] 构建过程中有以下错误（已降级处理）：", file=sys.stderr)
            for stage, err in result.errors.items():
                print(f"  - {stage}: {err}", file=sys.stderr)

        # 初始化 annotations.json（已存在时不覆盖）
        init_annotations(package_path)

        print(
            f"[ok] timeline.json 已生成：{result.timeline_path}",
            file=sys.stderr,
        )
        print(
            f"[ok] 块总数：{result.block_count}，章节：{result.chapter_count}",
            file=sys.stderr,
        )

    # 打印预览到 stdout
    preview_text = format_preview(package_path)
    print(preview_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
