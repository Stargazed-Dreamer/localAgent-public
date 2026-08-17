# skill_creator - Skill 创建精华

> 整合自 anthropics/skills 的 skill-creator。
> **注意**：本项目已有强化版 `f:\<project_root>\.agents\skills\skill-creator.md`（含 pushy description / 500 行软上限 / evals 机制 / 反模式）。
> 本文件补充 anthropic 版的精华（不重复本项目已有的），聚焦 anthropic 独有的 evals 设施 + description 优化器。

## 1. 概述

**Skill** 是给 LLM 用的可执行知识包，让 agent 在特定场景下知道"做什么、怎么做、不能做什么"。

### 1.1 Skill 不是文档

| 维度 | 文档 | Skill |
|------|------|-------|
| 受众 | 人 | LLM |
| 形式 | 自由散文 | 工作流指令 |
| 加载 | 全文阅读 | 渐进式披露（progressive disclosure） |
| 验证 | 人工评审 | evals 量化评估 |
| 触发 | 主动找 | description 匹配路由 |

### 1.2 双层结构

```
skill-name/
├── SKILL.md (必需)
│   ├── YAML frontmatter (name, description 必需)
│   └── Markdown 工作流指令
└── Bundled Resources (可选)
    ├── scripts/      - 可执行脚本（确定性逻辑封装）
    ├── references/   - 详细参考（按需加载）
    └── assets/       - 输出模板（图标 / 字体 / 模板文件）
```

### 1.3 三层加载（progressive disclosure）

| 层级 | 内容 | 加载时机 | 大小 |
|------|------|----------|------|
| 1. Metadata | name + description | 始终在上下文（agent 路由时读） | ~100 words |
| 2. SKILL.md body | 工作流指令 | skill 触发时加载 | <500 lines ideal |
| 3. Bundled resources | 脚本/参考/模板 | 干到那一步才读 | 不限 |

**核心思想**：不要一次性把所有细节塞给 agent。SKILL.md 给骨架，references/scripts 给细节，agent 干到那一步才去翻。

## 2. Progressive Disclosure 原则

来自 anthropic skill-creator。

### 2.1 SKILL.md 软上限 500 行

接近上限时按以下策略分层：

