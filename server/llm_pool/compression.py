"""LLM 池消息压缩模块（OmniRoute Phase 3.1 RTK + 3.2 Caveman 借鉴）

设计：
- mode: off | lite | standard | aggressive
  - off: 不压缩
  - lite: 仅对 code blocks / 工具输出应用 RTK 规则（去行号、合并 stack trace、截断中间）
  - standard: lite + Caveman prose 规则（去多余空白、合并标点、压缩常见短语）
  - aggressive: standard + 更激进 CJK 虚词过滤 + 更低 min_length 阈值
- Bloat Protection: 压缩后若更长则回退原文（单条和整体两层保护）
- Fail-Open: 任何异常返回原 messages，不阻塞主调用链
- min_length: 仅对估算 token 数 ≥ min_length 的 content 应用压缩（默认 2000）

参考：OmniRoute RTK 引擎（按内容类型过滤 + Bloat Protection）+ Caveman 规则包
（含中文文言输入包 + CJK 自动检测）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Token 估算：英文 ~4 char/token，CJK ~2 char/token（粗略近似）
_TOKEN_RATIO_LATIN = 4.0
_TOKEN_RATIO_CJK = 2.0

# CJK 字符检测（中日韩统一表意符号 + 平假名 + 片假名）
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]")

# 中文冗余虚词短语（aggressive 模式过滤）
_CJK_FILLER_PHRASES = [
    "事实上", "实际上", "总的来说", "总而言之", "综上所述",
    "在我看来", "我认为", "我觉得", "众所周知",
    "需要注意的是", "需要指出的是", "必须指出",
    "换句话说", "换言之", "也就是说",
]

# Code block 检测：```lang\n...\n```
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)\n```", re.DOTALL)
# 行号前缀：常见格式如 "  123: " / "123 | " / "123 > " / "123) "
_LINE_NUMBER_RE = re.compile(r"^\s*\d+[\s|:>\-)]+", re.MULTILINE)
# 重复 stack trace 行（如 Java/Kotlin/Python 的 "at xxx.yyy(zzz:NN)"）
_STACK_TRACE_RE = re.compile(r"^(\s*(?:at\s+|File\s+)[\w.$/]+\([^)]*\)\s*)$", re.MULTILINE)

# Latin 常见冗长短语（standard 模式压缩）
_LATIN_PHRASE_SUBS = [
    (re.compile(r"\bin order to\b", re.IGNORECASE), "to"),
    (re.compile(r"\bdue to the fact that\b", re.IGNORECASE), "because"),
    (re.compile(r"\bin spite of the fact that\b", re.IGNORECASE), "although"),
    (re.compile(r"\bwith regard to\b", re.IGNORECASE), "about"),
    (re.compile(r"\bfor the purpose of\b", re.IGNORECASE), "for"),
    (re.compile(r"\ba large number of\b", re.IGNORECASE), "many"),
    (re.compile(r"\bin the event that\b", re.IGNORECASE), "if"),
]

# 合法 mode 值
VALID_MODES = ("off", "lite", "standard", "aggressive")


@dataclass
class CompressionStats:
    """压缩统计信息（用于 /health 和监控面板）"""
    applied: bool = False
    mode: str = "off"
    original_chars: int = 0
    compressed_chars: int = 0
    rules_applied: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """压缩比 = compressed / original（< 1.0 表示有压缩）"""
        if self.original_chars == 0:
            return 1.0
        return self.compressed_chars / self.original_chars

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.compressed_chars)

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "mode": self.mode,
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "saved_chars": self.saved_chars,
            "ratio": round(self.ratio, 4),
            "rules_applied": list(self.rules_applied),
        }


def is_cjk(text: str) -> bool:
    """检测文本是否含 CJK 字符"""
    return bool(_CJK_PATTERN.search(text))


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（用于 min_length 阈值判断）

    英文 ~4 char/token，CJK ~2 char/token（粗略近似，不依赖 tokenizer）。
    """
    if not text:
        return 0
    cjk_count = len(_CJK_PATTERN.findall(text))
    other_count = len(text) - cjk_count
    return int(cjk_count / _TOKEN_RATIO_CJK + other_count / _TOKEN_RATIO_LATIN)


