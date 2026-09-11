---
name: office_pdf
description: |
  生成 PDF 文档（.pdf）。触发词：生成 PDF、md 转 pdf、归档材料、printable 报告、合并 PDF、拆分 PDF、PDF 加密、PDF 水印、PDF 旋转、PDF 表单填充、提取 PDF 文本、提取 PDF 表格。基于 reportlab + pypdf + pdfplumber，纯 Python 实现。
task_type: adhoc.office_pdf
---

# PDF 文档生成 (office_pdf)

## 触发词

生成 PDF、md 转 pdf、归档材料、printable 报告、合并 PDF、拆分 PDF、PDF 加密、PDF 水印、PDF 旋转、PDF 表单填充、提取 PDF 文本、提取 PDF 表格

## 概述

将 Markdown / 直接文本导出为标准 .pdf 文档，或对现有 PDF 进行合并/拆分/加密/旋转/水印/表单填充/文本提取。**纯 Python 实现**（reportlab + pypdf + pdfplumber），不依赖 JS 库或 LibreOffice。

> **注**：OfficeCli 项目没有 PDF skill，本项目独立设计 PDF 部分。借鉴 OfficeCli 设计哲学：Help-First Rule、Incremental Execution、QA Delivery Gate。

## 前置条件

- Python 依赖：`reportlab` / `pypdf` / `pdfplumber`
- 安装：`uv pip install reportlab pypdf pdfplumber`
- 检查：`uv run python -c "import reportlab, pypdf, pdfplumber"`

## Mental Model

PDF 是固定布局的页面描述语言（不是 ZIP+XML）。三个层次的库：

- **reportlab**：创建 PDF（Canvas 低层 + Platypus 高层）
- **pypdf**：操作现有 PDF（合并/拆分/加密/旋转/水印）
- **pdfplumber**：提取 PDF 内容（文本/表格）

PDF 与 docx/xlsx/pptx 不同——它是终端输出格式，不是可编辑格式。生成 PDF 时要"一次到位"，因为后期编辑困难。

## 6 步工作流（Common Workflow）

1. **确认输入源和输出路径**
   - 输入源：Markdown / 直接文本 / 现有 PDF
   - 输出路径：默认 `workspace/office_pdf/{描述}_{日期}.pdf`
   - 文件名规范：`{内容描述}_{YYYYMMDD}.pdf`

