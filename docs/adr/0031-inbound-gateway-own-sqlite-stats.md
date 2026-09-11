# 入站网关统计自持：网关 SQLite 是唯一数据源，不复用池统计

入站网关（`server/inbound_gateway/`）的调用日志与全部聚合统计（per-key/per-upstream tokens、TTFT p50/p95、成功率）由网关自己的 SQLite 表（`data/inbound_calls.db`）GROUP BY 算出，**不复用** `LLMPool` 的 `project_stats` / per-key 统计；TTFT 在网关层计时（迭代 `pool.stream()` 生成器，记首个 `text_delta` 到达时刻）。`server/llm_pool/` 整棵子树对本功能零改动。

## Status

accepted (2026-09-01)，**2026-09-02 两次修订**（见文末「Amendment」与「Amendment 续」）：① 决策 #4 的流式 `upstream_key` 缺失已知限制已解除、决策 #5 的"池子树只读"边界以纯增量方式有限放宽；② 同日续修——决策 #4「记 0 不估算」被 [ADR-0033](0033-stream-usage-collector-not-token-counter.md) 升格为跨池/网关全局原则，`pool.stream()` 补上池侧 token 记账，Anthropic 由降级伪流式升级为原生 SSE（Amendment #1 提到的 `_stream_anthropic_fallback` 已被 `_stream_anthropic_sse` 取代）。原决策文字保留为历史记录。

## Context

入站网关让外部 harness（Cline / Cherry Studio 等）把 `base_url` 指到 `http://127.0.0.1:8766/v1`、配一把本地 `sk-la-` key，像调 OpenAI 一样用整个模型池。「入站管理」面板需要四页签数据：Key 分发、调用日志、模型映射、Token 统计（含 TTFT p50/p95 与成功率）。

初版 spec 设想复用池侧统计：面板「按连接聚合」复用 `pool.project_stats`（per-project 统计），TTFT 通过给 `CallRecord` 加 `ttft_ms` 字段、在池内计时实现。开工前代码核验（2026-09-01）推翻了这个假设：

- `pool.py` 的 `_record_usage()` 只在 `call()` 和 `call_simple()` 内被调用，`_record_call()` 同理；**`stream()` 内部一处都不调**——流式调用在池侧零统计，既不进 `recent_calls` 也不进 `project_stats`。
- 而 harness 以流式为主（Cline / Cherry Studio 默认 stream）。沿用原假设，「按连接聚合」表在流式调用下会**全空**。
- `project_stats` 仅 4 字段（prompt/completion/total/calls），没有成功率与延迟分位数，满足不了面板需求。
- 池侧已有的 per-key + per-model token 统计、p50/p95/p99 延迟，同样只对 `call()` 生效。

## Decision

1. **网关自己的 SQLite 表 `inbound_calls` 是唯一统计真源**：每次调用（流式与非流式）的入站 key / 请求模型 / 解析后模型 / 上游 key / prompt·completion·total tokens / TTFT / 总耗时 / 状态码异步落库（后台队列 `inbound-call-log`，fail-open），30 天滚动清理。
2. **面板全部聚合数字由网关表 GROUP BY 算出**（per-key / per-upstream 的 requests、tokens、TTFT p50/p95、成功率），分位数计算自实现（约 30 行）。不复用 `pool.project_stats`、`pool.get_stats()` 的任何字段。
3. **TTFT 在网关层计时**：`_stream_sse` 迭代 `pool.stream()` 生成器，首个 `text_delta` 到达时刻减请求开始时刻。不改 `CallRecord`（`server/llm_pool/types.py`），不进池子树。
4. **流式 usage 缺失记 0，不估算**；已知限制：`pool.stream()` 事件不含上游 key 名，流式日志 `upstream_key` 记空串（面板显示 "—"）。
5. **`server/llm_pool/` 整棵子树只读**，作为 spec Bounds 的硬约束（后在 T6 验收中因两个与本功能无关的存量池 bug 破例，见 spec 勘误）。

## Considered Options

1. **复用 `pool.project_stats` + per-key 统计，给 `CallRecord` 加 `ttft_ms` 在池内计时**（初版 spec 方案）——被拒：`stream()` 不进池统计，面板核心数字对流式调用全空；且需改池子树，引入"池行为回归"风险面。
2. **修 `pool.stream()` 让它也进池统计，再复用**——被拒：改动面大（流式 usage 时序、失败路径、并发），违反本功能"池零改动"的安全边界；池统计字段（4 字段）也不够面板用，仍要补成功率/分位数。
3. **双数据源（池统计 + 网关日志），面板各取所需**——被拒：口径分裂（非流式看池、流式看网关），同一指标两个数字来源，排查时无所适从。单一数据源口径统一。

## Consequences

**正面**：
- `server/llm_pool/` 零改动，池行为零回归风险消除（本功能最大安全边界）。
- 统计口径统一：流式与非流式同表同算法，面板数字自洽。
- 网关对日志有完全控制权（筛选、分页、保留期都自己定）。

