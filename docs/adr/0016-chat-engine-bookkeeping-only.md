# chat 引擎只记账不控制（删 cost 字段 + 不引入 token 预算控制）

chat 引擎只记账不控制：删除 `RunnerConfig.max_budget_usd` / `RunOutcome.total_cost_usd` / `LLMResponse.cost_usd` 字段，新增 `RunOutcome.total_tokens: int` 累计 `usage.total_tokens`。不引入 `max_total_tokens` 预算控制。`wall_clock_budget_secs`（默认 0 不限制）是唯一的预算控制。理由：缓存比例不明本地算 cost 不准；server 端不返回 cost_usd。

## Context

v6-lite chat 引擎（`client/core/agent/`）原设计含三层预算控制：

1. `RunnerConfig.max_budget_usd`（每次 run 的美元预算上限）
2. `RunOutcome.total_cost_usd`（runner 主循环累计 `LLMResponse.cost_usd`）
3. `RunnerConfig.wall_clock_budget_secs`（wall clock 超时）

实际跑起来发现 cost 控制不可行：

1. **缓存比例不明**：DeepSeek / OpenAI 等提供商的 cached input token 折扣不同（DeepSeek 缓存命中 0.1x，OpenAI 缓存 0.5x），但 server 端 `/llm/pool/stream` 的 usage 事件不返回 cache hit 比例，client 无法准确算 cost。
2. **server 端不返回 cost_usd**：`LLMResponse.cost_usd` 字段在 client 侧从未被填充（grep `cost_usd` 在 runner.py 中只出现在"删 cost_usd 累积"注释里），永远是默认 0。
3. **`max_budget_usd` 永远不触发**：因 `total_cost_usd` 永远是 0，`budget_exceeded` 检查永远不触发，`max_budget_usd` 是 dead field——保留它等于"假装有预算控制"。

同时 wall_clock 超时是更可靠的预算控制：不受缓存比例影响、不受提供商定价变化影响、用户直觉理解（"这个任务跑 30 分钟"比"这个任务花 5 美元"更具体）。

## Decision

**删字段**：

- `RunnerConfig.max_budget_usd`（types.py 原位置）→ 删除
- `RunOutcome.total_cost_usd`（types.py 原位置）→ 删除
- `LLMResponse.cost_usd`（types.py 原位置）→ 删除
- `runner.py` 所有 `total_cost_usd += response.cost_usd` 累计逻辑 → 删除
- `runner.py` `budget_exceeded` 失败路径 → 删除

**加字段**：

- `RunOutcome.total_tokens: int = 0`：累计 `usage.total_tokens`，仅记账不控制
- `LLMResponse.usage: dict` 保留（含 `prompt_tokens` / `completion_tokens` / `total_tokens`）

**不引入 `max_total_tokens`**：虽然 token 是更"客观"的预算单位（不受缓存比例影响），但引入 `max_total_tokens` 会与"只记账不控制"原则冲突——任何预算控制都会让 runner 主循环多一个失败路径，增加复杂度。token 累计仅用于 GUI 顶栏显示和未来 usage 表统计（D20）。

**`wall_clock_budget_secs` 是唯一的预算控制**：

```python
# client/core/agent/types.py
@dataclass
class RunnerConfig:
    ...
    wall_clock_budget_secs: int = 0  # 0 = 不限制（T07+ 启用）
    # max_budget_usd 已删除（B2/D3）

@dataclass
class RunOutcome:
    ...
    total_tokens: int = 0  # B2/D3：累计 token，只记账不控制
    # total_cost_usd 已删除（B2/D3）
```

默认 0（不限制），用户可在 config 覆盖（如 headless 配置 1800s）。wall_clock 超时是"完成当前步，不开始下一步"的优雅停止（D21），不是立即终止——与 interrupt 的"立即停止"语义不同。

## Considered Options

