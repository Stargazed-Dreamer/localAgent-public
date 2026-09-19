"""SpaceSniffer 快照浏览器 —— 命令行入口。

用法::

    .venv\\Scripts\\python.exe tools/spacesniffer/main.py                # 打开空窗口
    .venv\\Scripts\\python.exe tools/spacesniffer/main.py <快照.sns>     # 直接加载
    .venv\\Scripts\\python.exe tools/spacesniffer/main.py --info <快照.sns>   # 只打信息不开窗

Windows 上也可以直接把 `.sns` 文件拖到 `start.bat` 上；窗口本身也支持拖放。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 与 viewer.py 相同的引导：tools/ 下各工具目录都不是包，同级模块走扁平导入。
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import sns_model  # noqa: E402


def _print_info(path: str) -> int:
    """`--info`：不开窗，只把解析结果的关键指标打出来（排查/脚本化用）。"""
    snapshot = sns_model.parse_file(path)
    print(f"文件        {path}")
    print(f"体积        {sns_model.human_size(snapshot.file_size or 0)}")
    print(f"数据时点    {snapshot.scanned_at() or '未知'}")
    print(f"卷容量      {sns_model.human_size(snapshot.total_bytes)}")
    print(f"已用        {sns_model.human_size(snapshot.used_bytes)}")
    print(f"可用        {sns_model.human_size(snapshot.free_bytes)}")
    print(f"目录 / 文件 {snapshot.n_dirs:,} / {snapshot.n_files:,}")
    print(f"覆盖完整    {'是' if snapshot.is_complete else '否'}（未计入 {sns_model.human_size(snapshot.unknown_bytes)}）")
    top = sorted(snapshot.root.children or [], key=lambda c: -c.size)[:10]
    print("顶层最大的 10 项（目录或根目录下的大文件）：")
    for entry in top:
        kind = "目录" if entry.is_dir else "文件"
        print(
            f"  {sns_model.human_size(entry.size):>10}  {kind}  "
            f"{entry.name}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spacesniffer",
        description="SpaceSniffer 快照浏览器：读取 .sns 快照并用 treemap 可视化",
    )
    parser.add_argument(
        "snapshot",
        nargs="?",
        help="要打开的 .sns 快照路径（省略则启动空窗口，可从菜单/拖放打开）",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="只在命令行打印快照摘要，不启动图形界面",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.snapshot and not Path(args.snapshot).exists():
        print(f"找不到快照文件：{args.snapshot}", file=sys.stderr)
        return 2

    if args.info:
        if not args.snapshot:
            parser.error("--info 需要同时给出快照路径")
        try:
            return _print_info(args.snapshot)
        except sns_model.SnsParseError as exc:
            print(f"解析失败：{exc}", file=sys.stderr)
            return 1

    import viewer

    # 解析在后台线程里做，失败时窗口会弹提示，这里不再重复捕获。
    return viewer.launch(args.snapshot, argv=sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())
