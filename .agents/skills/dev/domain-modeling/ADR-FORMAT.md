# ADR Format

ADRs 位于 `docs/adr/`，使用连续编号：`0001-slug.md`、`0002-slug.md` 等。

按需懒创建 `docs/adr/` 目录：只有第一个 ADR 需要出现时才创建。

**外部入口**：`code_review.md` Step 5.8 / `project_rules.md` 代码变更段 / `grill-with-docs` skill 都会触发"是否需要 ADR"的评估，统一指向本文件作为标准真源。

## Template

```md
# {Short title of the decision}

{1-3 sentences: what's the context, what did we decide, and why.}
```

就这些。ADR 可以只有一个段落。价值在于记录 *做出了某个决定* 以及 *为什么*，而不是填满章节。

## Optional sections

只有在真正增加价值时才包含这些。多数 ADRs 不需要。

- **Status** frontmatter（`proposed | accepted | deprecated | superseded by ADR-NNNN`）- 当 decisions 会被重新审视时有用
- **Considered Options** - 只有被拒绝的 alternatives 值得记住时才写
- **Consequences** - 只有需要指出非显而易见的下游影响时才写

## Numbering

扫描 `docs/adr/` 中已有的最高编号并递增一。

## When to offer an ADR

以下三项必须**全部**成立：

1. **Hard to reverse** - 之后改变主意的成本有意义
2. **Surprising without context** - 未来读者看到代码会疑惑 "why on earth did they do it this way?"
3. **The result of a real trade-off** - 确实存在替代方案，而你基于具体理由选择了其中一个

如果决定很容易反转，就跳过；你会直接反转它。如果它不意外，就没人会问为什么。如果没有真正 alternative，就没有超过 "we did the obvious thing" 的内容可记录。

**重要**：评估"Hard to reverse" 时必须考虑**当前**状态，而非未来假设状态。例如"如果将来 agent 开始消费某个字段，移除就难了"——这种"未来 hard to reverse"不满足标准；等到 agent 真的开始消费时再写也来得及。已经反转成本很低就老老实实跳过 ADR。

### What qualifies

- **Architectural shape.** "We're using a monorepo." "The write model is event-sourced, the read model is projected into Postgres."
- **Integration patterns between contexts.** "Ordering and Billing communicate via domain events, not synchronous HTTP."
- **Technology choices that carry lock-in.** Database、message bus、auth provider、deployment target。不是每个 library，只有那些替换要花一个季度的。
- **Boundary and scope decisions.** "Customer data is owned by the Customer context; other contexts reference it by ID only." 明确的 no 和 yes 一样有价值。
- **Deliberate deviations from the obvious path.** "We're using manual SQL instead of an ORM because X." 任何合理读者会默认相反方案的地方，都值得记录，避免下一个 engineer 把 deliberate choice "修掉"。
- **Constraints not visible in the code.** "We can't use AWS because of compliance requirements." "Response times must be under 200ms because of the partner API contract."
- **Rejected alternatives when the rejection is non-obvious.** 如果你考虑过 GraphQL，却因为微妙原因选择 REST，就记录；否则六个月后还会有人再提 GraphQL。

## What does NOT qualify（反模式）

以下任一成立就**不写 ADR**——直接做，CHANGELOG 已经足够：

- **Bug fix / 安全补丁 / 性能优化**：恢复到"正确行为"不算 trade-off，因为没有"反对正确"的 alternative。例：补全危险关键词黑名单、修复 O(n²) → O(n)、修复 NFKC 归一化绕过——这些都是常规修复，CHANGELOG 一行足矣。
- **错误码细分 / 字段语义显式化**：例如"返回 `tab_id_resolved=False` 而非静默置空"——这是修复语义不清，不是 trade-off。
- **内存泄漏修复 / asyncio task GC 修复**：例如"持有 task strong reference 避免事件循环回收"——这是 Python 文档明确要求的正确行为，没有 alternative。
- **可逆的代码层调整**：列表 → deque、`list.append` → `_append_pending` 封装——反转就是改回去，零认知成本。
- **"未来可能 hard to reverse"**：当前没人消费某字段，未来"如果"消费了就难移除——这种预测性反转成本不算数，等真的发生时再写。
- **单点焦点校验 / 单路径防御**：只在某一路径加焦点校验而不在其他同质路径加，本身已是"行为不一致"的代码味道，更应**撤销**而非文档化。

### 反模式示例（来自真实 case）

**Case A（来自第四轮 review Issue 8，已撤销）**：考虑给 UIA `set_value` 加 `verify_focus_for_input` 焦点校验。

表面看似符合：surprising（与既有 UIA "不受焦点漂移影响"的设计哲学冲突）+ real trade-off（信任 COM 隔离 vs 保守 UX 防御）。

但实际**不**符合：
- Hard to reverse 不成立：`fallback_reason` 字段当前没 agent 消费，移除很容易
- 只给 set_value 加而不给 invoke/toggle/scroll 加，本身就是行为不一致的代码味道
- UX 防御应该在更高层机制（全局热键监听），而非单路径焦点校验

**结论**：撤销修改，不写 ADR。在 CHANGELOG 中详细记录撤销理由。这正是 ADR-FORMAT.md 三项标准存在的价值——避免把"看起来值得文档化"实则是过度防御的决策固化下来。

## 如何评估

如果 review 时对"是否需要 ADR"有疑问，**先按三项标准逐项打分**：

| 标准 | 通过条件 | 失败条件 |
|------|---------|---------|
| Hard to reverse | 移除/反转需修改多个消费方、agent 行为契约、外部 API | 改一个 if-block、改一个字段名、改回 list |
| Surprising | 未来读者会问"为什么这里多了一层防御/校验/间接" | 行为与模块既有设计哲学一致 |
| Real trade-off | 有具体替代方案被拒绝且拒绝理由不显然 | 没有替代（如修复 bug），或替代方案显然不可行 |

3/3 通过 → 写 ADR
2/3 通过 → 边缘案例，**优先考虑撤销修改或调整设计**而非"为了写 ADR 而写 ADR"
<2/3 通过 → 直接做，CHANGELOG 足矣

边缘案例尤其危险：它往往意味着设计本身有矛盾（如 Case A）。与其文档化矛盾，不如消除矛盾。
