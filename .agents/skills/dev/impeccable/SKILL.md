---
name: impeccable
description: >
  前端设计工艺 skill。当用户要 design/redesign/shape/critique/audit/polish/clarify/distill/harden/optimize/adapt/animate/colorize/extract 或改进前端界面时使用。覆盖网站/落地页/仪表盘/产品 UI/组件/表单/设置/onboarding/空状态。也用于 UX review、视觉层级、信息架构、可访问性、响应式、theming、anti-patterns、typography、motion、micro-interactions。当用户说"UI 太丑"、"AI 味太重"、"polish 一下"、"craft 一个落地页"、"shape 这个组件"时调用，或 agent 在生成前端代码前/后主动调用以避免 AI slop。
task_type: dev.impeccable
trust: adapted
upstream_repo: https://github.com/pbakaus/impeccable
upstream_path: .agents/skills/dev/impeccable/upstream/
upstream_fused_at: 2026-07-21
upstream_license: Apache-2.0
upstream_version: 3.9.1
---

# impeccable — 前端设计工艺 skill

## 何时调用此 skill

**用户显式触发**：
- "帮我做一个落地页 / dashboard / 产品 UI"
- "这个页面看起来太丑 / 太 AI 味，改改"
- "design a new app / landing page"
- "polish 一下这个组件 / 这个页面"
- "audit 一下页面 AI 痕迹"
- "shape the visual identity / 调色板"
- "critique 我的 UI 设计"
- "distill 这个设计的 DNA 抽出来"

**Agent 主动触发**：
- 用户要做新前端页面/组件前，先调用 `craft` 或 `shape` 命令规范设计流程
- 现有 UI 触发了 absolute bans（side-stripe borders / gradient text / glassmorphism / identical card grids / tiny uppercase tracked eyebrow / numbered section markers 等），立即重写而非修补
- 生成大量前端代码后，用 `audit` 命令自检 AI slop
- 用户抱怨现有 UI "看起来 generic / 千篇一律 / 没设计感"时

## 与 hallmark 的边界

| Skill | 偏向 | 触发场景 |
|------|------|---------|
| **impeccable**（本 skill） | 工程化设计工艺 | 偏向"如何做好一个 UI"——含 23 个命令的完整设计工艺流程，覆盖 craft/audit/polish/critique/distill 等 |
| **hallmark** | 反 AI-slop 专项 | 偏向"如何避免 AI 味"——含 58 条 slop-test 检查门 + 20 个主题 + 4 动词（build/audit/redesign/study） |

**两者可叠加**：用 impeccable `craft` 出 UI 后，用 hallmark `audit` 检查是否还有 AI slop；反之亦然。

## 完整工作流文档

**完整工作流文档位于本地 upstream/ 档案**：[`.agents/skills/dev/impeccable/upstream/SKILL.md`](upstream/SKILL.md)（含 23 个命令的完整规范 + 设计规范 Color/Typography/Layout/Motion/Interaction + Absolute bans + AI slop test + Setup 流程）。本 wrapper 提供路径适配和命令路由决策，具体命令执行细节请读对应文件。

## 命令清单（23 个）

每个命令的详细规范位于 [`reference/<command>.md`](reference/)，原生平台（iOS/Android）变体读 `reference/<command>.native.md`。

