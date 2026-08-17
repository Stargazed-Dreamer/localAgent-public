---
name: skill-creator
description: >
  创建新 Skill、修改和优化已有 Skill。当用户想要从零创建一个 Skill、编辑或优化现有 Skill、
  或规范化 Skill 格式时触发。触发词：创建skill、新建skill、写个skill、优化skill、
  skill-creator、创建技能、新建技能。当用户提到"skill怎么写"、"skill格式"时也触发。
---

# Skill 创建器 (skill-creator)

## 触发词
创建skill、新建skill、写个skill、优化skill、skill-creator、创建技能、新建技能、skill怎么写、skill格式、编辑skill、修改skill

## 概述
帮助用户从零创建新 Skill、编辑或优化现有 Skill 的元技能。通过意图捕获→需求访谈→编写 Skill 文件→测试验证→迭代优化的流程，产出符合本项目规范的高质量 Skill。

## 核心理念

1. **Skill 是文件夹，不是文件**：一个 Skill 是 `.agents/skills/<skill_name>/` 下的一个目录，包含 `SKILL.md`（必需）+ 可选的 `scripts/`（现成脚本）、`references/`（参考资料/映射表）、`assets/`（输出模板）。agent 干到哪一步，自己去文件夹里翻对应材料，而不是一次性读入所有内容
2. **可执行的知识**：不是文档，而是一组精确的工作流指令，让 AI agent 知道"做什么、怎么做、不能做什么"
3. **唯一目录**：`.agents/skills/` 是 Skill 的唯一存放位置，每个 Skill 一个子文件夹
4. **渐进式细化**：先写出骨架，通过测试发现不足，再迭代完善

> 注：现有的扁平 `.md` skill（如 `accounting.md`）是历史遗留，按需逐步迁移成文件夹结构；新建 Skill 一律用文件夹结构。

## Skill 编写原则（借鉴 mattpocock writing-great-skills）

本节原则来自 mattpocock/skills 的 writing-great-skills，与本项目的"不分 model/user-invoked，所有 skill 都可被 agent 自主触发"决策已经本地化调整。

### Predictability 是根本美德

Skill 的存在，是为了从随机系统中拧出 determinism。**Predictability** 是根本美德：agent 每次采取相同的 _process_，而不是产出相同的 output。下面所有杠杆都服务于它。

### Information hierarchy（信息层次）

Skill 由两类内容构成：**steps** 与 **reference**。核心决策是把内容放在 information hierarchy 的哪一层：

1. **In-skill step** - `SKILL.md` 中的有序动作，是 primary tier。每个 step 以 **completion criterion** 结束。Criterion 要可检查，必要时要 exhaustive。
2. **In-skill reference** - `SKILL.md` 中的定义、规则或事实，按需查阅。
3. **External reference** - 从 `SKILL.md` 推到独立文件中（`references/*.md`），经 **context pointer** 触达。

**Progressive disclosure** 是把 reference 下移到链接文件中，让顶层保持清晰。Mechanics：skill folder 中的 linked `.md` 文件，用内容命名。多用途 skill 的每种用法都是一个 **branch**：所有 branches 都需要的内容内联，只有部分 branches 需要的内容放到 pointer 后面。**Context pointer** 的措辞，而不是目标文件，决定 agent 何时以及多可靠地触达材料。

**Co-location** 决定内容一旦下放后放在哪里：把一个概念的定义、规则和 caveats 放在同一 heading 下，而不是散落各处。

### Description 写法

本项目所有 skill 都可被 agent 自主触发（不分 model/user-invoked），所以 **description** 必须做好两件事：说明 skill 是什么，并列出应触发它的 branches。每个词都会增加 context load，所以 description 比正文更需要修剪：

- **把 skill 的 leading word 放前面。**
- **每个 branch 一个 trigger。** 同义词如果只是重命名单一 branch，就是 duplication；合并它。
- **删掉正文已有的 identity。** Description 只保留 triggers，以及必要的 "when another skill needs..." reach clause。
- **触发词列表显式列出**（参考现有 dev/daily skill 的 `触发词：xxx、xxx、xxx` 格式）。