2. **读对应 references**
   - 基础元素 → [references/pdf_basic.md](file:///<project_root>/.agents/skills/office_docs/references/pdf_basic.md)
   - 所有 PDF 操作（创建 + 操作 + 提取）都在 basic.md 中

3. **增量构建（结构 → 内容 → 格式）**
   - reportlab 用 Platypus 高层 API：Document → Story（Paragraphs/Tables/Images）→ Build
   - 中文字体必须先注册再使用
   - 复杂样式用 Canvas 低层 API

4. **验证 schema + 内容**
   - 用 `pypdf.PdfReader(path)` 重开不报错
   - 检查页数合理
   - 抽查文本内容

5. **视觉走查（Visual Audit）**
   - 无中文渲染为黑方块（默认字体不支持中文）
   - 无 Unicode 上下标字符（`₀₁₂` `⁰¹²` 渲染为黑方块）
   - 无长表格跨页断裂
   - 无图片溢出
   - 无 Paragraph XML 转义问题

6. **报告结果**
   - 输出文件路径、文件大小、页数、关键内容摘要

## QA Delivery Gate（3 gates 必过）

### Gate 1 — schema

`pypdf.PdfReader(path)` 重开不报错；任何 schema error → REJECT 并 fix

### Gate 2 — token leak

扫描 PDF 文本不能含 `$xxx$`、`{{var}}`、`<TODO>`、`xxxx`、`lorem`、字面 `\t`/`\n`

### Gate 3 — Visual audit

- 无中文渲染为黑方块（用 `pdfplumber` 提取文本检查）
- 无 Unicode 上下标字符
- 无长表格跨页断裂
- 无图片溢出
- 无 Paragraph XML 转义问题（`<` `>` `&` 必须用 `&lt;` `&gt;` `&amp;`）

## Help-First Rule

当不确定 reportlab/pypdf/pdfplumber API 时，**先查文档**再写代码：

- reportlab 官方文档：https://docs.reportlab.com/
- pypdf 官方文档：https://pypdf.readthedocs.io/
- pdfplumber 官方文档：https://github.com/jsvine/pdfplumber
- 一次 help query 胜过 guess-fail-retry 循环

## Incremental Execution

每个操作 → 检查结果 → 下一个操作。结构性操作（new section / table）后立即 `pypdf.PdfReader(path)` 重新打开验证，再堆叠更多。

## 反模式

### 必须遵守

1. **纯 Python 实现**：不引入 JS 库（pdfkit/wkhtmltopdf）或 LibreOffice 依赖
2. **输出路径**：默认 `workspace/office_pdf/`
3. **文件名规范**：`{内容描述}_{YYYYMMDD}.pdf`
4. **验证必做**：生成后必须 `pypdf.PdfReader(path)` 重开验证
5. **临时脚本**：复杂实现写到 `temp/`，不污染 skill 目录
6. **中文字体**：必须用 `pdfmetrics.registerFont(TTFont(...))` 注册中文字体，否则渲染为黑方块

### 反模式（不要做）

1. **不要用 JS 库**：pdfkit 是 JS 库的 wrapper，本项目用 reportlab
2. **不要用 LibreOffice**：避免外部进程依赖
3. **不要 Unicode 上下标字符**：`₀₁₂` `⁰¹²` 在 PDF 中会渲染成黑方块，用 `<sub>`/`<super>` 标签或 reportlab 原生 API
4. **不要默认中文字体**：reportlab 默认 Helvetica 不支持中文，必须注册 `SimSun` / `Microsoft YaHei`
5. **不要长表格跨页断裂**：用 `TableStyle` + `splitByRow=True` 控制
6. **不要图片溢出**：用 `Image` 的 `maxWidth` / `maxHeight` 缩放
7. **不要 Paragraph XML 转义**：`<` `>` `&` 必须用 `&lt;` `&gt;` `&amp;`
8. **不要 Spacer 单位错误**：Spacer 用 `inch` 或 `cm`，不要裸数字
9. **不要生成即完成**：必须验证输出文件的格式正确性

## 设计规范（anti-AI-slop）

参考项目 html-dev-debug skill 的设计规范：

- **主色用绿色**（`#2E7D32` primary, `#E8F5E9` light, `#4CAF50` accent），不用紫色
- **避免过度居中**，采用左对齐或不对称布局
- **Dominance over equality**：1 主色 60-70% + 1-2 辅色 + 1 强调色

## 依赖

| 依赖 | 库 | 说明 |
|------|-----|------|
| PDF 创建 | `reportlab` | Canvas 低层 + Platypus 高层 |
| PDF 操作 | `pypdf` | 合并/拆分/加密/旋转/水印 |
| PDF 提取 | `pdfplumber` | 文本/表格提取 |

## 输入/输出

- **输入**：Markdown 文件 / 直接文本 / 现有 PDF（放 `workspace/<task>/` 或用户指定路径）
- **输出**：`workspace/office_pdf/{文件名}.pdf`

## 详细参考

所有 PDF 操作的详细工作流、设计规范、反模式见 `references/`：

- [references/pdf_basic.md](file:///<project_root>/.agents/skills/office_docs/references/pdf_basic.md) - 基础元素参考（创建 + 操作 + 提取）

## 与现有 skill 的集成

| 现有 skill | 集成场景 |
|-----------|---------|
| `daily_summary` | 日总结 md → pdf 归档 |
| `web_archive` | 精选文章合集 → pdf 归档 |
| `niuke_review` | 面经评分报告 → pdf |
| `deep_research` | 研究报告 → pdf（替换原 office_export 调用，参考 deep_research/references/pdf_report_style.md）|
