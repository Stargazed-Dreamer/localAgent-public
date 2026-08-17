"""Agent Guide 端点 - 轻量级路由层

为 agent 提供任务指导入口，解决 agent 接到任务后需要读 _index.md 扫描全量 skill 的探索成本问题。

工作模式：
  - 无参 → GeneralGuide（全量分类清单 + 通用陷阱 + 文件位置）
  - task_type 精确命中 → TaskGuide（决策摘要 + first_action）
  - task 关键词匹配 → TaskGuide + candidates（top-5 候选清单，可纠错）
  - task 无匹配 → GeneralGuide + match_hint

设计原则：
  1. 不重复 — guide 是路由层，不替代 _index.md / AGENTS.md / skill 文件 / 记忆
  2. 两步走 — guide 给决策摘要，agent 确认对了再读 skill 原文件拿完整工作流
  3. 可纠错 — task 匹配返回候选清单，agent 能看到"从哪些里选的"，错了能重选
"""

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Query

from lib.schema import BaseSchema

# 以下两个模块顶部仅依赖 stdlib（json/logging/pathlib），无 import 副作用，
# 直接顶部 import 取代原函数级 lazy import（消除"循环依赖信号"误判）。
from server.mcp_whitelist import CATEGORY_ORDER, TOOL_CATEGORIES
from server.project_structure import (
    diff_structure,
    ensure_baseline,
    load_baseline,
    scan_project_structure,
)

logger = logging.getLogger("localagent.agent_guide")
router = APIRouter(prefix="/guide", tags=["AgentGuide"])

# ========== 使用埋点 ==========

USAGE_FILE = Path(__file__).parent.parent / "data" / "agent_guide" / "usage.json"
HISTORY_FILE = Path(__file__).parent.parent / "data" / "agent_guide" / "history.json"
HISTORY_MAX_SIZE = 50  # 最近 50 次调用记录（FIFO，超出砍最旧）

_usage_lock = threading.Lock()
_usage: dict = {}  # {task_type: {count, first_called, last_called, last_task_query}}
_usage_loaded = False

_history_lock = threading.Lock()
_history: list = []  # 最近 HISTORY_MAX_SIZE 次调用记录
_history_loaded = False


def _ensure_usage_loaded():
    """懒加载 usage 数据（避免 import 副作用）"""
    global _usage, _usage_loaded
    if _usage_loaded:
        return
    with _usage_lock:
        if _usage_loaded:
            return
        if USAGE_FILE.exists():
            try:
                _usage = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _usage = {}
        else:
            _usage = {}
        _usage_loaded = True


def _save_usage():
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(_usage, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _ensure_history_loaded():
    """懒加载 history 数据。

    注：用 clear()+extend() 原地修改 _history list，不重新绑定模块级变量。
    这样 `from server.agent_guide import _history` 的引用能看到加载后的数据。
    """
    global _history_loaded
    if _history_loaded:
        return
    with _history_lock:
        if _history_loaded:
            return
        loaded: list = []
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                # 兼容旧格式或损坏文件
                loaded = data.get("records", []) if isinstance(data, dict) else []
                if not isinstance(loaded, list):
                    loaded = []
            except (json.JSONDecodeError, OSError):
                loaded = []
        # 原地修改，避免重新绑定 _history 让外部 import 引用失效
        _history.clear()
        _history.extend(loaded)
        _history_loaded = True


def _save_history():
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "max_size": HISTORY_MAX_SIZE,
            "records": _history,
        }
        HISTORY_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _record_usage(
    mode: str,
    task_type: str | None,
    task_query: str | None,
    context: str | None = None,
    strong_match: bool | None = None,
):
    """记录一次 agent_guide 调用。

    两路记录：
    1. usage.json — 按 task_type 分桶的聚合统计（count/first_called/last_called/last_task_query）
    2. history.json — 最近 HISTORY_MAX_SIZE 次调用的实际文本（FIFO）

    后者用于未来分析"用户实际怎么问 guide"以改进 keywords 匹配。

    局限：仅追踪走后端 /guide 的调用。agent 直接 Read .md 文件不经过此端点，无法追踪。
    """
    _ensure_usage_loaded()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    key = task_type or "__general__"

    # 1. 聚合统计
    with _usage_lock:
        if key not in _usage:
            _usage[key] = {"count": 0, "first_called": now, "last_called": now, "last_task_query": ""}
        entry = _usage[key]
        entry["count"] += 1
        entry["last_called"] = now
        if task_query:
            entry["last_task_query"] = task_query[:100]
    threading.Thread(target=_save_usage, daemon=True).start()

    # 2. 历史记录（FIFO，最近 HISTORY_MAX_SIZE 条）
    _ensure_history_loaded()
    record = {
        "ts": now,
        "mode": mode,
        "task": (task_query or "")[:200],  # 截断防止超大 query 撑爆文件
        "task_type": task_type,
        "context": (context or "")[:100] if context else None,
        "strong_match": strong_match,
    }
    with _history_lock:
        _history.append(record)
        # FIFO 砍尾：保留最近 HISTORY_MAX_SIZE 条
        # 注：用 del 而非 _history = _history[...] 后者会让 Python 把 _history 当作局部变量
        if len(_history) > HISTORY_MAX_SIZE:
            del _history[:-HISTORY_MAX_SIZE]
    threading.Thread(target=_save_history, daemon=True).start()




# GUIDE_REGISTRY / _DEV_ROLES / _DEV_ENTRY_POINTS / GENERAL_GUIDE 从数据模块 import。
# workspace 可选组件条目由下方 _load_optional_guide_entries() 在运行时合并。
#
# 注意：GUIDE_REGISTRY 用浅拷贝创建独立 dict，而非直接 import 引用。
# 原因：test_component_e2e 通过 importlib.reload(agent_guide) 模拟组件删除/添加，
# 若直接 import 引用，reload(agent_guide) 不会 reload agent_guide_data，
# 导致 GUIDE_REGISTRY 保留上次的动态条目污染（stock_advisor 删除后仍存在）。
# 浅拷贝确保 reload 时 GUIDE_REGISTRY 重置为静态版本，再由 update 合并动态条目。
from server.agent_guide_data import (  # noqa: E402
    _DEV_ENTRY_POINTS,
    _DEV_ROLES,
    GENERAL_GUIDE,
)
from server.agent_guide_data import (  # noqa: E402
    GUIDE_REGISTRY as _STATIC_GUIDE_REGISTRY,
)

# GUIDE_REGISTRY = 静态内置条目（浅拷贝）+ 运行时合并的 workspace 可选组件条目
GUIDE_REGISTRY: dict[str, dict] = dict(_STATIC_GUIDE_REGISTRY)