**负面**：
- 池统计与网关统计并存，两套数字（池侧只覆盖非流式），未来读者需要本 ADR 才明白为何不合并。
- 分位数自实现；`upstream_key` 在流式下缺失（池事件不含），面板显示 "—"。

**回退路径**：若未来 `pool.stream()` 补上了池侧统计，可以评估合并，但需同时迁移面板四页签的全部聚合查询与既有 `inbound_calls` 历史——收益不明，默认不动。

## References

- 代码核验记录：`temp/sdd/inbound-gateway/design-decisions.md`（2026-09-01 修正表 A/B/C）
- spec：`temp/sdd/inbound-gateway/spec.md`（Implementation Decisions + Bounds 勘误）
- 实现：[server/inbound_gateway/call_log.py](file:///<project_root>/server/inbound_gateway/call_log.py)（落库 + 聚合查询）、[server/inbound_gateway/router.py](file:///<project_root>/server/inbound_gateway/router.py)（`_stream_sse` TTFT 计时）
- CHANGELOG `[Unreleased]` Added「入站网关（Inbound Gateway）· 类 newapi 本地中转」条

## Amendment (2026-09-02)：流式兜底透明化

入站网关交付后暴露两个可观测性问题，促成一次有意识的边界调整。**决策 #4 与 #5 的原始文字保留为历史，实际现状以本节为准。**

**背景**：网关按 `use_case="inbound_gateway"` 调池，池在目标模型未落在任何被授权 key 上时会**静默换 key/换模型兜底**（model 是软偏好）。这是刻意保留的透明兜底（用户明确要求"别禁用换模型，只要能看出来就行"），但当时有两点看不见：① 决策 #4 让流式 `upstream_key` 恒为空串，② 无任何字段标记"这次被兜底换了模型"。

**改动（均为纯增量，不改池调度/选 key 逻辑）**：

1. **解除决策 #4 的已知限制**：`server/llm_pool/pool.py` 的 `stream()`（OpenAI 分支）与 `_stream_anthropic_fallback` 现对每个事件 `setdefault("key_name", ...)` / `setdefault("model", ...)`，把上游真实身份透出。网关 `_stream_sse` 据此从 `usage`/`done` 事件回填 `upstream_key`/`model_used`（含 error 路径），不再记空串。
2. **决策 #5 的"池子树只读"边界以纯增量方式有限放宽**：这是继 T6 验收期间两个存量池 bug 之后，本功能第三次经用户批准触碰 `server/llm_pool/`。放宽仅限"让事件多带两个已有内部字段"，无任何调度行为改动，池 stream/tier 回归测试 15 项仍全过。**本 ADR 决策 #1/#2/#3 的"聚合走网关自己的 SQLite、不复用池统计"核心结论不变**——放宽只是让网关日志能记录真实上游身份，不引入对池统计的复用。
3. **新增可观测字段**：`inbound_calls` 表增 `resolved_model`（别名解析后的目标模型）与 `substituted`（实际模型 ≠ 目标模型则为 1，带 `ALTER TABLE` 迁移，存量行默认 0）；聚合统计与面板概览新增"兜底替换"计数；面板"请求模型 → 实际模型"被换时追加 `↺兜底` + tooltip。

**取舍**：曾考虑"禁用兜底换模型、目标不可用直接 404"——被拒，用户要保留透明兜底路由的可用性，只补可观测性。故方向是"照亮兜底"而非"消除兜底"。

**修订后仍成立的边界**：热重载逻辑独立在 `server/core/keys_watch.py`（见 [ADR-0032](0032-keys-json-hot-reload-guard.md)），未进池子树；本轮唯一的池内改动就是上述第 1 点的事件字段透出。

## Amendment 续 (2026-09-02)：流式 usage 采集器（承 [ADR-0033](0033-stream-usage-collector-not-token-counter.md)）

同日交付"流式调用 token 记账补齐"（[ADR-0033](0033-stream-usage-collector-not-token-counter.md)），对本 ADR 有三点后续影响，原决策文字仍保留为历史：

1. **决策 #4 的「流式 usage 缺失记 0，不估算」被升格为全局原则**：不再是入站网关一层的权宜，而是 `lib/llm_usage.py` 统一采集器语义——池侧与网关侧都只采 provider 权威 usage，缺失即 `source=provider_missing` 记 0 + 保留精确字符数，绝不本地估算 token。
2. **`pool.stream()` 现已补上池侧 token 记账**（此前"零统计"是本 ADR「不复用池统计」的语境之一）。这不改变本 ADR 核心结论：网关聚合统计仍以自身 SQLite 为准、不复用池数字——两套统计并存、各自自洽。上文「回退路径」中"若未来 `pool.stream()` 补上了池侧统计，可以评估合并"的假设已被部分兑现，但**合并的结论仍是不做**（口径统一诉求由采集器语义满足，无需合并数据源）。
3. **Amendment #1 提到的 `_stream_anthropic_fallback` 已被取代**：Anthropic 路径由降级伪流式（阻塞 `call()` + 一次性 yield）升级为原生 `event-stream` 解析 `_stream_anthropic_sse`（`message_start`/`message_delta` 采集 usage）。字段透出的语义不变，实现载体更名。
