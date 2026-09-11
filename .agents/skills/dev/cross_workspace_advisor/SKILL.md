---
name: cross_workspace_advisor
description: >
  工作区决策树：当用户在 LocalAgent 工作区提出与本项目无关的请求时（独立项目级开发 /
  生活临时任务 / 其他工作），按决策树引导选择合适的工作区位置（新项目文件夹 / temp/ /
  workspace/），建立后询问是否协助打开，完成时明确工作区位置。当用户说"开发一个网站"
  "做个 App""帮我整理一下生活记账""写个临时脚本算一下账""做个日程表"等与 LocalAgent
  无关的请求时使用。触发词：开发网站、开发游戏、写后端、做 App、跨工作区开发、无关开发、
  新项目开发、生活任务、临时任务、工作区决策、其他工作、记账、日程、临时脚本。
task_type: dev.cross_workspace_advisor
---

# Cross Workspace Advisor · 工作区决策树

当用户在 LocalAgent 工作区提出**非本项目维护**的请求时，按决策树引导选择合适的工作区位置，
建立后询问是否协助打开，完成时明确工作区位置，严防用户找不到产物。

## 决策树

```
用户提出请求
│
├─ 第一层：是本项目维护/功能开发吗？
│   是（OCR/浏览器/屏幕操控/记忆/MCP/skill 系统/client/server/lib 等模块）
│   → 不走本 skill，正常处理
│
└─ 否 → 进入第二层：请求类型分支
    │
    ├─ 分支 A：独立项目级开发（4 项同时满足）
    │   → 新开项目文件夹 + 复制 dev_toolkit + 重开对话
    │
    └─ 分支 B：生活/其他工作（不满足分支 A 第 4 项）
        → 判断临时性 → temp/ 或 workspace/
```

### 第一层判断：是否本项目维护

**是本项目维护**（不走本 skill）的判断依据：请求涉及 LocalAgent 的功能模块或代码库
（OCR / 浏览器 / 屏幕操控 / 记忆系统 / MCP / skill 系统 / client / server / lib / tests /
tools / workspace 下的已有子项目如 maa-patch/disk_manager 等）。

**非本项目维护** → 进入第二层。

### 第二层：请求类型分支

#### 分支 A：独立项目级开发（新开发任务）

4 项**全部满足**才走分支 A：

1. **不是项目相关要求**：请求的内容与 LocalAgent 的功能模块无关
2. **不使用项目工具**：请求不需要使用本项目的后端能力（exec_python/browser/OCR/screen 等）
3. **不是小的工具开发**：不是整理文件、操作文件、写个临时脚本之类的小任务
4. **是独立项目级开发**：例如开发一个网站后端、开发一个游戏、做一个 App、写一个独立服务

**典型场景**：
- "帮我开发一个电商网站后端"
- "我想做一个 2D 游戏"
- "写一个 Discord bot"
- "开发一个 React 前端项目"

→ 走「分支 A 流程」

#### 分支 B：生活/其他工作

不满足分支 A 第 4 项（不是独立项目级开发），但也不是本项目维护。例如：
- 生活琐事（记账 / 日程 / 购物清单 / 健身记录）
- 临时数据分析（算一下账 / 整理一下表 / 排个班）
- 一次性文档生成（写份报告 / 做个简历 / 排个版）
- 临时脚本工具（批量改文件名 / 算个公式 / 转个格式）

→ 走「分支 B 流程」

**不触发的场景**（直接做，不走本 skill）：
- "帮我整理一下 workspace 下的文件" → 小工具任务，直接做
- "给 LocalAgent 加个新 skill" → 项目相关，走 goal_engineering
- "用 exec_python 跑个数据分析脚本" → 用项目工具，直接做

---

## 分支 A 流程（独立项目级开发 → 新项目文件夹）

### A1. 识别并提示

确认用户请求满足 4 项条件后，用 AskUserQuestion 提示用户：

> 检测到您要进行的开发（<具体内容>）与 LocalAgent 项目无关，不适合在此工作区进行。
>
> 原因：
> - 此工作区（LocalAgent）是个人 AI Agent 项目，有其自己的 AGENTS.md、skill 系统和工具链
> - 在此工作区开发无关项目会导致：文件混杂、AGENTS.md 规则不匹配、项目结构漂移、skill 路由混乱
> - 建议在独立工作区进行开发

