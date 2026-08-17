---
name: hallmark
description: >
  反 AI-slop 设计 skill。当用户要 build/audit/redesign/study 网页时使用，目标是让 UI"看起来是手工做的，不是 AI 生成的"。覆盖落地页/营销页/作品集/产品页/组件。当用户说"这个页面 AI 味太重"、"audit 一下设计"、"redesign 一下"、"extract 这个网站的 DNA"、"build 一个新页面"时调用，或 agent 在生成前端代码后主动调用以通过 58 条 slop-test 检查门。
task_type: dev.hallmark
trust: adapted
upstream_repo: https://github.com/nutlope/hallmark
upstream_path: .agents/skills/dev/hallmark/upstream/
upstream_fused_at: 2026-07-21
upstream_license: MIT
upstream_version: 1.1.0
---

# hallmark — 反 AI-slop 设计 skill

## 何时调用此 skill

**用户显式触发**：
- "build 一个新落地页 / 营销页 / 作品集"
- "audit 一下这个页面的 AI 痕迹"
- "redesign 一下这个组件 / 页面"
- "study 这个网站的设计 DNA"
- "这个页面看起来 AI 味太重，改改"
- "extract 这个我喜欢的网站的设计"
- "给我一个 design.md"

**Agent 主动触发**：
- 用户要做新前端页面时，先调 `build` 命令按主题 + macrostructure 走设计流程
- 生成大量前端代码后，用 `audit` 命令对照 58 条 slop-test 自检
- 用户贴了截图或 URL 但没明确动词时，问"study（提取 DNA）还是 build（用 DNA 重建）？"
- 同一会话内已经做过一个页面，下一个页面要避免与上一个同质化（diversification rule）

## 与 impeccable 的边界

| Skill | 偏向 | 触发场景 |
|------|------|---------|
| **hallmark**（本 skill） | 反 AI-slop 专项 | 偏向"如何避免 AI 味"——4 动词（build/audit/redesign/study）+ 20 主题 + 58 slop-test 检查门 + 结构多样性规则 |
| **impeccable** | 工程化设计工艺 | 偏向"如何做好一个 UI"——23 命令覆盖 craft/audit/polish/critique/distill 等完整设计工艺流程 |

**两者可叠加**：用 hallmark `build` 出页面骨架后，用 impeccable `polish` 打磨 typography/motion 细节；反之亦然。

## 完整工作流文档

**完整工作流文档位于本地 upstream/ 档案**：[`.agents/skills/dev/hallmark/upstream/SKILL.md`](upstream/SKILL.md)（含 4 动词完整规范 + 6 大 disciplines + 主题 catalog + component-scope 流程 + slop test 详解 + design.md 协议）。本 wrapper 提供路径适配和动词路由决策，具体执行细节请读对应文件。

## 4 个动词

| Invocation | What it does | 触发场景 |
|------------|--------------|---------|
| *(default)* | 进入 Design flow 走完整设计流程 | 用户要"build / design / make"新页面 |
| `hallmark audit <target>` | 读取目标，对照 anti-pattern 列表打分，返回 ranked punch list。**不编辑**。 | "audit 一下这个页面的 AI 痕迹" |
| `hallmark redesign <target> [--mood <name>]` | 取目标内容和意图，在现有实现边界内重新设计视觉结构（除非用户明确要全部重建）。新节奏 / 新标题位置 / 新组件声音。保留路由 / 组件归属 / 文案意图 / brand / IA；只替换视觉/交互层。 | "redesign 一下这个页面" |
| `hallmark study <screenshot \| URL>` | 用户贴了截图或 URL，提取 DNA（macrostructure / archetypes / type-pairing / color anchor），产出 diagnosis report，可选重建或 emit 便携 `design.md`。URL 自动走 URL mode（可命名字体/颜色但无法判断节奏）；其他走 image mode。**永不复制像素，拒绝 template-marketplace URL**。 | "study 一下这个网站的设计 DNA" |

**Implementation safety rail**（所有动词通用）：
- 永不删除 production 文件 / route tree / 组件目录 / 旧网站，除非用户明确批准 file-level plan
- 默认 in-place 编辑 named files 或新增组件/tokens；若 redesign 需删除多组件，先停下问确认
- PDF/README/.md brief/docs/transcripts/pitch decks 视为参考材料，不要逐字复制到页面
- 编辑前先声明预期要修改/创建/删除的文件；删除需显式确认

## 6 大跨动词 disciplines（所有动词通用）