def _load_optional_guide_entries() -> dict[str, dict]:
    """加载 workspace 下所有可选组件的 agent_guide 条目。

    双轨策略：
    1. 优先读 manifest 的 agent_guide 入口字段（manifest 声明的组件）
    2. 回退旧机制扫 workspace/*/loop_actions.py 读 GUIDE_REGISTRY_ENTRIES（向后兼容）

    manifest 优先：若组件已通过 manifest 加载，旧机制跳过该组件避免重复。
    返回的字典结构与 GUIDE_REGISTRY 相同：{task_type: entry}。

    主代码库不直接 import 任何 workspace 组件，删除 workspace/<component>/
    后 manifest 消失，4 个插入点自动注销。
    """
    import importlib

    from server.component_manifest import load_manifests

    result: dict[str, dict] = {}
    loaded_components: set[str] = set()

    # 1. 优先从 manifest 加载（新机制）
    try:
        manifests = load_manifests()
    except Exception:
        manifests = {}
    for name, m in manifests.items():
        if not m.enabled or m.agent_guide is None:
            continue
        # 按 manifest 声明的 file 动态加载模块（去掉 .py 后缀）
        module_name = f"workspace.{name}.{m.agent_guide.file[:-3]}"
        try:
            mod = importlib.import_module(module_name)
        except ImportError as e:
            logger.warning("manifest 组件 %s agent_guide 模块加载失败 (%s): %s", name, module_name, e)
            continue
        entries = getattr(mod, m.agent_guide.entries_var, None)
        if isinstance(entries, dict) and entries:
            result.update(entries)
            loaded_components.add(name)

    # 2. 回退旧机制：扫 workspace/*/loop_actions.py，跳过已通过 manifest 加载的组件
    # 使用 component_manifest._WORKSPACE_DIR 而非 workspace.__path__，
    # 让 monkeypatch _WORKSPACE_DIR 的测试能同时影响 manifest 加载和 fallback 扫描。
    from server.component_manifest import _WORKSPACE_DIR as _ws_dir
    if not _ws_dir.exists():
        return result
    for child in sorted(_ws_dir.iterdir()):
        if not child.is_dir() or child.name.startswith('_') or child.name.startswith('.'):
            continue
        if child.name in loaded_components:
            continue
        try:
            mod = importlib.import_module(f'workspace.{child.name}.loop_actions')
        except ImportError:
            continue
        entries = getattr(mod, 'GUIDE_REGISTRY_ENTRIES', None)
        if isinstance(entries, dict) and entries:
            result.update(entries)

    return result


# 合并可选组件的 agent_guide 条目（在模块加载时执行一次）
GUIDE_REGISTRY.update(_load_optional_guide_entries())


def _build_mcp_tool_categories() -> dict:
    """从 mcp_whitelist 构建分桶概览（agent 看到分桶结构而非扁平 50 个工具）。

    配合 mcp_whitelist.py 的 TOOL_CATEGORIES + CATEGORY_ORDER，让 agent 在 GeneralGuide
    响应中看到 MCP 工具按桶分组，而非"一堆 browser_ 淹没列表"。TaskGuide 不返回此字段
    （task 上下文已聚焦，不需要全量工具概览）。
    """
    total_tools = sum(len(TOOL_CATEGORIES[c]) for c in CATEGORY_ORDER)
    return {
        "categories": {cat: TOOL_CATEGORIES[cat] for cat in CATEGORY_ORDER},
        "total_tools": total_tools,
        "note": (
            f"MCP 第一层工具按桶分组展示（6 桶 {total_tools} 个直连），桶间按 CATEGORY_ORDER 顺序输出。"
            "browser_legacy 4 个（open/click_element/fill_input/wait_for_load）从第一层下沉到 "
            "localagent_advanced_tool 网关（用 localagent_list_tools 查询可访问的网关工具），"
            "agent 优先用 browser_action / browser_session_create / browser_wait_for 新接口。"
            "UIA 语义层（screen_accessibility_snapshot / screen_semantic_action）已升入 perception 桶，"
            "与 browser_snapshot 对齐，支持 Computer Use 双语义快照（DOM + UIA）。"
            "状态查询工具（memory_status / ocr_status / vision_status / exec_cmd）已升入第一层，"
            "避免 agent 走网关多一跳。"
            f"三层定位：DIRECT_TOOLS（{total_tools} 直连）> advanced_tool 网关（legacy + 其他 REST）> GATEWAY_EXCLUDE（完全排除）。"
        ),
    }


def _build_task_categories() -> dict:
    """从 GUIDE_REGISTRY 构建 task_categories 全量清单（按 scope 分组）。

    dev scope 额外按 role 分层（entry/method/support/tool），让 agent 一眼看到入口。
    """
    scopes = {}
    for task_type, entry in GUIDE_REGISTRY.items():
        scope = task_type.split(".", 1)[0]
        if scope not in scopes:
            scope_desc = {
                "recurring": "周期循环任务（用户发起，按固定频率）",
                "adhoc": "一次性任务（用户发起，无固定频率）",
                "system": "系统元任务（后端/agent自动触发，非用户直接发起）",
                "dev": "开发任务（面向项目本身的工程化开发，借鉴 mattpocock/skills 改造）",
                "daily": "日常事务（计划打磨、学习教学等非开发任务，借鉴 mattpocock/skills 改造）",
            }
            scopes[scope] = {
                "description": scope_desc.get(scope, scope),
                "tasks": [],
            }
        task_item = {
            "task_type": task_type,
            "name": entry.get("name", task_type),
            "desc": entry.get("description", ""),
        }
        # dev 桶附 role 字段
        role = _DEV_ROLES.get(task_type)
        if role:
            task_item["role"] = role
        scopes[scope]["tasks"].append(task_item)
    # dev scope 按 role 分层展示（entry 优先，便于 agent 找入口）
    if "dev" in scopes:
        role_order = {"entry": 0, "method": 1, "support": 2, "tool": 3}
        scopes["dev"]["tasks"].sort(
            key=lambda t: (role_order.get(t.get("role", "tool"), 4), t["task_type"])
        )
        scopes["dev"]["role_legend"] = {
            "entry": "任务入口/编排流程（第一步）",
            "method": "方法论参考（被 entry 引用）",
            "support": "辅助审查/排障",
            "tool": "工具型 skill",
        }
        scopes["dev"]["entry_points"] = _DEV_ENTRY_POINTS
    return scopes


