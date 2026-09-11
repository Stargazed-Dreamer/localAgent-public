---
name: office_pptx
description: |
  生成 PowerPoint 演示文稿（.pptx）。当用户要求生成 PPT、md 转 pptx、汇报演示、复习 PPT、项目总结、融资路演、pitch deck、Series A/B/C、SAFE、Use-of-Funds、TAM/SAM/SOM、CAC/LTV、3D 模型、Morph transitions、字体配对、调色板时使用。基于 python-pptx，纯 Python 实现。完整触发词见正文。
task_type: adhoc.office_pptx
---

# PowerPoint 演示文稿生成 (office_pptx)

## 触发词
生成 PPT、生成 pptx、md 转 pptx、汇报演示、复习 PPT、项目总结、融资路演、pitch deck、Series A/B/C、seed round、SAFE、bridge、Use-of-Funds、traction、unit econ、TAM/SAM/SOM、CAC/LTV、Morph transitions、3D 模型、.glb、visual floor、12-column grid、anti-AI-slop、dominance over equality、字体配对、调色板、color palette

## 概述

将 Markdown / JSON 数据导出为标准 .pptx 演示文稿。**纯 Python 实现**（python-pptx），不依赖 JS 库（pptxgenjs）或 LibreOffice。借鉴 OfficeCli 项目的设计哲学：Visual Floor、6 anti-AI-slop 设计原则、QA Delivery Gate、Help-First Rule、Incremental Execution。

## 前置条件

- Python 依赖：`python-pptx`
- 安装：`uv pip install python-pptx`
- 检查：`uv run python -c "import pptx"`

## Mental Model

.pptx 是 ZIP + XML 的组合（`ppt/slides/slide1.xml` / `ppt/slideMasters/slideMaster1.xml` / `ppt/slideLayouts/slideLayoutN.xml` / `ppt/theme/theme1.xml` 等）。python-pptx 给你高层 API（`Presentation` / `Slide` / `Shape` / `TextFrame` / `Picture` / `Table` / `Chart`），shapes 必须**显式定位**（无 layout engine，自己算 grid）。16:9 widescreen 默认尺寸 33.87 × 19.05cm（13.333" × 7.5"）。

## 6 步工作流（Common Workflow）

### 1. 确认输入源和输出路径

- **输入源**：Markdown / JSON / 直接文本
- **输出路径**：默认 `workspace/office_pptx/{描述}_{日期}.pptx`
- **文件名规范**：`{内容描述}_{YYYYMMDD}.pptx`，如 `项目汇报_20260718.pptx`

### 2. 读对应 references

**不要一次读所有 references**——按用户场景读对应的一个：

- 基础元素 → [references/pptx_basic.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_basic.md)
- 设计原则 → [references/pptx_design_principles.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_design_principles.md)
- 融资路演 → [references/pptx_pitch_deck.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_pitch_deck.md)
- 通用配方 → [references/pptx_recipes.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_recipes.md)

### 3. 增量构建（结构 → 内容 → 格式）

- 结构操作后立即验证（`pptx.Presentation(path)` 重新打开不报错）
- 创建 shape 时即命名（用 `shape.name = 'HeroTitle'`），后续用 name 寻址（positional `[N]` 在 reorder 后会漂移）
- Z-order canon：后加的在上层。背景装饰先加，标题后加

### 4. 验证 schema + 内容

- 用 `pptx.Presentation(path)` 重新打开不报错
- 检查 placeholder token（`{{name}}`、`$fy$24`、`<TODO>`、`lorem`、`xxxx`、空 `()`/`[]`）不能在文档中

### 5. 视觉走查（Visual Audit）

- 每张 slide 至少一个视觉元素（无 bullet-only slide）
- 标题 ≥ 36pt bold
- body ≥ 18pt
- 无 dark-on-dark（fill brightness <30% 时 text/icon 必须 brightness >80%）
- 无 shape 碰撞 / 文本溢出 / 窄文本框

### 6. 报告结果

- 输出文件路径、文件大小、slide 数、关键内容摘要

## QA Delivery Gate（Gate 1-3 必过）

### Gate 1 — schema

`pptx.Presentation(path)` 重新打开不报错；任何 schema error → REJECT 并 fix

### Gate 2 — overflow / format / structure

- 无 `O1` overflow（shape 超出 slide 边界）
- 无 `C1` contrast（dark-on-dark）
- 无 `S1` structure（断裂的 shape 关系）

### Gate 2b — leftover placeholders

扫描 slide 文本不能含 `xxxx`、`lorem`/`ipsum`、`<TODO>`、`placeholder`、"this slide layout"、空 `()`/`[]`

### Gate 3 — Visual audit（MANDATORY）

逐 slide 检查：

- **overlap** — shapes/charts/巨型装饰数字碰撞
- **text overflow** — clipped at slide or shape boundary
- **narrow text box** — 内容技术上 fit 但 wrap 成多行短行
- **dark-on-dark** — fill brightness <30% 且 text/icon brightness <80%
- **image treatment** — 照片拉伸/扭曲、文字 raw 在 busy 图上、screenshot/logo cropped、透明图浮在白上
- **missing arrowheads** — flowchart connector 是 plain lines
- **decorative-line / title mismatch**
- **footer / citation collision**
- **tight margin / gap** — 元素距 slide 边 ~0.5" 内
- **uneven gaps** — 一侧大空、另一侧拥挤
- **column / repeat-element misalignment**
- **order sanity** — 序列匹配 narrative

