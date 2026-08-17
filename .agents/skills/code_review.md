---
name: code_review
description: >
  代码审查：全量/定向/diff-based 双轴审查。当用户说"code review"、"代码审查"、"review 一下"、
  "审查 PR"、"review since X"时使用。支持全量项目分批次审查、单文件/模块/PR 审查、
  以及基于 git diff 的 Standards+Spec 双轴审查（含 Fowler 12 smells baseline）。
  触发词：code review、代码审查、review、审查、PR review、review since、双轴审查。
task_type: dev.code_review
---

# Code Review Skill

> 对本项目（LocalAgent）进行全量或定向代码审查的标准化工作流。
> 触发：用户说"code review"/"代码审查"/"review 一下" + 可选范围（全量/某模块/某文件）。

## 适用范围

- **全量 review**：对整个项目分批次审查（适合周期性质量审计）
- **定向 review**：对单文件、单模块、单 PR/MR 审查
- **diff-based 双轴审查**：对 commit/branch/tag 范围内的变更做 Standards + Spec 双轴审查（适合 PR review、变更审计；借鉴 mattpocock/skills code-review）
- **不适用**：纯文档/配置文件（`.md`/`.json`/`.txt`）、UI 样式代码（CSS 数值）

## 工作流（6 步）

### Step 1: 确定范围与批次

- 全量 review 按"分层 + 风险优先"分批，参考批次划分：
  1. 安全层（command_guard / approval / http_guard / mcp_whitelist）
  2. 入口与生命周期（main.py / core/）
  3. 能力端点（ocr / vision / remote_vl / agent / mindforge）
  4. 执行端点（exec / browser / screen/）
  5. 数据与状态（memory/ / todos/ / inbox / apikey / llm_pool/）
  6. Agent 编排（agent_guide / advanced / templates / loop_*）
  7. 辅助模块（config / constants / route_tags / system / project_structure）
  8. 客户端（client/core + client/panels + client/widgets）
- 定向 review 跳过批次划分，直接进入 Step 2

### Step 2: 持久化准备（长任务必做）

如果是跨批次的 review，先创建持久化文件，避免上下文中断丢失进度：

- `temp/code_review_plan.md` — 计划 + 进度（✅/⏳/⏸ 状态标记）
- `temp/code_review_findings.md` — 已发现问题（按批次分节，表格形式）
- 每完成一个批次，更新 plan 文件的状态和 findings 文件的对应章节

### Step 3: 上下文收集

- 并行 Read 批次内所有文件（不超过 5 个并行）
- 长文件按需分页读取
- 必要时用 Grep/SearchCodebase 查找跨文件调用关系
- 必要时 Read 相关的 AGENTS.md / project_rules.md / docs/ 了解项目约定

### Step 4: 推断作者意图

分析代码整体模式，推断作者意图（如"增加错误处理"、"重构提升可读性"、"修复 off-by-one"）。
意图作为后续问题分析的上下文，避免脱离设计语境的苛责。

### Step 5: 扫描问题（按 7 类检查清单）

#### 5.1 正确性
- 边界条件（空值、零、负数、溢出）
- 异常处理是否完备（不吞异常、不暴露内部细节）
- 资源泄漏（文件未关闭、进程未回收、锁未释放）
- 并发安全（共享可变状态、SQLite 跨线程、async 中阻塞调用）

#### 5.2 安全性
- 输入验证（路径遍历、SQL 注入、命令注入）
- 权限校验（敏感端点是否需要 token/审批）
- 敏感信息泄露（错误消息、日志、响应体）
- 加密/签名（密码、token、webhook）

#### 5.3 可维护性
- 函数过长（>80 行考虑拆分）
- 圈复杂度过高（嵌套 >3 层）
- 重复代码（DRY）
- 死代码（未调用的函数/变量/注册表）

#### 5.4 性能
- async 函数中同步阻塞调用（urllib / requests / time.sleep）
- N+1 查询、循环中重复计算
- 大对象无限制加载到内存（base64 / 全量响应体）
- 缓存策略（TTL / 失效 / 并发）