def _char_bigrams(text: str) -> set[str]:
    """中文 2-gram 分词：把文本切成相邻字符对。

    "分析当前屏幕" → {"分析", "析当", "当前", "前屏", "屏幕"}
    用于解决"屏幕操作" vs "屏幕内容"顺序反转导致的子串匹配失败。
    过滤标点和空白字符。
    """
    # 去标点、空白，保留中文/英文/数字
    cleaned = "".join(c for c in text if c.isalnum())
    return {cleaned[i:i+2] for i in range(len(cleaned) - 1)} if len(cleaned) >= 2 else ({cleaned} if cleaned else set())


# 语气助词片段：从 task 中剥离后再算 bigram。
# 这些片段在中文里起语气调节作用，不承载核心语义，
# 但作为 bigram 会制造噪声（如"保存一下"的"一下"误匹配 prototype 的"试一下" kw_fuzzy 50%）。
# 剥离后"保存一下" → "保存"，"试一下"作为子串不再出现，避免误匹配。
# 注：剥离只在 bigram 计算时启用，kw_exact 仍用原文（保留 keyword 完整匹配能力）。
_AUX_PARTICLES: list[str] = [
    "一下儿", "看一看", "试一试", "弄一下", "搞一下",
    "一下", "看看", "试试", "弄下", "搞下", "下吧", "下来",
]


def _strip_aux(text: str) -> str:
    """剥离语气助词片段。多次扫描确保覆盖重叠模式。"""
    result = text
    for aux in _AUX_PARTICLES:
        result = result.replace(aux, "")
    return result


# 同义词表：task 中的词 → entry keywords/description 里可能出现的同义表达
# 用于解决"分析屏幕"匹配不上"截图/屏幕操作"的语义鸿沟
_SYNONYM_GROUPS: list[set[str]] = [
    {"屏幕", "画面", "桌面", "显示器", "截屏", "截图", "fullscreen", "screen"},
    {"识别", "分析", "解析", "读取", "辨认", "理解", "看", "ocr"},
    {"点击", "鼠标", "按键", "单击", "双击", "cursor", "click"},
    {"输入", "打字", "填写", "键入", "type", "input"},
    {"窗口", "应用", "程序", "软件", "window", "app"},
    {"操作", "控制", "操控", "自动化", "action", "control"},
    {"文字", "文本", "内容", "text", "content"},
    # 爬取/保存类动作同义组（解决"爬"vs"保存"语义鸿沟）：
    # 用户口语说"爬文章"，书面 keyword 写"保存网页/存档网页"——同组命中 + 跨词加分。
    # 注：把"爬"和"保存"放同组会让所有"爬+保存"场景同义命中，目前 web_archive 是唯一保存网页的
    # skill，安全；若未来加其他保存类 skill 需 review 是否需要拆组。
    {"爬", "爬取", "抓取", "扒", "scrape", "crawl",
     "保存", "存档", "归档", "下载", "采集", "save", "archive"},
]


def _synonym_hits(task_words: set[str], entry_words: set[str]) -> tuple[int, int]:
    """统计 task 词与 entry 词在同义词组中的命中数。

    返回 (base_hits, bonus_hits)：
    - base_hits: task 和 entry 在同义词组里都有命中（不论是否同词）→ 每组 +1
    - bonus_hits: task 和 entry 用了同义词组里**不同的词**（真正的同义词替换）→ 每组 +1
    """
    base = 0
    bonus = 0
    for group in _SYNONYM_GROUPS:
        task_in_group = task_words & group
        entry_in_group = entry_words & group
        if task_in_group and entry_in_group:
            base += 1
            # 不同词（真正的同义词替换，如 task 说"分析"，entry 说"识别"）→ 额外加分
            if not (task_in_group & entry_in_group):
                bonus += 1
    return base, bonus


# 弱匹配判定阈值：top-1 分数低于此值视为"匹配置信度低"，
# 不返回完整 TaskGuide（避免大量误导文本污染上下文），改走精简响应。
# 15 分大致对应"3 个 bigram 重叠（+6）+ 1 个同义词组基础+不同词（+3+2）= 11 分仍弱"，
# 真正的强匹配通常 ≥ 20 分（一个 kw_exact +10 + bigram/synonym 加分）。
# 注：分数只是初筛，最终是否走完整 TaskGuide 还需 strong_match=True（kw_exact 或 kw_fuzzy≥80%）。
WEAK_MATCH_THRESHOLD = 15


# ========== 上下文增强（C 改进） ==========
# 用户给 agent_guide 的查询常附带文件路径或 URL，例如：
#   task="爬一下文章", context="F:\\project_temp\\localAgent\\temp\\tabs_current-window_all_...json"
# 这些上下文含强信号（文件内是 xiaoheihe.cn 链接 → 几乎必中 web_archive），
# 但原算法只看 task 字符串，丢弃了上下文。本节实现上下文提取与加分。

_URL_PATTERN = re.compile(r'https?://([\w.-]+)', re.IGNORECASE)
# 二进制检测：读前 4KB，含 NUL 字节视为二进制（用户要求：二进制拒绝处理）
_BINARY_DETECT_BYTES = 4096
# context 文本最大长度（防止读超大文件拖慢匹配）
_CONTEXT_TEXT_MAX = 50_000


def _read_context_file(path_str: str) -> str | None:
    """读取上下文文件，二进制返回 None。

    严格按用户要求：只处理可解码文本文件（utf-8/ansi/gbk/latin-1），
    二进制（含 \\x00 字节）直接拒绝处理。

    返回文件文本（截断到 _CONTEXT_TEXT_MAX 字符）；不可读返回 None。
    """
    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return None
    try:
        raw = p.read_bytes()[:_BINARY_DETECT_BYTES]
    except OSError:
        return None
    # 二进制检测：含 NUL 字节视为二进制
    if b'\x00' in raw:
        return None
    # 依次尝试 utf-8 / gbk(ANSI) / latin-1
    for enc in ('utf-8', 'gbk', 'latin-1'):
        try:
            text = p.read_text(encoding=enc, errors='strict')
            return text[:_CONTEXT_TEXT_MAX]
        except UnicodeDecodeError:
            continue
    return None