| 命令 | 用途 | 触发场景 |
|------|------|---------|
| `init` | 项目初始化 | 全新项目无 PRODUCT.md 时 |
| `craft` | 从零精造 UI | "做一个新页面 / 新组件" |
| `shape` | 塑造视觉身份 | "设计调色板 / 字体配对 / 主题" |
| `audit` | 审计 AI 痕迹 | "这个页面看起来 generic，audit 一下" |
| `polish` | 打磨细节 | "差不多但还差点感觉，polish 一下" |
| `critique` | 评审设计 | "critique 我的 UI 设计" |
| `clarify` | 简化信息架构 | "信息太乱，clarify 一下" |
| `distill` | 提取设计 DNA | "distill 这个我喜欢的网站的 DNA" |
| `harden` | 强化边缘 case | "处理空状态 / 错误状态 / loading" |
| `optimize` | 优化性能/可访问性 | "optimize 性能 / a11y" |
| `adapt` | 跨平台适配 | "adapt 到 iOS / Android" |
| `animate` | 设计 motion | "animate 一下这个交互" |
| `colorize` | 调整颜色策略 | "colorize 一下配色" |
| `extract` | 提取设计系统 | "extract 我现有的 design tokens" |
| `bolder` | 大胆化 | "太保守，bolder 一点" |
| `quieter` | 安静化 | "太花哨，quieter 一点" |
| `delight` | 增加惊喜 | "delight 一下用户体验" |
| `onboard` | onboarding 流程 | "设计 onboarding" |
| `layout` | 布局重排 | "layout 重新组织一下" |
| `typeset` | 排版优化 | "typeset 一下 typography" |
| `live` | 浏览器实时迭代 | "live 模式调一下" |
| `product` | 产品 UI 风格 | dashboard / 工具类 UI |
| `brand` | 品牌 UI 风格 | 落地页 / 营销页 / 作品集 |

**Register 选择**：项目是 marketing/landing/ campaign/portfolio → 读 `reference/brand.md`；项目是 app/admin/dashboard/tool → 读 `reference/product.md`。

## 路径适配（与原版的差异）

| 原版路径 | 本项目路径 | 说明 |
|---------|-----------|------|
| `node .pi/skills/impeccable/scripts/*` | **不执行** | 本项目不安装 node；scripts/ 仅作为方法论参考，不调用 |
| `reference/<cmd>.md` | `.agents/skills/dev/impeccable/reference/<cmd>.md` | 命令详细规范（30 个文件含 native 变体） |
| `reference/<cmd>.native.md` | `.agents/skills/dev/impeccable/reference/<cmd>.native.md` | iOS/Android 平台原生变体 |
| `PRODUCT.md` / `DESIGN.md` | 项目根目录（如有） | context.mjs 原版会扫描，本项目可直接读 |
| `.pi/skills/impeccable/scripts/context.mjs` | **不执行** | 改为 agent 直接读项目根目录的 PRODUCT.md/DESIGN.md |

## 设计规范速查（必守红线）

### Absolute Bans（匹配即重写）

- **Side-stripe borders**：`border-left/right > 1px` 作为彩色 accent
- **Gradient text**：`background-clip: text` + gradient 背景
- **Glassmorphism as default**：装饰性 blur/glass card（除非有目的）
- **Hero-metric template**：大数字 + 小标签 + supporting stats + gradient accent（SaaS 陈词滥调）
- **Identical card grids**：同尺寸 card + icon + heading + text 无限重复
- **Tiny uppercase tracked eyebrow above every section**：每节标题上方都加小型全大写 tracking 字（"ABOUT"/"PROCESS"/"PRICING"）—— 2023 年 AI 标志
- **Numbered section markers as default scaffolding**：每节上方都加 `01 · About / 02 · Process` —— 比 eyebrow 更深一层的 AI 反射

### Color

- 用 OKLCH，不用 hex/rgb
- 对比度：body text ≥4.5:1，large text (≥18px 或 bold ≥14px) ≥3:1，placeholder 同样 ≥4.5:1（不是默认 muted-gray）
- 颜色策略 4 档：Restrained（tinted neutrals + accent ≤10%）/ Committed（一个饱和色占 30-60%）/ Full palette（3-4 named roles）/ Drenched（整面就是色）
- **2026 年 AI 默认陷阱**：cream/sand/beige body bg（OKLCH L 0.84-0.97, C<0.06, hue 40-100）+ token 名 `--paper`/`--cream`/`--sand`/`--bone` 都是 tell

### Typography

- body 行长 65-75ch
- 字体配对走对比轴（serif + sans / geometric + humanist），不要两个相似的 sans
- hero/display clamp() max ≤ 6rem (~96px)
- display letter-spacing ≥ -0.04em
- h1-h3 用 `text-wrap: balance`；长 prose 用 `text-wrap: pretty`

