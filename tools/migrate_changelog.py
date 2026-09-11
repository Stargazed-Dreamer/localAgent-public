#!/usr/bin/env python3
"""
CHANGELOG 迁移脚本：把旧 release 段从 CHANGELOG.md 移到 docs/changelog-archive.md

策略：
  - CHANGELOG.md 只保留 [Unreleased] + 最近 N 个 release（默认 N=1）
  - 较老的 release 移到 docs/changelog-archive.md（newest first，与 CHANGELOG.md 顺序一致）
  - 归档文件已存在时，新归档段插在归档头部（紧跟 # Changelog Archive 标题）
  - 幂等：已在归档文件中的版本不会被重复写入；CHANGELOG.md 若无可归档段则跳过

用法:
  uv run python tools/migrate_changelog.py             # 默认保留 1 个 release
  uv run python tools/migrate_changelog.py --keep 2     # 保留最近 2 个 release
  uv run python tools/migrate_changelog.py --dry-run    # 只打印，不写入
"""

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
DEFAULT_ARCHIVE = PROJECT_ROOT / "docs" / "changelog-archive.md"

ARCHIVE_HEADER = """# Changelog Archive

历史 release 条目归档。主 `CHANGELOG.md` 仅保留 `[Unreleased]` + 最近 1 个 release 段，
避免活跃段过长导致 agent 编辑时误伤历史版本段。

如需查询完整变更历史，请同时阅读 `CHANGELOG.md` 和本文件。本文件按版本号倒序排列（最新在最上）。

"""

SECTION_HEADER_RE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def parse_sections(content: str):
    """拆 CHANGELOG 内容为 (preamble, sections)。

    preamble 是首个 `## [...]` 行之前的所有内容（标题 + 说明 + 空行）。
    每个 section 是 (header_line, body)，header_line 不含末尾换行符；
    body 是 header 行换行后到下一个 section 起点之间的全部内容（含尾随空行）。
    """
    matches = list(SECTION_HEADER_RE.finditer(content))
    if not matches:
        return content, []

    preamble = content[: matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        header_start = m.start()
        newline_pos = content.find("\n", m.end())
        if newline_pos == -1:
            header_line = content[header_start:]
            body_start = len(content)
        else:
            header_line = content[header_start:newline_pos]
            body_start = newline_pos + 1

        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end]
        sections.append((header_line, body))
    return preamble, sections


def extract_version(header_line: str):
    """从 `## [0.7.1] - 2026-07-13` 提取 version 字符串；`## [Unreleased]` 返回 'Unreleased'。"""
    m = re.match(r"^## \[([^\]]+)\]", header_line)
    return m.group(1) if m else None


def main():
    parser = argparse.ArgumentParser(description="把旧 release 段从 CHANGELOG.md 迁到归档文件")
    parser.add_argument("--keep", type=int, default=1, help="保留最近 N 个 release 段（默认 1）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的操作，不写入文件")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG, help="CHANGELOG.md 路径")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="归档文件路径")
    args = parser.parse_args()

    if not args.changelog.exists():
        print(f"ERROR: CHANGELOG.md 不存在：{args.changelog}", file=sys.stderr)
        return 1

    content = args.changelog.read_text(encoding="utf-8")
    preamble, sections = parse_sections(content)

    unreleased = None
    releases = []
    for header_line, body in sections:
        version = extract_version(header_line)
        if version == "Unreleased":
            unreleased = (header_line, body)
        elif version is not None:
            releases.append((header_line, body))

    if unreleased is None:
        print("ERROR: CHANGELOG.md 缺少 [Unreleased] 段", file=sys.stderr)
        return 1

    print(f"CHANGELOG.md 现有 {len(releases)} 个 release 段：")
    for h, _ in releases:
        print(f"  {h}")

    if len(releases) <= args.keep:
        print(f"\n保留 {args.keep} 个，当前只有 {len(releases)} 个，无需归档")
        return 0

    to_keep = releases[: args.keep]
    to_archive = releases[args.keep :]

    print(f"\n保留 [Unreleased] + 最近 {len(to_keep)} 个 release：")
    for h, _ in to_keep:
        print(f"  {h}")
    print(f"待归档 {len(to_archive)} 个 release 到 {args.archive}：")
    for h, _ in to_archive:
        print(f"  {h}")

    if args.dry_run:
        print("\n[dry-run] 不写入文件")
        return 0

    # 读取已有归档（保留 release 段，丢掉老 header）
    existing_archive_releases = ""
    if args.archive.exists():
        old = args.archive.read_text(encoding="utf-8")
        m = SECTION_HEADER_RE.search(old)
        if m:
            existing_archive_releases = old[m.start():]

    already_archived_versions = set()
    for header_line, _ in parse_sections(existing_archive_releases)[1]:
        v = extract_version(header_line)
        if v:
            already_archived_versions.add(v)

    fresh_to_archive = []
    skipped = []
    for header_line, body in to_archive:
        v = extract_version(header_line)
        if v in already_archived_versions:
            skipped.append(header_line)
        else:
            fresh_to_archive.append((header_line, body))

    if skipped:
        print(f"\n跳过已归档的 {len(skipped)} 个版本：")
        for h in skipped:
            print(f"  {h}")

    # 重写 CHANGELOG.md：preamble + [Unreleased] + 保留的 release
    new_changelog_parts = [preamble.rstrip() + "\n\n"]
    new_changelog_parts.append(unreleased[0] + "\n" + unreleased[1].rstrip() + "\n")
    for header_line, body in to_keep:
        new_changelog_parts.append("\n" + header_line + "\n" + body.rstrip() + "\n")
    new_changelog_parts.append("\n")
    new_changelog = "".join(new_changelog_parts)
    args.changelog.write_text(new_changelog, encoding="utf-8")

    # 写归档文件：header + 新归档段（newest first）+ 已有归档段
    new_archive_parts = [ARCHIVE_HEADER]
    for header_line, body in fresh_to_archive:
        new_archive_parts.append(header_line + "\n" + body.rstrip() + "\n\n")
    new_archive_parts.append(existing_archive_releases)
    new_archive = "".join(new_archive_parts)

    args.archive.parent.mkdir(parents=True, exist_ok=True)
    args.archive.write_text(new_archive, encoding="utf-8")

    print(f"\nOK {args.changelog}: 保留 [Unreleased] + {len(to_keep)} 个 release")
    print(
        f"OK {args.archive}: 新增 {len(fresh_to_archive)} 个，"
        f"总计 {len(fresh_to_archive) + len(already_archived_versions)} 个归档版本"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
