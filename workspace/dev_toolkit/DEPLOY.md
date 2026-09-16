# DEPLOY.md — dev_toolkit 部署指南

> 部署 dev_toolkit 到新项目时的操作手册。
> Agent 读完本文件后应能独立完成 toolkit 的初始化适配。

## 这是什么

dev_toolkit 是一套 AI Agent 协作工具包，包含：

- **AGENTS.md** — Agent 协作指引模板（IDE 自动注入上下文）
- **checklist.md** — 功能变更检查清单
- **.agents/skills/** — 23 个 skill 蓝图（SDD 链 + 安全网 + 验证调试 + 元 skill + 工程化开发 + UI/UX）
- **.agents/rules/** — 编码准则 + skill 设计规范
- **参考文档** — `html_toolkit.md` / `mcp_builder.md` / `webapp_testing.md` / `skill_creator.md` / `project_essentials.md` / `team_playbook.md`

**本质**：参考蓝图，不是运行代码。部署后必须按项目实际情况裁剪。

## 部署 6 步流程

### 第 1 步：放置文件

把 toolkit 文件放入项目根目录：

```
你的项目/
├── AGENTS.md          ← 替换或新建
├── DEPLOY.md          ← 本文件（部署后可删）
├── checklist.md
├── README.md          ← toolkit 的 README，可选保留或替换为项目自己的
├── .agents/
│   ├── rules/
│   ├── skills/
│   ├── memory/
│   ├── specs/
│   └── wip/
├── temp/              ← 创建空目录，gitignore
└── ...（项目自身文件）
```

### 第 2 步：填写占位符（核心）

在 AGENTS.md 中搜索 `TODO_DEPLOY`，逐个替换为项目实际内容：

| 占位符位置 | 需要填写的内容 | 示例 |
|-----------|--------------|------|
| 项目定位 | 一句话描述项目做什么 | "一个基于 React 的在线商城" |
| 技术栈 > 语言 | 项目主语言 | TypeScript |
| 技术栈 > 框架/引擎 | 主框架 | Next.js 14 |
| 技术栈 > 包管理 | 包管理器 | pnpm |
| 技术栈 > 版本控制 | 是否需要 LFS | Git（无大文件，不需 LFS） |
| 技术栈 > 测试 | 测试框架 | Vitest + Playwright |
| 技术栈 > CI | CI 工具 | GitHub Actions |
| 关键约束 1-2 | 项目特有的技术约束 | "构建前必须运行 prisma generate" |
| 项目结构 | 实际目录结构 | 替换为项目真实目录树 |
| 测试 | 测试命令 | "pnpm test" / "pnpm test -- <file>" |

**填写完成后，删除所有 `<!-- TODO_DEPLOY -->` 注释行。**

### 第 3 步：裁剪 skill

23 个 skill 不是每个项目都全需要。按项目类型裁剪：

| 项目类型 | 保留 skill | 可删 skill |
|---------|-----------|-----------|
| **纯 Web 应用（SPA/Dashboard）** | 全部 SDD 链 + 安全网 + 验证调试 + 元 skill + dev/prototype + dev/tdd + dev/codebase-design + dev/domain-modeling + dev/resolving-merge-conflicts + dev/leader + dev/goal_engineering + ui-ux-pro-max + dev/impeccable + dev/hallmark | 无 |
| **纯后端 API（无 UI）** | SDD 链 + 安全网 + 验证调试 + 元 skill + dev/prototype + dev/tdd + dev/codebase-design + dev/domain-modeling + dev/resolving-merge-conflicts + dev/leader + dev/goal_engineering | ui-ux-pro-max、dev/impeccable、dev/hallmark |
| **CLI 工具 / 数据脚本** | SDD 链 + 安全网 + 验证调试 + 元 skill + dev/leader + dev/goal_engineering + dev/tdd | ui-ux-pro-max、dev/impeccable、dev/hallmark、dev/prototype（如无 UI 探索需求） |
| **游戏开发（Unity/Godot）** | SDD 链 + 安全网 + 验证调试 + 元 skill + dev/prototype + dev/tdd + dev/codebase-design + dev/domain-modeling + dev/resolving-merge-conflicts + dev/leader + dev/goal_engineering | ui-ux-pro-max、dev/impeccable、dev/hallmark（游戏 UI 不走 Web 设计规范） |
| **全栈项目** | 全部 23 个 | 无 |

**裁剪后必须同步更新 `_index.md`**：删除对应 skill 的条目，更新统计数字。

### 第 4 步：适配技术栈

skill 文件中的示例代码可能是 Python/TypeScript，按项目实际语言替换：

- `anti-hallucination` 中的 log 调试示例
- `dev/tdd` 中的测试框架示例
- `dev/prototype` 中的 UI 变体示例
- `webapp_testing.md` 中的 Playwright 示例

**注意**：只替换示例代码，不修改方法论步骤。

### 第 5 步：配置 .gitignore

确保以下目录被 gitignore：

```gitignore
# dev_toolkit
temp/
.agents/memory/
.agents/wip/
.agents/specs/
```

### 第 6 步：验证部署

- [ ] AGENTS.md 中搜索 `TODO_DEPLOY`，结果为 0（全部填完）
- [ ] `_index.md` 的 skill 清单与 `.agents/skills/` 实际文件夹一致
- [ ] `.gitignore` 包含 `temp/` 和 `.agents/memory/`
- [ ] `checklist.md` 的检查项适用于本项目
- [ ] 删除 AGENTS.md 顶部的「部署初始化」章节和本文件（DEPLOY.md）

## 常见误用

### ❌ 误用 1：部署后"完善 toolkit"

**错误**：用户说"dev toolkit 刚部署，完善一下"，agent 开始修改 skill 方法论文件（改流程、加步骤、重写描述）。

**正确**："完善"= 填占位符 + 裁剪 skill + 适配技术栈。skill 方法论是成熟蓝图，不要改。

### ❌ 误用 2：全部拷贝不裁剪

**错误**：把 23 个 skill 全部放入一个纯后端 API 项目，agent 每次会话都看到 ui-ux-pro-max / dev/impeccable 等 irrelevant skill。

**正确**：按项目类型裁剪，保持 `_index.md` 与实际一致。

### ❌ 误用 3：保留 DEPLOY.md 不删

**错误**：部署完成后仍保留 DEPLOY.md 和 AGENTS.md 顶部的部署初始化章节。

**正确**：部署完成后删除 DEPLOY.md 和 AGENTS.md 的部署初始化章节。这些是一次性指引，不是长期文档。

### ❌ 误用 4：修改 skill 文件名

**错误**：把 `anti-hallucination` 重命名为 `anti-hallucination-v2`，或把 `dev/tdd` 移到 `tdd/`。

**正确**：skill 文件名是 `_index.md` 和 AGENTS.md 交叉引用的 key，不要重命名。如需删除，直接删文件夹 + 更新 `_index.md`。

## 参考文档取用

toolkit 根目录下的参考文档（`html_toolkit.md` / `mcp_builder.md` / `webapp_testing.md` / `skill_creator.md` / `project_essentials.md`）按需保留：

| 文档 | 保留条件 |
|------|---------|
| `html_toolkit.md` | 项目有 HTML 页面生成需求 |
| `mcp_builder.md` | 项目构建 MCP 服务 |
| `webapp_testing.md` | 项目有 Web UI 需要测试 |
| `skill_creator.md` | 项目需要创建自定义 skill |
| `project_essentials.md` | 项目是 AI Agent 项目（含 MCP/工具调用） |
| `evals_template.json` | 项目需要为 skill 写 evals |

不需要的文档直接删除，不影响 skill 运行。
