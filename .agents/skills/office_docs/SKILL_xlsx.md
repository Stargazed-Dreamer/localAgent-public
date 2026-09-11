---
name: office_xlsx
description: |
  生成 Excel 表格（.xlsx）。当用户要求生成 Excel、md/JSON/CSV 转 xlsx、账单汇总、数据报表、多 sheet 分析、带公式、财务模型（DCF/LBO/3-statement）、unit economics、KPI/analytics dashboard、透视表、条件格式、迷你图时使用。基于 openpyxl，纯 Python 实现。完整触发词见正文。
task_type: adhoc.office_xlsx
---

# Excel 表格生成 (office_xlsx)

## 触发词
生成 Excel、生成 xlsx、md 转 xlsx、JSON 转 xlsx、CSV 转 xlsx、账单汇总、数据报表、多 sheet 分析、带公式、财务模型、3-statement model、DCF、LBO、WACC、NPV、IRR、XIRR、sensitivity table、unit economics、CAC/LTV、cap table forecast、KPI dashboard、analytics dashboard、executive dashboard、metrics dashboard、CSV to dashboard、数据可视化、conditional formatting、数据验证、命名范围、named range、透视表、PivotTable、迷你图、sparkline

## 概述
将 JSON / CSV / Markdown 表格数据导出为标准 .xlsx 表格。**纯 Python 实现**（openpyxl），不依赖 JS 库或 LibreOffice。借鉴 OfficeCli 项目的设计哲学：CFO 4-color code、Three-zone architecture、QA Delivery Gate、Incremental Execution。

## 前置条件
- Python 依赖：`openpyxl`
- 安装：`uv pip install openpyxl`
- 检查：`uv run python -c "import openpyxl"`

## Mental Model
.xlsx 是 ZIP + XML 的组合（xl/workbook.xml / xl/worksheets/sheet1.xml / xl/styles.xml / xl/sharedStrings.xml 等）。openpyxl 给你高层 API（Workbook / Worksheet / Cell / Row / Column），也支持公式、样式、条件格式、数据验证、命名范围、图表、透视表。公式不要写前导 `=`，openpyxl 自动添加。

## 6 步工作流（Common Workflow）

1. **确认输入源和输出路径**
   - 输入源：JSON / CSV / Markdown 表格
   - 输出路径：默认 `workspace/office_xlsx/{描述}_{日期}.xlsx`
   - 文件名规范：`{内容描述}_{YYYYMMDD}.xlsx`

2. **读对应 references**
   - 基础元素 → [references/xlsx_basic.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_basic.md)
   - 条件格式 → [references/xlsx_conditional_formatting.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_conditional_formatting.md)
   - 财务模型 → [references/xlsx_financial_model.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_financial_model.md)
   - 数据仪表盘 → [references/xlsx_data_dashboard.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_data_dashboard.md)
   - **不要一次读所有 references**——按用户场景读对应的一个

3. **增量构建（结构 → 公式 → 格式）**
   - Workbook → Sheet → Cell → Row/Column → Style → Formula → CF → Chart
   - 结构操作后立即验证（`openpyxl.load_workbook(path)` 重开不报错）

4. **验证 schema + 公式**
   - 用 `openpyxl.load_workbook(path)` 重开不报错
   - 检查 placeholder token
   - 抽 2-3 个公式 `cell.value` + `cell.cachedValue`（注意：openpyxl 不计算公式，需要 Excel 打开重算）

5. **视觉走查（Visual Audit）**
   - 无 `###`（列宽不够）
   - 无截断 title/label
   - 无 placeholder token

6. **报告结果**
   - 输出文件路径、文件大小、sheet 数、关键 KPI 摘要

## QA Delivery Gate（3 gates 必过）

### Gate 1 — schema
`openpyxl.load_workbook(path)` 重开不报错；任何 schema error → REJECT 并 fix

### Gate 2 — Excel error sweep
扫描每个 sheet 不能含 `#REF!`、`#DIV/0!`、`#VALUE!`、`#NAME?`、`#N/A`：
```python
from openpyxl import load_workbook
wb = load_workbook(path, data_only=False)
for sheet in wb.sheetnames:
    ws = wb[sheet]
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith('#'):
                print(f"ERROR: {sheet}!{cell.coordinate} = {cell.value}")
```

### Gate 3 — Visual audit
- 无 `###`（列宽不够）
- 无截断 title/label
- 无 placeholder token（`{{var}}`、`$fy$24`、`<TODO>`、`xxxx`）
- pie/doughnut slice 颜色不 collapse
- 无空 chart anchor