def _compress_code_block(code: str, aggressive: bool = False) -> tuple[str, list[str]]:
    """压缩单个 code block 内容

    规则：
    - 去行号前缀（"  123: " / "123 | " / "123 > " 等）
    - 合并连续重复的 stack trace 行（首尾各 1 行 + "  ... (N duplicate lines)"）
    - 超长（aggressive>30 / standard>50 行）截断中间保留头尾

    Args:
        code: code block 内部代码字符串
        aggressive: 是否启用更激进阈值

    Returns:
        (compressed_code, rules_applied)
    """
    rules: list[str] = []
    result = code

    # 1. 去行号前缀
    new_result = _LINE_NUMBER_RE.sub("", result)
    if len(new_result) < len(result):
        rules.append("strip_line_numbers")
        result = new_result

    # 2. 合并连续重复的 stack trace 行
    lines = result.split("\n")
    deduped: list[str] = []
    prev_stack_normalized: str | None = None
    stack_dup_count = 0

    def _flush_dups() -> None:
        """把当前累计的重复 stack 行合并到 deduped 末尾，并记录规则"""
        nonlocal stack_dup_count
        if stack_dup_count > 0 and deduped:
            deduped[-1] = deduped[-1] + f"  ... ({stack_dup_count} duplicate stack lines)"
            rules.append(f"dedup_stack_trace({stack_dup_count})")
            stack_dup_count = 0

    for line in lines:
        m = _STACK_TRACE_RE.match(line)
        if m:
            normalized = m.group(1).strip()
            if prev_stack_normalized == normalized:
                stack_dup_count += 1
                continue
            _flush_dups()
            prev_stack_normalized = normalized
            deduped.append(line)
        else:
            _flush_dups()
            prev_stack_normalized = None
            deduped.append(line)
    _flush_dups()  # 处理末尾的重复段
    result = "\n".join(deduped)

    # 3. 超长截断中间保留头尾
    max_lines = 30 if aggressive else 50
    lines = result.split("\n")
    if len(lines) > max_lines:
        head_n = max_lines // 2
        tail_n = max_lines - head_n
        head = lines[:head_n]
        tail = lines[-tail_n:] if tail_n > 0 else []
        omitted = len(lines) - max_lines
        result = "\n".join(head + [f"  ... ({omitted} lines omitted) ..."] + tail)
        rules.append(f"truncate_middle({omitted})")

    return result, rules


def _compress_prose_latin(text: str) -> tuple[str, list[str]]:
    """拉丁字符文本压缩（Caveman 规则）

    - 去多余空白（连续 2+ 空白/制表符 → 1 空白）
    - 合并连续相同标点（!!!→!, ???→?, 。。。→.）
    - 压缩常见冗长短语（"in order to" → "to" 等）
    """
    rules: list[str] = []
    result = text

    # 1. 连续空白 → 单空白
    new_result = re.sub(r"[ \t]{2,}", " ", result)
    if len(new_result) < len(result):
        rules.append("collapse_whitespace")
        result = new_result

    # 2. 连续相同标点 → 单个（拉丁标点）
    new_result = re.sub(r"([!?.])\1{1,}", r"\1", result)
    if len(new_result) < len(result):
        rules.append("merge_punctuation")
        result = new_result

    # 3. 常见短语替换
    phrase_count = 0
    for pat, repl in _LATIN_PHRASE_SUBS:
        new_result = pat.sub(repl, result)
        if len(new_result) < len(result):
            phrase_count += 1
            result = new_result
    if phrase_count > 0:
        rules.append(f"phrase_compress({phrase_count})")

    return result, rules