def _extract_context_signals(context: str) -> dict | None:
    """从 context 字符串提取信号：URL domains + 中文 bigrams + 全文本。

    context 可能是：
    - 文件路径（含分隔符且作为路径存在）→ 读文件内容
    - URL 或任意文本 → 直接用

    返回 None 表示无可用上下文。返回 dict 含：
    - text: 实际文本（文件内容或原 context）
    - domains: 提取到的域名集合（小写）
    - bigrams: 文本的中文 2-gram 集合（仅含中文字符对，过滤英文/数字，
      避免 URL 参数名如 link/description/camp 与英文 keywords 偶然重叠）

    注：bigrams 只保留中文 bigrams 是关键设计——context 文件常含 URL 编码片段
    和英文参数名（如 redirect_data/h_session_id），这些是结构噪音不是语义信号，
    全量并入会跟英文 keywords 的 skill（如 dev.impeccable 的 craft/polish/critique）
    产生大量误重叠。
    """
    if not context or not context.strip():
        return None

    text = context
    # 简单判断：context 像路径（含分隔符、无换行、不太长）且作为路径存在 → 读文件
    if (
        len(context) < 500
        and '\n' not in context
        and ('/' in context or '\\' in context)
    ):
        file_text = _read_context_file(context)
        if file_text is not None:
            text = file_text

    text_lower = text.lower()
    domains = {m.lower() for m in _URL_PATTERN.findall(text)}
    # 只保留中文 bigrams（两个字符都是中文），过滤英文/数字 bigrams
    all_bigrams = _char_bigrams(text_lower)
    cn_bigrams = {bg for bg in all_bigrams if all(c.isalpha() and ord(c) > 127 for c in bg)}
    # 剥离助词后的中文 bigrams 也并入
    stripped = _strip_aux(text_lower)
    if stripped != text_lower:
        stripped_bigrams = _char_bigrams(stripped)
        cn_bigrams = cn_bigrams | {bg for bg in stripped_bigrams if all(c.isalpha() and ord(c) > 127 for c in bg)}
    return {
        "text": text,
        "domains": domains,
        "bigrams": cn_bigrams,
    }