### Honest limit
`validate` 抓 schema 错误，**不**抓设计错误。Workbook 可以 pass schema 同时有错误的公式（hardcoded 结果代替公式）、假标题、placeholder token 当 body 文本。"validate pass" ≠ delivery；"opens cleanly in Excel with correct values" 才是。

## Help-First Rule
当不确定 openpyxl API 时，**先查文档**再写代码：
- openpyxl 官方文档：https://openpyxl.readthedocs.io/
- 不确定条件格式 API？查 `references/xlsx_conditional_formatting.md`
- 一次 help query 胜过 guess-fail-retry 循环

## Incremental Execution
每个操作 → 检查结果 → 下一个操作。结构性操作（new sheet / chart / pivot table）后立即 `load_workbook(path)` 重新打开验证，再堆叠更多。

## 反模式

### 必须遵守
1. **纯 Python 实现**：不引入 JS 库或 LibreOffice 依赖
2. **输出路径**：默认 `workspace/office_xlsx/`
3. **文件名规范**：`{内容描述}_{YYYYMMDD}.xlsx`
4. **验证必做**：生成后必须 `load_workbook(path)` 重开验证
5. **临时脚本**：复杂实现写到 `temp/`，不污染 skill 目录
6. **公式而非硬编码**：用 `=SUM(B2:B9)` 让表格保持动态，不要 hardcode 计算结果到 cell

### 反模式（不要做）
1. **不要 hardcode 计算结果到 xlsx cell**：用公式 `=SUM(B2:B9)` 让表格保持动态
2. **不要 date as text**：日期用 `datetime` 对象或 `YYYY-MM-DD` 格式
3. **不要 scientific notation**：用 numFmt `0.00` 或 `¥#,##0.00` 避免
4. **不要窄列中文**：column width 显式设置，中文字符比英文宽
5. **不要中文函数名**：用英文函数名 `SUM`、`VLOOKUP`，不要 `求和`
6. **不要 merged cell write loss**：合并单元格后只能写左上角，写入其他单元格会丢失
7. **不要 non-Chinese fonts**：默认字体可能不支持中文，显式设置 `Microsoft YaHei`
8. **不要 year 显示 `2,026`**：年份用 `numFmt="@"` 或字符串类型，不要数字格式
9. **不要跨表 `Sheet1!A1` 在 shell 里裸写**：用 python 代码内 string 处理
10. **不要 chart series 后改**：加/换 series 必须 remove + add

## 设计规范（anti-AI-slop）

- **主色用绿色**（`#2E7D32` primary, `#E8F5E9` light, `#4CAF50` accent）
- **CFO 4-color code**（财务模型专用）：
  - Blue `0000FF` — Hardcoded inputs
  - Black `000000` — ALL formulas
  - Green `008000` — Cross-sheet links
  - Red `FF0000` — External file links
  - Yellow fill `FFFF00` — Key assumptions needing review
- **Three-zone architecture**（财务模型专用）：
  - Inputs zone（Assumptions sheet, tabColor=Yellow FFC000）— Blue hardcodes
  - Calc zone（P&L/BS/CF/DCF/Debt, tabColor=Blue 4472C4）— Zero hardcodes
  - Outputs zone（Summary/Dashboard/Sensitivity/Returns, tabColor=Green 70AD47）— Only label hardcodes

## 依赖

| 依赖 | 库 | 说明 |
|------|-----|------|
| Excel 表格 | `openpyxl` | 纯 Python，公式+格式化 |

## 输入/输出

- **输入**：JSON / CSV / Markdown 表格（放 `workspace/<task>/` 或用户指定路径）
- **输出**：`workspace/office_xlsx/{文件名}.xlsx`

## 详细参考

每种场景的详细工作流、设计规范、反模式见 `references/`：
- [references/xlsx_basic.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_basic.md) - 基础元素参考（sheets/cells/ranges/formulas/styles/columns/merges/freeze-panes/named-ranges/pivot-tables/comments）
- [references/xlsx_conditional_formatting.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_conditional_formatting.md) - 条件格式 3 flavors（color scale / data bar / formula rule）+ icon set
- [references/xlsx_financial_model.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_financial_model.md) - 财务模型场景（3-statement / DCF / LBO）+ CFO 4-color + Three-zone
- [references/xlsx_data_dashboard.md](file:///<project_root>/.agents/skills/office_docs/references/xlsx_data_dashboard.md) - 数据仪表盘场景（KPI 卡 + chart + sparkline + CF）

## 与现有 skill 的集成

| 现有 skill | 集成场景 |
|-----------|---------|
| `accounting` | 账单数据 → xlsx 月度汇总（替换原 office_export 调用）|
| `daily_summary` | 日总结数据 → xlsx 报表 |
| `deep_research` | 研究数据 → xlsx 分析表 |
