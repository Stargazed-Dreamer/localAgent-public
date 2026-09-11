# 流式调用按 usage 采集器实现：只记 provider 权威值，缺失诚实标 provider_missing，绝不本地估算

LLM 池与入站网关在流式（SSE）调用中的 token 统计，定位为 **provider `usage` 字段的"采集器"，不是"token 计数器"**：能拿到 provider 回传的权威 usage 就记（`source=provider`），拿不到（部分代理在 SSE 里不回 usage）就记 0 并标 `source=provider_missing`，**不做任何本地 token 估算**，只保留与分词无关的精确字符数（`prompt_chars`/`completion_chars`）。此决策同时补齐了此前 `pool.stream()` 完全不给池侧记 token 的确定性缺陷。

## Status

accepted (2026-09-02)。是 [ADR-0031](0031-inbound-gateway-own-sqlite-stats.md) 决策 #4「流式 usage 缺失记 0，不估算」的**兑现与扩展到池侧**，并将该"不估算"从入站网关一层升格为跨池/网关的全局原则。

## Context

用户报「某专线连通测试可用，但调用统计 0 prompt」，并计划后续基于本数据计费 + "近 n 次请求备查"。排查（`data/inbound_calls.db` 实测）分两层：

1. **确定性 bug**：`pool.stream()` 的 `finally` 从不传 `tokens=`、从不调 `_record_usage` → 即使 provider 回了 usage，池的 per-key/per-model/project token 对流式流量恒 0（面板「Tokens」列空）。
2. **provider 行为（原判有出入，见文末 Correction）**：opencode/agnes 等聚合代理在非流式回 usage（实测同代理非流式 prompt 合计 834）。当时观察到"50 行流式全 0"，**初判为代理在 SSE 里不回 usage chunk**；后经查原始 SSE dump 推翻——opencode/zen 实际**有回** usage（放在 `finish_reason` 之后的独立 `choices:[]` 尾 chunk），是池解析器看到 `finish_reason` 就 break、漏读了尾 chunk 所致（属本端 bug，非代理限制）。见下文「## Correction」。

第 2 层"要不要让 0 变成有数"引出真权衡：常见做法是**本地估算 token**（CJK 启发式或 tiktoken 词表）。用户提供权威协议文档后拍板反对——token 的分词定义属服务端模型内部实现（模板开销 / 工具格式 / 多模态 / 版本差异会让本地估算偏离数十倍），**估算值一旦进账本就是错的**，且无法自证可信度。

## Decision

1. **采集器语义**：新增 `lib/llm_usage.py` 作为唯一归一处。`collect_usage`（OpenAI 风格 `prompt_tokens/completion_tokens/total_tokens`）与 `collect_usage_anthropic`（`input_tokens`/`output_tokens`）输出统一 dict：拿到任一非零 → `source=provider`（缺 `total` 时用 `p+c` 回填）；全零或缺失 → `source=provider_missing` 且 token 记 **0**（不臆造）。恒附带精确 `prompt_chars`/`completion_chars`（`text_from_messages` 拼 prompt 文本，含 tool_call arguments；completion 为累积输出文本）。
2. **池侧流式记账补齐**：`pool.stream()` 在循环里捕获 `type=="usage"` 事件，`finally` 成功时把其 `total` 经 `tokens=` 回填 `_release`、并调 `_record_usage(project, ...)`，与非流式 `call()` 对齐。OpenAI 与 Anthropic 两条原生流路径共用此 `finally`。
3. **Anthropic 升级为原生 SSE**：从降级伪流式（阻塞 `call()` + 一次性 yield）改为原生解析 `event-stream`：`message_start.input_tokens` + `message_delta.output_tokens`（**累计值，两者各取非零**，覆盖"message_start 先报 0、真值在 message_delta"场景）。与非流式共用 `_build_anthropic_messages`。
4. **入站账本携带来源**：`inbound_calls` 加列 `prompt_chars`/`completion_chars`/`usage_source`；流式读池的 usage 事件，非流式由网关现调 `collect_usage`。使"0 token"可区分「真没用量」与「provider 没回传」。
5. **计费只吃 `source=provider`**：后续计费对 `provider_missing` 行按"用量未知"处理（可回落到字符数做体积近似），**不得**把估算当账。

## Considered Options

1. **本地 token 估算（CJK 启发式 ±15% 或 tiktoken）**——被拒：分词定义在服务端，估算偏离可达数十倍且不可证；进账本即错。这是"计数器"思路，与"采集器"定位相悖。
2. **要求聚合代理必须回 usage，否则视为故障**——被拒：provider 行为不可控，代理不回 usage 是其能力缺失而非本端 bug；应如实记录而非误报或臆造。
3. **维持 `pool.stream()` 不记池统计，只在网关侧记**（ADR-0031 原边界）——被拒：池面板 Token 列对流式恒 0 是确定性缺陷，与是否复用网关账本无关；补记不改变「网关统计以自身 SQLite 为准、不复用池数字」的 ADR-0031 结论（两套并存，各自自洽）。

## Consequences

**正面**：token 数字要么权威要么显式"未知"，无静默失真；池面板流式 Token 不再恒 0；计费有可信度分层；未来读者不会误把 `provider_missing` 的 0 当"没消耗"。

**负面**：聚合代理流式流量的 token 仍大面积是 0（诚实的"未知"，非缺陷）——面板需展示 `usage_source` 以免误读。池统计与网关统计并存（承 ADR-0031），两套数字。