1. **Pre-emit self-critique**：产出前 1-5 分自评 6 维（Philosophy/Hierarchy/Execution/Specificity/Restraint/Variety），<3 触发 revision。在 artifact 顶部 stamp 评分（`/* Hallmark · pre-emit critique: P5 H4 E5 S4 R5 V5 */`）
2. **Honest copy — no fabricated content**：用户未提供 metric 就不要发明。"+47% conversion"/"trusted by 50,000+ teams" 是 slop。用真实数字、placeholder（`—` + 灰色块"metric to confirm"）或换 macrostructure
3. **Locked tokens — no mid-render improvisation**：选定 theme 后所有 color 和 `font-family` 必须引用命名 token（`var(--color-accent)` / `font-family: var(--font-display)`）。不允许 inline OKLCH/hex/rgb 或绕过 token block 的 `font-family: "Some Font"`
4. **Re-drawn chrome forbidden**：不要手画假 browser bar（URL pill + traffic-light dots）/假手机框/假 code-block window/假 IDE chrome。用真截图包在 `<figure>` 里，或省略 chrome 让内容独立
5. **Mobile responsiveness — every emit verified at 320/375/414/768 px**：4 档必检。非协商项：无横向滚动 + `overflow-x: clip`（非 `hidden`）；无可换行成两行的可点击文字；image-bearing grid tracks 用 `minmax(0, 1fr)`；display headers 用 `overflow-wrap: anywhere; min-width: 0`；section heads 在 mobile 全部塌缩为单列；radio-tab 模式不 scroll-jump
6. **Typography purity — no italic headers**：标题/display 永远 roman（`font-style: normal`）。`Built to <em>think</em>` 是 AI tell；all-italic display face 同样。强调用 weight / accent color / drawn underline。italic 仅作为 body copy 段内强调

## Component-scope flow（组件级，非页面级）

**Component-scope 信号**（任 2 个触发）：
- brief 命名单个 UI 元素（button/input/card/modal/dropdown/tooltip/select/checkbox/switch/tab strip/chip/badge/banner/snackbar/popover/slider/date picker/avatar）
- brief 短（≤30 词）且只指一个元素
- 目标文件是单组件（如 `./Button.tsx` / `./components/Input.css`）
- 用户明确说"just the X" / "only the Y" / "this one element"

**Component-scope 保留**：Step 0 Pre-flight scan / Step 1 Genre detection / Step 2.6 Theme route / 2+1 font discipline。**STRICTER**：所有交互组件必须覆盖 8 状态（default/hover/`:focus-visible`/`:active`/disabled/loading/error/success）。

## 20 个内置主题（catalog）

按 diversification rule 轮换：同一会话内不同 brief 不应使用相同主题。

`carnival / cobalt / hum / lumen / ...`（完整列表见 `references/themes/`）

**Custom 主题分支**：仅当 brief 含 creative-intent signal（用户命名 brand color / 命名 catalog 无法承载的多属性 vibe / 明确要 custom theme）时触发，构造一次性 OKLCH 调色板 + free-font 配对。

## 路径适配（与原版的差异）

| 原版路径 | 本项目路径 | 说明 |
|---------|-----------|------|
| `references/<topic>.md` | `.agents/skills/dev/hallmark/references/<topic>.md` | 完整方法论规范（25+ 个主题 + 5 子目录） |
| `.hallmark/log.json` | `workspace/hallmark/log.json` | diversification rule 记录（同一会话用过的主题） |
| `design.md` | `workspace/hallmark/<project>/design.md` | study 命令 emit 的便携 DNA 档案 |

## 调用惯例

- **URL vs screenshot 自动分流**：URL（http://https:// 前缀）走 URL mode（WebFetch 读 HTML/CSS）；其他走 image mode
- **`audit` 永不编辑**：只返回 ranked punch list
- **`redesign` 默认 in-place**：不重建 route tree；多个组件删除需用户显式确认
- **`study` URL mode 严格**：emit `design.md` 比 diagnosis 本身更严格——URL-mode emission 需用户 attest 来源是自己拥有或公共参考
- **diversification rule**：同一会话内多个页面不应使用相同主题；记到 `.hallmark/log.json`
- **mobile 4 档必检**：320 / 375 / 414 / 768 px

## 与项目其他 skill 的联动

- **`impeccable`**：用 hallmark `build` 出页面骨架后，用 impeccable `polish` 打磨 typography/motion 细节（互补）
- **`prototype`**：dev/prototype/SKILL.md 走 TUI/浮动底栏快速验证逻辑/UI 双分支；hallmark 走 production-grade 反 AI-slop 设计
- **`web_archive`**：若源是网页，先用 `web_archive` 抓取 HTML/CSS，再用 `study` 命令提取 DNA
- **`cangjie_extraction`**：若设计方法论值得抽象为可复用 skill，用 cangjie 蒸馏后路由到 `.agents/skills/dev/`
- **`deep_research`**：study 命令的深度调研可结合 deep_research 的 web_search 能力
- **`neat-freak`**：会话收尾时若新增了项目级设计规范文件，触发 neat-freak 同步

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 本地 upstream/ 档案 | `.agents/skills/dev/hallmark/upstream/` | 完整原版 SKILL.md + 4 个根文档（README/LICENSE/ROADMAP）+ docs/ 3 个文档（recipes/study-examples/talk-slides） |
| references/ 子目录 | `.agents/skills/dev/hallmark/references/` | 25+ 个主题文件 + 5 子目录（components/genres/macrostructures/themes/verbs） |
| LLM 池 | `localagent_agent_chat` 或 `localagent_advanced_tool(llm_pool_call)` | 生成设计代码 / DNA 提取 / audit 报告时使用 |
| 浏览器 | `browser_navigate` + `browser_snapshot` + `browser_take_screenshot` | audit 命令验证现有页面 / study URL mode 读页面 |
| WebFetch | `WebFetch` | study URL mode 读 HTML/CSS |
| _index.md / GUIDE_REGISTRY | 写入项目级设计规范后同步 | 若 build/study 产出可复用方法论，路由到 `.agents/skills/<scope>/` 时使用 |