#### 5.5 类型与契约
- 类型注解错误（`str = None` 应为 `str | None = None`）
- Pydantic 模型字段缺失/类型不一致
- 函数签名与调用方不匹配
- 返回值结构不稳定

#### 5.6 项目约定一致性（参考 project_rules.md）
- 硬编码路径/端口/密钥 → 应移入 config.toml
- 相对路径 → 应基于 `__file__` 或项目根
- 新增路由是否在 main.py 注册
- 新增端点是否在 /health 反映
- CHANGELOG 是否需要更新

#### 5.7 依赖与架构
- 循环依赖（模块间函数内 import 是技术债）
- 单一职责（模块/函数职责是否清晰）
- 启动顺序敏感性（是否有显式注释）
- 弃用 API（如 FastAPI on_event、Python deprecated 模块）

#### 5.8 ADR 触发评估（防"事后补 ADR"）

每个 issue 修复前/修复中都要评估：**这个修改是否应该写 ADR**？

判断标准真源：[`.agents/skills/dev/domain-modeling/ADR-FORMAT.md`](../../dev/domain-modeling/ADR-FORMAT.md) 的三项必须全部成立：
1. Hard to reverse（当前状态，非"未来可能"）
2. Surprising without context
3. Real trade-off

**关键反模式**（详见 ADR-FORMAT.md "What does NOT qualify" 段）：

- **Bug fix / 安全补丁 / 性能优化**：恢复正确行为不算 trade-off
- **错误码细分 / 字段语义显式化**：修复语义不清不是 trade-off
- **内存泄漏修复 / asyncio task GC**：Python 文档明确的正确行为
- **可逆代码层调整**：list → deque、`list.append` → 封装方法
- **"未来可能 hard to reverse"**：当前没消费方就不算
- **单路径防御 / 单点校验**：本身就是行为不一致的代码味道

**评估时机**：
- 修复 issue 时若涉及**设计哲学冲突**（如"UIA 不受焦点漂移影响"vs"加焦点校验"）→ 暂停，先评估 ADR 三项
- 修复 issue 时若涉及**跨模块语义变更**（如新增字段被多个消费方读取）→ 暂停，先评估 ADR 三项
- 修复 issue 时若**只在某一路径加防御而不在同质路径加**→ 不要写 ADR，**撤销修改**（行为不一致应消除而非文档化）

**评估结果**：
- 3/3 通过 → 在 fix 后追加"建议写 ADR-NNNN"，告知用户
- 2/3 通过 → **优先考虑撤销修改或调整设计**，不要"为了写 ADR 而写 ADR"
- <2/3 通过 → CHANGELOG 一行足矣，不写 ADR

**边缘案例尤其危险**：2/3 通过往往意味着设计本身有矛盾。与其文档化矛盾，不如消除矛盾。

