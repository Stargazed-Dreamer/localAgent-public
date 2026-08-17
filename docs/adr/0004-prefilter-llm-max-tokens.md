# 预检 LLM max_tokens 必须给足——思考模型 <think> 标签会吃光 token 导致预检失效

内容审核预检（`content_filter.find_sensitive_titles`）用二分法把窗口标题发给 LLM 判断是否含违禁内容，只需回答"否/是/不确定"。原本设 `max_tokens=300` 认为"够用了"，但 `hourly_summarize` use_case 在 tier 3-5，范围内的模型（DeepSeek-V4-Flash、GLM-5.2、big-pickle、mimo 等）全是思考模型，默认输出 `<think>` 标签。300 token 被思考链吃光，剥离后答案为空，预检判定"失败"→ 二分到深度上限 → 保守标记整批为敏感 → 正常标题（Chrome、微信、TRAE 等）被误遮罩。决策：预检 `max_tokens` 设为 16384（与主调用一致），模型会在输出答案后自然 stop，多给的是保底空间不是消耗额度。

## Context

2026-07-29 03:02 日志显示 `HourlySummarize 2026-07-29 02:00 LLM 返回触发内容审核`，调查发现是级联误判：

1. 预检 `_ask_llm_safe` 用 `max_tokens=300` 调用 tier 3-5 思考模型
2. 思考模型输出 `<think>...</think>` 吃光 300 token（实测 big-pickle 用了 975 token，DeepSeek-V4-Flash 用了 494 token）
3. `_strip_think_tags` 剥离后内容为空 → `_ask_llm_safe` 返回空串 → `_classify_answer("")` 返回 `"failed"`
4. `"failed"` ≠ `"no"` → `_bisect` 继续二分到深度上限 → 整批标题保守标记为敏感
5. 正常标题被 `[已遮罩]` 替换 → 主调用也用思考模型，思考链长导致剥离后内容 < 10 字 → `detect_filtered_response` 条件 4 误判为审核触发

搜索整个 `server.log`，预检从未成功过——每次都在第 2 步失败，每次都在误标正常标题。

## Decision

**预检 `max_tokens` 设为 16384**（`_ask_llm_safe` 中 `max_tokens=300` → `max_tokens=16384`）。

理由：
- 思考模型的 `<think>` 可能消耗数千 token，给足 token 确保思考链结束后仍有空间输出 1-3 字答案
- 模型会在输出答案后自然 stop（`finish_reason=stop`），不会浪费——多给的 token 是保底空间，不是消耗额度
- 与主调用 `max_tokens=16384` 对齐，减少认知负担（"为什么预检和主调用 token 数差这么多？"）

**附加修复**：`_bisect` 中 `verdict == "failed"` 时降级为本地关键词匹配（`_local_keyword_check`），不二分、不保守标记。只有 LLM 真正回答"是"或"不确定"时才保守二分。

## Considered Options

- **A. `max_tokens=300`（原值）**：否决。思考链轻松吃光，预检形同虚设。实测多个思考模型在 300 token 内无法完成思考并输出答案。
- **B. `max_tokens=2048`**：否决。DeepSeek-V4-Flash 实测用了 975 token 思考，big-pickle 用了 975 token。2048 可能够用但余量不足，换更大的思考模型可能再次吃光。不值得为了省这点 token 留隐患。
- **C. `max_tokens=16384`（通过）**：与主调用对齐，确保万无一失。模型自己会停，不浪费。

## Consequences

- **正向**：预检能正常工作，不再因思考链吃光 token 而误标正常标题；`_bisect` 失败降级本地关键词后，即使 LLM 不可用也不会误判。
- **负向**：预检调用的 `max_tokens` 上限变大，但实际消耗不变（模型回答 1-3 字后 stop）。如果未来预检切换到非思考模型，可以适当降低 `max_tokens`，但不应低于 1024。
- **反复犯的错**：这不是第一次因"省 token"导致功能失效。记录此 ADR 防止未来开发者再次把预检 `max_tokens` 调小。
