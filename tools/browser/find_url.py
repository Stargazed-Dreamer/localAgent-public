#!/usr/bin/env python
"""检索调试浏览器的本地书签和历史记录（通用，支持 Chromium 内核浏览器）。

对标 eze-is/web-access 的 scripts/find-url.mjs，用 Python 重写。
读取调试浏览器的用户数据目录（默认 chrome_debug/）下的:
  - Bookmarks (JSON): 收藏的书签
  - History (SQLite): 浏览历史

只读调试浏览器的数据，不碰用户工作浏览器（符合 AGENTS.md 隔离原则）。

用法:
  python tools/browser/find_url.py 关键词
  python tools/browser/find_url.py 关键词 --scope bookmarks
  python tools/browser/find_url.py 关键词 --scope history --limit 50
  python tools/browser/find_url.py 关键词 --since 7
  python tools/browser/find_url.py 关键词 --sort visits
  python tools/browser/find_url.py 关键词 --browser-data /path/to/user-data

参数:
  keyword       搜索关键词（匹配标题或 URL，大小写不敏感）
  --scope       搜索范围: bookmarks / history / all（默认 all）
  --limit       最多返回条数（默认 20）
  --since       只查最近 N 天的记录（仅对 history 有效，默认不限）
  --sort        排序方式: recent（按时间，默认）/ visits（按访问次数，仅 history）
  --browser-data 调试浏览器用户数据目录（默认项目根/chrome_debug）

退出码:
  0 = 正常（无论是否命中）
  1 = 参数错误或数据文件不存在
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# 调试浏览器用户数据目录：项目根/chrome_debug/（默认；可用 config.toml [browser] user_data_dir 覆盖）
DEFAULT_USER_DATA_DIR = Path(__file__).parent.parent.parent / "chrome_debug"


def find_bookmarks(keyword: str, user_data_dir: Path, limit: int = 20) -> list[dict]:
    """从 Bookmarks JSON 中搜索书签。

    Chromium 内核浏览器（Chrome/Edge/Brave 等）的 Bookmarks 文件结构:
    {
      "roots": {
        "bookmark_bar": { "children": [...] },
        "other": { "children": [...] },
        "synced": { "children": [...] }
      }
    }
    每个书签项: {"type": "url", "url": "...", "name": "...", "date_added": "..."}

    date_added 是 Chromium 时间戳（微秒，从 1601-01-01 起）。
    """
    bookmarks_file = user_data_dir / "Default" / "Bookmarks"
    if not bookmarks_file.exists():
        # 兼容旧版路径（无 Default 子目录）
        bookmarks_file = user_data_dir / "Bookmarks"
        if not bookmarks_file.exists():
            return []

    try:
        data = json.loads(bookmarks_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] 读取书签文件失败: {e}", file=sys.stderr)
        return []

    kw = keyword.lower()
    results = []

    def _walk(node: dict, folder_path: str = ""):
        if node.get("type") == "url":
            name = node.get("name", "")
            url = node.get("url", "")
            if kw in name.lower() or kw in url.lower():
                results.append({
                    "title": name,
                    "url": url,
                    "folder": folder_path,
                    "source": "bookmark",
                    "date_added": _chrome_time_to_iso(node.get("date_added")),
                })
        elif node.get("type") == "folder":
            sub_folder = f"{folder_path}/{node.get('name', '')}" if folder_path else node.get("name", "")
            for child in node.get("children", []):
                _walk(child, sub_folder)

    roots = data.get("roots", {})
    for root_name in ("bookmark_bar", "other", "synced"):
        root_node = roots.get(root_name, {})
        _walk(root_node, root_node.get("name", root_name))

    return results[:limit]


def find_history(
    keyword: str,
    user_data_dir: Path,
    limit: int = 20,
    since_days: int | None = None,
    sort: str = "recent",
) -> list[dict]:
    """从 History SQLite 中搜索浏览历史。

    Chromium 内核浏览器（Chrome/Edge/Brave 等）的 History SQLite 表结构:
      urls表: id, url, title, visit_count, typed_count, last_visit_time, hidden
      visits表: id, url, visit_time, from_visit, transition, ...

    last_visit_time / visit_time 是 Chromium 时间戳（微秒，从 1601-01-01 起）。
    """
    history_file = user_data_dir / "Default" / "History"
    if not history_file.exists():
        history_file = user_data_dir / "History"
        if not history_file.exists():
            return []

    # 浏览器运行时可能锁住 History.db，用 immutable 模式打开避免锁冲突
    try:
        conn = sqlite3.connect(f"file:{history_file}?immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print(f"[!] 打开历史数据库失败: {e}", file=sys.stderr)
        return []

    try:
        kw = f"%{keyword}%"
        params: list = [kw, kw]

        where_clause = "WHERE (u.url LIKE ? OR u.title LIKE ?)"
        if since_days is not None:
            # Chromium 时间戳：从 1601-01-01 起的微秒数
            # Unix 时间戳 = chrome_time / 1e6 - 11644473600
            cutoff_unix = time.time() - since_days * 86400
            cutoff_chrome = int((cutoff_unix + 11644473600) * 1e6)
            where_clause += " AND u.last_visit_time >= ?"
            params.append(cutoff_chrome)

        order = "u.last_visit_time DESC" if sort == "recent" else "u.visit_count DESC"

        sql = f"""
            SELECT u.url, u.title, u.visit_count, u.last_visit_time
            FROM urls u
            {where_clause}
            ORDER BY {order}
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "title": row["title"] or "",
                "url": row["url"],
                "visit_count": row["visit_count"],
                "source": "history",
                "last_visit": _chrome_time_to_iso(row["last_visit_time"]),
            }
            for row in rows
        ]
    except sqlite3.Error as e:
        print(f"[!] 查询历史失败: {e}", file=sys.stderr)
        return []
    finally:
        conn.close()


