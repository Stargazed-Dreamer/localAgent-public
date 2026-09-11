"""BOM (U+FEFF) 扫描/清理工具

背景（2026-08-26 实验定位）：文件头 U+FEFF 堆积的真凶是 PowerShell 5.1 的
Set-Content -Encoding UTF8 / Out-File -Encoding utf8（PS 5.1 的 UTF8 必带 BOM），
agent 为写中文不乱码常恰好选中这种写法。实测：yihuan_clean.py 曾堆 3 个、
SKILL.md 2 个、yihuan_simulator/__init__.py 3 个，导致 Python 脚本 SyntaxError。
（早期归因"harness 平台写入叠加"已被对照实验推翻：Trae Write/Edit 工具不加 BOM。）
项目代码自身从不写 BOM，本工具作为写入侧问题的"检测-清理"兜底。

用法：
  uv run python tools/clean_bom.py             # 扫描（默认 dry-run，只报告）
  uv run python tools/clean_bom.py --fix       # 清理头部 BOM（无 BOM UTF-8 写回）
  uv run python tools/clean_bom.py --fix-all   # 连文件中部的 U+FEFF 一起清（慎用）
  uv run python tools/clean_bom.py --path xxx  # 指定目录或文件（默认项目根）

清理策略：
- 头部 BOM（1 个或多个连续）：PowerShell -Encoding UTF8 / 记事本造成，.py 会直接
  SyntaxError → --fix 清除
- 中部 U+FEFF：可能是正文合法字符（零宽不换行空格，聊天记录导出常见）→ 只报告，
  --fix-all 才清理
- 数据导出目录（wechat_collection 等）默认跳过：Windows 记事本导出 txt 自带 BOM
  属正常现象，不该动用户数据
- MAA patch 工作区（workspace/maa-patch/）默认跳过：含中文 .ps1 依赖 UTF-8 BOM
  才能在 PS 5.1 下解析（无 BOM 时 PS 5.1 用 GBK 解读中文，here-string 被吞 →
  ParserError，详见该目录 LESSONS.md 第10节）。清理会直接破坏 sync-maa-patch.ps1

预防：PowerShell 写 UTF-8 文件用 [IO.File]::WriteAllText($path, $content)，
不要用 Set-Content -Encoding UTF8（PS 5.1 下必带 BOM）。

退出码：发现头部 BOM 且未修复 → 1；清理完成或无问题 → 0（便于挂到收尾流程）。

原理：UTF-8 自同步特性保证 EF BB BF 字节序列不会跨越多字节字符边界，
只会是完整的 U+FEFF 字符本身，因此在字节层面直接替换是安全的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 扫描的文本扩展名（BOM 会造成实际伤害的文件类型）
SCAN_EXTS = {
    ".py", ".md", ".toml", ".json", ".qss", ".txt",
    ".yaml", ".yml", ".bat", ".ps1", ".html", ".js", ".css",
    ".ini", ".cfg", ".example", ".spec",
}

# 跳过的目录（虚拟环境/版本库/缓存/归档/模型权重等）
SKIP_DIRS = {
    ".venv", "venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".trae", ".idea", ".vscode",
    "weights", "planning_archive", ".agents_cache", "dist", "build",
    # 数据导出目录：Windows 记事本/PowerShell 导出 txt 自带 BOM，属正常现象非 harness 问题
    "wechat_collection",
    # 第三方参考项目（外部代码，保留原样）
    "references",
    # release 产物目录（staging 复制快照，不直接改）
    "release",
    # chrome 调试 profile（浏览器自动生成）
    "chrome_debug",
    # 临时目录（复制产物/审计输出，非项目源码）
    "temp",
    # MAA 上游代码（外部项目同步，不直接改）
    "maa-upstream",
    # MAA patch 工作区：workspace/maa-patch/ 下的 .ps1 含中文 + here-string，
    # PS 5.1 读无 BOM 文件会用 GBK 解读中文导致 here-string 被吞 → ParserError
    # （详见 workspace/maa-patch/LESSONS.md 第10节）。sync-maa-patch.ps1 等
    # 脚本依赖 UTF-8 BOM 才能正常运行，严禁清理此目录下任何 BOM。
    "maa-patch",
}

BOM = b"\xef\xbb\xbf"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 超过 5MB 的文件跳过（避免误读非文本大文件）


def iter_target_files(root: Path):
    """遍历 root 下所有目标文本文件"""
    if root.is_file():
        if root.suffix.lower() in SCAN_EXTS:
            yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_EXTS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        yield path


def scan_file(path: Path) -> tuple[int, int]:
    """返回 (总BOM数, 头部连续BOM数)"""
    data = path.read_bytes()
    count = data.count(BOM)
    head = (len(data) - len(data.lstrip(BOM))) // len(BOM)
    return count, head


def strip_head_bom(data: bytes) -> bytes:
    """去除头部连续 BOM，保留中部内容不动"""
    return data.lstrip(BOM)


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描/清理文件中的 U+FEFF BOM")
    parser.add_argument("--fix", action="store_true", help="清理头部 BOM（默认只报告）")
    parser.add_argument("--fix-all", action="store_true",
                        help="连中部 U+FEFF 一起清（慎用：中部可能是正文合法字符）")
    parser.add_argument("--path", default=str(PROJECT_ROOT), help="扫描目录或文件（默认项目根）")
    args = parser.parse_args()
    fix_all = args.fix_all
    do_fix = args.fix or fix_all

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"[!] 路径不存在: {root}")
        return 2

    head_issues: list[tuple[Path, int, int]] = []  # (path, head_count, mid_count)
    fixed: list[Path] = []

    for path in iter_target_files(root):
        count, head = scan_file(path)
        if count == 0:
            continue
        mid = count - head
        head_issues.append((path, head, mid))
        if do_fix and (head > 0 or fix_all):
            data = path.read_bytes()
            cleaned = data.replace(BOM, b"") if fix_all else strip_head_bom(data)
            if cleaned != data:
                path.write_bytes(cleaned)
                fixed.append(path)

    if not head_issues:
        print(f"[OK] 未发现 BOM（扫描范围: {root}）")
        return 0

    print(f"发现 {len(head_issues)} 个文件含 U+FEFF BOM:")
    head_pending = False
    for path, head, mid in head_issues:
        rel = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
        parts = []
        if head:
            parts.append(f"头部{head}个")
            if path not in fixed:
                head_pending = True
        if mid:
            parts.append(f"中部{mid}个(内容字符{'已清' if fix_all else '不清'})")
        status = " -> 已清理" if path in fixed else ""
        print(f"  {rel} : {' + '.join(parts)}{status}")

    if do_fix:
        print(f"\n[OK] 已清理 {len(fixed)} 个文件的头部 BOM"
              + ("（含中部，--fix-all）" if fix_all else ""))
        return 0
    if head_pending:
        print("\n[提示] 运行 --fix 清理头部 BOM（中部的 U+FEFF 是正文合法字符，默认保留）")
        return 1
    print("\n[OK] 头部无 BOM，仅存在中部零宽字符（正文合法，无需处理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