### Leading words（引导词）

**Leading word** 是一个已经存在于模型预训练中的紧凑概念，agent 会在运行 skill 时用它思考（例如 _lesson_、_fog of war_、_tracer bullets_、_red-green-refactor_）。它在文本中反复出现，累积 distributed definition，并用最少 tokens 固定一片 behavior。

它从两方面服务 predictability：正文中它锚定 _execution_；description 中它锚定 _invocation_。当相同词出现在 prompts、docs 和 codebase 中，agent 更容易把 shared language 连到该 skill。

寻找机会把 skills 重构为使用 leading words。三处重复展开的 triad、花一句话绕一个概念的 description，都可能能 collapse 成一个 token。

### When to split（何时拆分 skill）

**Granularity** 是 skill 切分粒度。每次切分都有成本，所以只有切分有收益时才切：

- **By invocation** - 当你有一个独立 leading word 应自主触发，或另一个 skill 必须触达它时，拆出独立 skill。你要为新 description 支付 context load，所以独立触达必须值得。
- **By sequence** - 当后续 steps 会诱使 agent 急着结束前一步（**premature completion**）时，拆分 step sequence，把后面的内容隐藏起来。

### Pruning（修剪）

让每个 meaning 都有 **single source of truth**：一个权威位置，行为变化时只改一处。逐行检查 relevance：它是否仍支撑 skill 的工作？逐句寻找 no-ops，把每个句子单独做 no-op test；失败时删除整句，而不是只修剪词。要激进；多数失败 prose 应删除，不应重写。

### Failure modes（反模式）

- **Premature completion** - 当前 step 尚未真正完成就结束。防御顺序：先 sharpen completion criterion；只有当 criterion 不可避免地模糊且你观察到 rush 时，才通过拆分隐藏 post-completion steps。
- **Duplication** - 同一 meaning 出现在多个地方。它提高维护成本、浪费 tokens，并夸大该 meaning 在 hierarchy 中的重要性。
- **Sediment** - 因为添加看似安全、删除看似有风险而沉积的 stale layers。
- **Sprawl** - skill 太长，即使每一行都 live 且 unique。用 hierarchy 治疗：把 reference 放到 pointers 后，按 branch 或 sequence 拆分。
- **No-op** - 模型默认就会做的 instruction。测试：它是否改变默认 behavior？弱 leading word（如 _be thorough_，当 agent 已经大致 thorough）就是 no-op；修法是换更强的词（如 _relentless_）。
- **Negation** - 用禁止来引导会适得其反：_don't think of an elephant_ 点名了 elephant，让它更容易浮现。应 prompt **positive**：直接说明目标 behavior，让被禁止的行为不进入表述；只有无法正向表达的 hard guardrail 才保留 prohibition，而且仍要配上应该怎么做。

## 工作流程

### 阶段 1：意图捕获

当用户提出创建 Skill 的请求时，先明确以下信息：

1. **这个 Skill 解决什么问题？** — 一句话描述核心意图
2. **触发场景是什么？** — 用户会说什么话来触发它？
3. **涉及哪些已有能力？** — 是否依赖 OCR/屏幕操控/浏览器/LLM/代码执行？
4. **输入和输出是什么？** — 从哪里读数据，结果写到哪里？

如果用户已经说清楚了以上信息，直接进入阶段 2。如果信息不足，进行简短访谈（不超过 3 个问题）。

### 阶段 2：需求访谈

通过结构化访谈补充缺失信息。**只问不知道的，不问已经明确的。**

访谈问题清单（按需选取）：