详细评估流程和真实 case 见 [ADR-FORMAT.md "如何评估" 章节](../../dev/domain-modeling/ADR-FORMAT.md#如何评估)。

### Step 6: 输出格式

每个批次输出包含：

1. **作者意图**（1-2 句话总结）
2. **架构流 mermaid 图**（业务流 + 技术流，最多 2 张）
3. **问题表格**：

```
| No. | Issue Title | Suggestion | Code Link |
|-----|-------------|------------|-----------|
| 1   | 简述问题     | 改进建议    | [file:line](file:///path#L123-L145) |
```

4. Code Link 用 markdown 链接格式 `file:///` + `#L123-L145`，行号范围 ≤ 100 行
5. 同时把问题表格追加到 `temp/code_review_findings.md`

## diff-based 双轴审查流程（借鉴 mattpocock code-review）

当用户说"review since X"、"审查这个 PR"、"看看从 main 分出去的变更"、"review 一下最近这几个 commit"时，进入此模式，而不是全量/定向模式。

### D1. Pin the fixed point

用户说的任何内容都是 fixed point：commit SHA、branch name、tag、`main`、`HEAD~5` 等。如果用户没有指定，就询问。

捕获 diff command：`git diff <fixed-point>...HEAD`（three-dot，比较对象是 merge-base）。同时用 `git log <fixed-point>..HEAD --oneline` 记录 commits 列表。

继续前，确认 fixed point 能解析（`git rev-parse <fixed-point>`），并且 diff 非空。错误 ref 或空 diff 应在这里失败，而不是进入审查后才失败。

### D2. Identify the spec source

按以下顺序寻找来源 spec：

1. Commit messages 中的 issue references（`#123`、`Closes #45` 等）— 若项目用 wip_tasks 跟踪，查询 `wip_list(type="ticket"|"spec")` 匹配标题/关联
2. 用户作为 argument 传入的 path
3. `workspace/specs/` 或 `docs/` 下与 branch name 或 feature 匹配的 spec/PRD 文件
4. 如果什么都找不到，询问用户 spec 在哪里。如果用户说没有 spec，**Spec 轴**跳过并报告 "no spec available"

### D3. Identify the standards sources

Repo 中任何记录代码应该如何写的内容：

- `.trae/rules/project_rules.md`（功能变更检查清单）
- `AGENTS.md`（项目大纲与约定）
- `server/agent_guide.py` 的 `GUIDE_REGISTRY`（task_type 命名规范）
- 本 skill 的"项目特定关注点"章节

在 repo 自己记录的 standards 之外，Standards 轴始终带有下面的 **smell baseline**（Fowler _Refactoring_ 第 3 章 12 smells）。两条规则：

- **The repo overrides.** 已记录的 repo standard 永远优先；如果它认可 baseline 会标记的东西，就压制该 smell。
- **Always a judgement call.** 每个 smell 都是带 label 的 heuristic，不是硬性违规；跳过 tooling 已经强制检查的内容。

每个 smell 按 _what it is_ → _how to fix_ 读取，并对照 diff：

- **Mysterious Name** — function、variable 或 type 的名称没有说明它做什么或装什么。→ rename it；如果找不到诚实名称，设计本身可能浑浊。
- **Duplicated Code** — 同一 logic shape 出现在多个 hunk 或 file 中。→ 抽出共享形状，让两边调用。
- **Feature Envy** — method 访问另一个 object 的 data 多于自己的 data。→ 把 method 移到它羡慕的数据上。
- **Data Clumps** — 同几组 fields 或 params 总是一起出现。→ 包成一个 type 来传。
- **Primitive Obsession** — primitive 或 string 代替了值得拥有自有 type 的 domain concept。→ 给该 concept 一个小 type。
- **Repeated Switches** — 对同一 type 的相同 `switch`/`if` cascade 在改动中重复。→ 换成 polymorphism，或共享一个 map。
- **Shotgun Surgery** — 一个 logical change 迫使 diff 分散修改很多文件。→ 把一起变化的东西收拢进一个 module。
- **Divergent Change** — 一个 file 或 module 因多个无关原因被修改。→ 拆分，让每个 module 只因一个原因变化。
- **Speculative Generality** — 为 spec 没有的需求增加 abstraction、params 或 hooks。→ 删除它，inline 回来，直到有真实需要。
- **Message Chains** — caller 不该依赖的长链式导航 `a.b().c().d()`。→ 把这段导航藏到第一个 object 的一个 method 后面。
- **Middle Man** — class 或 function 基本只是在继续委托。→ 删掉它，直接调用真实目标。
- **Refused Bequest** — subclass 或 implementer 忽略或 override 了继承来的大部分内容。→ 去掉 inheritance，使用 composition。

### D4. 双轴审查

如果上下文允许并行（subagent 可用），Standards 与 Spec 两轴作为并行 sub-agents 运行，避免互相污染 context；否则顺序执行。

**Standards 轴**：对照 D3 的 standards-source files 与 smell baseline，报告 (a) 每处 diff 违反已记录 standard 的地方：cite standard（file + rule）；(b) 任何 baseline smell：命名它并引用 hunk。区分硬违规与 judgement call — documented-standard breach 可以是 hard，baseline smell 永远是 judgement call，documented repo standard 优先于 baseline。跳过 tooling 强制检查的内容。

**Spec 轴**：报告 (a) spec 要求但 diff 缺失或部分实现的； (b) diff 中 spec 没要求的 behavior（scope creep）； (c) 看起来已实现但实现有误的要求。每条 finding 引用 spec 行。

### D5. Aggregate

在 `## Standards` 和 `## Spec` 两个 heading 下分别展示两轴 reports。**不要**合并或重新排序 findings；两轴刻意保持分离——一个变更可能 Standards pass 但 Spec fail，反之亦然，分离避免一轴掩盖另一轴。

最后用一行总结：每轴 findings 总数，以及每轴内最严重的问题（如果有）。不要跨轴选总冠军。

## 评论准则（必读）

### 必须报告
- 高优：正确性 bug、安全漏洞、资源泄漏、数据丢失风险
- 中优：性能阻塞、循环依赖、函数过长、死代码
- 低优：类型注解错误、命名不一致、注释过时

### 禁止报告
- 纯描述性评论（"这里用了 if/else"、"这改进了 X"）
- 赞美性评论（"很好"、"不错"）
- 不基于证据的猜测（"可能"、"也许"、"你可以检查一下"）
- UI 样式数值（字体大小、间距、颜色）— 默认用户已确认
- 静态类型语言已编译通过的类型问题（除非有证据）
- 代码风格（缩进、引号风格）— 除非项目有明确规范

### 谨慎报告
- 注释清晰度（除非有重大误导）
- 标识符拼写（除非是新引入且与定义不一致）
- 大功能删除（默认用户是有意的）

## 项目特定关注点（LocalAgent）

### 高频踩坑（来自 AGENTS.md 和 project_rules.md）
- **相对路径**：`Path("data/xxx")` 在 cwd 错位时写错位置，应基于 `__file__`
- **硬编码**：路径/端口/密钥应移入 `config.toml`
- **base64 撑爆上下文**：截图/文件不得返回 base64 到 LLM，应用 `format=path` 或 `inline`
- **PowerShell curl 陷阱**：`curl -s` 在 PowerShell 中会卡住（curl 是 Invoke-WebRequest 别名）
- **`&&` 不支持**：Windows PowerShell 5.1 不支持 `&&`，用 `;` 或 `$LASTEXITCODE`
- **SQLite 跨线程**：需 `check_same_thread=False` + 写锁
- **on_show() 性能**：客户端面板切换不能做重活，需 mtime 检测/懒加载/异步加载
- **disk_cleanup 强制审核**：删除任何文件前必须先列清单给用户审核

### 项目架构层次（review 时按层判断）
- 入口层：main.py + core/ — 启动顺序敏感
- 业务层：server/ 下 30+ 平铺模块 — 按"安全/能力/执行/数据/编排/辅助"分类
- 客户端：client/ PySide6 面板式架构
- 工作区：workspace/ 任务级数据 + 脚本混放
- 文档：.agents/skills/ + docs/ — 与代码同步是关键约束

### 历史包袱预判（来自 .agents/wip/）
- main.py 曾拆分过（main_py_split.md）
- screen 模块曾重构（screen_module_refactor.md）
- module_refactor.md 有整体改造方案
- 部分模块可能仍有遗留的旧字段/兼容代码

## 恢复指引

如果 review 跨会话中断，新会话恢复步骤：

1. Read `temp/code_review_plan.md` 了解进度
2. Read `temp/code_review_findings.md` 了解已发现问题
3. Read 本文件（`.agents/skills/code_review.md`）了解方法论
4. 从下一个未完成批次继续（用 TodoWrite 重建 todo list）
5. 每完成一个批次，更新 plan 和 findings 文件
6. 全部完成后输出汇总报告（共性问题 / Top 问题 / 改进建议）

## 输出语言

- 所有输出使用中文（用户偏好）
- 代码链接用英文路径
- 代码块用 markdown 标准格式
- mermaid 图节点文字可用中文，颜色需指定 fill + color 保证深浅主题可读
