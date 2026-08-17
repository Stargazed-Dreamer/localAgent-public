---
name: office_docx
description: |
  生成 Word 文档（.docx）。当用户要求生成 Word、md 转 docx、生成复习提纲/报告/简历、正式文档、学术论文（APA/Chicago/IEEE/MLA 引用）、word form、可填表单（SDT/content control）、SOW/合同/HR 入职表模板、mail merge 时使用。基于 python-docx + oxml，纯 Python 实现。完整触发词见正文。
task_type: adhoc.office_docx
---

# Word 文档生成 (office_docx)

## 触发词

生成 Word、生成 docx、md 转 docx、生成复习提纲、生成报告、生成简历、正式文档、长文本、学术论文、academic paper、APA/Chicago/IEEE/MLA 引用、word form、可填表单、SDT、content control、SOW 模板、合同模板、HR 入职表、医疗问卷、MERGEFIELD、mail merge、documentProtection。

## 概述

将 Markdown / JSON / 直接文本导出为标准 .docx 文档。**纯 Python 实现**（python-docx + oxml），不依赖 JS 库（docx-js）或 LibreOffice。借鉴 OfficeCli 项目的设计哲学：Help-First Rule、Incremental Execution、Scene-layer inheritance、QA Delivery Gate。

## 前置条件

- Python 依赖：`python-docx`
- 安装：`uv pip install python-docx`
- 检查：`uv run python -c "import docx"`

## Mental Model

.docx 是 ZIP + XML 的组合（document.xml / styles.xml / numbering.xml / header*.xml / footer*.xml / comments.xml 等）。python-docx 给你高层 API（Document / Paragraph / Run / Table），oxml 给你底层 XML 访问（用于 East Asia 字体、复杂 fields、SDT）。当高层 API 不够时，下钻到 oxml；当 oxml 不够时，用 lxml 直接操作 XML。

## 6 步工作流（Common Workflow）

1. **确认输入源和输出路径**
   - 输入源：Markdown / JSON / 直接文本
   - 输出路径：默认 `workspace/office_docs/{描述}_{日期}.docx`
   - 文件名规范：`{内容描述}_{YYYYMMDD}.docx`

2. **读对应 references**
   - 基础元素 → [references/docx_basic.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_basic.md)
   - 学术论文 → [references/docx_academic_paper.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_academic_paper.md)
   - Word 表单/SDT → [references/docx_word_form.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_word_form.md)
   - Report 配方 → [references/docx_report_recipes.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_report_recipes.md)
   - **不要一次读所有 references**——按用户场景读对应的一个

3. **增量构建（结构 → 内容 → 格式）**
   - 结构操作后立即验证（`docx.Document(path)` 重新打开不报错）
   - 不要写 50 个操作的大脚本在命令 3 失败时静默级联
   - Build order: styles & numbering defs → sections/page setup → headings & body → tables/images/fields/TOC → headers/footers → comments

4. **验证 schema + 内容**
   - 用 `docx.Document(path)` 重新打开不报错
   - 检查 placeholder token（`{{name}}`、`<TODO>`、`xxxx`、`lorem`）不能在文档中

5. **视觉走查（Visual Audit）**
   - 文本提取检查关键内容
   - 无截断标题或溢出单元格
   - TOC 在 3+ H1 时存在
   - 封面 ≥ 60% 填充，末页 ≥ 40% 填充

6. **报告结果**
   - 输出文件路径、文件大小、页数估计、关键内容摘要

## QA Delivery Gate（3 gates 必过）

### Gate 1 — schema

`docx.Document(path)` 重新打开不报错；任何 schema error → REJECT 并 fix

### Gate 2 — token leak

扫描文档文本不能含 `$xxx$`、`{{var}}`、`<TODO>`、`xxxx`、`lorem`、字面 `\t`/`\n`

### Gate 3 — Visual audit

- 无 placeholder token 渲染为数据
- 无截断标题或溢出单元格
- TOC 在 3+ headings 时存在
- 封面 ≥ 60% 填充，末页 ≥ 40% 填充

### Honest limit

`validate` 抓 schema 错误，**不**抓设计错误。文档可以 pass schema 同时有错误的 heading 层级、假 H1 size、placeholder token 当 body 文本。"validate pass" ≠ delivery；"looks like a real document" 才是。