- **工作流**：这个任务你平时怎么做？分几步？
- **边界条件**：什么情况下应该停止或报错？
- **安全约束**：有没有不能做的事？（如自动删除、自动支付）
- **异常处理**：出错时应该怎么处理？重试还是跳过？
- **依赖检查**：需要后端哪些模块？需要管理员权限吗？
- **工具脚本**：是否需要写新的 Python 脚本？还是只用现有 API？
- **输入格式**：用户需要准备什么文件？放在哪个目录？
- **输出格式**：结果以什么形式呈现？文件？API 返回？

### 阶段 3：编写 Skill 文件

按照本项目标准模板编写 Skill 文件，保存到 `.agents/skills/` 目录。

#### 目录结构规则

- 每个 Skill 是一个文件夹：`.agents/skills/<skill_name>/`，文件夹内必需 `SKILL.md`
- 英文小写 + 下划线，如 `disk_manager/`、`yihuan_gacha/`
- 名称应简洁反映功能
- 子目录可选：`scripts/`（可执行脚本）、`references/`（参考资料/映射表）、`assets/`（输出模板）

#### YAML Frontmatter（写在 SKILL.md 顶部）

每个 `SKILL.md` **必须**以 YAML frontmatter 开头：

```yaml
---
name: skill-name
description: >
  一段描述，说明这个 Skill 做什么、什么时候触发。
  包含触发词和触发场景。
---
```

#### Description 编写原则（pushy 原则）

`description` 是 skill 触发的核心机制——agent 路由时优先读 description 匹配用户意图。**Claude 倾向于 undertrigger skills**（该用的时候不用），所以 description 要主动"推"：

1. **明确列全触发场景**：不要只写"做什么"，要写"什么时候用"——列出所有相关的用户意图关键词和上下文
2. **用"主动触发"措辞**：避免被动描述，用 "当用户提到 X / Y / Z 时使用此 skill" 的句式
3. **覆盖边界场景**：用户可能不会直接说"我要用 X skill"，但描述里要包含间接表达（如"整理 temp"应触发 temp_cleanup，description 里要有"temp 太乱"、"临时文件整理"等表述）
4. **中英文双语触发词**：本项目用户用中文，但 agent 可能匹配英文关键词，description 里中英文触发词都列

**反例**（不够 pushy）：
> 创建 HTML 工具页面的 skill。

**正例**（pushy）：
> 编写和调试 HTML 工具页面。当用户要求创建或修改 HTML 页面（如可视化 Dashboard、监控面板、审核页面、工具页面）时触发。当用户提到"写个页面"、"前端"、"Dashboard"、"可视化"时也使用此 skill。核心方法：浏览器控制 + 截图 + OCR 闭环验证。

> 注：本项目的 `agent_guide(task='...')` 路由不完全依赖 description 匹配（有 GUIDE_REGISTRY 硬编码），但优化 description 仍能提升触发准确率，尤其是新 skill 未注册到 GUIDE_REGISTRY 时的兜底匹配。

#### Skill 目录结构

一个五脏俱全的 Skill 长这样：

```
.agents/skills/<skill_name>/
├── SKILL.md            # 必需：何时用我 + 操作指引 + 坑点清单
├── scripts/            # 可选：现成的可执行脚本，agent 直接调用而非现场重写
│   └── collect.py
├── references/         # 可选：正文放不下的细节、映射表、API 参数说明
│   └── mapping.json
└── assets/             # 可选：输出模板（如发布报告格式）
    └── report_template.md
```

只有 `SKILL.md` 是必需的，`scripts/`/`references/`/`assets/` 按需添加。这些子文件**不会**一次性塞给 agent，而是 SKILL.md 正文里写明"需要 X 时读 scripts/collect.py"，agent 干到那一步才自己去取（渐进式披露）。

#### 行数与分层规范

**SKILL.md 软上限 500 行**：超过时 agent 加载成本上升、上下文污染加剧。接近上限时按以下策略分层：

