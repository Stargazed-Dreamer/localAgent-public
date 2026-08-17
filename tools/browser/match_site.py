#!/usr/bin/env python
"""按域名匹配 browser_lessons 站点经验文件。

对标 eze-is/web-access 的 scripts/match-site.mjs，用 Python 重写。
扫描 .agents/skills/browser_lessons/sites/*.md，按主域 + aliases 匹配，
输出命中文件的正文（去掉 frontmatter）。

用法:
  python tools/browser/match_site.py xiaoheihe.cn
  python tools/browser/match_site.py mp.weixin.qq.com
  python tools/browser/match_site.py bilibili

退出码:
  0 = 正常（无论是否命中）
  1 = 参数错误

设计要点:
  - 直接扫目录，不依赖 site_index.md（消除索引不同步隐患）
  - 支持 frontmatter 中的 aliases 字段（主域的别名列表）
  - 正则匹配，大小写不敏感
  - 输出正文时去掉 YAML frontmatter（--- 包裹的部分）
"""

import re
import sys
from pathlib import Path

# sites 目录：项目根/.agents/skills/browser_lessons/sites/
SITES_DIR = Path(__file__).parent.parent.parent / ".agents" / "skills" / "browser_lessons" / "sites"


def _strip_frontmatter(raw: str) -> str:
    """去掉 Markdown 开头的 YAML frontmatter（--- 包裹），返回正文。"""
    fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
    if len(fences) >= 2:
        return raw[fences[1].end():].lstrip("\r\n")
    return raw


def _extract_aliases(raw: str) -> list[str]:
    """从 frontmatter 中提取 aliases 字段。

    支持两种格式:
      aliases: [示例, Example]      # 行内数组
      aliases:                       # 多行数组
        - 示例
        - Example
    """
    # 找 frontmatter 区域
    fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
    if len(fences) < 2:
        return []
    fm = raw[fences[0].end():fences[1].start()]

    # 行内数组格式: aliases: [a, b, c]
    m = re.search(r"^aliases:\s*\[(.*)\]\s*$", fm, re.MULTILINE)
    if m:
        return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]

    # 多行数组格式: aliases:\n  - a\n  - b
    m = re.search(r"^aliases:\s*$\n((?:\s*-\s+.+\n?)+)", fm, re.MULTILINE)
    if m:
        return [
            line.strip().lstrip("-").strip().strip("'\"")
            for line in m.group(1).splitlines()
            if line.strip().startswith("-")
        ]

    return []


def _escape_regex(text: str) -> str:
    """转义正则特殊字符。"""
    return re.escape(text)


def match_site(query: str, sites_dir: Path = SITES_DIR) -> list[dict]:
    """匹配站点经验文件。

    Args:
        query: 查询关键词（域名或别名）
        sites_dir: sites 目录路径

    Returns:
        命中文件列表，每项含 domain/aliases/body
    """
    if not query or not sites_dir.exists():
        return []

    results = []
    for entry in sites_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        # 跳过模板文件
        if entry.name.startswith("_"):
            continue

        domain = entry.stem  # 文件名去掉 .md
        raw = entry.read_text(encoding="utf-8")
        aliases = _extract_aliases(raw)

        # 构建正则：主域 + 所有别名，用 | 连接
        pattern = "|".join(_escape_regex(t) for t in [domain, *aliases])
        if not re.search(pattern, query, re.IGNORECASE):
            continue

        results.append({
            "domain": domain,
            "aliases": aliases,
            "body": _strip_frontmatter(raw).rstrip(),
        })

    return results


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/browser/match_site.py <域名或关键词>", file=sys.stderr)
        print("示例: python tools/browser/match_site.py xiaoheihe.cn", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1].strip()
    results = match_site(query)

    if not results:
        print(f"[未命中] 没有找到匹配 '{query}' 的站点经验文件。", file=sys.stderr)
        print(f"  扫描目录: {SITES_DIR}", file=sys.stderr)
        print(f"  可用站点: {[f.stem for f in SITES_DIR.glob('*.md') if not f.name.startswith('_')]}", file=sys.stderr)
        sys.exit(0)

    for r in results:
        aliases_str = f"（aliases: {', '.join(r['aliases'])}）" if r["aliases"] else ""
        print(f"--- 站点: {r['domain']}{aliases_str} ---")
        print(r["body"])
        print()


if __name__ == "__main__":
    main()