## Help-First Rule

当不确定 python-docx API 或 OOXML schema 时，**先查文档**再写代码：

- python-docx 官方文档：https://python-docx.readthedocs.io/
- OOXML schema：ECMA-376 第 5 版
- 不确定 East Asia 字体如何设？查 `references/docx_basic.md` 的 Chinese Font Pattern 章节
- 一次 help query 胜过 guess-fail-retry 循环

## Incremental Execution

每个操作 → 检查结果 → 下一个操作。结构性操作（new section / table / TOC / section break）后立即 `docx.Document(path)` 重新打开验证，再堆叠更多。

## 反模式

### 必须遵守

1. **纯 Python 实现**：不引入 JS 库（docx-js）或 LibreOffice 依赖
2. **输出路径**：默认 `workspace/office_docs/`
3. **文件名规范**：`{内容描述}_{YYYYMMDD}.docx`
4. **验证必做**：生成后必须 `docx.Document(path)` 重开验证
5. **临时脚本**：复杂实现写到 `temp/`，不污染 skill 目录
6. **中文字体**：必须用 oxml 设置 `w:eastAsia`，否则中文可能用默认字体渲染

### 反模式（不要做）

1. **不要用 JS 库**：docx-js 是 JS 库，本项目用 python-docx
2. **不要用 LibreOffice**：避免外部进程依赖
3. **不要用空段落造间距**：用 `spaceBefore`/`spaceAfter`
4. **不要用前导空格缩进**：用 `indent=`
5. **不要把 `{{customer_name}}` 当 body 文本写**：用 MERGEFIELD field
6. **不要两种 page break 机制叠加**：`pageBreakBefore=true` 在 heading 上 OR `pagebreak`，不要同时用
7. **不要用 `set differentFirstPage=true`**：用 first 类型 footer 触发
8. **不要把 Table caption 放在表下方**：Table caption 在表**上方**，Figure caption 在图**下方**（所有 4 种引用风格一致）

## 设计规范（anti-AI-slop）

参考项目 html-dev-debug skill 的设计规范，办公文档同样适用：

- **主色用绿色**（`#2E7D32` primary, `#E8F5E9` light, `#4CAF50` accent），不用紫色渐变
- **避免过度居中**，采用左对齐或不对称布局
- **Dominance over equality**：1 主色 60-70% + 1-2 辅色 + 1 强调色

## 依赖

| 依赖 | 库 | 说明 |
|------|-----|------|
| Word 文档 | `python-docx` | 纯 Python，创建/读取 .docx |
| XML 操作 | `lxml`（python-docx 自带） | 底层 OOXML 操作 |

## 输入/输出

- **输入**：Markdown 文件 / JSON 数据 / 直接文本（放 `workspace/<task>/` 或用户指定路径）
- **输出**：`workspace/office_docs/{文件名}.docx`

## 详细参考

每种场景的详细工作流、设计规范、反模式见 `references/`：

- [references/docx_basic.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_basic.md) - 基础元素参考（headings/paragraphs/lists/tables/<data_drive>:\Pictures/headers-footers/page-breaks/fields）
- [references/docx_academic_paper.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_academic_paper.md) - 学术论文场景（APA/Chicago/IEEE/MLA Citation、OMML 公式、SEQ+PAGEREF 交叉引用）
- [references/docx_word_form.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_word_form.md) - Word 表单（SDT 7 types、MERGEFIELD、documentProtection 4 modes）
- [references/docx_report_recipes.md](file:///f:/<project_root>/.agents/skills/office_docs/references/docx_report_recipes.md) - Report 配方（封面/Page X of Y/表头/财务表/SWOT/TOC）

## 与现有 skill 的集成

| 现有 skill | 集成场景 |
|-----------|---------|
| `daily_summary` | 日总结 md → docx 归档 |
| `exam_prep_organizer` | 复习提纲 md → docx 提交版 |
| `niuke_review` | 面经评分报告 → docx |
| `community_review` | 社群精选帖子 → docx 汇编 |
| `deep_research` | 研究报告 → docx（替换原 office_export 调用） |