def match_task_candidates(task: str, top_n: int = 5, context: str | None = None) -> list[dict]:
    """从用户任务描述匹配候选 task_type 列表，按得分降序返回 top_n。

    匹配策略（五路加权）：
    1. **完整子串匹配**（+10/词）：entry keyword 完整出现在 task 中。最强信号。
    2. **2-gram 重叠**（+2/重叠 bigram，上限 16）：task 和 entry 的 2-gram 集合交集。
       双向匹配，解决"屏幕操作"vs"屏幕内容"顺序反转。每个重叠的 2-gram（如"屏幕"）给分。
    3. **同义词组命中**（+3/组基础 +2/组不同词额外）：语义相关。
       如 task 说"分析"，entry 说"识别"→ 同义词组命中 + 不同词额外加分。
    4. **keyword 高重叠**（+3/词 if ratio≥50%）：keyword 的 2-gram 大部分在 task 中。
    5. **domain_hints 加分**（C 改进）：当 context 含特定 domain 时，对应 entry 加分。
       如 context 文件含 xiaoheihe.cn → web_archive +10。

    context 参数（C 改进）：
    - 文件路径（含分隔符且存在）→ 读文件内容（utf-8/ansi/gbk，二进制拒绝）
    - URL 或任意文本 → 直接用
    - 提取的 bigrams 并入 task_bigrams，domains 用于 domain_hints 加分

    返回 [{"task_type": ..., "name": ..., "score": ..., "strong_match": bool,
            "desc": ..., "matched": [...]}]。
    空列表表示无匹配（调用方应回退到 GeneralGuide）。
    strong_match=False 的候选代表弱匹配（仅字符偶然重叠），调用方应避免基于它返回完整 TaskGuide。
    """
    task_lower = task.lower()
    # 助词过滤：剥离"一下/看看/试试"等再算 bigram（避免"保存一下"误匹配"试一下"）
    task_stripped = _strip_aux(task_lower)
    task_bigrams = _char_bigrams(task_stripped)
    # task 的词集合（用于同义词匹配）：2-gram + 单字
    task_words = task_bigrams | {c for c in task_stripped if c.isalnum()}

    # 提取 context 信号（C 改进）
    ctx_signals = _extract_context_signals(context) if context else None
    if ctx_signals:
        # 把 context 的 bigrams 并入 task_bigrams（扩大匹配面）
        task_bigrams = task_bigrams | ctx_signals["bigrams"]
        # context 文本中的字符也加入 task_words（用于同义词匹配）
        ctx_chars = {c for c in ctx_signals["text"].lower() if c.isalnum()}
        task_words = task_words | ctx_chars

    scores = []
    for task_type, entry in GUIDE_REGISTRY.items():
        score = 0
        matched_reasons = []

        entry_keywords = entry.get("keywords", [])
        entry_desc = entry.get("description", "")
        # entry 的词集合（2-gram + 单字 + 分词）
        entry_text = " ".join(entry_keywords) + " " + entry_desc
        entry_bigrams = _char_bigrams(entry_text.lower())
        entry_words = entry_bigrams | {c for c in entry_text.lower() if c.isalnum()}

        # 1. 完整子串匹配（用原文 task_lower，不剥离助词，保留 keyword 完整匹配能力）
        for kw in entry_keywords:
            if kw.lower() in task_lower:
                score += 10
                matched_reasons.append(f"kw_exact:{kw}")

        # 2. 2-gram 重叠（双向）：task 和 entry 的 bigram 集合交集
        overlap = task_bigrams & entry_bigrams
        if overlap:
            bonus = min(len(overlap) * 2, 16)
            score += bonus
            matched_reasons.append(f"bigram_overlap:{len(overlap)}")

        # 3. 同义词组命中
        base_syn, bonus_syn = _synonym_hits(task_words, entry_words)
        if base_syn > 0:
            score += base_syn * 3 + bonus_syn * 2
            matched_reasons.append(f"synonym:{base_syn}+{bonus_syn}")

        # 4. keyword 高重叠（ratio ≥ 50%）
        for kw in entry_keywords:
            kw_lower = kw.lower()
            # 短英文 keyword（纯 ASCII 字母/数字/点，去空格后 ≤4 字符）跳过 kw_fuzzy：
            # 这类 keyword（IRR/NPV/DCF/TDD/ADR/WIP 等）bigram 仅 2-3 个，
            # 跟任何含相同字符对的英文查询会 100% 误匹配
            # （如 "list current directory" 命中 IRR(100%) → office_xlsx strong_match）。
            # 短英文 keyword 仍参与 kw_exact（完整子串），只跳过模糊匹配。
            kw_stripped = kw_lower.replace(" ", "")
            if re.fullmatch(r'[a-z0-9.]+', kw_stripped) and len(kw_stripped) <= 4:
                continue
            kw_bigrams = _char_bigrams(kw_lower)
            if not kw_bigrams:
                continue
            kw_overlap = kw_bigrams & task_bigrams
            if kw_overlap:
                ratio = len(kw_overlap) / len(kw_bigrams)
                if ratio >= 0.5:
                    score += 3
                    matched_reasons.append(f"kw_fuzzy:{kw}({ratio:.0%})")

        # 5. domain_hints 加分（C 改进：上下文增强）
        # entry 可声明 domain_hints: {domain: bonus}，当 context 含对应 domain 时加分
        if ctx_signals:
            domain_hints = entry.get("domain_hints", {})
            if domain_hints and ctx_signals["domains"]:
                hit_domains = []
                for domain, bonus in domain_hints.items():
                    for cd in ctx_signals["domains"]:
                        if cd == domain or cd.endswith('.' + domain):
                            score += bonus
                            hit_domains.append(domain)
                            break
                if hit_domains:
                    matched_reasons.append(f"domain_hint:{','.join(hit_domains)}")

        if score > 0:
            scores.append((score, task_type, matched_reasons))

    if not scores:
        # 关键词全空匹配时，用向量化兜底构建初始 candidates
        # （如"捋一下时间线"无任何 keyword 命中，但向量化可能有语义关联）
        # 失败静默返回 []，调用方走 GeneralGuide
        try:
            from server.agent_guide_embedder import semantic_match
            sem_results = semantic_match(task, top_k=top_n)
            if sem_results:
                fallback = []
                for task_type, cosine in sem_results:
                    if task_type not in GUIDE_REGISTRY:
                        continue
                    entry = GUIDE_REGISTRY[task_type]
                    score = int(cosine * 20)
                    # 双条件 strong_match：cosine ≥ 0.6 OR (score ≥ 阈值 AND cosine ≥ 0.4)
                    strong = cosine >= 0.6 or (score >= WEAK_MATCH_THRESHOLD and cosine >= 0.4)
                    fallback.append({
                        "task_type": task_type,
                        "name": entry.get("name", task_type),
                        "score": score,
                        "strong_match": strong,
                        "desc": entry.get("description", ""),
                        "matched": [f"semantic_only:{cosine:.2f}"],
                    })
                if fallback:
                    return fallback
        except Exception as e:
            logger.debug(f"空匹配向量化兜底失败: {e}")
        return []

    scores.sort(key=lambda x: -x[0])
    result = []
    for score, task_type, reasons in scores[:top_n]:
        entry = GUIDE_REGISTRY[task_type]
        # 强匹配判定（保守，宁可漏判让 agent 走精简响应，也不误判返回完整 TaskGuide）：
        # - kw_exact：keyword 完整子串命中（最可靠信号）
        # - kw_fuzzy ratio ≥ 80%：keyword 高度重叠（50% 太松，"怎么样"3字能命中"股票怎么样"）
        has_kw_exact = any(r.startswith("kw_exact:") for r in reasons)
        has_strong_fuzzy = False
        for r in reasons:
            if r.startswith("kw_fuzzy:"):
                # 解析 "kw_fuzzy:keyword(50%)" 中的百分比
                try:
                    pct_str = r[r.rfind("(") + 1:r.rfind(")")]
                    if int(pct_str.rstrip("%")) >= 80:
                        has_strong_fuzzy = True
                except (ValueError, IndexError):
                    pass
        # 强匹配判定（保守）：只有 kw_exact 或 kw_fuzzy ≥ 80% 才算强匹配。
        # 曾经的"中文 bigram_overlap ≥ 3"判定已移除——中文 2-gram 字符级重叠太易偶然
        # （"内容/工具/agent"等高频字会跟任何描述性 skill 撞上 3+ bigram），导致大量误匹配。
        # 纯 bigram 重叠的弱匹配走 low_confidence 路径，agent 仍能看到候选清单并精确重调。
        strong_match = has_kw_exact or has_strong_fuzzy
        result.append({
            "task_type": task_type,
            "name": entry.get("name", task_type),
            "score": score,
            "strong_match": strong_match,
            "desc": entry.get("description", ""),
            "matched": reasons,
        })

    # === 向量化语义补强（仅当 top-1 弱匹配时触发）===
    # 关键词匹配受限于 keywords 覆盖面，口语化查询（如"我今天都干了啥"vs"今日工作总结"）
    # 字符重合度极低，score 偏低。此时用 EmbeddingEngine 做语义匹配补强：
    # - semantic_bonus = cosine * 20（cosine 0.5 → +10，0.7 → +14）
    # - cosine ≥ SEMANTIC_STRONG_THRESHOLD(0.6) → 强制 strong_match=True
    # - 失败静默 fallback（engine 不可用 / numpy 未装等），不影响关键词匹配结果
    #
    # 只在 top-1 弱匹配时触发：强匹配场景跳过，避免影响现有测试和性能。
    if result:
        top_score = result[0].get("score", 0)
        top_strong = result[0].get("strong_match", False)
        if top_score < WEAK_MATCH_THRESHOLD or not top_strong:
            try:
                from server.agent_guide_embedder import (
                    SEMANTIC_STRONG_THRESHOLD,
                    semantic_match,
                )
                sem_results = semantic_match(task, top_k=len(result))
                sem_map = dict(sem_results) if sem_results else {}
                if sem_map:
                    for r in result:
                        tt = r["task_type"]
                        if tt in sem_map:
                            cosine = sem_map[tt]
                            bonus = int(cosine * 20)
                            r["score"] += bonus
                            r["matched"].append(f"semantic:{cosine:.2f}")
                            # 语义强匹配判定（双重条件，避免误匹配）：
                            # - cosine ≥ 0.6 → 直接 strong_match（语义强相关）
                            # - score ≥ 阈值 + cosine ≥ 0.4 → strong_match
                            #   （向量化补强后分数达标 + 语义相关，如"我今天都干了啥"cosine=0.51）
                            # 0.4 是语义相关下限（无关查询 cosine 通常 <0.4），
                            # 0.6 是语义强相关（同义改写、同主题同意图）
                            if cosine >= SEMANTIC_STRONG_THRESHOLD:
                                r["strong_match"] = True
                            elif r["score"] >= WEAK_MATCH_THRESHOLD and cosine >= 0.4:
                                r["strong_match"] = True
                    # 重新按 score 降序排序（向量化加分可能改变 top-1）
                    result.sort(key=lambda x: -x["score"])
            except Exception as e:
                logger.debug(f"向量化补强失败（不影响关键词匹配）: {e}")
    return result


_PROJECT_ROOT = Path(__file__).parent.parent