1. **拆分到 references/**：详细的映射表、API 参数说明、长示例代码移到 `references/*.md`，SKILL.md 中用 "详见 references/xxx.md" 指向
2. **Domain organization（变体分文件）**：当 skill 支持多种引擎/框架/模式时，按变体分文件，agent 只读相关那一个：
   ```
   ocr/
   ├── SKILL.md          # 工作流 + 引擎选择决策树
   └── references/
       ├── paddleocr.md  # 经典 OCR 引擎细节
       └── qwen_vl.md    # 远程 VL 引擎细节
   ```
3. **大参考文件加 TOC**：`references/` 下单个文件 >300 行时，文件顶部加目录（`## 目录` + 锚点链接），方便 agent 定位
4. **scripts/ 封装确定性逻辑**：可执行的标准流程放 `scripts/*.py`，agent 调用而非现场重写，SKILL.md 只写"调用 scripts/xxx.py"

**反模式**：不要把所有细节塞进 SKILL.md 让 agent 一次性读完——那是 context 噪音。渐进式披露（progressive disclosure）是 skill 系统的核心设计。

`SKILL.md` 的正文结构：

```markdown
---
name: skill-name
description: >
  触发词和功能描述（写成"什么场景下用我"的触发条件，不是给人看的摘要）
---

# Skill 名称 (skill_id)

## 触发词
触发词1、触发词2、触发词3

## 概述
一句话描述这个 Skill 做什么。

## 前置条件
- 需要什么依赖（后端模块、浏览器状态等）
- 需要什么权限

## 工作流
1. 步骤1：具体操作 + 对应的 API/脚本（脚本能放 scripts/ 就放，别让 agent 现场重写）
2. 步骤2：...
3. 步骤3：...

## 坑点清单（Gotchas）
- 只写 agent 靠读代码推断不出来的信息（字段别名、环境差异、踩过的雷）
- 持续攒：每次 agent 栽进新坑就回头补一条

## 关键规则
- 必须遵守的约束（用"必须/绝不/禁止"，不用"建议"）
- 安全注意事项
- 异常处理策略

## 依赖
| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 后端接口 | POST /xxx | ... |
| 配套脚本 | scripts/yyy.py | ... |

## 输入/输出
- 输入：`workspace/<skill_name>/`（任务数据：账单、原始记录等用户数据）
- 输出：`workspace/<skill_name>/`（产出文件）
- 注意：用户数据放 `workspace/`，skill 专属脚本/参考放 skill 文件夹内，不要混
```

#### 编写要点

1. **工作流要具体**：每一步都写清楚"做什么 + 用什么工具/API"，不要写模糊的描述
2. **关键规则要硬性**：用"必须""绝不""禁止"等词，不用"建议""可以考虑"
3. **依赖表要完整**：列出所有用到的后端接口、工具脚本、配置文件
4. **输入输出要明确**：指定目录路径和文件格式
5. **坑点清单含金量最高**：只写 agent 推断不出来的信息（字段别名、环境差异、踩过的雷），删掉它本来就会的（如"写完代码要跑测试"）。每条 gotcha 都在为 agent 排掉一个它必然会踩的雷
6. **脚本能封装就封装**：skill 专属的可执行逻辑放 `scripts/`，让 agent 只管编排而非现场重写样板代码
7. **不陈述显而易见**：agent 本来就会的事不要写进 SKILL.md，那是纯噪音
8. **写作风格权衡（解释 why 优于 MUST）**：安全规则必须用"必须/绝不/禁止"硬约束（如"删除操作必须用户确认"），但工作流指引优先用"解释为什么这样做"而非堆砌 MUST——理解 why 的 agent 能在未覆盖的场景做出正确判断，只会照搬 MUST 的 agent 遇到新情况就僵在轨道上。用 theory of mind：写 skill 时假设 agent 是聪明的但缺上下文，给它理由而非只给命令

### 阶段 4：同步与注册

完成 Skill 文件后，执行以下同步操作：

1. **更新 `_index.md`**：在 `.agents/skills/_index.md` 中添加新条目，"Skill 文件"路径指向 `.agents/skills/<skill_name>/SKILL.md`
2. **更新 `GUIDE_REGISTRY`**：在 `server/agent_guide.py` 的 `GUIDE_REGISTRY` 中添加条目，`skill_file` 字段指向 `.agents/skills/<skill_name>/SKILL.md`（agent_guide 有路径自动解析：先查 `{name}.md`，不存在则查 `{name}/SKILL.md`，但建议显式写对路径）
3. **如果需要工具脚本**：在 `tools/` 下创建对应子目录（如 `tools/my_feature/`）
4. **如果需要后端路由**：在 `server/` 下创建模块，在 `main.py` 中注册

### 阶段 5：测试验证

手动测试新创建的 Skill：

1. **后端可用性**：确认依赖的后端接口正常工作（`/health` 检查）
2. **工作流可执行**：按 Skill 定义的工作流走一遍，确认每一步都能完成
3. **边界条件**：测试异常输入和错误场景
4. **触发词匹配**：确认触发词能正确匹配到这个 Skill

### 阶段 5.5：量化评估（evals，可选但强烈推荐）

手动测试（阶段 5）只能发现"能不能跑"，无法回答"好不好用"。对于有客观可验证输出的 skill（数据转换、代码生成、固定工作流），引入简化版 evals 机制：

#### evals/evals.json 结构

每个 skill 文件夹下可选 `evals/evals.json`，记录 2-5 个测试用例：

```json
{
  "skill_name": "my-skill",
  "evals": [
    {
      "id": 1,
      "name": "descriptive-name",
      "prompt": "用户的真实任务描述（像真人会说的话）",
      "expected_output": "期望结果的描述（用于人工对比）",
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
      ],
      "files": []
    }
  ]
}
```

#### 断言类型（assertions[].type）

| type | 说明 | 示例 |
|------|------|------|
| `contains` | 输出包含 target 字符串 | 检查关键字段 |
| `not_contains` | 输出不包含 target | 检查未做禁止操作 |
| `regex` | 输出匹配 target 正则 | 检查格式 |
| `file_exists` | target 路径文件存在 | 检查输出文件 |
| `json_path` | target 是 `path->value`，输出 JSON 的 path 等于 value | 检查结构化输出 |

#### 编写要点

1. **prompt 要像真人说的话**：不要写"测试 skill 的 X 功能"，写"帮我整理 temp 目录"（真实用户语气）
2. **断言要客观可验证**：避免"输出质量高"这种主观判断，用"包含 X 字段""未触发 Y 操作"
3. **覆盖关键路径**：2-5 个用例覆盖正常流程 + 1 个边界场景（如空输入、异常数据）
4. **主观输出不强求 evals**：写作风格、设计美观等主观 skill 不需要 evals，靠人工评审

#### 运行方式（简化版）

本项目暂不引入 anthropic 的 eval-viewer/benchmark/analyst 重型设施。evals 的使用方式：

1. **新增/修改 skill 时**：agent 读取 `evals/evals.json`，按 prompt 执行 skill，对照 assertions 自检
2. **回归测试**：用户说"测试 X skill"时，agent 跑 evals 并报告通过率
3. **迭代依据**：evals 失败的用例指向 skill 需要优化的具体环节

> 未来若需要量化对比（with_skill vs without_skill、新旧版本对比），可参考 anthropic skill-creator 的完整 evals 实现（含 benchmark.json / eval-viewer / analyst pass），路径见 `workspace/dev_toolkit/skill_evals/`。

### 阶段 6：迭代优化

根据测试结果优化 Skill：

- 工作流步骤缺失 → 补充
- 规则不够明确 → 细化
- 依赖遗漏 → 添加
- 触发词不够 → 扩充

每次修改后都要更新 `_index.md`。

## 修改已有 Skill

当用户要求修改或优化已有 Skill 时：

1. **读取现有 Skill**：从 `.agents/skills/` 目录读取当前版本
2. **明确修改意图**：用户想改什么？为什么改？
3. **评估影响范围**：修改是否影响 `_index.md`、工具脚本、后端接口？
4. **执行修改**：只改需要改的部分，不要重写整个文件
5. **更新 `_index.md`**：如果修改影响了 Skill 的触发词、依赖或接口
6. **验证**：确认修改后的 Skill 仍然可用

## 常见 Skill 类型与模板

### 类型 1：数据处理类（输入→转换→输出）

典型模式：读取文件 → 处理转换 → 输出结果

适用：记账、数据清洗、格式转换

关键要素：
- 输入文件格式和目录
- 处理规则和映射表
- 输出格式和目录
- 增量更新策略

### 类型 2：信息采集类（浏览→提取→整理）

典型模式：打开页面 → 提取信息 → 整理汇总

适用：面经筛选、社群帖子、抽奖整理

关键要素：
- 数据源和访问方式
- 提取规则（CSS 选择器、OCR、API）
- 停止条件（翻到哪停）
- 去重和增量策略

### 类型 3：屏幕操控类（截图→理解→操作→验证）

典型模式：截图 → 视觉AI解析 → 决策 → 执行操作 → 验证

适用：游戏抽卡记录、自动操作、UI 自动化

关键要素：
- 窗口定位方式
- 截图→解析→操作的循环
- 安全确认机制
- 错误恢复策略

### 类型 4：工具服务类（API 封装）

典型模式：接收请求 → 调用后端接口 → 返回结果

适用：OCR 识别、LLM 对话、代码执行

关键要素：
- 接口选择逻辑
- 参数映射
- 结果格式化
- 模型管理策略

## 反模式（不要这样做）

1. **不要只写一个扁平 .md**：Skill 是文件夹（`SKILL.md + scripts/ + references/ + assets/`），不是单个 md 文件。只写一份 md 等于只用了机制十分之一的能力
2. **不要写教程**：Skill 不是教程，是工作流指令。不要写"你可以这样做"，要写"这样做"
3. **不要模糊描述**：不要写"适当处理错误"，要写"出错时重试 3 次，仍失败则跳过并记录"
4. **不要陈述显而易见**：agent 本来就会的事（如"写完代码跑测试"）不要写，那是 context 噪音。只写能把它推离默认思路的信息
5. **不要把步骤锁死**：给足信息，但把怎么走的自由留给 agent，避免它遇到指令没覆盖的情况就僵在轨道上
6. **不要遗漏安全规则**：涉及删除/支付/关机等操作，必须写明确认流程
7. **不要硬编码路径**：skill 专属脚本/参考放 skill 文件夹内，用户数据放 `workspace/<task_name>/`，通用工具放 `tools/`，临时脚本放 `temp/`
8. **不要忘记更新索引**：每次修改 Skill 后都必须更新 `.agents/skills/_index.md` 和 `server/agent_guide.py` 的 `GUIDE_REGISTRY`
9. **description 不要不够 pushy**：description 是 skill 触发的核心机制，写得过于克制会导致 agent 该用时不用（undertrigger）。必须列全触发场景和关键词，用"当用户提到 X 时使用"的主动句式（详见"Description 编写原则"）
10. **不要 SKILL.md 过长**：超过 500 行时 agent 加载成本上升、上下文污染加剧。按"行数与分层规范"拆分到 references/scripts/，渐进式披露优于一次性塞满

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| Skill 目录 | `.agents/skills/` | Skill 定义文件 |
| Skill 索引 | `.agents/skills/_index.md` | 所有 Skill 的索引 |
| 项目指引 | `AGENTS.md` | 项目结构和规范 |
| 变更清单 | `.trae/rules/project_rules.md` | 功能变更检查清单 |

## 输入/输出

- **输入**：用户的口头描述（意图 + 需求）
- **输出**：`.agents/skills/xxx/SKILL.md`（+ 可选 `scripts/`/`references/`/`assets/`）+ 更新 `_index.md` + 更新 `GUIDE_REGISTRY`