1. **保留 cost 字段但不控制（dead field）**——被拒绝：保留 `max_budget_usd` / `total_cost_usd` / `cost_usd` 但不触发任何检查 = dead field，给读者"假装有预算控制"的错觉，违反"代码即文档"原则；后续开发者会困惑"为什么累计了 cost 却不检查"。
2. **引入 `max_total_tokens`（token 预算控制）**——被拒绝：与 D3 "只记账不控制"原则冲突；token 预算控制需要处理"工具调用消耗的 token vs LLM 调用消耗的 token"、"压缩后 token 重置"等边界，复杂度高；wall_clock 已是更直观的预算控制。
3. **server 端算 cost_usd 返回给 client**——被拒绝：server 端 `/llm/pool/stream` 是流式 SSE，cost_usd 需在 stream 结束后聚合计算，破坏流式语义；且 server 端也要面对同样的缓存比例问题，并不更准；引入 server↔client cost 同步会新增耦合。
4. **删 cost 字段 + 加 total_tokens 记账 + 不加 max_total_tokens（本决策）**——采用：诚实承认"本地算 cost 不准"，删 dead field；token 累计用于显示和统计；wall_clock 作为唯一预算控制，直观可靠。

## Consequences

**正面**：
- 删 3 个 dead field，runner 主循环少一个永远不触发的检查路径，代码更诚实。
- `total_tokens` 累计为 GUI 顶栏"输入输出 token 显示"提供数据源（chat-panel-v2 T10），也为 D20 usage 表全局统计铺路。
- `wall_clock_budget_secs` 作为唯一预算控制，语义清晰：默认不限制，用户可选加；超时优雅停止不立即终止（D21）。
- runner 主循环不再需要 `cost_usd` 累计逻辑，删 ~10 行代码。

**负面**：
- 用户失去"按美元预算限制"能力——本就从未真正生效（cost_usd 永远 0），删除是诚实承认而非功能损失。
- `total_tokens` 不做预算控制意味着 LLM 失控循环（如 DoomLoop）只能靠 wall_clock + DoomLoop 检测器兜底——DoomLoop 检测已由 D10 处理（停止 + sys 系统消息），wall_clock 是兜底的兜底。
- 未来若 provider 真的开始返回 cache hit 比例，重新引入 cost 计算需要补 server 端字段 + client 端累计逻辑——但届时是"基于真实数据的新功能"而非"恢复 dead field"。

**回退路径**：若未来 provider 开始稳定返回 cache hit 比例且 server 端愿意算 cost_usd，可重新引入 `RunOutcome.total_cost_usd`（仅记账，仍不做控制）+ GUI 顶栏 cost 显示。但 `max_budget_usd` 预算控制不会恢复——wall_clock 已证明是更可靠的预算控制机制。

## References

- SDD 来源：`temp/sdd/chat-engine-safety-fixes/design-decisions.md` D2（不算 cost_usd 只记 token）+ D3（只记账不控制，废弃 cost 字段全删）+ D8（不加 max_total_tokens）+ D5（wall_clock 默认 0）+ D21（wall_clock SSE 优雅停止）
- 关键代码：
  - [client/core/agent/types.py](file:///<project_root>/client/core/agent/types.py)（`RunnerConfig.wall_clock_budget_secs: int = 0` L444；`RunOutcome.total_tokens: int = 0` L484；`LLMResponse.usage` dict L181；max_budget_usd / total_cost_usd / cost_usd 已删，注释 L438/L483）
  - [client/core/agent/runner.py](file:///<project_root>/client/core/agent/runner.py)（`total_tokens` 累计 L488 / L752-L753；"删 cost_usd 累积"注释 L1172 / L1243）
- 相关 ADR：无直接关联 ADR，但与 chat-engine-safety-fixes spec 的 D5（wall_clock 默认 0）/ D10（DoomLoop 停止）/ D20（usage 表）/ D21（wall_clock 优雅停止）共同构成 chat 引擎预算与安全模型
