"""chat-panel-v2 T03: 模板存储 + skill 路径解析

设计依据（decisions.md）：
- D2: skill 注入语义 = system prompt 提示 + 路径，agent 自己读 SKILL.md（修订）
  会话启动时，SessionRunner 把模板 skills[] 转成 system prompt 提示段，格式如：
    本会话已激活以下 skill：
    1. accounting (workspace/accounting/SKILL.md)
    2. arknights_gacha (workspace/arknights_gacha/SKILL.md)
    请先读取相关 SKILL.md 了解流程。
  agent 看到后用 file_read 工具读取 SKILL.md。
- D3: 模板存储 = data/chat_templates.json
  Schema: {id, name, prompt, skills[], created_at, updated_at}

模块职责：
- Template dataclass + JSON 文件 CRUD（load_all / save_all / create / update / delete / get）
- build_skill_path_map()：解析 .agents/skills/_index.md 构建 task_type → SKILL.md 路径映射
- format_skills_prompt(skills)：task_type list → system prompt 段（路径列表 + 引导语）

不耦合 Qt，纯 Python 供 SessionRunner / ChatPanel 共用。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("localagent.agent.template_store")

# ============================================================================
# 路径常量
# ============================================================================

# template_store.py 位于 client/core/agent/template_store.py → 项目根 = parents[3]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATES_PATH = _PROJECT_ROOT / "data" / "chat_templates.json"
SKILL_INDEX_PATH = _PROJECT_ROOT / ".agents" / "skills" / "_index.md"

# 默认空白模板（首次初始化用）
_BLANK_TEMPLATE_ID = "blank"


# ============================================================================
# Template dataclass
# ============================================================================


@dataclass
class Template:
    """对话模板（data/chat_templates.json 中一项）。

    字段（decisions D3）：
    - id: 模板 ID（"blank" 固定 / 新建用 uuid4 hex[:12]）
    - name: 用户可读名（如"空白" / "问股"）
    - prompt: 预设 prompt（作为首条 user 消息文本，UI 发送时传给 facade.start）
    - skills: task_type list（如 ["recurring.accounting"]）
    - created_at / updated_at: Unix 时间戳（秒）
    """

    id: str
    name: str
    prompt: str = ""
    skills: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        """序列化为 JSON 可写 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Template:
        """从 dict 反序列化（容忍缺失字段，向后兼容）。"""
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            prompt=str(data.get("prompt", "")),
            skills=list(data.get("skills", []) or []),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )


# ============================================================================
# CRUD
# ============================================================================