def _resolve_skill_file(skill_file: str) -> str:
    """自动解析 skill 文件路径，兼容扁平 .md 和文件夹 SKILL.md 两种结构。

    先查原始路径（如 .agents/skills/foo.md），不存在则尝试文件夹结构（.agents/skills/foo/SKILL.md）。
    """
    if not skill_file:
        return skill_file
    if (_PROJECT_ROOT / skill_file).exists():
        return skill_file
    if skill_file.endswith(".md"):
        folder_path = skill_file[:-3] + "/SKILL.md"
        if (_PROJECT_ROOT / folder_path).exists():
            return folder_path
    return skill_file


def _build_task_guide(task_type: str, candidates: list[dict] | None = None, include_workflow: bool = False) -> dict:
    """从 GUIDE_REGISTRY 构建 TaskGuide 响应

    include_workflow=False（默认）时只返核心决策字段，agent 确认 task_type 对了之后
    可传 include_workflow=true 调用一次获取 workflow_summary/mcp_tools_priority/key_pitfalls 详情。
    这样避免匹配错误时白返 skill 概要污染上下文。
    """
    entry = GUIDE_REGISTRY[task_type]

    # 收集相关记忆 key：静态 memory_key + 静态 related_memory_keys
    memory_index = []
    mem_keys = entry.get("memory_key")
    if mem_keys:
        if isinstance(mem_keys, str):
            mem_keys = [mem_keys]
    else:
        mem_keys = []
    related_keys = entry.get("related_memory_keys") or []
    static_keys = list(mem_keys) + [k for k in related_keys if k not in mem_keys]

    mgr = None
    try:
        # 故意保留函数级 import —— 不是循环依赖信号（manager 不反向 import 本模块），
        # 而是 fail-soft：memory.manager 顶部依赖 9 个 memory.* 子模块
        # （CompressionPipeline / EmbeddingEngine / EvidenceLedger 等），
        # 在缺配置或依赖缺失的运行环境下加载即失败；放函数内 + try/except 让
        # /guide 在 manager 不可用时仍返回基础响应（不阻断 agent 决策路径）。
        from server.memory.manager import get_memory_manager
        mgr = get_memory_manager()
    except Exception as e:
        logger.debug(f"获取 MemoryManager 失败: {e}")

    # 静态关联：按 key 查索引
    if static_keys and mgr:
        try:
            memory_index = mgr.get_memory_index(static_keys)
        except Exception as e:
            logger.debug(f"获取记忆索引失败: {e}")
            memory_index = []

    # 动态关联：按 consumption_contexts / trigger_keywords 反向查询
    dynamic_memories = []
    if mgr:
        try:
            task_query = candidates[0].get("name", "") if candidates else None
            dynamic_memories = mgr.find_consumable_memories(task_type, task_query=task_query)
        except Exception as e:
            logger.debug(f"动态关联查询失败: {e}")
            dynamic_memories = []

    # 合并静态+动态（去重，动态匹配的标记 matched_by）
    existing_keys = {m["key"] for m in memory_index}
    for dm in dynamic_memories:
        if dm["key"] not in existing_keys:
            dm["matched_by"] = dm.get("matched_by", "dynamic")
            memory_index.append(dm)
            existing_keys.add(dm["key"])

    # v6.1 T32: memory_index 每项追加 staleness 字段（spec D14 + D9）
    # 让首轮就能看到过时记忆警告，模型可决定是否优先 memory_get 验证
    try:
        from server.memory.router import _enrich_with_staleness
        _enrich_with_staleness(memory_index)
    except Exception as e:
        logger.debug(f"memory_index staleness 注入失败（不阻断）: {e}")

    # first_action 后处理注入"【必读记忆】"（零侵入路由层增强）
    # 只对实际存在的记忆（updated_at 非 None）注入，避免对空 key 占位条目误注入
    first_action = entry.get("first_action", "")
    existing_memories = [m for m in memory_index if m.get("updated_at") is not None]
    if existing_memories:
        must_read_lines = ["【必读记忆】以下记忆与当前任务相关，执行前先 memory_get 读取："]
        for m in existing_memories[:5]:
            summary = m.get("summary") or "(无摘要)"
            must_read_lines.append(f"- {m['key']}: {summary}")
        must_read_block = "\n".join(must_read_lines)
        first_action = must_read_block + "\n\n" + first_action

    result = {
        "mode": "task",
        "task_type": task_type,
        "matched_skill": entry.get("skill", ""),
        "name": entry.get("name", task_type),
        "skill_file": _resolve_skill_file(entry.get("skill_file", "")),
        "memory_key": entry.get("memory_key"),
        "memory_index": memory_index,
        "description": entry.get("description", ""),
        "first_action": first_action,
    }
    # workflow_summary / mcp_tools_priority / key_pitfalls 按需返回：
    # 默认不返（避免匹配错误时白返 skill 概要污染上下文）。
    # agent 确认 task_type 对了之后传 include_workflow=true 获取详情。
    # general_guide_compact（global_pitfalls/environment_notes/file_locations）已移除——
    # 这些全局信息在 AGENTS.md 已有，mode=general 也返一次，TaskGuide 不再重复。
    # next_step_hint（固定提示文字）已移除——每次都一样，浪费 token。
    if include_workflow:
        result["workflow_summary"] = entry.get("workflow_summary", "")
        result["mcp_tools_priority"] = entry.get("mcp_tools_priority", [])
        result["key_pitfalls"] = entry.get("key_pitfalls", [])
    if entry.get("prerequisites"):
        result["prerequisites"] = entry["prerequisites"]
    if entry.get("memory_generation_workflow"):
        result["memory_generation_workflow"] = entry["memory_generation_workflow"]
    if entry.get("task_closure_workflow"):
        result["task_closure_workflow"] = entry["task_closure_workflow"]
    if entry.get("do_not_store"):
        result["do_not_store"] = entry["do_not_store"]
    # dev 桶 role 标注 + entry 执行类前置提示
    role = _DEV_ROLES.get(task_type)
    if role:
        result["role"] = role
        # entry role 的执行类 skill（implement/to_tickets）假设目标已定义，附前置提示
        if role == "entry" and task_type in ("dev.implement", "dev.to_tickets"):
            result["prerequisite_check"] = (
                "⚠️ 本 skill 假设目标已定义（spec/tickets 已存在）。"
                "若目标仍模糊，建议先走 dev.goal_engineering 或 dev.grill_me 定义目标。"
            )
    if candidates is not None:
        # 精简 candidates：只保留 agent 决策需要的字段（task_type/name/score/strong_match）
        # 去掉 desc（一句长描述，5 候选浪费 ~500 字符）和 matched（"bigram_overlap:5" 技术内部信息）
        result["candidates"] = [
            {
                "task_type": c["task_type"],
                "name": c.get("name", c["task_type"]),
                "score": c.get("score", 0),
                "strong_match": c.get("strong_match", False),
            }
            for c in candidates
        ]
    return result


