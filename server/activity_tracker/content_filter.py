"""内容审核保护模块 — 防 hourly_summarize 因 GLM 内容过滤（code 1301）失败

三层保护：
1. 预检（find_sensitive_titles）：二分法把窗口标题列表发给 LLM，
   回答"否"→安全；"是"/"不确定"→保守二分标记敏感；LLM 调用失败→降级本地关键词匹配。
2. 模糊化（mask_title / mask_sensitive_in_records / mask_sensitive_in_text）：
   把敏感标题替换为 [已遮罩] + 保留应用名后缀（如 - Google Chrome）。
   screen VL 描述里出现的敏感关键词同样替换。
3. 后置检查（detect_filtered_response）：LLM 主调用返回后，
   若 finish_reason 指示内容审核、或 content（去除脱敏标记后）含 1301/contentFilter/违禁等关键词、
   或 content 极短（< 10 字）且非 maxtoken 截断，判定为触发审核，
   返回 True 让调用方走更激进的脱敏 + 重试。

设计权衡：
- 预检 LLM 系统提示词：'以下窗口标题列表是否含有通常意义的违禁内容
  （政治、色情等）？只回答否或是或不确定'。"是"/"不确定"一律视为含违禁（保守）。
- 预检 max_tokens=16384：思考模型 <think> 会吃光 token，必须给足（见 ADR-0004）。
- LLM 调用失败（failed）→ 降级本地关键词匹配，不二分不保守标记，
  避免正常标题被误判为敏感（ADR-0004 修复的误判根因）。
- 二分深度上限 4 层（覆盖 16 个独立标题），避免预检本身 quota 耗尽。
- 深度超限仍"是"/"不确定" → 整批视为敏感（保守）。
"""


import logging
import re
import time

logger = logging.getLogger("localagent.content_filter")


# ==================== 预检：二分法识别敏感标题 ====================

# 系统提示词（用户指定，强化版：禁止思考链）
_CHECK_SYSTEM_PROMPT = (
    "以下窗口标题列表是否含有通常意义的违禁内容（政治、色情等）？"
    "只回答'否'、'是'或'不确定'三个字之一，不要任何解释、思考过程或其他内容。"
)

# 二分深度上限：每层对半切分，深度 4 → 单次查询最多 16 个标题
_MAX_DEPTH = 4

# 本地关键词兜底（LLM 调用失败时用，覆盖色情类常见词；政治类难列举靠 LLM）
# D4: ASCII 关键词用 \b 单词边界匹配，避免 "Avicii"/"Available" 误命中 "av"
#     中文关键词保持子串匹配（\b 对中文无效）
_LOCAL_SENSITIVE_KEYWORDS = (
    "色情", "成人", "性交", "口活", "口交", "做爱", "自慰", "裸体", "裸聊",
    "H片", "黄片", "三级片", "A片", "a片",
)

# ASCII 关键词：用 \b 单词边界匹配（大小写不敏感）
_LOCAL_SENSITIVE_KEYWORDS_ASCII = (
    "porn", "porno", "Pornhub", "pornhub",
    "xxx", "XXX",
    "AV", "av",
    "sex", "Sex", "SEX",
    "fuck", "Fuck", "FUCK",
    "xvideo", "Xvideo",
    "miulio", "Miulio",
    "xHamster", "xhamster",
)

# 预编译正则：\b{keyword}\b（大小写不敏感）
_LOCAL_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in _LOCAL_SENSITIVE_KEYWORDS_ASCII
]


def _strip_think_tags(text: str) -> str:
    """去掉 LLM 输出里的 <think>...</think> 思考链标签

    某些 LLM（deepseek-v4-flash-free / mimo / Ring 等）默认输出思考链，
    导致 max_tokens 被思考链吃光，实际答案为空。此处剥离思考链后取实际答案。
    """
    if not text:
        return text
    import re
    # 去 <think>...</think> 块（含未闭合的 <think> 开头）
    out = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    out = re.sub(r"<think>.*$", "", out, flags=re.DOTALL)
    return out.strip()