def _chrome_time_to_iso(chrome_time: str | int | None) -> str:
    """Chromium 时间戳（微秒，从 1601-01-01 起）转 ISO 字符串。"""
    if not chrome_time:
        return ""
    try:
        # Chromium 时间戳可能是字符串（JSON）或整数（SQLite）
        ct = int(chrome_time)
        if ct == 0:
            return ""
        unix_time = ct / 1_000_000 - 11644473600
        if unix_time < 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_time))
    except (ValueError, TypeError, OSError):
        return ""


def main():
    parser = argparse.ArgumentParser(
        description="检索调试浏览器的本地书签和历史记录（通用，支持 Chromium 内核浏览器）",
    )
    parser.add_argument("keyword", help="搜索关键词（匹配标题或 URL）")
    parser.add_argument(
        "--scope", choices=["bookmarks", "history", "all"], default="all",
        help="搜索范围（默认 all）",
    )
    parser.add_argument("--limit", type=int, default=20, help="最多返回条数（默认 20）")
    parser.add_argument(
        "--since", type=int, default=None,
        help="只查最近 N 天的记录（仅 history 有效）",
    )
    parser.add_argument(
        "--sort", choices=["recent", "visits"], default="recent",
        help="排序方式（默认 recent；visits 仅 history 有效）",
    )
    parser.add_argument(
        "--browser-data", type=Path, default=DEFAULT_USER_DATA_DIR,
        help=f"调试浏览器用户数据目录（默认 {DEFAULT_USER_DATA_DIR}）",
    )
    args = parser.parse_args()

    if not args.browser_data.exists():
        print(f"[!] 调试浏览器用户数据目录不存在: {args.browser_data}", file=sys.stderr)
        print("  请先启动调试浏览器: uv run python tools/browser/start_debug_browser.py", file=sys.stderr)
        sys.exit(1)

    all_results: list[dict] = []

    if args.scope in ("bookmarks", "all"):
        bm = find_bookmarks(args.keyword, args.browser_data, args.limit)
        all_results.extend(bm)
        print(f"[*] 书签命中 {len(bm)} 条", file=sys.stderr)

    if args.scope in ("history", "all"):
        hist = find_history(
            args.keyword, args.browser_data, args.limit,
            since_days=args.since, sort=args.sort,
        )
        all_results.extend(hist)
        print(f"[*] 历史命中 {len(hist)} 条", file=sys.stderr)

    if not all_results:
        print(f"[未命中] 没有找到匹配 '{args.keyword}' 的书签或历史记录。", file=sys.stderr)
        sys.exit(0)

    # 输出 JSON 到 stdout（便于 agent 解析）
    print(json.dumps(all_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