提供选项：
- ①「引导我建立新工作区（推荐）」—— 复制 dev_toolkit 工具包到指定位置，建立基础工作区
- ②「就在这里做，我不管」—— 放行但提示风险（文件混杂、规则不匹配）
- ③「取消」—— 不做了

### A2. 询问工作区位置

用户选①后，用 AskUserQuestion 询问：

> 新工作区要建在哪里？
>
> 请提供目标路径（如 `D:\projects\my-website` 或 `F:\dev\my-game`）

提供选项：
- ①「我来指定路径」—— 用户提供具体路径
- ②「建在 <project_root> 同级目录下」—— 与 LocalAgent 同级
- ③「建在 D:\ 下」—— 独立位置

### A3. 复制 dev_toolkit 并建立基础工作区

用户指定路径后，执行以下操作：

**A3.1 创建目录结构**
```
<目标路径>/
├── .agents/
│   ├── rules/
│   │   ├── coding_principles.md  （从 dev_toolkit 复制）
│   │   └── skill_design.md       （从 dev_toolkit 复制）
│   ├── skills/
│   │   ├── _index.md             （从 dev_toolkit 复制）
│   │   ├── grill-me/             （从 dev_toolkit 复制）
│   │   ├── spec/                 （从 dev_toolkit 复制，含 Harness 融合）
│   │   ├── plan/                 （从 dev_toolkit 复制）
│   │   ├── tasks/                （从 dev_toolkit 复制）
│   │   ├── implement/            （从 dev_toolkit 复制）
│   │   ├── anti-hallucination/   （从 dev_toolkit 复制）
│   │   ├── verify/               （从 dev_toolkit 复制）
│   │   ├── neat-freak/           （从 dev_toolkit 复制）
│   │   ├── wip-tracker/          （从 dev_toolkit 复制）
│   │   ├── skill-creator/        （从 dev_toolkit 复制）
│   │   └── dev/                  （从 dev_toolkit 复制，含 leader + goal_engineering）
│   ├── memory/                   （空目录 + .gitkeep）
│   ├── specs/                    （空目录 + .gitkeep）
│   └── wip/                      （空目录 + .gitkeep）
├── temp/                         （空目录 + .gitkeep）
├── AGENTS.md                     （从 dev_toolkit 复制模板，用户需替换 {{...}} 占位符）
├── checklist.md                  （从 dev_toolkit 复制）
└── README.md                     （从 dev_toolkit 复制，用户需自定义）
```

**A3.2 用 exec_python 执行复制**
```python
import shutil
from pathlib import Path

source = Path(r"<project_root>\workspace\dev_toolkit")
target = Path(r"<用户指定的路径>")

# 复制 .agents/ 目录
if (target / ".agents").exists():
    shutil.rmtree(target / ".agents")
shutil.copytree(source / ".agents", target / ".agents",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

# 复制顶层文件
for f in ["AGENTS.md", "checklist.md", "README.md"]:
    shutil.copy2(source / f, target / f)

# 创建空目录
for d in ["temp", ".agents/memory", ".agents/specs", ".agents/wip"]:
    (target / d).mkdir(parents=True, exist_ok=True)
    (target / d / ".gitkeep").touch()

print(f"工作区已建立：{target}")
```

**A3.3 提示用户替换占位符**
AGENTS.md 中有 `{{...}}` 占位符（项目定位、技术栈、关键约束等），提示用户根据新项目情况替换。

### A4. 告知用户重开对话

工作区建立完成后，明确告知用户：

> 新工作区已建立在 `<路径>`。
>
> **下一步**：
> 1. 在 IDE 中打开 `<路径>` 作为工作区
> 2. 编辑 `AGENTS.md`，替换 `{{...}}` 占位符为新项目信息
> 3. 在新工作区重新开始对话，agent 会读取新的 AGENTS.md 和 skill 系统
> 4. 开始开发时，agent 会自动引导走 goal_engineering 流程
>
> **不要**在当前 LocalAgent 工作区继续这个开发任务。

分支 A 不走「通用收尾」（因为要重开对话，当前工作区不继续任务）。

---

## 分支 B 流程（生活/其他工作 → temp/ 或 workspace/）

### B1. 自主判断临时性

**先尝试自主判断**，减少打扰用户。判断标准（满足任一即为**临时**）：

- **一次就结束**：任务产出是一次性的，用完即弃（如"算一下这个月的账""生成一份文档""排一下明天的会议"）
- **一天以内失效**：任务产出在 24 小时内失去意义（如"明天的提醒""今天的临时待办"）