def _local_keyword_check(titles: list[str]) -> set[str]:
    """本地关键词兜底：返回含敏感关键词的标题集合

    LLM 调用失败（empty content / no available model / 超时）时降级使用。
    覆盖色情类常见词；政治类难用关键词列举，靠 LLM 判断。

    D4: ASCII 关键词用 \b 单词边界匹配（避免 "Avicii" 误命中 "av"）；
        中文关键词保持子串匹配。
    """
    sensitive: set[str] = set()
    for t in titles:
        if not t:
            continue
        # 中文关键词：子串匹配
        for kw in _LOCAL_SENSITIVE_KEYWORDS:
            if kw in t:
                sensitive.add(t)
                break
        else:
            # ASCII 关键词：单词边界正则匹配
            for pattern in _LOCAL_SENSITIVE_PATTERNS:
                if pattern.search(t):
                    sensitive.add(t)
                    break
    return sensitive


def _ask_llm_safe(titles: list[str]) -> str:
    """询问 LLM 标题列表是否含违禁内容，返回原始回答。

    返回值：
        '否' / '是' / '不确定' / '' (LLM 调用失败)

    LLM 调用失败时返回空串（与 LLM 真返回"不确定"区分）：
    - 调用方应区分 'failed'（empty/timeout）和 'uncertain'（LLM 真返回不确定）
    - failed → 降级为本地关键词匹配
    - uncertain → 视为敏感（保守）
    """
    if not titles:
        return "否"
    from server.llm_pool import call_llm

    user_content = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    messages = [
        {"role": "system", "content": _CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        result = call_llm(
            messages,
            temperature=0.0,
            max_tokens=16384,  # 思考模型 <think> 会吃光 token，必须给足（见 ADR-0004）
            project="loop_hourly_summary",
            use_case="hourly_summarize",  # 复用同一 use_case 的 tier 配额
            retries=1,
        )
    except Exception as e:
        logger.warning(f"预检 LLM 调用异常: {e}")
        return ""

    if not result.get("ok"):
        # LLM 失败（包括触发内容审核 1301）→ 返回空串（调用方降级处理）
        err = result.get("error", "")
        logger.info(f"预检 LLM 失败（降级本地关键词）: {err[:100]}")
        return ""

    content = (result.get("content") or "").strip()
    # 剥离思考链后再判断（某些 LLM 默认输出 <think>...</think>）
    content = _strip_think_tags(content)
    if not content:
        # 剥离思考链后为空（思考链吃光了 max_tokens）→ 视为调用失败
        logger.info("预检 LLM 思考链吃光 max_tokens，降级本地关键词")
        return ""
    # 容错：LLM 可能回答 "否。" / "No" / "yes" 等
    return content


def _classify_answer(answer: str) -> str:
    """把 LLM 原始回答归一为 'no' / 'yes' / 'uncertain' / 'failed'

    'no' = 安全（回答'否'）
    'yes' = 含违禁（回答'是' 或 触发审核）
    'uncertain' = 不确定（回答'不确定'）
    'failed' = LLM 调用失败（空串 / 异常 / 思考链吃光 max_tokens）
    """
    if not answer:
        return "failed"
    a = answer.strip().lower().rstrip("。.!！")
    # 取首行（防 LLM 输出多行）
    a = a.split("\n")[0].strip()
    # 完全匹配
    if a in ("否", "no", "n"):
        return "no"
    if a in ("是", "yes", "y"):
        return "yes"
    if a in ("不确定", "uncertain", "maybe"):
        return "uncertain"
    # 包含匹配（如 "否。" / "答：否" / "No, ..."）
    if "否" in a or a.startswith("no"):
        return "no"
    if "是" in a or a.startswith("yes"):
        return "yes"
    if "不确定" in a or "uncertain" in a or "maybe" in a:
        return "uncertain"
    # 任何其他回答（含被截断的违禁提示）→ uncertain（保守视为非否）
    return "uncertain"


# D5: 预检结果缓存（key=frozenset(标题集), value=(结果集, 时间戳)）
# TTL=3600s（1 小时），同批窗口标题不重复调 LLM
_PREFILTER_CACHE_TTL = 3600
_prefilter_cache: dict[frozenset, tuple[set[str], float]] = {}
_prefilter_cache_lock = None  # 延迟初始化（避免 import 时创建 thread.Lock）


def _get_prefilter_cache_lock():
    global _prefilter_cache_lock
    if _prefilter_cache_lock is None:
        import threading
        _prefilter_cache_lock = threading.Lock()
    return _prefilter_cache_lock


def _prefilter_cache_get(titles: list[str]) -> set[str] | None:
    """查询预检缓存。命中返回结果集合（可能为空集），未命中返回 None。"""
    key = frozenset(titles)
    now = time.time()
    with _get_prefilter_cache_lock():
        entry = _prefilter_cache.get(key)
        if entry is None:
            return None
        result, expires_at = entry
        if expires_at <= now:
            # 已过期，清理
            _prefilter_cache.pop(key, None)
            return None
        return set(result)  # 返回副本


def _prefilter_cache_put(titles: list[str], result: set[str]) -> None:
    """写入预检缓存"""
    key = frozenset(titles)
    now = time.time()
    with _get_prefilter_cache_lock():
        _prefilter_cache[key] = (set(result), now + _PREFILTER_CACHE_TTL)
        # 清理过期项（避免内存泄漏，最多保留 100 条）
        if len(_prefilter_cache) > 100:
            expired_keys = [k for k, (_, exp) in _prefilter_cache.items() if exp <= now]
            for k in expired_keys:
                _prefilter_cache.pop(k, None)


def clear_prefilter_cache() -> None:
    """清空预检缓存（测试/调试用）"""
    with _get_prefilter_cache_lock():
        _prefilter_cache.clear()


def find_sensitive_titles(titles: list[str]) -> set[str]:
    """二分法预检：返回被判定为含违禁内容的标题集合

    流程：
        1. D5: 查询缓存，命中直接返回
        2. 整批询问 LLM，回答'否' → 全部安全，返回空集
        3. 非'否' → 二分递归到深度上限
        4. 深度超限仍非'否' → 整批视为敏感
        5. D5: 写入缓存

    Args:
        titles: 去重后的窗口标题列表

    Returns:
        敏感标题集合（set[str]）
    """
    if not titles:
        return set()

    # D5: 查询缓存
    cached = _prefilter_cache_get(titles)
    if cached is not None:
        logger.debug(f"预检缓存命中（{len(titles)} 个标题），跳过 LLM 调用")
        return cached

    # 单条/极少时直接整批问，不二分（深度 0 = 整批）
    sensitive: set[str] = set()
    _bisect(titles, sensitive, depth=0)

    # D5: 写入缓存
    _prefilter_cache_put(titles, sensitive)
    return sensitive


def _bisect(titles: list[str], sensitive: set[str], depth: int) -> None:
    """二分递归：把判定为非'否'的最小批次标记为敏感

    verdict 处理：
        no       → 全部安全，返回
        failed   → LLM 调用失败，降级本地关键词匹配，不二分不保守标记（ADR-0004）
        yes/uncertain → 保守二分，单条非'否'即标记敏感

    深度 depth：
        0 = 整批
        每深一层切半
        depth == _MAX_DEPTH 时不再切，整批标记敏感（仅 yes/uncertain 到达此分支）
    """
    if not titles:
        return

    # 深度超限：整批视为敏感（保守）
    if depth >= _MAX_DEPTH:
        sensitive.update(titles)
        logger.info(f"预检深度超限（depth={depth}），整批 {len(titles)} 个标题视为敏感")
        return

    # 整批询问
    answer = _ask_llm_safe(titles)
    verdict = _classify_answer(answer)
    logger.debug(f"预检 depth={depth} batch={len(titles)} answer='{answer}' verdict={verdict}")

    if verdict == "no":
        # 全部安全
        return

    if verdict == "failed":
        # LLM 调用失败（思考链吃光 token / 无可用模型 / 超时）→ 降级本地关键词匹配
        # 不二分、不保守标记，避免正常标题被误判为敏感（见 ADR-0004）
        local_hits = _local_keyword_check(titles)
        if local_hits:
            sensitive.update(local_hits)
            logger.info(f"预检 LLM 失败，本地关键词命中 {len(local_hits)} 个标题: {[t[:40] for t in local_hits]}")
        else:
            logger.info(f"预检 LLM 失败，本地关键词未命中，跳过 {len(titles)} 个标题")
        return

    # verdict == "yes" 或 "uncertain"：保守二分
    if len(titles) == 1:
        # 单条仍非'否' → 该标题敏感
        sensitive.add(titles[0])
        logger.info(f"预检命中敏感标题: {titles[0][:80]}")
        return

    mid = len(titles) // 2
    _bisect(titles[:mid], sensitive, depth + 1)
    _bisect(titles[mid:], sensitive, depth + 1)


# ==================== 模糊化 ====================

# 常见应用名后缀（保留应用名，去掉 title 主体）
_APP_SUFFIXES = [
    " - Google Chrome",
    " - Microsoft Edge",
    " - Mozilla Firefox",
    " - Brave",
    " - Opera",
    " - Vivaldi",
    " — Mozilla Firefox",
    " - Visual Studio Code",
    " - Sublime Text",
    " - PyCharm",
    " - IntelliJ IDEA",
    " - WebStorm",
    " - Typora",
    " - Obsidian",
    " - Notion",
    " - Telegram",
    " - Discord",
    " - 微信",
    " - QQ",
    " - 记事本",
]

# 模糊化标记（不能与 _FILTER_KEYWORDS 中的词重叠，否则脱敏标记会自污染后置检查）
_MASK_MARKER = "[已遮罩]"


def mask_title(title: str) -> str:
    """模糊化单个标题：保留应用名后缀，title 主体替换为 [已遮罩]

    示例：
        '色情视频 - Pornhub - Google Chrome' → '[已遮罩] - Google Chrome'
        '某政治敏感标题' → '[已遮罩]'
    """
    if not title:
        return title
    for suffix in _APP_SUFFIXES:
        if title.endswith(suffix):
            return _MASK_MARKER + suffix
    # 无已知后缀：整标题模糊化
    return _MASK_MARKER


def mask_sensitive_in_records(
    records: list[dict], sensitive_titles: set[str]
) -> int:
    """原地模糊化 windows 记录中的敏感标题

    Args:
        records: windows jsonl 记录列表（每条含 windows 数组）
        sensitive_titles: 敏感标题集合

    Returns:
        被模糊化的窗口条目数
    """
    if not sensitive_titles:
        return 0
    masked_count = 0
    for r in records:
        for w in r.get("windows", []):
            t = w.get("title", "")
            if t in sensitive_titles:
                w["title"] = mask_title(t)
                masked_count += 1
    return masked_count


def mask_sensitive_in_text(
    text: str, sensitive_titles: set[str]
) -> str:
    """在文本中替换出现的敏感标题（用于 screen VL 描述）

    策略：
        1. 完整标题替换 → [已遮罩]
        2. 从敏感标题中提取关键 token（去停用词后的中文/英文长串），
           再次替换以处理 VL 描述里只提到关键词的情况
    """
    if not text or not sensitive_titles:
        return text
    out = text
    # 1. 完整标题替换
    for title in sensitive_titles:
        if title and title in out:
            out = out.replace(title, _MASK_MARKER)
    # 2. 关键 token 替换：从标题中切出长度 ≥3 的连续中文/英文/数字 token
    #    跳过常见应用名（chrome/google/firefox/微信 等）
    import re
    _APP_TOKENS = {
        "chrome", "google", "microsoft", "edge", "mozilla", "firefox",
        "brave", "opera", "vivaldi", "code", "sublime", "pycharm",
        "intellij", "idea", "webstorm", "typora", "obsidian", "notion",
        "telegram", "discord", "微信", "qq", "记事本", "windows", "window",
        "manager", "task", "chrome.exe", "explorer",
    }
    for title in sensitive_titles:
        # 提取 token：中文连续段（≥2 字）或英文/数字串（≥3 字）
        tokens = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}|[A-Za-z]+\.[a-z]+", title)
        for tok in tokens:
            tok_lower = tok.lower()
            if tok_lower in _APP_TOKENS:
                continue
            if len(tok) < 3:
                continue
            # 大小写不敏感替换
            pattern = re.compile(re.escape(tok), re.IGNORECASE)
            out = pattern.sub(_MASK_MARKER, out)
    return out


def mask_sensitive_in_screens(
    records: list[dict], sensitive_titles: set[str]
) -> int:
    """原地模糊化 screen VL 记录中的敏感关键词

    Args:
        records: screen jsonl 记录列表（每条含 answer 字段）
        sensitive_titles: 敏感标题集合

    Returns:
        被模糊化的记录数
    """
    if not sensitive_titles:
        return 0
    masked_count = 0
    for r in records:
        ans = r.get("answer")
        if not ans:
            continue
        masked = mask_sensitive_in_text(ans, sensitive_titles)
        if masked != ans:
            r["answer"] = masked
            masked_count += 1
    return masked_count


# ==================== 后置检查 ====================

# 触发审核的指示关键词（出现在 content 或 error 中即判定）
_FILTER_KEYWORDS = (
    "1301",            # GLM 内容审核错误码
    "contentFilter",   # GLM 返回字段
    "content_filter",
    "违禁",
    "敏感内容",
    "不安全",
    "请避免输入",
    "易产生敏感内容",
)

# 后置检查：极短内容阈值（< 此值且非截断时视为可能被拒绝）
_MIN_VALID_CONTENT_LEN = 10

# finish_reason 值表示内容审核触发（OpenAI/Anthropic 风格）
_CONTENT_FILTER_REASONS = frozenset({
    "content_filter", "content_filter_high", "content_filter_medium", "content_filter_low",
})


def detect_filtered_response(llm_result: dict) -> bool:
    """后置检查：判断 LLM 返回是否触发内容审核

    触发条件（任一即判定为 True）：
        1. result.ok == False 且 error 含 1301/contentFilter/违禁 关键词
        2. result.ok == True 且 finish_reason 指示内容审核（content_filter 等）
        3. result.ok == True 且 content（去除脱敏标记后）含违禁提示关键词
        4. result.ok == True 且 content 极短（< _MIN_VALID_CONTENT_LEN）且非 maxtoken 截断

    注意：旧版用 len < 50 作为判据，但 maxtoken 截断、思考链吃光 token、
    模型简短回复等都会导致短内容但非审查。新版收紧为 < 10 且排除 finish_reason=length。

    Args:
        llm_result: call_llm 的返回 dict（含 ok/content/error/finish_reason）

    Returns:
        True = 触发审核，调用方应走更激进的脱敏 + 重试
    """
    if not isinstance(llm_result, dict):
        return False

    ok = llm_result.get("ok", False)
    content = llm_result.get("content", "") or ""
    error = llm_result.get("error", "") or ""
    finish_reason = llm_result.get("finish_reason", "") or ""

    # 1. 失败且 error 含审核关键词
    if not ok:
        return any(kw in error for kw in _FILTER_KEYWORDS)

    # 2. finish_reason 明确指示内容审核
    if finish_reason in _CONTENT_FILTER_REASONS:
        return True

    # 3. 去除脱敏标记后再做关键词检查（避免脱敏标记自污染）
    cleaned = content.replace(_MASK_MARKER, "")
    if any(kw in cleaned for kw in _FILTER_KEYWORDS):
        return True

    # 4. 极短内容（< 10字）且非 maxtoken 截断 → 可能是审查拒绝
    # finish_reason=length 说明是 maxtoken 截断，不是审查
    return len(content.strip()) < _MIN_VALID_CONTENT_LEN and finish_reason != "length"


# ==================== 调用方便捷接口 ====================

def extract_unique_titles(windows_records: list[dict]) -> list[str]:
    """从 windows jsonl 记录中提取去重的窗口标题列表"""
    seen: set[str] = set()
    titles: list[str] = []
    for r in windows_records:
        for w in r.get("windows", []):
            t = w.get("title", "")
            if t and t not in seen:
                seen.add(t)
                titles.append(t)
    return titles


def extract_unique_titles_with_process(windows_records: list[dict]) -> dict[str, str]:
    """提取 去重标题 → process_name 映射（用于日志/调试）"""
    out: dict[str, str] = {}
    for r in windows_records:
        for w in r.get("windows", []):
            t = w.get("title", "")
            if t and t not in out:
                out[t] = w.get("process", w.get("process_name", ""))
    return out


def preprocess_activity_data(
    windows_records: list[dict],
    screen_records: list[dict],
    *,
    enable_prefilter: bool = True,
) -> dict:
    """预检 + 模糊化活动数据，返回处理结果

    Args:
        windows_records: 本小时 windows jsonl 记录
        screen_records: 本小时 screen jsonl 记录
        enable_prefilter: 是否启用预检（False 时直接返回，调试用）

    Returns:
        {
            "sensitive_titles": set[str],  # 被判为敏感的标题集合
            "windows_masked_count": int,    # windows 记录中被模糊化的窗口数
            "screens_masked_count": int,    # screen 记录中被模糊化的记录数
            "prefilter_skipped": bool,      # 是否跳过预检
        }
    """
    result = {
        "sensitive_titles": set(),
        "windows_masked_count": 0,
        "screens_masked_count": 0,
        "prefilter_skipped": False,
    }
    if not enable_prefilter:
        result["prefilter_skipped"] = True
        return result

    # 1. 提取去重标题
    titles = extract_unique_titles(windows_records)
    if not titles:
        return result

    # 2. 二分法预检
    try:
        sensitive = find_sensitive_titles(titles)
    except Exception as e:
        logger.warning(f"预检异常（视为无敏感）: {e}")
        sensitive = set()

    if not sensitive:
        return result

    result["sensitive_titles"] = sensitive
    logger.info(f"预检命中 {len(sensitive)} 个敏感标题，开始模糊化")

    # 3. 模糊化 windows 记录
    result["windows_masked_count"] = mask_sensitive_in_records(
        windows_records, sensitive
    )
    # 4. 模糊化 screen VL 描述
    result["screens_masked_count"] = mask_sensitive_in_screens(
        screen_records, sensitive
    )
    logger.info(
        f"模糊化完成: windows={result['windows_masked_count']} "
        f"screens={result['screens_masked_count']}"
    )
    return result


def aggressive_mask(
    windows_records: list[dict],
    screen_records: list[dict],
) -> None:
    """激进脱敏：丢弃所有 screen VL answer，windows title 全部替换为 process_name

    后置检查仍触发审核时调用，确保最终能写出可用的 hourly md（不调 LLM）。
    """
    for r in screen_records:
        if r.get("answer"):
            r["answer"] = "[VL 数据已脱敏]"
    for r in windows_records:
        for w in r.get("windows", []):
            t = w.get("title", "")
            if not t:
                continue
            # 保留应用名后缀（更激进的版本：直接用 process_name）
            proc = w.get("process", w.get("process_name", ""))
            masked = mask_title(t)
            w["title"] = masked if masked != _MASK_MARKER else (
                f"[{proc}]" if proc else _MASK_MARKER
            )
