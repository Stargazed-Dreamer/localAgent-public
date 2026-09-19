"""站点经验库匹配（评估文档 P2 第 1 项）。

按 URL 域名匹配 .agents/skills/browser_lessons/sites/*.md 站点经验库，
结果用于 TabSession.site_lessons 注入和 snapshot/action 响应 hint。

从 server/browser_session.py 迁移至 server/browser/ 包（Ticket 02）。
代码与原定义完全一致，仅调整：
- 文件路径计算（site_lessons.py 位于 server/browser/，比原 browser_session.py
  深一层，Path(__file__).parent.parent.parent 才是项目根目录）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from lib.schema import BaseSchema

from ..config import get_browser_config
from .routes import router

# 知识老化阈值（天）— 与 screen/app_lessons.py 保持一致
STALE_WARN_DAYS = 90
STALE_CRITICAL_DAYS = 180


def _extract_last_updated(raw: str) -> str | None:
    """从元信息表格提取"最后更新"日期（YYYY-MM-DD）。"""
    m = re.search(r"\|\s*\*\*最后更新\*\*\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|", raw)
    return m.group(1) if m else None


def _compute_staleness(last_updated: str | None) -> tuple[int | None, str]:
    """根据"最后更新"日期计算 staleness_days 和 staleness_level。

    返回 (staleness_days, staleness_level)：
    - staleness_days: None（无日期）/ 0+（距今天数）
    - staleness_level: "fresh" / "warn" / "critical"
    """
    if not last_updated:
        return None, "warn"  # 无日期视为 warn，提示 agent 谨慎
    try:
        from datetime import datetime
        last = datetime.strptime(last_updated, "%Y-%m-%d")
        now = datetime.now()
        days = (now - last).days
    except (ValueError, TypeError):
        return None, "warn"
    if days >= STALE_CRITICAL_DAYS:
        return days, "critical"
    if days >= STALE_WARN_DAYS:
        return days, "warn"
    return days, "fresh"


def match_site_for_domain(domain: str) -> list[dict]:
    """按域名匹配 .agents/skills/browser_lessons/sites/*.md 站点经验库。

    与 browser.py 的 /match_site 路由共用此逻辑（P2-1：自动注入 session）。
    命中返回 [{domain, aliases, body, last_updated, staleness_days, staleness_level}]，未命中返回 []。

    知识老化：响应含 staleness_level（fresh/warn/critical），超过 90 天未更新标记 warn，
    超过 180 天标记 critical。陈旧经验可能因网站改版失效，agent 应验证后再用。

    匹配规则：
    1. 文件名 stem（去 .md）与 domain 子串匹配（大小写不敏感）
    2. frontmatter 中的 aliases 列表与 domain 子串匹配
    """
    sites_dir = Path(__file__).parent.parent.parent / ".agents" / "skills" / "browser_lessons" / "sites"
    if not sites_dir.exists():
        return []

    def _strip_frontmatter(raw: str) -> str:
        fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
        if len(fences) >= 2:
            return raw[fences[1].end():].lstrip("\r\n")
        return raw

    def _extract_aliases(raw: str) -> list[str]:
        fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
        if len(fences) < 2:
            return []
        fm = raw[fences[0].end():fences[1].start()]
        m = re.search(r"^aliases:\s*\[(.*)\]\s*$", fm, re.MULTILINE)
        if m:
            return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]
        m = re.search(r"^aliases:\s*$\n((?:\s*-\s+.+\n?)+)", fm, re.MULTILINE)
        if m:
            return [
                line.strip().lstrip("-").strip().strip("'\"")
                for line in m.group(1).splitlines()
                if line.strip().startswith("-")
            ]
        return []

    query = domain.strip()
    if not query:
        return []
    results = []
    for entry in sites_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        if entry.name.startswith("_"):
            continue
        site_domain = entry.stem
        raw = entry.read_text(encoding="utf-8")
        aliases = _extract_aliases(raw)
        pattern = "|".join(re.escape(t) for t in [site_domain, *aliases])
        if not re.search(pattern, query, re.IGNORECASE):
            continue
        last_updated = _extract_last_updated(raw)
        staleness_days, staleness_level = _compute_staleness(last_updated)
        results.append({
            "domain": site_domain,
            "aliases": aliases,
            "body": _strip_frontmatter(raw).rstrip(),
            "last_updated": last_updated,
            "staleness_days": staleness_days,
            "staleness_level": staleness_level,
        })
    return results


def match_site_for_url(url: str) -> list[dict]:
    """从 URL 提取 hostname 并匹配站点经验库。"""
    if not url:
        return []
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return []
    if not host:
        return []
    return match_site_for_domain(host)


def build_site_lessons_hint(lessons: list[dict]) -> str | None:
    """根据 site_lessons 生成简短提示（用于 snapshot/action 响应）。

    返回 None 表示无 lessons，调用方应跳过该字段。
    含 staleness 提示，让 agent 知道经验新鲜度。
    """
    if not lessons:
        return None
    domains = [lesson.get("domain", "?") for lesson in lessons]
    total_chars = sum(len(lesson.get("body", "")) for lesson in lessons)
    staleness_parts = []
    for les in lessons:
        if les.get("staleness_level") == "critical":
            staleness_parts.append(f"{les.get('domain', '?')}(已 {les.get('staleness_days')} 天未更新，高度可能过时)")
        elif les.get("staleness_level") == "warn":
            days_str = f"{les.get('staleness_days')} 天" if les.get("staleness_days") is not None else "未知时间"
            staleness_parts.append(f"{les.get('domain', '?')}({days_str} 未更新，请验证)")
    staleness_note = ""
    if staleness_parts:
        staleness_note = f" ⚠️ 部分经验可能过时: {', '.join(staleness_parts)}"
    return (
        f"已加载 {len(lessons)} 条站点经验（{', '.join(domains)}，"
        f"共 {total_chars} 字）。操作前请参考已知 DOM 坑/反爬规则；"
        f"完整内容见 browser_session_create 响应或 browser_match_site 查询。{staleness_note}"
    )


# ========== 站点经验匹配 / 本地书签历史检索端点 ==========
# 对标 eze-is/web-access 的 match-site.mjs 和 find-url.mjs，用 Python 重写。
# 这两个工具不依赖 Playwright/CDP，直接读取本地文件，适合作为直连 MCP 工具暴露。
#
# 从 server/browser.py 迁移至 server/browser/ 包（Ticket 05）。
# 代码与原定义完全一致，仅调整 import 路径与文件路径计算：
# - router：本包 routes 模块顶部定义（原 browser.py 顶部定义）
# - get_browser_config：`from .config import` → `from ..config import`（包嵌套层级变化）
# - get_session_manager：`from .browser_session import` → `from .session.manager import`
#   （browser_find_url auto_open 分支内延迟 import，原文件即延迟 import）
# - 文件路径：`Path(__file__).parent.parent` → `Path(__file__).parent.parent.parent`
#   （site_lessons.py 位于 server/browser/，比 browser.py 深一层）

class MatchSiteRequest(BaseSchema):
    """站点经验匹配请求"""
    domain: str  # 域名或关键词（如 xiaoheihe.cn, bilibili, mp.weixin.qq.com）


class MatchSiteResponse(BaseSchema):
    """站点经验匹配响应"""
    success: bool
    matched: int  # 命中文件数
    results: list[dict]  # [{domain, aliases, body, last_updated, staleness_days, staleness_level}]
    available: list[str]  # 未命中时返回可用站点列表
    staleness_note: str | None = None  # 老化提示汇总


@router.post("/match_site", response_model=MatchSiteResponse, operation_id="browser_match_site")
async def browser_match_site(req: MatchSiteRequest):
    """Match browser_lessons site experience files by domain keyword.

    Scans .agents/skills/browser_lessons/sites/*.md, matches by filename (domain)
    or frontmatter aliases. Returns the body (frontmatter stripped) of matched files.
    Use this BEFORE any CDP browser task to load known pitfalls for the target site.

    知识老化提示：响应含 staleness_level（fresh/warn/critical），超过 90 天未更新标记
    warn，超过 180 天标记 critical。陈旧经验可能因网站改版失效，agent 应验证后再用，
    验证通过后可用 browser_write_lesson 更新"最近验证日期"列。

    - domain: domain keyword, e.g. "xiaoheihe.cn", "bilibili", "mp.weixin.qq.com".
      Matches filenames (with . replaced by nothing) and frontmatter aliases.

    Example:
      browser_match_site(domain="xiaoheihe.cn")
      → returns the xiaoheihe.md content with known DOM pitfalls, anti-scraping rules, etc.

    If no match, returns available sites in `available` field for reference.
    """
    import re
    from pathlib import Path

    sites_dir = Path(__file__).parent.parent.parent / ".agents" / "skills" / "browser_lessons" / "sites"
    if not sites_dir.exists():
        return MatchSiteResponse(success=False, matched=0, results=[], available=[])

    def _strip_frontmatter(raw: str) -> str:
        fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
        if len(fences) >= 2:
            return raw[fences[1].end():].lstrip("\r\n")
        return raw

    def _extract_aliases(raw: str) -> list[str]:
        fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
        if len(fences) < 2:
            return []
        fm = raw[fences[0].end():fences[1].start()]
        m = re.search(r"^aliases:\s*\[(.*)\]\s*$", fm, re.MULTILINE)
        if m:
            return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]
        m = re.search(r"^aliases:\s*$\n((?:\s*-\s+.+\n?)+)", fm, re.MULTILINE)
        if m:
            return [
                line.strip().lstrip("-").strip().strip("'\"")
                for line in m.group(1).splitlines()
                if line.strip().startswith("-")
            ]
        return []

    query = req.domain.strip()
    results = []
    available = []
    for entry in sites_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        if entry.name.startswith("_"):
            continue
        domain = entry.stem
        available.append(domain)
        raw = entry.read_text(encoding="utf-8")
        aliases = _extract_aliases(raw)
        pattern = "|".join(re.escape(t) for t in [domain, *aliases])
        if not re.search(pattern, query, re.IGNORECASE):
            continue
        last_updated = _extract_last_updated(raw)
        staleness_days, staleness_level = _compute_staleness(last_updated)
        results.append({
            "domain": domain,
            "aliases": aliases,
            "body": _strip_frontmatter(raw).rstrip(),
            "last_updated": last_updated,
            "staleness_days": staleness_days,
            "staleness_level": staleness_level,
        })

    staleness_parts = []
    for res in results:
        if res["staleness_level"] == "critical":
            staleness_parts.append(f"{res['domain']}(已 {res['staleness_days']} 天未更新，高度可能过时)")
        elif res["staleness_level"] == "warn":
            days_str = f"{res['staleness_days']} 天" if res["staleness_days"] is not None else "未知时间"
            staleness_parts.append(f"{res['domain']}({days_str} 未更新，请验证)")
    staleness_note = "; ".join(staleness_parts) if staleness_parts else None

    return MatchSiteResponse(
        success=True,
        matched=len(results),
        results=results,
        available=available if not results else [],
        staleness_note=staleness_note,
    )


# ========== 站点经验写入端点（agent 自更新方法论闭环）==========
#
# 闭环关键：agent 摸索出新方法/踩坑后，调本端点把经验写入 sites/<domain>.md，
# 下次访问该网站时 browser_session_create 自动注入 site_lessons，agent 即可读到。
# 解决 MCP-only agent 不能直接写文件、需知命名规则/frontmatter/9段结构的问题。
#
# 安全：
# - 文件名仅允许 [a-z0-9-]（domain 规范化后），防路径穿越
# - content 大小限制 50KB
# - section 必须是预定义章节列表之一
# - 仅写入 .agents/skills/browser_lessons/sites/ 目录

# _template.md 中的标准章节列表（agent 可写入的目标 section）
_ALLOWED_SECTIONS: tuple[str, ...] = (
    "网站概况", "浏览器方案", "反爬/风控", "DOM 结构与提取规则",
    "URL 规则与重定向", "图片/资源加载", "评论/动态内容",
    "已知坑", "遗留问题",
)


class WriteLessonRequest(BaseSchema):
    """站点经验写入请求。

    闭环用法：agent 在某网站踩坑或摸索出新方法后，调本端点把经验写入该网站的
    sites 文件，下次访问时 browser_session_create 会自动注入，agent 即可读到。

    - domain: 网站主域名（如 xiaoheihe.cn, mp.weixin.qq.com）。点号会被替换为横线
      作为文件名（xiaoheihe-cn.md）。仅允许字母数字横线，防路径穿越。
    - content: 要写入的 markdown 内容。可以是表格行（自动追加到 section 末尾）、
      段落或代码块。最大 50KB。
    - section: 要追加到的章节名。必须是 _template.md 中的标准章节之一
      （默认"已知坑"）。章节不存在时返回错误。
    - aliases: 可选，frontmatter 别名列表（首次创建时使用，已存在时合并去重）。
    - source_url: 可选，触发本次经验记录的 URL，写入"修改历史"表格备注列。
    """
    domain: str
    content: str
    section: str = "已知坑"
    aliases: list[str] | None = None
    source_url: str | None = None


class WriteLessonResponse(BaseSchema):
    """站点经验写入响应"""
    success: bool
    action: str  # created(新建文件) | appended(追加到已有文件) | error
    file_path: str  # 写入的文件相对路径
    domain: str  # 原始 domain（点改横线前）
    section: str  # 写入的章节
    char_count: int  # 文件最终大小
    aliases_merged: int  # 本次合并到 frontmatter 的新增别名数（去重后）
    staleness_level: str = "fresh"  # 写入后立即 fresh
    error: str | None = None


def _extract_domain_host(domain: str) -> str:
    """提取 hostname（去协议、路径、端口），返回小写；空串表示无法解析。"""
    if "://" in domain:
        try:
            host = urlparse(domain).hostname or ""
        except Exception:
            return ""
        domain = host
    # 去端口
    return domain.split(":")[0].strip().lower()


def _normalize_domain_to_filename(domain: str) -> str | None:
    """规范化域名为文件名 stem。点改横线，仅允许字母数字横线。

    返回 None 表示域名非法（含路径穿越字符等）。
    """
    domain = _extract_domain_host(domain)
    if not domain:
        return None
    # 点改横线
    name = domain.replace(".", "-")
    # 仅允许 a-z0-9-
    if not re.fullmatch(r"[a-z0-9-]+", name):
        return None
    return name


def _find_existing_lesson_file(sites_dir: Path, host: str, name: str) -> Path | None:
    """查找已存在的同一站点档案，避免同一站点被写成两份。

    背景：write_lesson 的默认命名是「点改横线」（scnu.edu.cn → scnu-edu-cn.md），
    但站点档案也可能由人工按主域命名（scnu.edu.cn.md，如 scnu.edu.cn / js.design /
    bilibili.com 等）。若写入前只按规范名查文件是否存在，人工维护的那份会被无视，
    同一站点被写成第二份；match_site_for_domain 随后对同一域名返回两份，
    agent 可能读到内容较少的那一份。

    匹配顺序（均为精确匹配，不做子串，避免误并相邻域名）：
    ① 规范名 `<name>.md`（scnu-edu-cn.md）
    ② 点式主域 `<host>.md`（scnu.edu.cn.md）
    ③ 某档案的 frontmatter aliases 精确含该 host

    返回 None 表示无既有档案，调用方按 `<name>.md` 新建。
    """
    exact = sites_dir / f"{name}.md"
    if exact.exists():
        return exact

    dotted = sites_dir / f"{host}.md"
    if dotted.exists():
        return dotted

    for entry in sorted(sites_dir.glob("*.md")):
        if entry.name.startswith("_"):
            continue
        try:
            raw = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        aliases = [a.strip().lower() for a in _parse_frontmatter(raw)[0].get("aliases", [])]
        if host in aliases:
            return entry

    return None


def _parse_frontmatter(raw: str) -> tuple[dict, str, str]:
    """解析 markdown 文件，返回 (frontmatter_dict, frontmatter_raw_text, body)。

    frontmatter_dict 至少含 aliases 字段（list[str]）。
    """
    fences = list(re.finditer(r"^---\s*$", raw, re.MULTILINE))
    if len(fences) < 2:
        return {"aliases": []}, "", raw
    fm_raw = raw[fences[0].end():fences[1].start()]
    body = raw[fences[1].end():].lstrip("\r\n")
    aliases: list[str] = []
    m = re.search(r"^aliases:\s*\[(.*)\]\s*$", fm_raw, re.MULTILINE)
    if m:
        aliases = [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]
    else:
        m = re.search(r"^aliases:\s*$\n((?:\s*-\s+.+\n?)+)", fm_raw, re.MULTILINE)
        if m:
            aliases = [
                line.strip().lstrip("-").strip().strip("'\"")
                for line in m.group(1).splitlines()
                if line.strip().startswith("-")
            ]
    return {"aliases": aliases}, fm_raw, body


def _build_frontmatter(aliases: list[str]) -> str:
    """构造 frontmatter 文本。"""
    if not aliases:
        return ""
    alias_yaml = ", ".join(f"'{a}'" for a in aliases)
    return f"---\naliases: [{alias_yaml}]\n---\n\n"


def _insert_section_content(body: str, section: str, content: str) -> tuple[str, bool]:
    """把 content 追加到 body 中指定 section 的末尾。

    表格行（以 | 开头）紧跟上一行，不加空行（避免断开 markdown 表格）。
    段落/代码块等非表格内容前加空行分隔。

    返回 (new_body, section_found)。section_found=False 表示该 section 在 body 中不存在。
    """
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
    m = pattern.search(body)
    if not m:
        return body, False
    section_start = m.end()
    # 找下一个 ## 标题位置（章节末尾）
    next_section = re.search(r"^##\s+", body[section_start:], re.MULTILINE)
    section_end = section_start + next_section.start() if next_section else len(body)
    # 在 section 末尾插入 content
    insert_point = section_end
    while insert_point > section_start and body[insert_point - 1] in "\r\n":
        insert_point -= 1
    prefix = body[:insert_point]
    suffix = body[insert_point:]
    is_table_row = content.lstrip().startswith("|")
    if not prefix.endswith("\n"):
        prefix += "\n"
    if is_table_row:
        # 表格行紧跟前一行，不加额外空行
        if not content.endswith("\n"):
            content += "\n"
    else:
        # 段落/代码块前加空行分隔
        if not content.startswith("\n"):
            content = "\n" + content
        if not content.endswith("\n"):
            content += "\n"
    if not suffix.startswith("\n"):
        suffix = "\n" + suffix
    return prefix + content + suffix, True


def _update_meta_dates(body: str, today: str) -> str:
    """更新元信息表格的"最后更新"日期。"""
    return re.sub(
        r"(\|\s*\*\*最后更新\*\*\s*\|\s*)\d{4}-\d{2}-\d{2}(\s*\|)",
        rf"\g<1>{today}\g<2>",
        body,
        count=1,
    )


def _append_history_row(body: str, today: str, note: str) -> str:
    """在"修改历史"表格末尾追加一行。"""
    pattern = re.compile(r"^##\s+修改历史\s*$\n*((?:\|[^\n]+\|\s*\n)+)", re.MULTILINE)
    m = pattern.search(body)
    if not m:
        return body
    table_block = m.group(1)
    note_safe = note.replace("|", "\\|")
    new_row = f"| {today} | {note_safe} |\n"
    new_table = table_block + new_row
    return body[:m.start(1)] + new_table + body[m.end(1):]


def _build_initial_file(domain: str, aliases: list[str], today: str) -> str:
    """按 _template.md 结构生成初始文件内容。"""
    fm = _build_frontmatter(aliases)
    body = f"""# {domain} ({domain})

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | {domain} |
| **首次接触日期** | {today} |
| **相关 skill** | （待补） |
| **登录态要求** | （待补） |
| **抓取频次** | （待补） |
| **文件创建日期** | {today} |
| **最后更新** | {today} |

## 网站概况

（待补）

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `user-data-dir` |

## 反爬/风控

（无）

## DOM 结构与提取规则

（待补）

## URL 规则与重定向

（无）

## 图片/资源加载

（无）

## 评论/动态内容

（无）

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|

## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| {today} | 初始创建（由 browser_write_lesson 自动生成） |
"""
    return fm + body


@router.post("/write_lesson", response_model=WriteLessonResponse, operation_id="browser_write_lesson")
async def browser_write_lesson(req: WriteLessonRequest):
    """Write site experience to .agents/skills/browser_lessons/sites/<domain>.md.

    Closes the self-updating loop for browser methodology: after an agent discovers
    a pitfall or best practice on a website, it calls this endpoint to persist the
    lesson. On the next visit, browser_session_create auto-injects site_lessons,
    so the agent (or another agent) reads it without re-discovering.

    知识老化：写入会自动更新"最后更新"日期，重置 staleness_level 为 fresh。
    "已知坑"表格含"最近验证日期"列，agent 验证某条坑仍适用时可用本端点更新该列。

    Solves the MCP-only agent problem: cannot directly write files, must know
    naming rules (dots → hyphens), frontmatter format (aliases), and 9-section
    template structure. This endpoint handles all of that automatically.

    - domain: site main domain (e.g. "xiaoheihe.cn", "mp.weixin.qq.com"). Dots
      become hyphens in filename (xiaoheihe-cn.md). Only [a-z0-9-] allowed.
      If a file for the same site already exists under the dotted main-domain name
      (e.g. scnu.edu.cn.md) or declares it in frontmatter aliases, the lesson is
      appended there instead of creating a duplicate file.
    - content: markdown to append. Can be a table row (auto-appended to section's
      table), paragraph, or code block. Max 50KB.
    - section: target section name (default "已知坑"). Must be one of the standard
      sections from _template.md. Section must exist in the file.
    - aliases: optional frontmatter aliases (used on file creation, merged on update).
    - source_url: optional URL that triggered this lesson; written to 修改历史 note.

    Returns action=created (new file) or action=appended (existing file updated).

    Example — record a DOM pitfall:
      browser_write_lesson(
        domain="xiaoheihe.cn",
        section="已知坑",
        content="| 列表懒加载未触发 | 直接 querySelector | 先 scroll 500px 再查 | 2026-08-08 |",
        source_url="https://www.xiaoheihe.cn/community/list"
      )
    """
    import time as _time
    from pathlib import Path

    # 1. 校验 domain
    name = _normalize_domain_to_filename(req.domain)
    if not name:
        return WriteLessonResponse(
            success=False, action="error", file_path="", domain=req.domain,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"invalid domain: {req.domain!r} (only [a-z0-9.-] allowed after hostname extraction)",
        )

    # 2. 校验 section
    if req.section not in _ALLOWED_SECTIONS:
        return WriteLessonResponse(
            success=False, action="error", file_path="", domain=req.domain,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"invalid section: {req.section!r}. Allowed: {list(_ALLOWED_SECTIONS)}",
        )

    # 3. 校验 content 大小
    if len(req.content) > 50_000:
        return WriteLessonResponse(
            success=False, action="error", file_path="", domain=req.domain,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"content too large: {len(req.content)} > 50000 chars",
        )
    if not req.content.strip():
        return WriteLessonResponse(
            success=False, action="error", file_path="", domain=req.domain,
            section=req.section, char_count=0, aliases_merged=0,
            error="content is empty",
        )

    sites_dir = Path(__file__).parent.parent.parent / ".agents" / "skills" / "browser_lessons" / "sites"
    sites_dir.mkdir(parents=True, exist_ok=True)
    host = _extract_domain_host(req.domain)
    # 先查既有档案（含人工按主域命名的点式文件），避免同一站点写成两份
    file_path = _find_existing_lesson_file(sites_dir, host, name) or (sites_dir / f"{name}.md")

    today = _time.strftime("%Y-%m-%d")
    req_domain_safe = req.domain or name

    # 4. 文件不存在 → 按 _template 结构创建
    if not file_path.exists():
        alias_items: set[str] = {name, req_domain_safe}
        if req.aliases:
            alias_items.update(req.aliases)
        all_aliases = [a for a in alias_items if a != name]
        initial = _build_initial_file(req_domain_safe, all_aliases, today)
        new_body, found = _insert_section_content(initial, req.section, req.content)
        if not found:
            return WriteLessonResponse(
                success=False, action="error", file_path="", domain=req.domain,
                section=req.section, char_count=0, aliases_merged=0,
                error=f"section {req.section!r} not found in initial template (should not happen)",
            )
        note = f"追加 {req.section}（来自 {req.source_url}）" if req.source_url else f"追加 {req.section}"
        new_body = _append_history_row(new_body, today, note)
        file_path.write_text(new_body, encoding="utf-8")
        return WriteLessonResponse(
            success=True, action="created",
            file_path=str(file_path.relative_to(Path(__file__).parent.parent.parent)),
            domain=req.domain, section=req.section, char_count=len(new_body),
            aliases_merged=len(all_aliases), staleness_level="fresh",
        )

    # 5. 文件已存在 → 解析、合并 aliases、追加 content、更新元信息、追加历史
    raw = file_path.read_text(encoding="utf-8")
    fm_dict, _fm_raw, body = _parse_frontmatter(raw)

    existing_aliases = set(fm_dict.get("aliases", []))
    new_aliases = []
    for a in [req_domain_safe, *(req.aliases or [])]:
        if a and a != name and a not in existing_aliases:
            new_aliases.append(a)
            existing_aliases.add(a)
    merged_aliases = sorted(existing_aliases)

    new_body, found = _insert_section_content(body, req.section, req.content)
    if not found:
        return WriteLessonResponse(
            success=False, action="error", file_path="", domain=req.domain,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"section {req.section!r} not found in existing file {file_path.name}. "
                  f"Section must exist before appending. Use one of: {list(_ALLOWED_SECTIONS)}",
        )

    new_body = _update_meta_dates(new_body, today)
    note = f"追加 {req.section}（来自 {req.source_url}）" if req.source_url else f"追加 {req.section}"
    new_body = _append_history_row(new_body, today, note)

    new_fm = _build_frontmatter(merged_aliases)
    final = new_fm + new_body if new_fm else new_body

    file_path.write_text(final, encoding="utf-8")
    return WriteLessonResponse(
        success=True, action="appended",
        file_path=str(file_path.relative_to(Path(__file__).parent.parent.parent)),
        domain=req.domain, section=req.section, char_count=len(final),
        aliases_merged=len(new_aliases), staleness_level="fresh",
    )


class FindUrlRequest(BaseSchema):
    """本地书签/历史检索请求（P2-2：与 tab open 打通）"""
    keyword: str  # 搜索关键词（匹配标题或 URL，大小写不敏感）
    scope: str = "all"  # bookmarks | history | all
    limit: int = 20  # 最多返回条数
    since_days: int | None = None  # 只查最近 N 天（仅 history）
    sort: str = "recent"  # recent | visits（仅 history）
    auto_open: bool = False  # P2-2: True=对首个结果自动创建 session 并打开（返回 session_id）


class FindUrlResponse(BaseSchema):
    """本地书签/历史检索响应（P2-2：增加 openable + open_hint + session_id）"""
    success: bool
    matched: int
    results: list[dict]  # [{title, url, source, openable, ...}]
    open_hint: str | None = None  # P2-2: 提示如何打开（含 browser_session_create 调用示例）
    opened_session_id: str | None = None  # P2-2: auto_open=True 时返回创建的 session_id
    opened_session_tab_id: str | None = None  # P2-2: 对应的 CDP target id
    opened_url: str | None = None  # P2-2: 实际打开的 URL
    error: str | None = None


@router.post("/find_url", response_model=FindUrlResponse, operation_id="browser_find_url")
async def browser_find_url(req: FindUrlRequest):
    """Search local browser bookmarks and history of the debug browser.

    Reads the debug browser's user-data-dir (默认 chrome_debug/) for:
    - Bookmarks (JSON): saved bookmarks with folder paths
    - History (SQLite): browsing history with visit counts and timestamps

    Only reads the DEBUG browser's data (默认 chrome_debug/), never touches the user's
    working browser (isolation principle per AGENTS.md). Use this to find pages
    the user has visited but not publicly bookmarked (internal systems, history).

    - keyword: search term, matches title OR url (case-insensitive).
    - scope: bookmarks | history | all (default all).
    - limit: max results (default 20).
    - since_days: only history within last N days (optional, history only).
    - sort: recent (by time, default) | visits (by visit count, history only).

    Example:
      browser_find_url(keyword="内部系统", scope="all", limit=20)
      browser_find_url(keyword="api docs", scope="history", since_days=7, sort="visits")

    If user_data_dir doesn't exist, returns success=False with guidance to start
    the debug browser first.
    """
    from pathlib import Path

    # 从 config 读取用户数据目录，未配置则用默认 chrome_debug/
    cfg = get_browser_config()
    configured_dir = cfg.get("user_data_dir", "")
    user_data_dir = Path(configured_dir) if configured_dir else Path(__file__).parent.parent.parent / "chrome_debug"
    if not user_data_dir.exists():
        return FindUrlResponse(
            success=False, matched=0, results=[],
            error=f"调试浏览器用户数据目录不存在（{user_data_dir}）。请先启动调试浏览器: uv run python tools/browser/start_debug_browser.py",
        )

    def _chrome_time_to_iso(chrome_time):
        # Chromium 系（Chrome/Edge/Brave 等）共用 1601-01-01 起的微秒时间戳
        if not chrome_time:
            return ""
        try:
            ct = int(chrome_time)
            if ct == 0:
                return ""
            unix_time = ct / 1_000_000 - 11644473600
            if unix_time < 0:
                return ""
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_time))
        except (ValueError, TypeError, OSError):
            return ""

    def _find_bookmarks(kw, limit):
        bm_file = user_data_dir / "Default" / "Bookmarks"
        if not bm_file.exists():
            bm_file = user_data_dir / "Bookmarks"
            if not bm_file.exists():
                return []
        try:
            data = json.loads(bm_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        kw_l = kw.lower()
        out = []

        def _walk(node, folder=""):
            if node.get("type") == "url":
                name = node.get("name", "")
                url = node.get("url", "")
                if kw_l in name.lower() or kw_l in url.lower():
                    out.append({
                        "title": name, "url": url, "folder": folder,
                        "source": "bookmark",
                        "date_added": _chrome_time_to_iso(node.get("date_added")),
                    })
            elif node.get("type") == "folder":
                sub = f"{folder}/{node.get('name', '')}" if folder else node.get("name", "")
                for child in node.get("children", []):
                    _walk(child, sub)

        for root_name in ("bookmark_bar", "other", "synced"):
            root_node = data.get("roots", {}).get(root_name, {})
            _walk(root_node, root_node.get("name", root_name))
        return out[:limit]

    def _find_history(kw, limit, since_days, sort):
        hist_file = user_data_dir / "Default" / "History"
        if not hist_file.exists():
            hist_file = user_data_dir / "History"
            if not hist_file.exists():
                return []
        try:
            conn = sqlite3.connect(f"file:{hist_file}?immutable=1", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        try:
            kw_like = f"%{kw}%"
            params: list[object] = [kw_like, kw_like]
            where = "WHERE (u.url LIKE ? OR u.title LIKE ?)"
            if since_days is not None:
                cutoff_unix = time.time() - since_days * 86400
                cutoff_chrome = int((cutoff_unix + 11644473600) * 1e6)
                where += " AND u.last_visit_time >= ?"
                params.append(cutoff_chrome)
            order = "u.last_visit_time DESC" if sort == "recent" else "u.visit_count DESC"
            sql = f"""
                SELECT u.url, u.title, u.visit_count, u.last_visit_time
                FROM urls u {where} ORDER BY {order} LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "title": row["title"] or "", "url": row["url"],
                    "visit_count": row["visit_count"], "source": "history",
                    "last_visit": _chrome_time_to_iso(row["last_visit_time"]),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    all_results = []
    if req.scope in ("bookmarks", "all"):
        all_results.extend(_find_bookmarks(req.keyword, req.limit))
    if req.scope in ("history", "all"):
        all_results.extend(_find_history(req.keyword, req.limit, req.since_days, req.sort))

    # P2-2: 给每个结果加 openable 标记（http/https 才可打开）
    for r in all_results:
        url = r.get("url", "")
        r["openable"] = url.startswith(("http://", "https://"))

    # P2-2: open_hint 提示 agent 如何用 browser_session_create 打开首个结果
    open_hint = None
    opened_session_id = None
    opened_session_tab_id = None
    opened_url = None
    if all_results:
        first_openable = next((r for r in all_results if r.get("openable")), None)
        if first_openable:
            open_hint = (
                f"用 browser_session_create(url={first_openable['url']!r}) 打开此页面，"
                f"或本接口传 auto_open=true 一键打开。"
            )
            # P2-2: auto_open=True 时自动创建 session
            if req.auto_open:
                try:
                    from .session.manager import get_session_manager
                    mgr = await get_session_manager()
                    info = await mgr.create_session(url=first_openable["url"])
                    opened_session_id = info.get("session_id")
                    opened_session_tab_id = info.get("tab_id")
                    opened_url = info.get("url")
                except Exception as e:
                    return FindUrlResponse(
                        success=True, matched=len(all_results), results=all_results,
                        open_hint=open_hint,
                        error=f"auto_open 创建 session 失败: {e}（结果仍返回，可手动 browser_session_create）",
                    )

    return FindUrlResponse(
        success=True, matched=len(all_results), results=all_results,
        open_hint=open_hint,
        opened_session_id=opened_session_id,
        opened_session_tab_id=opened_session_tab_id,
        opened_url=opened_url,
    )