def _ensure_default_file(path: Path) -> None:
    """若文件不存在，初始化默认空白模板并写入。幂等。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    default_templates = [
        Template(
            id=_BLANK_TEMPLATE_ID,
            name="空白",
            prompt="",
            skills=[],
            created_at=now,
            updated_at=now,
        )
    ]
    save_all(default_templates, path=path)


def load_all(path: Path | None = None) -> list[Template]:
    """加载全部模板。文件不存在时自动初始化默认空白模板。

    Args:
        path: 模板文件路径，None 用 DEFAULT_TEMPLATES_PATH

    Returns:
        list[Template]：全部模板（按文件中的顺序）
    """
    p = path or DEFAULT_TEMPLATES_PATH
    if not p.exists():
        _ensure_default_file(p)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("load_all: 读取模板文件失败 %s: %s", p, e)
        return []
    if not isinstance(raw, list):
        logger.error("load_all: 模板文件不是 list: %r", type(raw).__name__)
        return []
    return [Template.from_dict(item) for item in raw if isinstance(item, dict)]


def save_all(templates: list[Template], path: Path | None = None) -> None:
    """写入全部模板（覆盖式）。

    Args:
        templates: 全部模板列表
        path: 模板文件路径，None 用 DEFAULT_TEMPLATES_PATH
    """
    p = path or DEFAULT_TEMPLATES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [t.to_dict() for t in templates]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get(template_id: str, path: Path | None = None) -> Template | None:
    """按 id 查模板。找不到返回 None。"""
    for t in load_all(path=path):
        if t.id == template_id:
            return t
    return None


def create(
    name: str,
    prompt: str = "",
    skills: list[str] | None = None,
    *,
    path: Path | None = None,
    template_id: str | None = None,
) -> Template:
    """新建模板并保存。

    Args:
        name: 模板名（必填）
        prompt: 预设 prompt
        skills: task_type list
        path: 模板文件路径
        template_id: 自定义 id（默认 uuid4 hex[:12]）

    Returns:
        新建的 Template
    """
    templates = load_all(path=path)
    now = time.time()
    new_id = template_id or _uuid.uuid4().hex[:12]
    # 防止 id 冲突
    existing_ids = {t.id for t in templates}
    if new_id in existing_ids:
        # 冲突时追加随机后缀
        new_id = f"{new_id}_{_uuid.uuid4().hex[:4]}"
    tpl = Template(
        id=new_id,
        name=name,
        prompt=prompt,
        skills=list(skills or []),
        created_at=now,
        updated_at=now,
    )
    templates.append(tpl)
    save_all(templates, path=path)
    return tpl


def update(
    template_id: str,
    *,
    name: str | None = None,
    prompt: str | None = None,
    skills: list[str] | None = None,
    path: Path | None = None,
) -> Template | None:
    """更新模板字段（仅传非 None 的字段）。找不到返回 None。"""
    templates = load_all(path=path)
    for t in templates:
        if t.id == template_id:
            if name is not None:
                t.name = name
            if prompt is not None:
                t.prompt = prompt
            if skills is not None:
                t.skills = list(skills)
            t.updated_at = time.time()
            save_all(templates, path=path)
            return t
    return None


def delete(template_id: str, path: Path | None = None) -> bool:
    """删除模板。成功返回 True，找不到返回 False。"""
    templates = load_all(path=path)
    before = len(templates)
    templates = [t for t in templates if t.id != template_id]
    if len(templates) == before:
        return False
    save_all(templates, path=path)
    return True


# ============================================================================
# Skill 路径解析 + system prompt 段格式化
# ============================================================================


# 模块级缓存（首次解析后缓存，避免重复读 _index.md）
_skill_path_map_cache: dict[str, str] | None = None


def build_skill_path_map(index_path: Path | None = None) -> dict[str, str]:
    """解析 .agents/skills/_index.md 构建 task_type → SKILL 文件路径映射。

    _index.md 有两种 skill 段格式：
    1. "组件化模块（自动生成）"段：每个 skill 用 `### <name> (<name>) [<scope>]`
       三级标题开头，内部用 - 列表项：
       - **task_type**：`recurring.accounting`
       - **Skill 文件**：`workspace/accounting/SKILL.md`
    2. "核心 Skill"段：每个 skill 用 `### N. <name> (<name>)` 三级标题开头，
       内部用 markdown table：
       | **task_type** | `dev.client_dev` |
       | **Skill 文件** | `.agents/skills/client_dev.md` |

    分段策略：按 `### ` 三级标题切段（每个三级标题到下一个三级标题之间为一段），
    每段同时提取 task_type 和 Skill 文件路径。

    Args:
        index_path: _index.md 路径，None 用 SKILL_INDEX_PATH

    Returns:
        dict[task_type, skill_path_relative]，找不到映射的 task_type 不在 dict 中
    """
    global _skill_path_map_cache
    if _skill_path_map_cache is not None and index_path is None:
        return _skill_path_map_cache

    p = index_path or SKILL_INDEX_PATH
    result: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("build_skill_path_map: 读取 %s 失败: %s", p, e)
        if index_path is None:
            _skill_path_map_cache = result
        return result

    # 按 `### ` 三级标题切段（保留每段开头的 `### ` 内容，便于解析）
    # 第一切片是文件开头到第一个 `### ` 之前的内容（不含 skill 定义，跳过）
    sections = re.split(r"(?=^### )", text, flags=re.MULTILINE)

    # task_type 正则：匹配 `recurring.accounting` / `adhoc.apikey_test` / `dev.client_dev` 等
    # 支持 - 列表项（**task_type**：`xxx`）和 table（| **task_type** | `xxx` |）
    tt_pattern = re.compile(
        r"task_type[^\n`]*?`([a-z_]+\.[a-z_]+)`",
        re.IGNORECASE,
    )
    # Skill 文件路径正则：匹配 `workspace/accounting/SKILL.md` 或 `.agents/skills/ocr.md`
    # 支持 - 列表项（**Skill 文件**：`...`）和 table（| **Skill 文件** | `...` |）
    # 路径字符集：字母数字 / . / _ / - / /
    # 注：不排除 `|`，否则 table 格式中 "**Skill 文件** | `...`" 的管道符会阻断匹配
    path_pattern = re.compile(
        r"Skill\s*文件[^\n`]*?`([\w./\-]+)`",
        re.IGNORECASE,
    )

    for section in sections:
        tt_match = tt_pattern.search(section)
        path_match = path_pattern.search(section)
        if not tt_match or not path_match:
            continue
        task_type = tt_match.group(1)
        skill_path = path_match.group(1).strip()
        if not skill_path:
            continue
        # 跳过明显异常路径（如未填的占位）
        if skill_path in ("(unknown)", "TODO"):
            continue
        result[task_type] = skill_path

    if index_path is None:
        _skill_path_map_cache = result
    return result


def _fallback_skill_path(task_type: str) -> str | None:
    """task_type → fallback SKILL.md 路径（_index.md 找不到时用）。

    规则：
    - task_type 形如 "recurring.accounting" / "adhoc.auto_shutdown" / "dev.implement"
    - 取最后一段作为 name（accounting / auto_shutdown / implement）
    - 尝试两种路径模板：
      1. workspace/<name>/SKILL.md
      2. .agents/skills/<name>/SKILL.md
      3. .agents/skills/<name>.md
    - 第一个存在的路径即为 fallback

    Args:
        task_type: 如 "recurring.accounting"

    Returns:
        找到的相对路径字符串，找不到返回 None
    """
    if not task_type or "." not in task_type:
        return None
    name = task_type.rsplit(".", 1)[-1]
    candidates = [
        f"workspace/{name}/SKILL.md",
        f".agents/skills/{name}/SKILL.md",
        f".agents/skills/{name}.md",
    ]
    for rel in candidates:
        abs_path = _PROJECT_ROOT / rel
        if abs_path.exists():
            return rel
    return None


def resolve_skill_path(task_type: str, index_path: Path | None = None) -> str | None:
    """解析 task_type → SKILL.md 相对路径。

    优先查 _index.md 映射表；找不到时走 _fallback_skill_path 兜底。

    Args:
        task_type: 如 "recurring.accounting"

    Returns:
        相对路径字符串（如 "workspace/accounting/SKILL.md"），找不到返回 None
    """
    if not task_type:
        return None
    path_map = build_skill_path_map(index_path=index_path)
    if task_type in path_map:
        return path_map[task_type]
    # fallback：用 name 探测路径
    return _fallback_skill_path(task_type)


def format_skills_prompt(
    skills: list[str],
    index_path: Path | None = None,
) -> str:
    """把 task_type list 转 system prompt 段（decisions D2）。

    输出格式：
        # Active Skills

        本会话已激活以下 skill：
        1. accounting (workspace/accounting/SKILL.md)
        2. arknights_gacha (workspace/arknights_gacha/SKILL.md)
        请先读取相关 SKILL.md 了解流程。

    找不到路径的 task_type：跳过 + log warning。

    Args:
        skills: task_type list（如 ["recurring.accounting", "adhoc.deep_research"]）

    Returns:
        system prompt 段字符串；skills 为空或全部解析失败时返回空字符串
    """
    if not skills:
        return ""

    lines: list[str] = []
    skipped: list[str] = []
    for idx, task_type in enumerate(skills, start=1):
        path = resolve_skill_path(task_type, index_path=index_path)
        if path is None:
            skipped.append(task_type)
            continue
        # 路径里取 name 部分（"workspace/accounting/SKILL.md" → "accounting"）
        # 兼容 .agents/skills/auto_shutdown.md → "auto_shutdown"
        name = task_type.rsplit(".", 1)[-1]
        lines.append(f"{idx}. {name} ({path})")

    if skipped:
        logger.warning(
            "format_skills_prompt: 跳过无法解析的 task_type: %s",
            ", ".join(skipped),
        )

    if not lines:
        return ""

    return (
        "# Active Skills\n\n"
        "本会话已激活以下 skill：\n"
        + "\n".join(lines)
        + "\n请先读取相关 SKILL.md 了解流程。"
    )


# ============================================================================
# 模块重载：重置缓存（测试用）
# ============================================================================


def list_all_task_types(index_path: Path | None = None) -> list[str]:
    """列出 _index.md 中所有可解析的 task_type（按 UTF-8 顺序排序）。

    用于模板编辑器的 skill 复选框列表渲染（decisions D7：全部 task_type）。
    实际数量随 _index.md 增长变化（spec 写 49 是估值，实际 60+）。

    Args:
        index_path: _index.md 路径，None 用 SKILL_INDEX_PATH

    Returns:
        排序后的 task_type list（如 ["adhoc.apikey_test", "adhoc.auto_shutdown", ...]）
    """
    path_map = build_skill_path_map(index_path=index_path)
    return sorted(path_map.keys())


def _reset_skill_path_map_cache() -> None:
    """重置路径映射缓存（测试用，正常代码勿调）。"""
    global _skill_path_map_cache
    _skill_path_map_cache = None