def _compress_prose_cjk(text: str, aggressive: bool = False) -> tuple[str, list[str]]:
    """CJK 文本压缩（Caveman CJK 规则）

    - 去多余空白（全角空格 + 半角连续 → 单空白）
    - aggressive 模式：过滤冗余虚词短语（"事实上" / "总的来说" 等）
    """
    rules: list[str] = []
    result = text

    # 1. 连续全角/半角空白 → 单个半角
    new_result = re.sub(r"[ \t\u3000]{2,}", " ", result)
    if len(new_result) < len(result):
        rules.append("collapse_cjk_whitespace")
        result = new_result

    # 2. aggressive: 过滤冗余虚词短语
    if aggressive:
        filler_count = 0
        for phrase in _CJK_FILLER_PHRASES:
            if phrase in result:
                result = result.replace(phrase, "")
                filler_count += 1
        if filler_count > 0:
            rules.append(f"cjk_filler_filter({filler_count})")

    return result, rules


def _compress_content(content: str, mode: str, aggressive: bool) -> tuple[str, list[str]]:
    """压缩单个 content 字符串

    步骤：
    1. 识别 code blocks（``` fenced），对每个 block 应用 RTK 规则
    2. 对非 code 部分应用 Caveman prose 规则（standard / aggressive 才启用）

    Bloat Protection 在 compress_messages 顶层做单条+整体两层检查。
    """
    rules: list[str] = []

    # 1. Code blocks: RTK 规则（lite/standard/aggressive 都启用）
    if mode in ("lite", "standard", "aggressive"):
        def _replace_block(m: re.Match) -> str:
            lang = m.group(1)
            code = m.group(2)
            new_code, code_rules = _compress_code_block(code, aggressive=aggressive)
            rules.extend(code_rules)
            return f"```{lang}\n{new_code}\n```"
        new_content = _CODE_BLOCK_RE.sub(_replace_block, content)
        if new_content != content:
            content = new_content

    # 2. Prose: Caveman 规则（仅 standard / aggressive 启用，lite 不动 prose）
    if mode in ("standard", "aggressive"):
        if is_cjk(content):
            new_content, prose_rules = _compress_prose_cjk(content, aggressive=aggressive)
        else:
            new_content, prose_rules = _compress_prose_latin(text=content)
        if prose_rules:
            content = new_content
            rules.extend(prose_rules)

    return content, rules


def compress_messages(
    messages: list[dict],
    mode: str = "off",
    min_length: int = 2000,
) -> tuple[list[dict], CompressionStats]:
    """压缩 messages 列表

    Args:
        messages: OpenAI 格式 messages 列表 [{"role", "content"}, ...]
        mode: off | lite | standard | aggressive
        min_length: 仅对估算 token 数 ≥ min_length 的 content 应用压缩

    Returns:
        (compressed_messages, stats)
        - Bloat Protection: 单条压缩后变长则回退；整体变长则回退全部
        - Fail-Open: 任何异常返回原 messages + 空 stats
    """
    stats = CompressionStats(mode=mode)

    if mode == "off":
        return messages, stats

    if mode not in VALID_MODES:
        # 非法 mode 当作 off 处理（Fail-Open）
        stats.mode = "off"
        return messages, stats

    try:
        aggressive = (mode == "aggressive")
        new_messages: list[dict] = []
        total_orig = 0
        total_compressed = 0
        any_compressed = False

        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, str):
                # 多模态消息（content 是 list）不动；原样保留
                new_messages.append(msg)
                continue

            total_orig += len(content)
            est_tokens = estimate_tokens(content)

            if est_tokens >= min_length:
                new_content, rules = _compress_content(content, mode, aggressive)
                if rules:
                    stats.rules_applied.extend(rules)
                # Bloat Protection: 单条若变长（或相等）则回退原文
                if len(new_content) >= len(content):
                    new_content = content
                else:
                    any_compressed = True
                total_compressed += len(new_content)
            else:
                # 未达 min_length 阈值，不动
                new_content = content
                total_compressed += len(content)

            new_msg = dict(msg)
            new_msg["content"] = new_content
            new_messages.append(new_msg)

        # 全局 Bloat Protection：整体未压缩则回退原 messages
        if not any_compressed or total_compressed >= total_orig:
            return messages, stats

        stats.applied = True
        stats.original_chars = total_orig
        stats.compressed_chars = total_compressed
        return new_messages, stats
    except Exception:
        # Fail-Open: 任何异常返回原 messages
        stats.applied = False
        stats.rules_applied = []
        return messages, stats
