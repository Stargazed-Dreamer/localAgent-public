"""软件经验库匹配与写入（computer use 闭环）。

按 process_name 匹配 .agents/skills/computer_use/apps/*.md 软件经验库，
结果用于 list_windows/app_list/uia_snapshot 响应 hint 注入。

复刻 server/browser/site_lessons.py 的 6 环闭环结构，差异：
- 标识键：domain → process_name（去扩展名小写，如 Code.exe → code）
- 目录：browser_lessons/sites/ → computer_use/apps/
- 章节列表：9 段 → 10 段（加"UIA 友好度与定位策略"/"常用快捷键"/"菜单路径"）
- 知识老化：match 响应含 staleness_days + staleness_level，提示 agent 验证后再用

知识老化规则（防陈旧经验腐化任务）：
- 文件"最后更新"日期超过 STALE_WARN_DAYS(90) 天 → staleness_level="warn"
- 超过 STALE_CRITICAL_DAYS(180) 天 → staleness_level="critical"
- "已知坑"表格每行可选"最近验证日期"列，agent 验证后用 screen_write_lesson 更新
- 不自动删除（保留历史），仅提示，agent 自行判断
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from lib.schema import BaseSchema

from .routes import router

# 知识老化阈值（天）
STALE_WARN_DAYS = 90
STALE_CRITICAL_DAYS = 180


def _apps_dir() -> Path:
    """软件经验目录：.agents/skills/computer_use/apps/"""
    return Path(__file__).parent.parent.parent / ".agents" / "skills" / "computer_use" / "apps"


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


def match_app_for_process(process_name: str) -> list[dict]:
    """按 process_name 匹配 .agents/skills/computer_use/apps/*.md 软件经验库。

    命中返回 [{process_name, aliases, body, last_updated, staleness_days, staleness_level}]，
    未命中返回 []。

    匹配规则：
    1. 文件名 stem（去 .md）与 process_name 去扩展名小写匹配
    2. frontmatter aliases 列表与 process_name 子串匹配
    """
    apps_dir = _apps_dir()
    if not apps_dir.exists():
        return []

    query = process_name.strip().lower()
    # 去扩展名（Code.exe → code，WINWORD.EXE → winword）
    query_base = re.sub(r"\.(exe|app|msix|uwp)$", "", query, flags=re.IGNORECASE)

    results: list[dict] = []
    for entry in apps_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        if entry.name.startswith("_"):
            continue
        name = entry.stem.lower()  # 文件名 stem（如 code / winword / qbittorrent）
        raw = entry.read_text(encoding="utf-8")
        aliases = [a.lower() for a in _extract_aliases(raw)]

        # 匹配：文件名或 aliases 与 query/query_base 子串匹配
        patterns = [name, *aliases]
        pattern = "|".join(re.escape(t) for t in patterns if t)
        if not pattern:
            continue
        test_str = f"{query}\n{query_base}"
        if not re.search(pattern, test_str, re.IGNORECASE):
            continue

        body = _strip_frontmatter(raw).rstrip()
        last_updated = _extract_last_updated(raw)
        staleness_days, staleness_level = _compute_staleness(last_updated)
        results.append({
            "process_name": entry.stem,
            "aliases": _extract_aliases(raw),  # 原始大小写
            "body": body,
            "last_updated": last_updated,
            "staleness_days": staleness_days,
            "staleness_level": staleness_level,
        })
    return results


def build_app_lessons_hint(lessons: list[dict]) -> str | None:
    """生成简短提示文本，注入 list_windows/app_list/uia_snapshot 响应。

    含 staleness 提示，让 agent 知道经验新鲜度。
    """
    if not lessons:
        return None
    names = [les["process_name"] for les in lessons]
    total_chars = sum(len(les["body"]) for les in lessons)
    staleness_parts = []
    for les in lessons:
        if les["staleness_level"] == "critical":
            staleness_parts.append(f"{les['process_name']}(已 {les["staleness_days"]} 天未更新，高度可能过时)")
        elif les["staleness_level"] == "warn":
            days_str = f"{les["staleness_days"]} 天" if les["staleness_days"] is not None else "未知时间"
            staleness_parts.append(f"{les['process_name']}({days_str} 未更新，请验证)")
    staleness_note = ""
    if staleness_parts:
        staleness_note = f" ⚠️ 部分经验可能过时: {', '.join(staleness_parts)}"
    return (
        f"已加载 {len(lessons)} 条软件经验（{', '.join(names)}，"
        f"共 {total_chars} 字）。操作前请参考已知 UIA 坑/快捷键/菜单路径；"
        f"完整内容见 screen_match_app 查询。{staleness_note}".strip()
    )


def match_app_for_process_names(process_names: list[str]) -> list[dict]:
    """批量匹配多个 process_name，去重返回。

    用于 list_windows/app_list 响应注入：遍历返回的 process_name 列表，
    匹配 apps/*.md，去重后返回。
    """
    if not process_names:
        return []
    seen: set[str] = set()
    results: list[dict] = []
    for pn in process_names:
        for match in match_app_for_process(pn):
            if match["process_name"] not in seen:
                seen.add(match["process_name"])
                results.append(match)
    return results


# ========== 软件经验匹配端点 ==========

class MatchAppRequest(BaseSchema):
    """软件经验匹配请求"""
    process_name: str  # 进程名或关键词（如 qbittorrent.exe, Code.exe, winword）


class MatchAppResponse(BaseSchema):
    """软件经验匹配响应"""
    success: bool
    matched: int
    results: list[dict]  # [{process_name, aliases, body, last_updated, staleness_days, staleness_level}]
    available: list[str]
    staleness_note: str | None = None  # 老化提示汇总


@router.post("/match_app", response_model=MatchAppResponse, operation_id="screen_match_app")
async def screen_match_app(req: MatchAppRequest):
    """Match computer_use app experience files by process_name.

    Scans .agents/skills/computer_use/apps/*.md, matches by filename (process_name
    without extension, lowercased) or frontmatter aliases. Returns the body
    (frontmatter stripped) of matched files, plus staleness info.

    Use this BEFORE any screen_*/execute_action task on a software to load known
    pitfalls (UIA friendliness, shortcuts, menu paths, dialog handling).

    知识老化提示：响应含 staleness_level（fresh/warn/critical），超过 90 天未更新标记
    warn，超过 180 天标记 critical。陈旧经验可能因软件更新/界面改版失效，agent 应验证
    后再用，验证通过后可用 screen_write_lesson 更新"最近验证日期"列。

    - process_name: 进程名或关键词，如 "qbittorrent.exe", "Code.exe", "winword"。
      匹配文件名（去扩展名小写）和 frontmatter aliases。

    Example:
      screen_match_app(process_name="qbittorrent.exe")
      → returns qbittorrent.md content with UIA pitfalls, shortcuts, menu paths.

    If no match, returns available apps in `available` field for reference.
    """
    lessons = match_app_for_process(req.process_name)
    apps_dir = _apps_dir()
    available = []
    if apps_dir.exists():
        available = [
            entry.stem
            for entry in apps_dir.iterdir()
            if entry.is_file() and entry.suffix == ".md" and not entry.name.startswith("_")
        ]

    staleness_parts = []
    for les in lessons:
        if les["staleness_level"] == "critical":
            staleness_parts.append(f"{les['process_name']}(已 {les["staleness_days"]} 天未更新，高度可能过时)")
        elif les["staleness_level"] == "warn":
            days_str = f"{les["staleness_days"]} 天" if les["staleness_days"] is not None else "未知时间"
            staleness_parts.append(f"{les['process_name']}({days_str} 未更新，请验证)")
    staleness_note = "; ".join(staleness_parts) if staleness_parts else None

    return MatchAppResponse(
        success=True,
        matched=len(lessons),
        results=lessons,
        available=available if not lessons else [],
        staleness_note=staleness_note,
    )


# ========== 软件经验写入端点（agent 自更新闭环）==========

# apps/_template.md 中的标准章节列表（10 段）
_ALLOWED_SECTIONS: tuple[str, ...] = (
    "软件概况", "软件识别", "UIA 友好度与定位策略",
    "常用快捷键", "菜单路径", "对话框处理",
    "已知坑", "遗留问题",
)


class WriteLessonRequest(BaseSchema):
    """软件经验写入请求。

    闭环用法：agent 在某软件踩坑或摸索出新方法后，调本端点把经验写入该软件的
    apps 文件，下次操作时 list_windows/app_list 自动注入 app_lessons_hint。

    - process_name: 进程名（如 qbittorrent.exe, Code.exe, WINWORD.EXE）。
      去扩展名转小写作为文件名（WINWORD.EXE → winword.md）。仅允许字母数字横线。
    - content: 要写入的 markdown 内容。表格行/段落/代码块均可。最大 50KB。
    - section: 要追加到的章节名。必须是 _template.md 标准章节之一（默认"已知坑"）。
    - aliases: 可选，frontmatter 别名列表（首次创建时使用，已存在时合并去重）。
    - source_window_title: 可选，触发本次经验记录的窗口标题，写入"修改历史"备注列。
    """
    process_name: str
    content: str
    section: str = "已知坑"
    aliases: list[str] | None = None
    source_window_title: str | None = None


class WriteLessonResponse(BaseSchema):
    """软件经验写入响应"""
    success: bool
    action: str  # created(新建文件) | appended(追加到已有文件) | error
    file_path: str
    process_name: str  # 原始 process_name
    section: str
    char_count: int
    aliases_merged: int
    staleness_level: str = "fresh"  # 写入后立即 fresh
    error: str | None = None


def _normalize_process_to_filename(process_name: str) -> str | None:
    """规范化进程名为文件名 stem。去扩展名，仅允许字母数字横线。

    Code.exe → code
    WINWORD.EXE → winword
    qbittorrent.exe → qbittorrent
    返回 None 表示非法（含路径穿越字符等）。
    """
    name = process_name.strip().lower()
    if not name:
        return None
    # 去扩展名
    name = re.sub(r"\.(exe|app|msix|uwp)$", "", name, flags=re.IGNORECASE)
    # 仅允许 a-z0-9-（横线可来自某些多词软件名，但 process_name 一般不含）
    if not re.fullmatch(r"[a-z0-9-]+", name):
        return None
    return name


def _parse_frontmatter(raw: str) -> tuple[dict, str, str]:
    """解析 markdown，返回 (frontmatter_dict, frontmatter_raw_text, body)。"""
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
    if not aliases:
        return ""
    alias_yaml = ", ".join(f"'{a}'" for a in aliases)
    return f"---\naliases: [{alias_yaml}]\n---\n\n"


def _insert_section_content(body: str, section: str, content: str) -> tuple[str, bool]:
    """把 content 追加到 body 中指定 section 的末尾。

    表格行（以 | 开头）紧跟上一行，不加空行（避免断开 markdown 表格）。
    段落/代码块等非表格内容前加空行分隔。
    """
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
    m = pattern.search(body)
    if not m:
        return body, False
    section_start = m.end()
    next_section = re.search(r"^##\s+", body[section_start:], re.MULTILINE)
    section_end = section_start + next_section.start() if next_section else len(body)
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
    """更新元信息表格的"最后更新"日期（知识老化关键字段）。"""
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


def _build_initial_file(process_name: str, aliases: list[str], today: str) -> str:
    """按 _template.md 结构生成初始文件内容（10 段）。"""
    fm = _build_frontmatter(aliases)
    body = f"""# {process_name} ({process_name})