1. **拆到 references/**：详细的映射表 / API 参数说明 / 长示例代码移到 `references/*.md`，SKILL.md 中用 "详见 references/xxx.md" 指向
2. **Domain organization**（变体分文件）：当 skill 支持多种引擎/框架时，按变体分文件，agent 只读相关那一个：
   ```
   ocr/
   ├── SKILL.md          # 工作流 + 引擎选择决策树
   └── references/
       ├── paddleocr.md  # 经典 OCR 引擎细节
       └── qwen_vl.md    # 远程 VL 引擎细节
   ```
3. **大参考文件加 TOC**：`references/` 下单个文件 >300 行时，文件顶部加目录（`## 目录` + 锚点链接）
4. **scripts/ 封装确定性逻辑**：可执行的标准流程放 `scripts/*.py`，agent 调用而非现场重写，SKILL.md 只写"调用 scripts/xxx.py"

### 2.2 反模式：context 噪音

不要把所有细节塞进 SKILL.md 让 agent 一次性读完——那是 context 噪音，反而降低 agent 决策质量。

**典型案例**：
- ❌ SKILL.md 800 行 + 全是 API 参数表 → agent 加载后上下文污染
- ✅ SKILL.md 200 行 + references/api_params.md 单独存参数表 → agent 干到调 API 才去读

### 2.3 Reference 文件加载指引

SKILL.md 中要明确告诉 agent 何时去读 reference：

```markdown
## 工作流

1. 截图：用 `screen_ocr` 工具
2. **OCR 引擎选择**：详见 `references/ocr_engines.md`，按场景选 paddleocr / qwen_vl
3. 解析结果...

## 详细参数

API 参数完整列表见 `references/api_params.md`。
```

模糊的"详见"不如具体的"调 API 前先读 references/api_params.md"。

## 3. Description 编写（pushy 原则）

来自 anthropic skill-creator + 本项目强化。

### 3.1 为什么 pushy

> Claude 倾向于 undertrigger skills（该用的时候不用）。为对抗这点，description 要主动"推"。

description 是 skill 触发的核心机制——agent 路由时优先读 description 匹配用户意图。写得克制 → 该用时不用。

### 3.2 pushy 三原则

1. **明确列全触发场景**：不要只写"做什么"，要写"什么时候用"——列出所有相关用户意图关键词和上下文
2. **用"主动触发"措辞**：避免被动描述，用 "当用户提到 X / Y / Z 时使用此 skill" 的句式
3. **覆盖边界场景**：用户可能不会直接说"我要用 X skill"，但描述里要包含间接表达

### 3.3 pushy 反例与正例

**反例**（不够 pushy）：
> 创建 HTML 工具页面的 skill。

**正例**（pushy）：
> 编写和调试 HTML 工具页面。当用户要求创建或修改 HTML 页面（如可视化 Dashboard、监控面板、审核页面、工具页面）时触发。当用户提到"写个页面"、"前端"、"Dashboard"、"可视化"时也使用此 skill。核心方法：浏览器控制 + 截图 + OCR 闭环验证。

### 3.4 中英文双语触发词

本项目用户用中文，但 agent 可能匹配英文关键词。description 里中英文触发词都列：

```yaml
description: >
  编写和调试 HTML 工具页面。当用户要求创建或修改 HTML 页面（如可视化 Dashboard、
  监控面板、审核页面、工具页面）时触发。当用户提到"写个页面"、"前端"、"Dashboard"、
  "可视化"、"create html page"、"build dashboard"、"frontend"时也使用此 skill。
```

## 4. 5 阶段工作流

来自 anthropic skill-creator。

### 阶段 1：Capture Intent（意图捕获）

明确以下信息：
1. 这个 skill 解决什么问题？
2. 触发场景是什么？（用户会说什么话）
3. 涉及哪些已有能力？（OCR/屏幕操控/浏览器/LLM/代码执行）
4. 输入和输出是什么？

如果用户已经说清楚了，直接进阶段 2。信息不足时简短访谈（不超过 3 个问题）。

### 阶段 2：Interview and Research（访谈研究）

主动问 edge cases / IO 格式 / 示例文件 / 成功标准 / 依赖。

**等访谈充分了再写测试 prompt**——不要在还没搞清楚时就开测。

并行做研究：
- 检查可用 MCP（搜索文档 / 找相似 skill / 查最佳实践）
- 用 subagent 并行研究，否则 inline

### 阶段 3：Write SKILL.md（写 skill）

按本项目模板写：
- YAML frontmatter（name + description 必需）
- 工作流（每步写清"做什么 + 用什么工具/API"）
- 关键规则（硬约束用"必须/绝不/禁止"）
- 坑点清单（agent 推断不出来的信息）
- 依赖表 + 输入输出

**写作风格权衡**：
- 安全规则用"必须/绝不/禁止"硬约束
- 工作流指引优先用"解释为什么这样做"而非堆砌 MUST
- 理解 why 的 agent 能在未覆盖的场景做出正确判断
- 只会照搬 MUST 的 agent 遇到新情况就僵在轨道上

### 阶段 4：Test（测试）

写 2-3 个真实测试 prompt——像真人会说的话，分享给用户确认。

**不要写**："测试 skill 的 X 功能"
**要写**："帮我整理 temp 目录"（真实用户语气）

### 阶段 5：Iterate（迭代）

根据测试结果优化：
- 工作流步骤缺失 → 补充
- 规则不够明确 → 细化
- 依赖遗漏 → 添加
- 触发词不够 → 扩充
- evals 失败的用例 → 指向 skill 需要优化的具体环节

**循环直到满意**：扩大测试集，更大规模再试。

## 5. evals 量化评估机制

来自 anthropic skill-creator，是 anthropic 独有的精华。

### 5.1 为什么需要 evals

**手动测试只能发现"能不能跑"，无法回答"好不好用"**。

evals 让你：
- 量化对比 with-skill vs without-skill
- 量化对比新旧版本（修改 skill 后是否变好）
- 发现 skill 哪些环节容易失败
- 客观评估，不靠"我觉得"

### 5.2 evals/evals.json 结构

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "name": "descriptive-name",
      "prompt": "用户的真实任务描述（像真人会说的话）",
      "expected_output": "期望结果的描述（用于人工对比）",
      "files": [],
      "assertions": [
        {
          "text": "输出文件包含 X 字段",
          "type": "contains",
          "target": "X"
        },
        {
          "text": "未调用删除操作",
          "type": "not_contains",
          "target": "delete"
        }
      ]
    }
  ]
}
```

### 5.3 5 种断言类型

| type | 说明 | target 格式 | 例子 |
|------|------|------------|------|
| `contains` | 输出包含 target 字符串 | 字符串 | `"version"` |
| `not_contains` | 输出不包含 target | 字符串 | `"delete"` |
| `regex` | 输出匹配 target 正则 | 正则字符串 | `"[0-9]{4}-[0-9]{2}"` |
| `file_exists` | target 路径文件存在 | 文件路径 | `"/tmp/output.json"` |
| `json_path` | target 是 `path->value`，输出 JSON 的 path 等于 value | `"data->result->status->success"` |

### 5.4 双轨对照（with-skill vs without-skill）

来自 anthropic skill-creator 的核心方法：

**每个 eval 跑两次**：
1. **with-skill**：agent 加载 skill 跑 prompt
2. **without-skill**（baseline）：agent 不加载 skill 跑同一 prompt

**对照意义**：
- skill 应让 with-skill 比 baseline 表现更好
- 如果 with-skill 和 baseline 一样 → skill 没起到作用，可能 description 没触发
- 如果 with-skill 比 baseline 还差 → skill 写错了，反而误导

### 5.5 简化版（本项目采用）

本项目暂不引入 anthropic 的 eval-viewer/benchmark/analyst 重型设施。evals 使用方式：

1. **新增/修改 skill 时**：agent 读取 `evals/evals.json`，按 prompt 执行 skill，对照 assertions 自检
2. **回归测试**：用户说"测试 X skill"时，agent 跑 evals 并报告通过率
3. **迭代依据**：evals 失败的用例指向 skill 需要优化的具体环节

**完整版参考**（未来需要时引入）：
- benchmark.json：记录 pass_rate / time / tokens
- eval-viewer：HTML 查看器， Outputs tab + Benchmark tab
- analyst pass：发现 patterns（non-discriminating assertions / 高方差 evals）

详见 `workspace/dev_toolkit/evals_template.json` 模板。

### 5.6 evals 编写要点

1. **prompt 要像真人说的话**：不要写"测试 skill 的 X 功能"，写"帮我整理 temp 目录"
2. **断言要客观可验证**：避免"输出质量高"主观判断，用"包含 X 字段""未触发 Y 操作"
3. **覆盖关键路径**：2-5 个用例覆盖正常流程 + 1 个边界场景（空输入 / 异常数据）
4. **主观输出不强求 evals**：写作风格、设计美观等主观 skill 不需要 evals，靠人工评审

### 5.7 evals 评估 6 要求

来自 anthropic mcp-builder 的 eval 设计原则（每条 eval 必须）：

- **Independent**：不依赖其他问题
- **Read-only**：只非破坏性操作
- **Complex**：需要多次工具调用 + 深度探索
- **Realistic**：基于真实用例
- **Verifiable**：单一明确答案，字符串比较即可
- **Stable**：答案不随时间变化

## 6. Description 优化器

来自 anthropic skill-creator，是 anthropic 独有的精华。

### 6.1 为什么需要

description 是 skill 触发的核心，但人工写的 description 不一定是触发率最高的版本。

**优化器**：用 LLM 自动改写 description，跑 evals 对比触发率，迭代到最优。

### 6.2 优化流程

1. **基线**：当前 description 跑 evals，记录触发率
2. **生成候选**：LLM 基于 description 生成 N 个改写候选
3. **A/B 测试**：每个候选跑同一组 evals
4. **选最优**：触发率最高的候选作为新 description
5. **回归**：用新 description 跑所有 evals，确认未引入 regression

### 6.3 优化方向

LLM 改写时通常做：
- 加触发词覆盖（同义词、口语表达、间接表达）
- 改成更主动的句式（"当用户...时使用"而非"用于..."）
- 加边界场景关键词
- 中英文双语触发词

### 6.4 不要过度优化

优化器可能让 description 变成"关键词堆砌"——读起来像 SEO 垃圾。

**约束**：
- description 仍要可读（人看了不觉得尴尬）
- 保留原 description 的语义清晰性
- 触发词不能离题（避免不相关任务误触发）

## 7. 反模式

### 7.1 SKILL.md 过长

**反模式**：800 行 + 全是 API 参数表

**纠正**：拆到 references/，SKILL.md < 500 行

### 7.2 SKILL.md 过短

**反模式**：50 行 + 只有"用 X 工具做 Y"

**纠正**：补工作流细节、坑点清单、依赖表

### 7.3 模糊触发词

**反模式**：description 写"处理数据"

**纠正**：写"当用户提到 X / Y / Z 时使用此 skill"，列全触发词

### 7.4 无 evals

**反模式**：只手动测试"能不能跑"

**纠正**：有客观可验证输出的 skill 必须写 evals（含 5 种断言类型）

### 7.5 陈述显而易见

**反模式**：写"写完代码要跑测试"（agent 本来就会）

**纠正**：删掉 agent 本来就会的，只写能把它推离默认思路的信息

### 7.6 步骤锁死

**反模式**：写"必须按 A→B→C 顺序，每步用 X 工具"

**纠正**：给足信息，把怎么走的自由留给 agent

### 7.7 遗漏安全规则

**反模式**：涉及删除/支付/关机操作没写确认流程

**纠正**：必须写明确认流程（用户确认 / 审批 token / 备份）

### 7.8 硬编码路径

**反模式**：写"读取 `C:\<user_home>\project\data.json`"

**纠正**：用变量 `<project_root>/data/data.json` 或环境变量

### 7.9 description 不够 pushy

**反模式**：被动描述"用于 X"

**纠正**：主动句式"当用户提到 X / Y / Z 时使用"

### 7.10 忘记更新索引

**反模式**：修改 skill 后不更新 `_index.md` / `GUIDE_REGISTRY`

**纠正**：每次修改后同步更新索引和路由

## 8. 与本项目 skill-creator 对比

本项目 `f:\<project_root>\.agents\skills\skill-creator.md` 已强化含以下内容（不在此重复）：

| 已吸收内容 | 本项目实现位置 |
|----------|---------------|
| pushy description 编写原则 | 本项目 skill-creator.md § "Description 编写原则" |
| 500 行软上限 + 分层规范 | 本项目 skill-creator.md § "行数与分层规范" |
| evals 简化版（5 种断言） | 本项目 skill-creator.md § "阶段 5.5：量化评估" |
| 反模式 10 条 | 本项目 skill-creator.md § "反模式" |
| 5 阶段工作流 | 本项目 skill-creator.md § "工作流程" |
| Skill 是文件夹不是文件 | 本项目 skill-creator.md § "核心理念" |
| Domain organization | 本项目 skill-creator.md § "行数与分层规范" |
| 写作风格权衡（why 优于 MUST） | 本项目 skill-creator.md § "编写要点" |

### 8.1 本文件补充 anthropic 独有的内容

| 本文件独有 | 章节 |
|----------|------|
| 完整 evals 设施介绍（含 viewer / benchmark / analyst） | § 5 |
| 双轨对照（with-skill vs without-skill） | § 5.4 |
| Description 优化器（LLM 自动改写） | § 6 |
| evals 6 要求（Independent / Read-only / Complex / Realistic / Verifiable / Stable） | § 5.7 |
| Progressive Disclosure 三层加载详细 | § 2 |
| Reference 加载指引的写法 | § 2.3 |

### 8.2 取用建议

- **基础 skill 编写**：直接用本项目 `skill-creator.md`（已含核心）
- **需要量化评估 skill 表现**：读本文件 § 5 + `evals_template.json`
- **description 触发率不够想优化**：读本文件 § 6
- **想引入完整 evals 设施**：参考 anthropic 仓库的 `eval-viewer/` 和 `scripts/aggregate_benchmark.py`

## 9. 实施清单

新项目构建 skill 系统时按此清单逐项确认：

- [ ] Skill 目录结构规范（`<skill_name>/SKILL.md` + 可选 `scripts/` / `references/` / `assets/`）
- [ ] YAML frontmatter 含 name + description
- [ ] description 用 pushy 句式 + 中英文双语触发词
- [ ] SKILL.md < 500 行（接近时拆 references/）
- [ ] 工作流每步写"做什么 + 用什么工具/API"
- [ ] 关键规则用"必须/绝不/禁止"硬约束
- [ ] 坑点清单只写 agent 推断不出来的
- [ ] 依赖表 + 输入输出明确
- [ ] 有客观可验证输出 → 写 evals/evals.json
- [ ] evals 用 5 种断言类型
- [ ] 测试 prompt 像真人说话
- [ ] 修改 skill 后同步更新索引
- [ ] 涉及删除/支付/关机 → 写安全规则

## 10. 参考资源

- **anthropics/skills skill-creator**：`https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md`
- **anthropics/skills 完整仓库**：`https://github.com/anthropics/skills/tree/main/skills`
- **LocalAgent 项目强化版 skill-creator**：`f:\<project_root>\.agents\skills\skill-creator.md`
- **evals 模板（本工具包）**：`workspace/dev_toolkit/evals_template.json`
- **LocalAgent 项目 _index.md**：`f:\<project_root>\.agents\skills\_index.md`
