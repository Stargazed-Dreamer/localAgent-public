# 外部 skill 吸收判断标准——不因当前使用频率低就放弃吸收方法论 skill

当 vendor 仓库（如 `pbakaus/impeccable`、`nutlope/hallmark`、`kangarooking/cangjie-skill`）经过安全审查后，agent 不应以"当前项目前端/相关场景不紧密"为由放弃吸收方法论 skill。本 ADR 沉淀 4 项吸收判断标准，避免未来重蹈"因当前联系不紧密就跳过"的判断模式。

## Context

用户原话反馈（2026-07-21）：

> "主要是，当前不紧密，不代表未来没需求，而且 dev-toolkits 也能用得上……这不是第一次被认为前端联系不紧密就放弃了，考虑记一个记忆或者 ADR？"

历史快照：

| Vendor | 上游 | 首次评估结论 | 后续动作 |
|---|---|---|---|
| cangjie-skill | kangarooking/cangjie-skill | ✅ 已融合到 `daily/cangjie_extraction/` | 第一次评估就通过 |
| impeccable | pbakaus/impeccable | ⏸️ "未接入项目本体（如需接入参考原仓库）" | 第二次评估才融合到 `dev/impeccable/` |
| hallmark | nutlope/hallmark | ⏸️ "未接入项目本体（如需接入参考原仓库）" | 第二次评估才融合到 `dev/hallmark/` |
| mattpocock-zh | vinvcn/mattpocock-skills-zh-CN | ⏸️ "未接入项目本体" | `engineering/` 和 `productivity/` 桶被 `dev/` 和 `daily/` 改造吸收，`workspace/dev_toolkit/` 同步保留 |

impeccable 和 hallmark 在首次评估时被标记"暂未接入项目本体"——这正是用户提到的"被认为前端联系不紧密就放弃"的判断模式。第二次评估（本次会话）按本 ADR 的 4 项标准全部通过，融合到 `dev/` 桶。

`workspace/dev_toolkit/` 是新项目启动模板，方法论 skill 在此处同样有价值——即使本仓库当前前端场景少，dev_toolkit 派生的新项目可能正好需要这些方法论。

## Decision

外部 skill 吸收判断 4 项标准（**全部成立才吸收**）：

1. **方法论普适性**：skill 包含可在多个项目复用的方法论，而非项目特定代码或一次性脚本
2. **未来场景预期**：用户原话确认未来场景可能用到，或 skill 主题与项目方向（前端 / 数据 / 自动化 / 文档等）有重合可能
3. **dev_toolkit 适配性**：skill 值得纳入 `workspace/dev_toolkit/` 作为新项目启动模板的一部分
4. **融合成本可控**：能按 "Vendor 克隆 + 适配 wrapper" 模式融合（`upstream/` 原版档案 + wrapper SKILL.md 路径适配），不破坏项目本体

**明确反对**的判断模式：
- ❌ "当前项目前端联系不紧密，暂不接入" → 当前不紧密不代表未来没需求
- ❌ "用户没显式调用，先放着" → 方法论 skill 是被动能力储备，不需要主动调用频次证明价值
- ❌ "本体仓库体积大就少吸收" → 已通过精简 95% 控制体积（impeccable 43.84MB → 2.35MB，hallmark 12.90MB → 0.70MB）

吸收后必做（4 处同步）：
1. 融合到本体 `.agents/skills/<scope>/<name>/`（含 `upstream/` 原版档案 + wrapper SKILL.md 路径适配）
2. 同步更新 `_index.md` + `server/agent_guide.py` 的 `GUIDE_REGISTRY`
3. 同步到 `workspace/dev_toolkit/.agents/skills/<scope>/<name>/` 作为新项目模板
4. 更新 `.agents/skills/_vendor/README.md` 标注融合状态 + 写 CHANGELOG 条目

## Considered Options

- **A. 仅在用户每次显式要求时才融合**：否决。用户原话"考虑记一个记忆或者 ADR"是把判断标准沉淀为可重用规则，而非每次重新评估。每次都问"是否融合"会让用户疲于决策。
- **B. 全量吸收所有 vendor skill**：否决。部分 vendor 是项目特定代码而非方法论（如 `mattpocock-zh` 是 mattpocock 个人风格的中文翻译，已通过 `workspace/dev_toolkit/` 间接吸收 `engineering/` 和 `productivity/` 桶，不需融合到本体）。本 ADR 的 4 项判断标准正是为了避免"全量吸收"和"按需吸收"两个极端。
- **C. 用本 ADR 的 4 项判断标准**：通过。在"吸收什么"和"何时吸收"之间取得平衡，且把"为什么吸收"显式化，未来 agent 评估 vendor 时按标准打分而非凭直觉。

## Consequences

- **正向**：未来 agent 看到 vendor skill 时按 4 项标准判断，避免重蹈"前端不紧密就放弃"的覆辙；`workspace/dev_toolkit/` 同步保持完整方法论库；用户不再需要每次显式推动吸收。
- **负向**：本体仓库体积增长（impeccable 2.35MB + hallmark 0.70MB + upstream 档案），但已通过精简 95% 控制；融合改造需同步 4 处（本体 + _index + agent_guide + dev_toolkit），单次改造成本上升。
- **未来反转成本**：若需撤销某次融合，删除 `.agents/skills/<scope>/<name>/` + 回退 `_index.md` + `agent_guide.py` + `workspace/dev_toolkit/` 对应副本 + 更新 `_vendor/README.md` 标注即可，无破坏性影响。但因为本 ADR 已固化为规则，未来 agent 不会主动提议撤销已融合的 vendor skill——撤销必须由用户显式提出。
- **触发更新本 ADR 的条件**：①如果未来 vendor 吸收出现新维度（如付费 skill / 需在线服务 skill / 需要原生依赖 skill），扩展本 ADR 的判断标准；②如果吸收数量超过 10 个导致本体仓库臃肿，重新评估"全量 vs 按需"的平衡。