# ========== Pydantic 响应模型 ==========

class GeneralGuideResponse(BaseSchema):
    mode: str = "general"
    usage: str
    session_startup: list[str]
    session_closure: list[str] = []
    task_categories: dict
    mcp_tool_categories: dict = {}
    global_pitfalls: list[str]
    file_locations: dict
    mcp_priority: str
    dev_entry_points: dict = {}
    match_hint: str | None = None


# ========== 路由 ==========

@router.get("", operation_id="agent_guide")
def get_agent_guide(
    task: str | None = Query(None, description="用户任务描述（自然语言），用于关键词匹配"),
    task_type: str | None = Query(None, description="已知任务类型标识（如 recurring.accounting），直接命中"),
    include_structure: bool = Query(False, description="true=返回 project_structure 字段（当前扫描+已知 baseline 描述），用于了解项目目录布局。task_closure 自动包含 diff 无需显式传"),
    include_workflow: bool = Query(False, description="true=返回 workflow_summary/mcp_tools_priority/key_pitfalls 详情；默认不返，agent 确认 task_type 对了再传 true 获取"),
    context: str | None = Query(None, description="上下文信号（文件路径/URL/任意文本）。若是文本文件路径则读取内容（utf-8/ansi/gbk，二进制拒绝），提取 URL domain 用于 domain_hints 加分。如 task='爬一下文章' + context='tabs_xxx.json' → web_archive 高分命中"),
):
    """Agent Guide 端点 — 轻量级路由层，帮助 agent 快速定位任务指导。

    ⚠️ 调用完全安全：无副作用、不修改状态、不消耗配额、不触发审批。
    每个用户任务开始前都应调用，不用担心"要不要调"——调就对了。
    系统内置 60+ skill（日报/股票/抽卡/记账/浏览器/屏幕操作/记忆管理等），
    调用一次即可发现你需要的技能是否已内置，避免重复造轮子。

    **无参**：返回 GeneralGuide（全量分类清单 + 通用陷阱 + 文件位置 + 会话启动流程）。
    浏览一遍即可知道系统能做什么。

    **task_type 精确命中**：返回该任务的 TaskGuide（决策摘要 + first_action + 候选清单为空）。
    特殊：task_type='system.task_closure' 时自动扫描项目结构并与 baseline 对比，在响应中加 structure_diff 字段。

    **task 关键词匹配**：返回最优匹配的 TaskGuide + candidates（top-5 候选清单）。
    agent 看 candidates 确认 top-1 是否正确，错了能从候选重选。

    **task 无匹配**：返回 GeneralGuide + match_hint 提示。

    **include_structure=true**：额外返回 project_structure 字段（当前扫描的顶层目录 + baseline 中的描述），
    供 agent 在会话开始时了解项目布局，替代读 AGENTS.md 的项目结构章节。

    工作流（两步走）：
    1. 调 agent_guide(task='...') → 拿决策摘要 + 候选清单
    2. 确认对了 → 读 skill_file 拿完整工作流；错了 → 从 candidates 选 task_type 重调
    """
    structure_payload = _build_structure_payload() if include_structure else None

    # 1. task_type 精确命中
    if task_type:
        if task_type in GUIDE_REGISTRY:
            _record_usage("task", task_type, task, context=context, strong_match=True)
            result = _build_task_guide(task_type, candidates=[], include_workflow=include_workflow)
            # task_closure 自动注入结构漂移检查
            if task_type == "system.task_closure":
                result["structure_diff"] = _compute_structure_diff()
            if structure_payload:
                result["project_structure"] = structure_payload
            return result
        # task_type 传了但不存在 → 回退到 GeneralGuide
        _record_usage("general", None, f"[invalid task_type={task_type}] {task or ''}", context=context)
        resp = {
            "mode": "general",
            "usage": GENERAL_GUIDE["usage"],
            "session_startup": GENERAL_GUIDE["session_startup"],
            "session_closure": GENERAL_GUIDE["session_closure"],
            "task_categories": _build_task_categories(),
            "mcp_tool_categories": _build_mcp_tool_categories(),
            "global_pitfalls": GENERAL_GUIDE["global_pitfalls"],
            "environment_notes": GENERAL_GUIDE["environment_notes"],
            "file_locations": GENERAL_GUIDE["file_locations"],
            "mcp_priority": GENERAL_GUIDE["mcp_priority"],
            "dev_entry_points": _DEV_ENTRY_POINTS,
            "match_hint": f"task_type='{task_type}' 不存在，请浏览 task_categories 全量清单选正确的 task_type",
        }
        if structure_payload:
            resp["project_structure"] = structure_payload
        return resp

    # 2. task 关键词匹配
    if task:
        candidates = match_task_candidates(task, context=context)
        if candidates:
            top_candidate = candidates[0]
            top_task_type = top_candidate["task_type"]
            # 弱匹配判定：top-1 分数低于阈值，或无强匹配标志 → 走精简响应
            # 避免"仅字符偶然重叠"就返回完整 TaskGuide 污染上下文
            is_weak = (
                top_candidate.get("score", 0) < WEAK_MATCH_THRESHOLD
                or not top_candidate.get("strong_match", False)
            )
            if is_weak:
                _record_usage(
                    "general",
                    None,
                    f"[weak match] {task}",
                    context=context,
                    strong_match=False,
                )
                # 精简响应：candidates 含 task_type/name/score/strong_match
                # （strong_match 帮 agent 挑出真正强匹配的候选用 task_type= 精确调一次）
                slim_candidates = [
                    {
                        "task_type": c["task_type"],
                        "name": c.get("name", c["task_type"]),
                        "score": c.get("score", 0),
                        "strong_match": c.get("strong_match", False),
                    }
                    for c in candidates
                ]
                resp = {
                    "mode": "low_confidence",
                    "usage": GENERAL_GUIDE["usage"],
                    "candidates": slim_candidates,
                    "match_hint": (
                        f"匹配置信度低（top-1 score={top_candidate.get('score', 0)}，"
                        f"阈值={WEAK_MATCH_THRESHOLD}）。候选可能不准确，建议："
                        "①换更具体的关键词重试；②从 candidates 选 task_type 用 task_type= 精确调一次；"
                        "③浏览 task_categories 全量清单选 task_type（调 task_type='__list__' 或无参）。"
                    ),
                }
                # low_confidence 分支不注入 project_structure——agent 此刻在判断 candidates
                # 哪个 task_type 对，项目结构信息是噪音（~3500 字符的 top_level 描述+subdirs）。
                # agent 确认 task_type 后如需项目结构，可再调一次 include_structure=true。
                return resp
            _record_usage(
                "task",
                top_task_type,
                task,
                context=context,
                strong_match=True,
            )
            result = _build_task_guide(top_task_type, candidates=candidates, include_workflow=include_workflow)
            if top_task_type == "system.task_closure":
                result["structure_diff"] = _compute_structure_diff()
            if structure_payload:
                result["project_structure"] = structure_payload
            return result
        # 无匹配 → GeneralGuide + 提示
        _record_usage("general", None, task, context=context)
        resp = {
            "mode": "general",
            "usage": GENERAL_GUIDE["usage"],
            "session_startup": GENERAL_GUIDE["session_startup"],
            "session_closure": GENERAL_GUIDE["session_closure"],
            "task_categories": _build_task_categories(),
            "mcp_tool_categories": _build_mcp_tool_categories(),
            "global_pitfalls": GENERAL_GUIDE["global_pitfalls"],
            "environment_notes": GENERAL_GUIDE["environment_notes"],
            "file_locations": GENERAL_GUIDE["file_locations"],
            "mcp_priority": GENERAL_GUIDE["mcp_priority"],
            "dev_entry_points": _DEV_ENTRY_POINTS,
            "match_hint": "未匹配到任务，请浏览 task_categories 全量清单选 task_type",
        }
        if structure_payload:
            resp["project_structure"] = structure_payload
        return resp

    # 3. 无参 → GeneralGuide（防呆）
    _record_usage("general", None, None, context=None)
    resp = {
        "mode": "general",
        "usage": GENERAL_GUIDE["usage"],
        "session_startup": GENERAL_GUIDE["session_startup"],
        "session_closure": GENERAL_GUIDE["session_closure"],
        "task_categories": _build_task_categories(),
        "mcp_tool_categories": _build_mcp_tool_categories(),
        "global_pitfalls": GENERAL_GUIDE["global_pitfalls"],
        "environment_notes": GENERAL_GUIDE["environment_notes"],
        "file_locations": GENERAL_GUIDE["file_locations"],
        "mcp_priority": GENERAL_GUIDE["mcp_priority"],
        "dev_entry_points": _DEV_ENTRY_POINTS,
    }
    if structure_payload:
        resp["project_structure"] = structure_payload
    return resp