**回退成本**：一旦有消费方（计费/面板/脚本）依赖 `usage_source`/字符列语义、或据此出账，改回"估算填充"就会污染既有账本一致性——故本决策 hard to reverse。

## Correction (2026-09-02)：Stream 侧"代理不回 usage"判断被推翻，真因是池解析器提前 break

本 ADR 的**核心决策不变且仍成立**：流式 token 只做 provider `usage` 采集器、缺失诚实标 `provider_missing`、绝不本地估算。被推翻的是其中一个**事实前提**——"聚合代理（含 opencode/zen）在 SSE 里不回 usage chunk"。

复核方式：用真实 key 直连 `/zen/go/v1`（qwen3.8-flash）、逐行 dump 原始 SSE，测三种请求写法（`stream_options.include_usage` / 顶层 `include_usage` / 裸流）均拿到尾部 chunk `{"choices":[],"usage":{prompt,completion,total,completion_tokens_details.reasoning_tokens}}`，随后才是 `[DONE]`。**代理本就回 usage。**

真因（本端 bug）：`_stream_openai_sse` 旧逻辑在**看到 `finish_reason` 的当刻**就 `collect_usage` + `yield done` + `break`，而 OpenAI 规范的 usage 恰在 `finish_reason` **之后**的独立空-choices 尾 chunk——break 使其永远读不到 → `provider_usage` 恒 `None` → 误标 `provider_missing`、token 记 0。

**踩过的弯路（记此以防重犯）**：基于"代理不回 usage"这一错误前提，一度在入站网关层做过一个"缓冲伪流式"开关（`[inbound_gateway].buffered_stream_for_usage`）——客户端仍发 `stream:true`、仍收标准 SSE，但网关内部改调非流式 `pool.call()` 拿权威 usage 再切片回吐，用牺牲真流式 TTFT 换取准确 token（该路径 `usage_source` 恒为 `provider`）。它绕过一个**并不存在的代理限制**。真因定位后，该开关连同其配置项与专项测试**已全部移除**——本 ADR 不留一个已删特性作为在案方案。教训：**下"provider 做不到 X"的结论前必须先 raw dump 实测拿证据**，否则会为一个本可修复的本端 bug 建起昂贵的规避路径；"让池读到它本就收到的数据"是修 bug，不是造数，不违背采集器"不估算"原则。

修复：`_stream_openai_sse` 改为不在 `finish_reason` break、记录后继续读到 `[DONE]`/流自然结束，循环后统一 flush tool_calls → usage → done（镜像一直正确的 `_stream_anthropic_sse` 收尾）；usage 与 finish_reason 同 chunk 时走快速路径立即 break。真实端点端到端实测（重启后端）：真流式 `usage_source=provider total=395`，同流 `reasoning_chars=529`（首思维 token 1.31s）——**逐字直播思维与精确 provider token 同时成立**。

**配套（补齐当初缓冲方案想解决的"客户端要准确 token"这一真实诉求）**：入站网关真流式路径现在把 provider 的 usage 尾 chunk 原样转发给外部 OpenAI 兼容客户端（`_sse_usage_chunk`），并透传池返回的真实 `finish_reason`（此前硬写成 `"stop"`，无视 `tool_calls`/`length`）——让 provider 本已发出的数据抵达客户端，而非绕道非流式重放。池未发 usage 事件时不伪造尾 chunk、`provider_missing`（全 0）如实转发 0。LocalAgent 自带对话面板走内部 `/llm/pool/stream`（typed usage 事件），不经入站网关，不受影响。

影响面：修正所有"usage 在尾部独立 chunk"型 provider 的流式记账（远不止该代理）。回归测试见 `test_pool_stream_usage_collector.py`（`..._in_trailing_empty_choices_chunk` / `..._finish_without_any_usage_marks_missing`）与 `test_inbound_gateway.py::TestChatStream`（网关转发 usage 尾 chunk / 透传真实 finish_reason / provider_missing 转发 0 / 无 usage 不伪造）。

## References

- 实现：[lib/llm_usage.py](file:///<project_root>/lib/llm_usage.py)、[server/llm_pool/pool.py](file:///<project_root>/server/llm_pool/pool.py)（`stream()` finally 记账 + `_stream_openai_sse` 续读到尾部 usage chunk + `_stream_anthropic_sse` 原生流）、[server/inbound_gateway/call_log.py](file:///<project_root>/server/inbound_gateway/call_log.py) + [router.py](file:///<project_root>/server/inbound_gateway/router.py)（三列采集器回填 + `_stream_sse`/`_sse_usage_chunk` 向客户端转发 usage 尾 chunk 与真实 finish_reason）
- 测试：[tests/server_endpoints/test_pool_stream_usage_collector.py](file:///<project_root>/tests/server_endpoints/test_pool_stream_usage_collector.py)（Anthropic 原生 / OpenAI 尾 chunk usage / provider_missing）、[tests/server_endpoints/test_inbound_gateway.py](file:///<project_root>/tests/server_endpoints/test_inbound_gateway.py)（`TestChatStream`：网关转发 usage 尾 chunk / 透传真实 finish_reason / provider_missing 转发 0 / 无 usage 不伪造）
- 上游文档：用户提供的大模型 API usage 字段协议说明（采集器定位的直接依据）
- 关联：[ADR-0031](0031-inbound-gateway-own-sqlite-stats.md) 决策 #4（本决策是其兑现）、设计记录 `temp/sdd/stream-token-accounting/spec.md`
- CHANGELOG `[Unreleased]` Added/Changed/Fixed「流式 token 记账补齐」相关条目