**持久**的判断标准（满足任一即为持久）：

- **反复使用**：产出会反复查阅/更新（如"生活记账工具""健身记录表""项目进度看板"）
- **作为项目插件**：要长期集成到 LocalAgent 的 workspace 下（如"写个辅助脚本""做个 maa-patch 工具"）

判断结果：
- 能明确判断为**临时** → B2（temp/）
- 能明确判断为**持久** → B3（workspace/）
- **判断不了** → B4（询问用户）

### B2. 临时任务 → temp/ 新建文件夹

- **路径**：`temp/<task-slug>/`（如 `temp/monthly-account-202608/`）
- **不复制 dev_toolkit**：临时任务不需要完整 skill 系统，轻量起步
- **建文件夹后** → 走「通用收尾」

### B3. 持久任务/项目插件 → workspace/ 新开文件夹

- **路径**：`workspace/<task-slug>/`（如 `workspace/life-tracker/`、`workspace/gh-helper/`）
- **不自动复制 dev_toolkit**：workspace/ 下已有的子项目（maa-patch/disk_manager）都是
  独立维护自己的脚本和 README，不带完整 skill 系统。若任务复杂需走 SDD 流程，agent 可按需复制
  dev_toolkit 的子集（grill-me / spec / tasks / implement）
- **建文件夹后** → 走「通用收尾」

### B4. 不能判断 → 显式询问用户

用 AskUserQuestion 询问，**必须给出明确判断标准**：

> 这个任务的产出是一次性的还是会反复使用？
>
> **判断标准**：
> - **临时**：产出用完即弃，或一天以内失效（如算一次账、生成一份临时文档、明天的提醒）
> - **持久**：产出会反复使用，或要作为 LocalAgent 的插件长期存在（如生活记账工具、定期数据看板、项目辅助脚本）
>
> 临时任务建在 `temp/`（用完可清理），持久任务建在 `workspace/`（长期保留）。

提供选项：
- ①「临时」→ B2（temp/）
- ②「持久」→ B3（workspace/）

---

## 通用收尾（分支 B 所有路径都要执行）

### C1. 建立工作区后、未开始任务前

工作区文件夹建好后，**未开始实际任务前**，用 AskUserQuestion 询问：

> 工作区已建立在 `<绝对路径>`。
>
> 是否需要我协助打开工作区以便使用？

提供选项：
- ①「帮我打开文件夹」—— 用 `exec_python` 调 `os.startfile(<路径>)` 打开资源管理器
- ②「不用，我自己开」—— 直接开始任务

### C2. 任务完成时汇总

任务完成后的汇总报告/回复中，**必须明确标注工作区位置**：

> ✅ 任务完成。
>
> **工作区位置**：`<绝对路径>`
> - 临时任务：`temp/<task-slug>/` 下，可随时清理（会被 system.temp_cleanup 管理）
> - 持久任务：`workspace/<task-slug>/` 下，长期保留
>
> 严防用户找不到产物。

---

## 关键规则

- **决策树第一层先判本项目维护**：是本项目维护就不走本 skill，避免误路由
- **分支 A 严守 4 项条件**：独立项目级开发才走新项目文件夹流程，不要把生活临时任务误导到新项目
- **分支 B 先自主判断**：能判断就不问用户，减少打扰；判断不了再问，问时必须给明确判断标准
- **不跨区操作**：绝不在 LocalAgent 工作区写另一项目的代码，即使只是"先写个原型"
- **dev_toolkit 是复制源**：分支 A 复制后新工作区独立维护；分支 B 默认不复制，按需取子集
- **必须告知工作区位置**：分支 B 建立后询问是否打开，完成时汇总明确路径，严防用户找不到
- **temp/ 可清理**：临时任务产物在 `temp/` 下，会被 `system.temp_cleanup` 管理
- **workspace/ 长期保留**：持久任务产物在 `workspace/` 下，不会自动清理

## 与其他 skill 的关系

- **上游**：GeneralGuide 的 dev_entry_points 决策树把 `cross_workspace_dev` 指向本 skill
- **互补**：`dev.goal_engineering` 处理本项目内的开发任务；本 skill 处理不属于本项目的请求
  （含独立项目级开发 + 生活/其他工作）
- **工具源**：`workspace/dev_toolkit/` 是复制到新工作区的工具包源（仅分支 A 用）
- **清理**：`system.temp_cleanup` 负责清理 `temp/` 下的临时任务产物
