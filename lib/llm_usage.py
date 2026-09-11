"""LLM usage 采集辅助（转发器定位：采集 provider 权威 usage，而非自造 token 计数）。

原则（2026-09-02 与用户对齐）：token 的分词定义属服务端模型内部实现，客户端无法
准确复现（模板开销 / 工具格式 / 多模态 / 版本差异都会让本地估算偏离数十倍）。
因此本模块只做两件事：

1. `text_from_messages` / 字符数统计：记录**精确字符数**（prompt/completion 原始字符，
   零成本、与分词无关），作为"用量体积"的可靠下界与离线分析用。
2. `collect_usage*`：从 provider 返回的 usage 归一出统一 dict，并区分来源：
   - `provider`         —— 拿到了非零 usage（权威、可计费）
   - `provider_missing` —— provider 没给 / 只给了全零 usage chunk（兼容代理常见），
                           token 记 0，**不臆造**，计费按"未知"处理。

OpenAI 与 Anthropic 字段名不同，各自一个入口，统一输出 OpenAI 风格键
（prompt_tokens/completion_tokens/total_tokens）+ prompt_chars/completion_chars/source。
"""

from __future__ import annotations

SOURCE_PROVIDER = "provider"
SOURCE_MISSING = "provider_missing"


def text_from_messages(messages: list[dict]) -> str:
    """把 OpenAI messages 的文本部分拼成一段（多模态只取 text 分片；计入
    tool_calls 的 arguments 文本）。仅用于精确字符统计，不用于估算 token。"""
    parts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):  # 多模态分片
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                parts.append(str(fn.get("arguments") or ""))
    return "\n".join(parts)


def _result(p: int, c: int, t: int, prompt_text: str, completion_text: str,
            source: str, cached: int | None = None,
            reasoning: int | None = None, cache_creation: int | None = None) -> dict:
    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": t,
        "prompt_chars": len(prompt_text),
        "completion_chars": len(completion_text),
        "source": source,
        # 上游缓存命中 token（prompt cache read）。None = provider 未上报（不臆造 0，
        # 便于区分"没缓存机制/没命中"与"没上报"两种情况）。
        "cached_tokens": cached,
        # 思考 token（OpenAI completion_tokens_details.reasoning_tokens，o 系/R1 等）。
        # Anthropic 无对应字段（thinking 已计入 output_tokens）→ None。
        "reasoning_tokens": reasoning,
        # Anthropic 缓存写入 token（cache_creation_input_tokens，计费 1.25x/2x）。
        # OpenAI 无对应字段 → None。
        "cache_creation_tokens": cache_creation,
    }


def _extract_cached(provider_usage: dict) -> int | None:
    """从 usage 提取缓存命中 token，两种输入形态兼容：
    - OpenAI 原生 usage：prompt_tokens_details.cached_tokens
    - 池已归一的 dict（anthropic 非流式转换产物）：顶层 cached_tokens
    """
    cached = provider_usage.get("cached_tokens")
    if cached is not None:
        return int(cached)
    det = provider_usage.get("prompt_tokens_details") or {}
    if det.get("cached_tokens") is not None:
        return int(det["cached_tokens"])
    return None


def _extract_reasoning(provider_usage: dict) -> int | None:
    """思考 token：OpenAI completion_tokens_details.reasoning_tokens。
    池已归一的 dict 顶层 reasoning_tokens 同样兼容（与 _extract_cached 同模式）。"""
    reasoning = provider_usage.get("reasoning_tokens")
    if reasoning is not None:
        return int(reasoning)
    det = provider_usage.get("completion_tokens_details") or {}
    if det.get("reasoning_tokens") is not None:
        return int(det["reasoning_tokens"])
    return None


def collect_usage(provider_usage: dict | None, prompt_text: str,
                  completion_text: str) -> dict:
    """OpenAI 风格 usage 归一。全零或缺失视为 provider_missing（记 0，不估算）。"""
    pu = provider_usage or {}
    p = int(pu.get("prompt_tokens") or 0)
    c = int(pu.get("completion_tokens") or 0)
    t = int(pu.get("total_tokens") or 0)
    if p or c or t:
        if not t:
            t = p + c
        return _result(p, c, t, prompt_text, completion_text, SOURCE_PROVIDER,
                       _extract_cached(pu), _extract_reasoning(pu))
    return _result(0, 0, 0, prompt_text, completion_text, SOURCE_MISSING)


def collect_usage_anthropic(input_tokens: int | None, output_tokens: int | None,
                            prompt_text: str, completion_text: str,
                            cached_tokens: int | None = None,
                            cache_creation_tokens: int | None = None) -> dict:
    """Anthropic 风格 usage（input_tokens/output_tokens）归一为 OpenAI 键。
    两者皆 0/None 视为 provider_missing。cached_tokens 对应 cache_read_input_tokens；
    cache_creation_tokens 对应 cache_creation_input_tokens（缓存写入，计费 1.25x/2x）。"""
    p = int(input_tokens or 0)
    c = int(output_tokens or 0)
    if p or c:
        return _result(p, c, p + c, prompt_text, completion_text, SOURCE_PROVIDER,
                       cached_tokens, cache_creation=cache_creation_tokens)
    return _result(0, 0, 0, prompt_text, completion_text, SOURCE_MISSING)