## 元信息

| 属性 | 值 |
|------|------|
| **process_name** | {process_name} |
| **class_name** | （待补） |
| **UIA 友好度** | （待补：友好/部分/不支持） |
| **首次接触日期** | {today} |
| **相关 task_type** | （待补） |
| **文件创建日期** | {today} |
| **最后更新** | {today} |

## 软件概况

（待补：这是什么软件、agent 通常在上面做什么任务）

## 软件识别

| 标识 | 值 | 备注 |
|------|------|------|
| process_name | {process_name} | 主键 |
| class_name | （待补） | 辅助消歧 |
| 窗口标题模式 | （待补） | 动态部分标注 |
| UWP/WinUI 多层 HWND | 否 | （是则说明 ApplicationFrameHost 外框+子窗口） |

## UIA 友好度与定位策略

| 控件类型 | UIA 支持 | 推荐 locator | 备注 |
|----------|---------|-------------|------|

## 常用快捷键

| 操作 | 快捷键 | 备注 |
|------|--------|------|

## 菜单路径（常见任务）

| 任务 | 路径 | 备注 |
|------|------|------|

## 对话框处理

| 对话框 | 处理方式 | 备注 |
|--------|---------|------|

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|

## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| {today} | 初始创建（由 screen_write_lesson 自动生成） |
"""
    return fm + body


@router.post("/write_lesson", response_model=WriteLessonResponse, operation_id="screen_write_lesson")
async def screen_write_lesson(req: WriteLessonRequest):
    """Write software experience to .agents/skills/computer_use/apps/<process_name>.md.

    Closes the self-updating loop for computer use methodology: after an agent
    discovers a pitfall or best practice on a software, it calls this endpoint
    to persist the lesson. On the next operation, list_windows/app_list auto-
    injects app_lessons_hint, so the agent (or another agent) reads it without
    re-discovering.

    Solves the MCP-only agent problem: cannot directly write files, must know
    naming rules (process_name → lowercase without extension), frontmatter
    format (aliases), and 10-section template structure. This endpoint handles
    all of that automatically.

    知识老化：写入会自动更新"最后更新"日期，重置 staleness_level 为 fresh。
    "已知坑"表格含"最近验证日期"列，agent 验证某条坑仍适用时可用本端点更新该列。

    - process_name: 进程名（如 "qbittorrent.exe", "Code.exe", "WINWORD.EXE"）。
      去扩展名转小写作为文件名（WINWORD.EXE → winword.md）。仅允许 [a-z0-9-]。
    - content: markdown 内容。表格行/段落/代码块均可。最大 50KB。
    - section: 目标章节名（默认"已知坑"）。必须是标准 10 段之一。
    - aliases: 可选 frontmatter 别名（首次创建时使用，已存在时合并去重）。
    - source_window_title: 可选，触发本次经验记录的窗口标题，写入修改历史备注。

    Example — record a UIA pitfall:
      screen_write_lesson(
        process_name="qbittorrent.exe",
        section="已知坑",
        content="| 配置改 ini 被界面覆盖 | 直接改 ini | 通过界面操作保存 | 2026-08-08 | |",
        source_window_title="qBittorrent v5.2.2"
      )
    """
    # 1. 校验 process_name
    name = _normalize_process_to_filename(req.process_name)
    if not name:
        return WriteLessonResponse(
            success=False, action="error", file_path="", process_name=req.process_name,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"invalid process_name: {req.process_name!r} (only [a-z0-9.-] allowed after extension removal)",
        )

    # 2. 校验 section
    if req.section not in _ALLOWED_SECTIONS:
        return WriteLessonResponse(
            success=False, action="error", file_path="", process_name=req.process_name,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"invalid section: {req.section!r}. Allowed: {list(_ALLOWED_SECTIONS)}",
        )

    # 3. 校验 content
    if len(req.content) > 50_000:
        return WriteLessonResponse(
            success=False, action="error", file_path="", process_name=req.process_name,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"content too large: {len(req.content)} > 50000 chars",
        )
    if not req.content.strip():
        return WriteLessonResponse(
            success=False, action="error", file_path="", process_name=req.process_name,
            section=req.section, char_count=0, aliases_merged=0,
            error="content is empty",
        )

    apps_dir = _apps_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    file_path = apps_dir / f"{name}.md"

    today = time.strftime("%Y-%m-%d")
    pn_safe = req.process_name or name

    # 4. 文件不存在 → 按 _template 创建
    if not file_path.exists():
        alias_items: list[str] = list(req.aliases or [])
        all_aliases = list({pn_safe, name, *alias_items})
        all_aliases = [a for a in all_aliases if a != name]
        initial = _build_initial_file(pn_safe, all_aliases, today)
        new_body, found = _insert_section_content(initial, req.section, req.content)
        if not found:
            return WriteLessonResponse(
                success=False, action="error", file_path="", process_name=req.process_name,
                section=req.section, char_count=0, aliases_merged=0,
                error=f"section {req.section!r} not found in initial template (should not happen)",
            )
        note = f"追加 {req.section}（来自 {req.source_window_title}）" if req.source_window_title else f"追加 {req.section}"
        new_body = _append_history_row(new_body, today, note)
        file_path.write_text(new_body, encoding="utf-8")
        return WriteLessonResponse(
            success=True, action="created",
            file_path=str(file_path.relative_to(Path(__file__).parent.parent.parent)),
            process_name=req.process_name, section=req.section, char_count=len(new_body),
            aliases_merged=len(all_aliases), staleness_level="fresh",
        )

    # 5. 文件已存在 → 解析、合并 aliases、追加 content、更新日期、追加历史
    raw = file_path.read_text(encoding="utf-8")
    fm_dict, _fm_raw, body = _parse_frontmatter(raw)

    existing_aliases = set(fm_dict.get("aliases", []))
    new_aliases = []
    for a in [pn_safe, *(req.aliases or [])]:
        if a and a != name and a not in existing_aliases:
            new_aliases.append(a)
            existing_aliases.add(a)
    merged_aliases = sorted(existing_aliases)

    new_body, found = _insert_section_content(body, req.section, req.content)
    if not found:
        return WriteLessonResponse(
            success=False, action="error", file_path="", process_name=req.process_name,
            section=req.section, char_count=0, aliases_merged=0,
            error=f"section {req.section!r} not found in existing file {name}.md. "
                  f"Section must exist before appending. Use one of: {list(_ALLOWED_SECTIONS)}",
        )

    new_body = _update_meta_dates(new_body, today)  # 重置 staleness
    note = f"追加 {req.section}（来自 {req.source_window_title}）" if req.source_window_title else f"追加 {req.section}"
    new_body = _append_history_row(new_body, today, note)

    new_fm = _build_frontmatter(merged_aliases)
    final = new_fm + new_body if new_fm else new_body

    file_path.write_text(final, encoding="utf-8")
    return WriteLessonResponse(
        success=True, action="appended",
        file_path=str(file_path.relative_to(Path(__file__).parent.parent.parent)),
        process_name=req.process_name, section=req.section, char_count=len(final),
        aliases_merged=len(new_aliases), staleness_level="fresh",
    )