**Fix-verify**：max 3 cycles。Fix → re-run Gate 3 → repeat 直到零新问题

## Help-First Rule

当不确定 python-pptx API 时，**先查文档**再写代码：

- python-pptx 官方文档：https://python-pptx.readthedocs.io/
- 不确定 shape 类型？查 [references/pptx_basic.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_basic.md)
- 一次 help query 胜过 guess-fail-retry 循环

## Incremental Execution

每个操作 → 检查结果 → 下一个操作。结构性操作（new slide / chart / animation / connector）后立即 `pptx.Presentation(path)` 重新打开验证，再堆叠更多。50 命令脚本在命令 3 失败会静默级联。

## 反模式

### 必须遵守

1. **纯 Python 实现**：不引入 JS 库（pptxgenjs）或 LibreOffice 依赖
2. **输出路径**：默认 `workspace/office_pptx/`
3. **文件名规范**：`{内容描述}_{YYYYMMDD}.pptx`
4. **验证必做**：生成后必须 `pptx.Presentation(path)` 重开验证
5. **临时脚本**：复杂实现写到 `temp/`，不污染 skill 目录
6. **shape 必须命名**：创建时即命名 `shape.name = 'HeroTitle'`，positional `[N]` 在 reorder 后会漂移
7. **每张 slide 必须有 speaker notes**：内容 slide 必须有 speaker notes（讲者需要 script）
8. **每张 slide 至少一个视觉元素**：bullet-only slide 等同 Word doc

### 反模式（不要做）

1. **不要用 JS 库**：pptxgenjs 是 JS 库，本项目用 python-pptx
2. **不要用 LibreOffice**：避免外部进程依赖
3. **不要 bullet-only slide**：每页至少有一个视觉元素（shape/chart/icon/gradient band）
4. **不要 emoji as iconography**：除非品牌使用 emoji，否则用 shape 或 real icon asset
5. **不要 decorative underline under slide titles**：用 whitespace 或 background-color change 替代
6. **不要 rounded-corner card with colored left-border accent stripe**：用 solid fill / top accent band / whitespace separation 替代
7. **不要 hockey-stick y-axis**：line chart y 轴不从 0 起 = VC 2 秒看穿 visual lie；必须 `axismin=0`
8. **不要 team slide = portfolio**：仅 `{headshot + name + role}` 卡 = VC credibility fail；每卡需 prior-company 或 prior-achievement line
9. **不要 TAM without methodology**：claimed number 无 "top-down" / "bottom-up" source footnote = fabricated
10. **不要 Use-of-Funds as 3-bucket or 5-bucket**：4-bucket (Eng/GTM/G&A/Reserve) 是 convention
11. **不要连续两 slide 用同一 pattern**：即使数据不同
12. **不要 `<a:br/>` 字面量当换行**：会以 7 个字面字符存储；用 paragraph 换行
13. **不要 chart title 含 `()` `[]` `TBD`**：以字面量 ship

## 设计规范（anti-AI-slop 6 设计原则）

参考 OfficeCli pptx skill 的 6 设计原则：

1. **Every slide carries a non-text visual** — Shape / chart / icon / gradient band that **carries meaning, not decoration**
2. **Dominance over equality**（60-70% + 10% + 10% + <5%）— One dominant brand color + one supporting + one accent；body 中**绝不混 4+ 色**
3. **Compose, don't web-center**（避免过度居中）— Whitespace is structural；Intentional asymmetry 比 centering everything 读起来更 designed
4. **Vary layout across slides** — 5 layout patterns building blocks；**永不连续两 slide 用同一 pattern**
5. **No emoji as iconography** — Use a shape or a real icon asset
6. **Visual motif commitment** — Pick ONE distinctive element and carry it to **every slide**

详细规范见 [references/pptx_design_principles.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_design_principles.md)

## 依赖

| 依赖 | 库 | 说明 |
|------|-----|------|
| PowerPoint | `python-pptx` | 纯 Python，创建/读取 .pptx |

## 输入/输出

- **输入**：Markdown 文件 / JSON 数据 / 直接文本（放 `workspace/<task>/` 或用户指定路径）
- **输出**：`workspace/office_pptx/{文件名}.pptx`

## 详细参考

每种场景的详细工作流、设计规范、反模式见 `references/`：

- [references/pptx_basic.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_basic.md) - 基础元素参考（slide/shape/paragraph/run/chart/picture/connector/table/placeholder/group/animation/transition/notes）
- [references/pptx_design_principles.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_design_principles.md) - 6 anti-AI-slop 设计原则 + Visual Floor + 字体配对 8 种 + 调色板 10 种
- [references/pptx_pitch_deck.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_pitch_deck.md) - 融资路演场景（10 必备 slide + stage diagnosis + 6 layout canon）
- [references/pptx_recipes.md](file:///<project_root>/.agents/skills/office_docs/references/pptx_recipes.md) - 通用配方（cover/divider/content/chart/thank-you + project report 示例）

## 与现有 skill 的集成

| 现有 skill | 集成场景 |
|-----------|---------|
| `daily_summary` | 日总结 → pptx 汇报 |
| `deep_research` | 研究报告 → pptx 演示 |