### Layout

- Card 是懒人答案，不要默认用；nested card 永远错
- Flexbox for 1D, Grid for 2D
- 响应式 grid 无 breakpoint：`repeat(auto-fit, minmax(280px, 1fr))`
- 语义化 z-index scale（dropdown → sticky → modal-backdrop → modal → toast → tooltip），不用 999/9999

### Motion

- 用指数曲线 ease-out（quart/quint/expo），不要 bounce/elastic
- 每个动画必须有 `@media (prefers-reduced-motion: reduce)` fallback
- 不要 animate CSS layout 属性
- Reveal animation 必须增强已可见的 default，不要 gate 内容可见性

### Interaction

- Dropdown 在 `overflow: hidden/auto` 容器内会被 clip，用 `<dialog>` / popover API / `position: fixed` / portal
- 组件必须覆盖完整状态：default / hover / `:focus-visible` / `:active` / disabled / loading / error / success

## 调用惯例

- **不主动执行 `node .pi/skills/impeccable/scripts/*`**——本项目无 node 环境，scripts/ 仅作为方法论参考
- **Setup 阶段跳过 `context.mjs`**：改为 agent 直接读项目根目录的 PRODUCT.md/DESIGN.md（如有）；缺失时按原版 SKILL.md 的 `NO_PRODUCT_MD` 分支处理
- **命令选择**：根据用户动词映射到对应命令；模糊动词（"改改"/"弄一下"）默认 `polish`
- **register 选择必读**：读 `reference/brand.md` 或 `reference/product.md` 之一（不可跳过）
- **absolute bans 触发即重写**：不要修补，整元素重写
- **mobile 必检 4 档**：320 / 375 / 414 / 768 px

## 与项目其他 skill 的联动

- **`hallmark`**：用 impeccable `craft` 出 UI 后，用 hallmark `audit` 检查是否还有 AI slop（互补）
- **`prototype`**：dev/prototype/SKILL.md 走 TUI/浮动底栏快速验证逻辑/UI 双分支；impeccable 走 production-grade 完整设计工艺
- **`web_archive`**：若源是网页/参考站点，先用 `web_archive` 抓取 HTML/CSS，再用 `distill` 命令提取 DNA
- **`cangjie_extraction`**：若设计方法论值得抽象为可复用 skill，用 cangjie 蒸馏后路由到 `.agents/skills/dev/`
- **`neat-freak`**：会话收尾时若新增了项目级设计规范文件，触发 neat-freak 同步

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 本地 upstream/ 档案 | `.agents/skills/dev/impeccable/upstream/` | 完整原版 SKILL.md + 8 个根文档（README/LICENSE/NOTICE/AGENTS/CLAUDE/DESIGN/PRODUCT）+ docs/ 5 个开发文档（DEVELOP/HARNESSES/STYLE/adr-live-variant-mode/openai-plugin-submission） |
| reference/ 子目录 | `.agents/skills/dev/impeccable/reference/` | 30 个命令规范（adapt/audit/bolder/brand/clarify/codex/colorize/craft/critique/delight/distill/document/extract/harden/hooks/init/interaction-design/ios/layout/live/onboard/optimize/overdrive/polish/product/quieter/shape/typeset + adapt.native/audit.native + android/animate 等共 30 个） |
| scripts/ 子目录 | `.agents/skills/dev/impeccable/scripts/` | 11 类 mjs 脚本（context/detect/hook-lib/live/palette/pin 等），仅方法论参考，本地不执行 |
| LLM 池 | `localagent_agent_chat` 或 `localagent_advanced_tool(llm_pool_call)` | 生成设计代码 / 文档时使用 |
| 浏览器 | `browser_navigate` + `browser_snapshot` + `browser_take_screenshot` | audit 命令验证现有页面 / live 命令浏览器实时迭代 |
| _index.md / GUIDE_REGISTRY | 写入项目级设计规范后同步 | 若 craft/distill 产出可复用方法论，路由到 `.agents/skills/<scope>/` 时使用 |