def _build_structure_payload() -> dict | None:
    """构建 project_structure 响应载荷（当前扫描 + baseline 描述合并）。

    失败时返回 None，不影响 guide 主流程。
    """
    try:
        current = scan_project_structure()
        baseline = load_baseline()
        base_top = (baseline or {}).get("top_level", {})
        # 合并：当前扫描的顶层条目 + baseline 的描述
        top_with_desc = {}
        for path, info in current["top_level"].items():
            base_entry = base_top.get(path, {})
            top_with_desc[path] = {
                "type": info["type"],
                "description": base_entry.get("description", ""),
                "in_baseline": path in base_top,
            }
        return {
            "top_level": top_with_desc,
            "subdirs": current["subdirs"],
            "baseline_updated": (baseline or {}).get("last_updated"),
            "note": "include_structure=true 提供。未在 baseline 的路径 in_baseline=false，可考虑补全描述。task_closure 时自动提供 diff。",
        }
    except Exception as e:
        logger.debug(f"构建 project_structure 载荷失败: {e}")
        return None


def _compute_structure_diff() -> dict | None:
    """计算项目结构漂移。task_closure 时调用。

    失败时返回 None，不阻断收尾流程。
    """
    try:
        ensure_baseline()
        current = scan_project_structure()
        baseline = load_baseline()
        if not baseline:
            return {"summary": "baseline 不存在（首次运行）", "unknown_paths": [], "missing_paths": []}
        diff = diff_structure(baseline, current)
        return diff
    except Exception as e:
        logger.debug(f"计算结构漂移失败: {e}")
        return None


@router.get("/usage", operation_id="agent_guide_usage")
def get_agent_guide_usage():
    """Agent Guide 使用统计 — 检测 undertriggering（哪些 skill 从未被路由到）。

    局限：仅追踪走后端 /guide 的调用。agent 直接 Read .md 文件不经过此端点，
    无法追踪。因此 "未被调用" 不等于 "未被使用"，仅表示未通过 agent_guide 路由。
    """
    _ensure_usage_loaded()
    with _usage_lock:
        usage_copy = copy.deepcopy(_usage)

    called_keys = set(usage_copy.keys()) - {"__general__"}
    all_keys = set(GUIDE_REGISTRY.keys())
    never_called = sorted(all_keys - called_keys)

    total_calls = sum(e["count"] for e in usage_copy.values())
    general_calls = usage_copy.get("__general__", {}).get("count", 0)

    per_scope = {}
    for task_type in all_keys:
        scope = task_type.split(".", 1)[0]
        if scope not in per_scope:
            per_scope[scope] = {"total": 0, "called": 0, "never": 0}
        per_scope[scope]["total"] += 1
        if task_type in called_keys:
            per_scope[scope]["called"] += 1
        else:
            per_scope[scope]["never"] += 1

    return {
        "total_calls": total_calls,
        "general_guide_calls": general_calls,
        "task_guide_calls": total_calls - general_calls,
        "called_count": len(called_keys),
        "never_called_count": len(never_called),
        "never_called": never_called,
        "per_scope": per_scope,
        "usage_detail": usage_copy,
        "limitation_note": "仅追踪 /guide 端点调用。agent 直接 Read .md 文件不经过此端点，无法追踪。never_called 不等于未被使用，仅表示未通过 agent_guide 路由。",
    }


def reset_usage() -> None:
    """重置 usage 内存状态（测试隔离用）。

    不删除磁盘 USAGE_FILE，仅清空内存缓存并强制下次访问时重新加载。
    """
    global _usage, _usage_loaded
    with _usage_lock:
        _usage = {}
        _usage_loaded = False


def get_status() -> dict:
    """供 /health 聚合的状态"""
    _ensure_usage_loaded()
    with _usage_lock:
        called = len(set(_usage.keys()) - {"__general__"})
        total_calls = sum(e["count"] for e in _usage.values())
    return {
        "available": True,
        "registry_count": len(GUIDE_REGISTRY),
        "scopes": sorted({k.split(".", 1)[0] for k in GUIDE_REGISTRY}),
        "usage": {
            "total_calls": total_calls,
            "called_skills": called,
            "never_called": len(GUIDE_REGISTRY) - called,
        },
    }

