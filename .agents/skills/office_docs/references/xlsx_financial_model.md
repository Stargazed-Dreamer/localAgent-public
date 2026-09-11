# Excel 财务模型参考 (financial_model)

本文件是 [office_xlsx skill](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md) 的财务模型场景参考。借鉴 OfficeCli 项目的设计哲学（CFO 4-color code / Three-zone architecture / QA Delivery Gate / Incremental Execution），用 openpyxl 实现。

## 何时用此 skill

trigger 关键词：
- `3-statement model` / `P&L + BS + CF` / `三大表`
- `DCF` / `WACC` / `NPV` / `terminal value`
- `LBO` / `debt schedule` / `cash sweep` / `MOIC` / `IRR / XIRR`
- `sensitivity table` / `scenario analysis`
- `ARR model` / `unit economics` / `CAC / LTV`
- `cap table forecast`

## 何时回退到 basic.md

- budget tracker（预算跟踪表）
- CSV-to-report dump（CSV 直接转报表）
- operational KPI sheet（运营 KPI 表，无公式链）
- cap table without forecast logic（cap table 不含预测逻辑）

→ 路由到 [basic.md](file:///<project_root>/.agents/skills/office_xlsx/references/basic.md)

## CFO 4-color code 完整规范

### 适用范围
**仅适用于 financial-model 范围**。**不适用于** template / tracker / CSV import / operational sheet。

### 4 colors 表

| 颜色 | hex | 含义 | 示例 |
|---|---|---|---|
| Blue | `0000FF` | Hardcoded inputs | 营收基准值 100、增长率 5% |
| Black | `000000` | ALL formulas | `=B5*(1+GrowthRate)` |
| Green | `008000` | Cross-sheet links | `='P&L'!B5`（BS 引用 P&L 的 NI）|
| Red | `FF0000` | External file links | `='[Other Model.xlsx]Sheet1'!A1` |
| Yellow fill | `FFFF00` | Key assumptions needing review | 关键假设需评审，配 Comment 写来源 |

### 契约
评审者在读公式**之前**仅凭颜色就能判断 cell 是什么：
- 蓝色 → 知道这是假设，可质疑来源
- 黑色 → 知道这是计算，可验证公式
- 绿色 → 知道这是跨表引用，去对应表找源
- 红色 → 知道这是外部链接，需打开外部文件
- 黄底 → 知道这是关键假设，重点审查

### 配套 number format 标准

```python
# Year 是文本不是数字（2026 不是 2,026）
ws['B1'] = '2024A'
ws['B1'].number_format = '@'   # 或 type=string

# Currency 在 header 带单位，不每格重复
ws['A3'] = 'Revenue ($mm)'   # ✓ header 带单位
# ❌ 每格都写 ¥1234 或 1234 万元

# 零显示为 - 不显示 0
cell.number_format = '$#,##0;($#,##0);"-"'   # 正数 $1,234 / 负数 ($1,234) / 零 -

# 百分比默认一位小数
cell.number_format = '0.0%'   # 12.3%

# 负数用括号
cell.number_format = '#,##0;(#,##0);"-"'   # 1234 / (1234) / -

# 估值倍数用 0.0x
cell.number_format = '0.0x"'   # 8.5x (EV/EBITDA)
# 注意 openpyxl numFmt 里 x 需用 \"x\" 或直接 x（视 viewer）
cell.number_format = '0.0"x"'   # 安全写法
```

### Assumption 纪律
**假设在 cell 里，不在公式里**。

```python
# ❌ 错：5% 增长率硬编进公式
ws['C5'] = '=B5*1.05'

# ✓ 对：引用 Assumptions sheet 或 named range
ws['C5'] = '=B5*(1+Assumptions!GrowthRate)'
ws['C5'] = '=B5*(1+GrowthRate)'   # GrowthRate 是 defined name
```

### 每个蓝色硬编输入必须写来源
```python
from openpyxl.comments import Comment

ws['B2'] = 100   # Revenue base ($M)
ws['B2'].font = Font(color='0000FF')   # 蓝色
ws['B2'].comment = Comment(
    'Source: Company 10-K, FY2024, Page 45, Revenue Note\n'
    'Bloomberg: 2026-05-02, AAPL US Equity',
    'analyst'
)
```

来源格式参考：
- `Source: Company 10-K, FY2024, Page 45, Revenue Note`
- `Source: Bloomberg, 2026-05-02, AAPL US Equity`
- `Source: Management guidance, Q2 2026 earnings call`
- `Assumption: 行业平均增速 5%，参考 McKinsey 2024 报告`

## Three-zone architecture 完整说明

### 硬规则
Inputs → Calc → Outputs。命名、tabColor、executable audit 三层强制。**崩 zone = unauditable model 的最常见原因**。

### 3 zones 表

| Zone | Sheet 名 | Tab color | 内容 | Hardcodes | Formulas |
|---|---|---|---|---|---|
| **Inputs** | `Assumptions` | `FFC000`（黄）| 增长率、税率、WACC、TerminalGrowth、NetDebt、SharesOut | Blue hardcodes（带来源）| 无 |
| **Calc** | `P&L` / `Balance Sheet` / `Cash Flow` / `DCF` / `Debt` | `4472C4`（蓝）| 计算逻辑 | **0 hardcodes**（全部公式）| 全部 |
| **Outputs** | `Summary` / `Dashboard` / `Sensitivity` / `Returns` | `70AD47`（绿）| KPI、图表、敏感性表 | Only label hardcodes | 引用 Calc |

### Build order（cross-zone-aware）
1. **Assumptions 先**：定义所有 named range
2. **Calc 按依赖链 bottom-up**：P&L → BS → CF（BS 需要 P&L 的 NI，CF 需要 BS 的 Cash）
3. **Outputs 最后**：Summary 引用 Calc，Sensitivity 引用 DCF

### Executable zone audit 代码
```python
from openpyxl import load_workbook
from openpyxl.styles import Font

def audit_zones(path):
    wb = load_workbook(path)
    issues = []

    # Inputs zone: 蓝色 cell 必须
    inputs_ws = wb['Assumptions']
    blue_count = 0
    for row in inputs_ws.iter_rows():
        for cell in row:
            if cell.value is not None and isinstance(cell.value, (int, float)):
                if cell.font and cell.font.color and cell.font.color.rgb in ('FF0000FF', '0000FF'):
                    blue_count += 1
                    # 检查是否有 comment（来源）
                    if cell.comment is None:
                        issues.append(f'Inputs!{cell.coordinate} 蓝色输入但无 comment 来源')
    print(f'Inputs zone: {blue_count} blue hardcodes')

    # Calc zone: 0 hardcodes（除 label）
    for sheet_name in ['P&L', 'Balance Sheet', 'Cash Flow', 'DCF', 'Debt']:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                # 数字且非公式 = hardcode
                if isinstance(cell.value, (int, float)):
                    issues.append(f'Calc zone {sheet_name}!{cell.coordinate} 含 hardcode: {cell.value}')
                # 公式但字色蓝 = 硬编进公式
                if isinstance(cell.value, str) and cell.value.startswith('='):
                    if cell.font and cell.font.color and cell.font.color.rgb in ('FF0000FF', '0000FF'):
                        issues.append(f'Calc zone {sheet_name}!{cell.coordinate} 公式但蓝色字（应在 Inputs）')

    # Outputs zone: only label hardcodes
    for sheet_name in ['Summary', 'Dashboard', 'Sensitivity', 'Returns']:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)):
                    issues.append(f'Outputs zone {sheet_name}!{cell.coordinate} 含数字 hardcode（应为公式引用 Calc）')

    if issues:
        print('⚠️ Zone audit issues:')
        for i in issues:
            print(f'  {i}')
    else:
        print('✓ Zone audit pass')
    return issues

audit_zones('workspace/office_xlsx/3statement_model_20260721.xlsx')
```

### Print delivery（board/IC/LP）
只 emit Outputs zone：
```python
# 隐藏所有非 Outputs sheet
for sheet_name in ['Assumptions', 'P&L', 'Balance Sheet', 'Cash Flow', 'DCF', 'Debt']:
    if sheet_name in wb.sheetnames:
        wb[sheet_name].sheet_state = 'hidden'
# 只留 Summary / Dashboard / Sensitivity / Returns 可见
```

## 6 步 Common Workflow

1. **open/save lifecycle**
   - `wb = Workbook()` 创建 / `wb = load_workbook(path)` 加载
   - 每个结构性操作后 `wb.save(path)` + `load_workbook(path)` 验证

2. **create or load**
   - 新模型：`wb = Workbook()`
   - 修改现有：`wb = load_workbook(path)`

3. **增量构建（一条命令一次）**
   - 结构操作后验证 shape 再叠加
   - `ws_assumptions = wb.create_sheet('Assumptions')` → save → load → 验证
   - `ws_pl = wb.create_sheet('P&L')` → save → load → 验证

4. **format**
   - CFO 4-color code（见上）
   - Three-zone tabColor

5. **save + cache-drift 处理**
   - 上游 cell 改后，下游公式 cachedValue stale
   - `wb.calculation.fullCalcOnLoad = True` schedule runtime recalc
   - 但 build-time cachedValue 不刷新，需 re-touch 每个 downstream cell

6. **QA（assume there are problems）**
   - Gate 4-6（见下）

## Recipe A — 3-statement model（P&L + BS + CF）

### 4 表结构
- `Assumptions`：增长率、税率、毛利率、CapEx 比率、营运资金天数
- `P&L`：Revenue → COGS → Gross Profit → OpEx → EBITDA → D&A → EBIT → Interest → Tax → NI
- `Balance Sheet`：Cash / AR / Inventory / PPE / AP / Debt / Equity / RetainedEarnings
- `Cash Flow`：CFO（NI + D&A - WC change）/ CFI（CapEx）/ CFF（Debt / Equity / Dividends）
- `Summary`：4+ KPI + 3 chart

### 年列
```
2024A · 2025E · 2026E · 2027E
```

### 强制 build order
```
Assumptions → P&L → Balance Sheet → Cash Flow → Summary
```

### 关键检查行
```python
# BS Balance Check
ws_bs['B30'] = '=IF(ABS(Assets-Liab-Equity)<0.01, "OK", "IMBALANCED: "&TEXT(Assets-Liab-Equity, "0.00"))'

# CF Cash Recon
ws_cf['B20'] = '=IF(ABS(CF_EndingCash - BS_Cash)<0.01, "OK", "CF != BS CASH: "&TEXT(CF_EndingCash-BS_Cash, "0.00"))'
```

### RetainedEarnings 每期公式
```python
# Y1 历史 RE 用 BS identity 算
ws_bs['C20'] = '=C8-C13-C18'   # TotalAssets - TotalLiab - PaidInCapital

# Y2+ RE = 前期 RE + NI - Dividends
ws_bs['D20'] = '=C20+P&L!D18-D22'   # RE(t-1) + NI(t) - Dividends(t)
ws_bs['E20'] = '=D20+P&L!E18-E22'
```

### CF Y2+ Opening Cash = 前期 Ending Cash
```python
ws_cf['C17'] = '=B19'   # Y2 Opening = Y1 Ending
ws_cf['D17'] = '=C19'   # Y3 Opening = Y2 Ending
```

### BS.Cash 永远 = CF Ending Cash
```python
# ❌ 错：BS.Cash 独立 plug
ws_bs['B8'] = 100   # hardcode

# ✓ 对：BS.Cash = CF Ending Cash（永远，含 Y1）
ws_bs['B8'] = "='Cash Flow'!B19"
ws_bs['C8'] = "='Cash Flow'!C19"
```

### Summary 至少 4 KPI
```python
# 27E Revenue / 27E EBITDA Margin / 27E Ending Cash / NI CAGR
ws_summary['B2'] = "=P&L!E5"               # 27E Revenue
ws_summary['B3'] = "=P&L!E10/P&L!E5"        # 27E EBITDA Margin
ws_summary['B4'] = "='Cash Flow'!E19"       # 27E Ending Cash
ws_summary['B5'] = "=(P&L!E18/P&L!B18)^(1/3)-1"   # NI CAGR (3 年)
```

### Dashboard 至少 3 个 chart
1. column 营收 + EBITDA
2. line 利润率趋势
3. area 期末现金

### 完整 openpyxl 代码示例（简化版）

```python
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, LineChart, AreaChart, Reference
from openpyxl.workbook.defined_name import DefinedName

GREEN_800 = '2E7D32'
GREEN_50 = 'E8F5E9'
BLUE_INPUT = '0000FF'
BLACK_FORMULA = '000000'
GREEN_XSHEET = '008000'

wb = Workbook()

# === Sheet 1: Assumptions (Inputs zone) ===
ws_a = wb.active
ws_a.title = 'Assumptions'
ws_a.sheet_properties.tabColor = 'FFC000'   # 黄
ws_a['A1'] = 'Assumption'
ws_a['B1'] = 'Value'
ws_a['C1'] = 'Source'
ws_a['A2'] = 'Revenue Y1 ($M)'; ws_a['B2'] = 100
ws_a['A3'] = 'Revenue Growth'; ws_a['B3'] = 0.05
ws_a['A4'] = 'Gross Margin'; ws_a['B4'] = 0.40
ws_a['A5'] = 'OpEx % Rev'; ws_a['B5'] = 0.20
ws_a['A6'] = 'Tax Rate'; ws_a['B6'] = 0.25
ws_a['A7'] = 'CapEx % Rev'; ws_a['B7'] = 0.08
ws_a['A8'] = 'D&A % Rev'; ws_a['B8'] = 0.05
ws_a['A9'] = 'DSO (days)'; ws_a['B9'] = 45
ws_a['A10'] = 'DIO (days)'; ws_a['B10'] = 60
ws_a['A11'] = 'DPO (days)'; ws_a['B11'] = 30
ws_a['A12'] = 'Dividend Payout'; ws_a['B12'] = 0.30
ws_a['A13'] = 'Y1 Cash'; ws_a['B13'] = 15
ws_a['A14'] = 'Y1 Debt'; ws_a['B14'] = 50
ws_a['A15'] = 'Y1 Equity'; ws_a['B15'] = 35

# 蓝色硬编 + Comment 来源
for r in range(2, 16):
    c = ws_a.cell(row=r, column=2)
    c.font = Font(name='Microsoft YaHei', color=BLUE_INPUT)
    c.number_format = '0.0%' if r in (3, 4, 5, 6, 7, 8, 12) else '#,##0'

# Named ranges
for name, ref in [('RevenueY1', 'Assumptions!$B$2'), ('GrowthRate', 'Assumptions!$B$3'),
                  ('GrossMargin', 'Assumptions!$B$4'), ('OpExPct', 'Assumptions!$B$5'),
                  ('TaxRate', 'Assumptions!$B$6'), ('CapExPct', 'Assumptions!$B$7'),
                  ('DAPct', 'Assumptions!$B$8'), ('DSO', 'Assumptions!$B$9'),
                  ('DIO', 'Assumptions!$B$10'), ('DPO', 'Assumptions!$B$11'),
                  ('DivPayout', 'Assumptions!$B$12'), ('Y1Cash', 'Assumptions!$B$13'),
                  ('Y1Debt', 'Assumptions!$B$14'), ('Y1Equity', 'Assumptions!$B$15')]:
    wb.defined_names[name] = DefinedName(name, attr_text=ref)

# === Sheet 2: P&L (Calc zone) ===
ws_pl = wb.create_sheet('P&L')
ws_pl.sheet_properties.tabColor = '4472C4'   # 蓝
ws_pl['A1'] = '($M)'
ws_pl['B1'] = '2024A'; ws_pl['C1'] = '2025E'; ws_pl['D1'] = '2026E'; ws_pl['E1'] = '2027E'
ws_pl['A2'] = 'Revenue'
ws_pl['B2'] = '=RevenueY1'
ws_pl['C2'] = '=B2*(1+GrowthRate)'
ws_pl['D2'] = '=C2*(1+GrowthRate)'
ws_pl['E2'] = '=D2*(1+GrowthRate)'
ws_pl['A3'] = 'COGS'
ws_pl['B3'] = '=-B2*(1-GrossMargin)'; ws_pl['C3'] = '=-C2*(1-GrossMargin)'
ws_pl['D3'] = '=-D2*(1-GrossMargin)'; ws_pl['E3'] = '=-E2*(1-GrossMargin)'
ws_pl['A4'] = 'Gross Profit'
ws_pl['B4'] = '=B2+B3'; ws_pl['C4'] = '=C2+C3'; ws_pl['D4'] = '=D2+D3'; ws_pl['E4'] = '=E2+E3'
ws_pl['A5'] = 'OpEx'
ws_pl['B5'] = '=-B2*OpExPct'; ws_pl['C5'] = '=-C2*OpExPct'
ws_pl['D5'] = '=-D2*OpExPct'; ws_pl['E5'] = '=-E2*OpExPct'
ws_pl['A6'] = 'EBITDA'
ws_pl['B6'] = '=B4+B5'; ws_pl['C6'] = '=C4+C5'; ws_pl['D6'] = '=D4+D5'; ws_pl['E6'] = '=E4+E5'
ws_pl['A7'] = 'D&A'
ws_pl['B7'] = '=-B2*DAPct'; ws_pl['C7'] = '=-C2*DAPct'
ws_pl['D7'] = '=-D2*DAPct'; ws_pl['E7'] = '=-E2*DAPct'
ws_pl['A8'] = 'EBIT'
ws_pl['B8'] = '=B6+B7'; ws_pl['C8'] = '=C6+C7'; ws_pl['D8'] = '=D6+D7'; ws_pl['E8'] = '=E6+E7'
ws_pl['A9'] = 'Interest Expense'
ws_pl['B9'] = "=-'Balance Sheet'!B13*0.06"   # Debt * 6%
ws_pl['A10'] = 'EBT'
ws_pl['B10'] = '=B8+B9'
ws_pl['A11'] = 'Tax'
ws_pl['B11'] = '=-B10*TaxRate'
ws_pl['A12'] = 'Net Income'
ws_pl['B12'] = '=B10+B11'

# 全部公式黑色字
for r in range(2, 13):
    for col in range(2, 6):
        ws_pl.cell(row=r, column=col).font = Font(name='Microsoft YaHei', color=BLACK_FORMULA)
        ws_pl.cell(row=r, column=col).number_format = '#,##0;(#,##0);"-"'

# === Sheet 3: Balance Sheet (Calc zone) ===
ws_bs = wb.create_sheet('Balance Sheet')
ws_bs.sheet_properties.tabColor = '4472C4'
ws_bs['A1'] = '($M)'
ws_bs['B1'] = '2024A'; ws_bs['C1'] = '2025E'; ws_bs['D1'] = '2026E'; ws_bs['E1'] = '2027E'
# Assets
ws_bs['A3'] = 'Assets'
ws_bs['A4'] = 'Cash'; ws_bs['B4'] = "='Cash Flow'!B19"
ws_bs['A5'] = 'AR'; ws_bs['B5'] = "=P&L!B2*DSO/365"
ws_bs['A6'] = 'Inventory'; ws_bs['B6'] = "=P&L!B3*DIO/365*-1"
ws_bs['A7'] = 'PPE'; ws_bs['B7'] = 0
ws_bs['A8'] = 'Total Assets'; ws_bs['B8'] = '=SUM(B4:B7)'
# Liabilities
ws_bs['A10'] = 'Liabilities'
ws_bs['A11'] = 'AP'; ws_bs['B11'] = "=-P&L!B3*DPO/365"
ws_bs['A12'] = 'Short-term Debt'
ws_bs['A13'] = 'Long-term Debt'; ws_bs['B13'] = '=Y1Debt'
ws_bs['A14'] = 'Total Liab'; ws_bs['B14'] = '=SUM(B11:B13)'
# Equity
ws_bs['A16'] = 'Equity'
ws_bs['A17'] = 'Paid-in Capital'; ws_bs['B17'] = '=Y1Equity'
ws_bs['A18'] = 'Retained Earnings'; ws_bs['B18'] = '=B8-B14-B17'
ws_bs['A19'] = 'Total Equity'; ws_bs['B19'] = '=B17+B18'
ws_bs['A20'] = 'Balance Check'; ws_bs['B20'] = '=IF(ABS(B8-B14-B19)<0.01, "OK", "IMBALANCED: "&TEXT(B8-B14-B19, "0.00"))'

# === Sheet 4: Cash Flow (Calc zone) ===
ws_cf = wb.create_sheet('Cash Flow')
ws_cf.sheet_properties.tabColor = '4472C4'
ws_cf['A1'] = '($M)'
ws_cf['B1'] = '2024A'; ws_cf['C1'] = '2025E'; ws_cf['D1'] = '2026E'; ws_cf['E1'] = '2027E'
ws_cf['A3'] = 'CFO'
ws_cf['A4'] = 'Net Income'; ws_cf['B4'] = "=P&L!B12"
ws_cf['A5'] = '+ D&A'; ws_cf['B5'] = "=-P&L!B7"
ws_cf['A6'] = '- AR change'; ws_cf['B6'] = '=-B5'   # Y1 无前期
ws_cf['A7'] = '- Inventory change'; ws_cf['B7'] = '=-B6'
ws_cf['A8'] = '+ AP change'; ws_cf['B8'] = '=-B7'
ws_cf['A9'] = 'CFO Total'; ws_cf['B9'] = '=SUM(B4:B8)'
ws_cf['A11'] = 'CFI'
ws_cf['A12'] = 'CapEx'; ws_cf['B12'] = '=-P&L!B2*CapExPct'
ws_cf['A13'] = 'CFI Total'; ws_cf['B13'] = '=B12'
ws_cf['A15'] = 'CFF'
ws_cf['A16'] = 'Debt change'; ws_cf['B16'] = 0
ws_cf['A17'] = 'Dividends'; ws_cf['B17'] = '=-P&L!B12*DivPayout'
ws_cf['A18'] = 'CFF Total'; ws_cf['B18'] = '=B16+B17'
ws_cf['A20'] = 'Opening Cash'; ws_cf['B20'] = '=Y1Cash'
ws_cf['A21'] = 'Net Change'; ws_cf['B21'] = '=B9+B13+B18'
ws_cf['A22'] = 'Ending Cash'; ws_cf['B22'] = '=B20+B21'
ws_cf['A23'] = 'Cash Recon'; ws_cf['B23'] = '=IF(ABS(B22-Balance_Sheet!B4)<0.01, "OK", "CF != BS CASH")'

# Y2+ 公式（示例 C 列）
ws_cf['C4'] = "=P&L!C12"
ws_cf['C5'] = "=-P&L!C7"
ws_cf['C6'] = '=-(Balance_Sheet!C5-Balance_Sheet!B5)'
ws_cf['C9'] = '=SUM(C4:C8)'
ws_cf['C12'] = '=-P&L!C2*CapExPct'
ws_cf['C13'] = '=C12'
ws_cf['C16'] = '=Balance_Sheet!C13-Balance_Sheet!B13'
ws_cf['C17'] = '=-P&L!C12*DivPayout'
ws_cf['C18'] = '=C16+C17'
ws_cf['C20'] = '=B22'   # Y2 Opening = Y1 Ending
ws_cf['C21'] = '=C9+C13+C18'
ws_cf['C22'] = '=C20+C21'

# === Sheet 5: Summary (Outputs zone) ===
ws_s = wb.create_sheet('Summary')
ws_s.sheet_properties.tabColor = '70AD47'   # 绿
ws_s['A1'] = 'KPI'; ws_s['B1'] = '2024A'; ws_s['C1'] = '2025E'; ws_s['D1'] = '2026E'; ws_s['E1'] = '2027E'
ws_s['A2'] = 'Revenue ($M)'; ws_s['B2'] = "=P&L!B2"
ws_s['A3'] = 'EBITDA Margin'; ws_s['B3'] = "=P&L!B6/P&L!B2"; ws_s['B3'].number_format = '0.0%'
ws_s['A4'] = 'Ending Cash'; ws_s['B4'] = "='Cash Flow'!B22"
ws_s['A5'] = 'NI CAGR (3y)'; ws_s['E5'] = "=(P&L!E12/P&L!B12)^(1/3)-1"; ws_s['E5'].number_format = '0.0%'

# Chart 1: Revenue + EBITDA (column)
chart1 = BarChart()
chart1.type = 'col'
chart1.title = 'Revenue & EBITDA'
data1 = Reference(ws_pl, min_col=2, min_row=2, max_row=6, max_col=5)
cats1 = Reference(ws_pl, min_col=1, min_row=2, max_row=6)
chart1.add_data(data1, titles_from_data=False)
chart1.set_categories(cats1)
chart1.anchor = 'A8'
ws_s.add_chart(chart1)

# Chart 2: Margin trend (line)
chart2 = LineChart()
chart2.title = 'Margin Trend'
chart2.add_data(Reference(ws_s, min_col=2, min_row=3, max_row=3, max_col=5))
chart2.set_categories(Reference(ws_s, min_col=2, min_row=1, max_row=1, max_col=5))
chart2.anchor = 'A24'
ws_s.add_chart(chart2)

# Chart 3: Ending Cash (area)
chart3 = AreaChart()
chart3.title = 'Ending Cash'
chart3.add_data(Reference(ws_s, min_col=2, min_row=4, max_row=4, max_col=5))
chart3.set_categories(Reference(ws_s, min_col=2, min_row=1, max_row=1, max_col=5))
chart3.anchor = 'A40'
ws_s.add_chart(chart3)

wb.calculation.fullCalcOnLoad = True
os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/3statement_model_20260721.xlsx')
```

## Recipe B — DCF valuation

### 表结构
- `Assumptions`：WACC、TaxRate、TerminalGrowth、NetDebt、SharesOut、Y1 FCF
- `FCF`：10 年 FCF 预测
- `WACC`：WACC 计算面板（cost of equity / cost of debt / weights）
- `DCF`：NPV + TV + equity bridge
- `Sensitivity`：5×5 grid（WACC × TerminalGrowth）

### Build order
```
Assumptions → FCF → WACC → DCF → Sensitivity
```

### 关键 named range
```python
wb.defined_names['WACC'] = DefinedName('WACC', attr_text='WACC!$B$10')
wb.defined_names['TaxRate'] = DefinedName('TaxRate', attr_text='Assumptions!$B$2')
wb.defined_names['TerminalGrowth'] = DefinedName('TerminalGrowth', attr_text='Assumptions!$B$3')
wb.defined_names['NetDebt'] = DefinedName('NetDebt', attr_text='Assumptions!$B$4')
wb.defined_names['SharesOut'] = DefinedName('SharesOut', attr_text='Assumptions!$B$5')
```

### TV 公式
```python
# FCF!K11 是 Y10 FCF
ws_dcf['B5'] = '=FCF!K11*(1+TerminalGrowth)/(WACC-TerminalGrowth)'
```

### 显式期 PV
```python
# NPV(WACC, FCF Y1-Y10)
ws_dcf['B4'] = '=NPV(WACC, FCF!B11:K11)'
# 或显式 SUMPRODUCT（审计可读）
ws_dcf['B4_alt'] = '=SUMPRODUCT(FCF!B11:K11/(1+WACC)^FCF!B2:K2)'
```

### 5×5 Sensitivity grid
```python
ws_s = wb.create_sheet('Sensitivity')
ws_s['A1'] = 'WACC \\ g'
ws_s['B1'] = '1.5%'; ws_s['C1'] = '2.0%'; ws_s['D1'] = '2.5%'; ws_s['E1'] = '3.0%'; ws_s['F1'] = '3.5%'
waccs = [0.075, 0.085, 0.095, 0.105, 0.115]
gs = [0.015, 0.020, 0.025, 0.030, 0.035]

for i, w in enumerate(waccs, start=2):
    ws_s.cell(row=i, column=1, value=f'{w*100:.1f}%')
    for j, g in enumerate(gs, start=2):
        # self-contained 公式重算 DCF（不能用 Excel Data Tables）
        # EV = SUMPRODUCT(FCF Y1-Y10 / (1+w)^t) + TV
        # TV = FCF_Y10 * (1+g) / (w-g)
        formula = (
            f'=SUMPRODUCT(FCF!$B$11:$K$11/(1+{w})^FCF!$B$2:$K$2)'
            f'+FCF!$K$11*(1+{g})/({w}-{g})'
        )
        c = ws_s.cell(row=i, column=j, value=formula)
        c.number_format = '#,##0'

# ColorScale: green=upside, red=downside
from openpyxl.formatting.rule import ColorScaleRule
ws_s.conditional_formatting.add(
    'B2:F6',
    ColorScaleRule(
        start_type='min', start_color='FFCDD2',
        mid_type='percentile', mid_value=50, mid_color='FFFFFF',
        end_type='max', end_color='C8E6C9'
    )
)
```

**不能用 Excel Data Tables**：openpyxl 对 Data Tables 支持不可靠，用 self-contained 公式复制每格。

## Recipe C — LBO model

### 表结构
- `Assumptions`：Entry EV / Entry EBITDA / Debt % / Sponsor equity / Exit multiple / Growth
- `S&U`（Sources & Uses）：资金来源与用途
- `Debt`：multi-tranche debt schedule
- `P&L`：Revenue → EBITDA → Interest → NI
- `CF`：CFO / Debt paydown / CFI / CFF
- `Exit`：Exit EV / Equity value / Returns
- `Returns`：IRR / MOIC

### Build order
```
Assumptions → S&U → P&L → Debt → CF → Exit → Returns
```

### S&U 必平衡
```python
ws_su['A1'] = 'Sources'; ws_su['D1'] = 'Uses'
ws_su['A2'] = 'Revolver'; ws_su['B2'] = 0
ws_su['A3'] = 'Term Loan A'; ws_su['B3'] = '=Assumptions!B5*0.3'
ws_su['A4'] = 'Term Loan B'; ws_su['B4'] = '=Assumptions!B5*0.4'
ws_su['A5'] = 'Mezzanine'; ws_su['B5'] = '=Assumptions!B5*0.1'
ws_su['A6'] = 'Sponsor Equity'; ws_su['B6'] = '=Assumptions!B6'
ws_su['A7'] = 'Total Sources'; ws_su['B7'] = '=SUM(B2:B6)'
ws_su['D2'] = 'Equity Purchase'; ws_su['E2'] = '=Assumptions!B3'
ws_su['D3'] = 'Refinance Debt'; ws_su['E3'] = '=Assumptions!B7'
ws_su['D4'] = 'Fees'; ws_su['E4'] = '=Assumptions!B3*0.02'
ws_su['D5'] = 'Total Uses'; ws_su['E5'] = '=SUM(E2:E4)'
ws_su['A9'] = 'Check'; ws_su['B9'] = '=IF(ABS(B7-E5)<1, "BALANCED", "S&U IMBALANCE: "&TEXT(B7-E5, "0"))'
```

### Sponsor equity 二选一
- **(a) Stated**：`B6 = Assumptions!SponsorEquity`（硬编）
- **(b) Solved**：`B6 = E5 - SUM(B2:B5)`（求解）
- **两者并用 = silent fee absorption bug**（fees 被 sponsor equity 吸收而不可见）

### Debt schedule 列
```
BeginningBalance | Mandatory amort | Cash sweep | EndingBalance | AverageBalance | InterestExpense
```

```python
# Y2 BeginningBalance = Y1 EndingBalance
ws_debt['C2'] = '=B5'   # Term Loan A Y2 Beg = Y1 End
# Mandatory amort（固定）
ws_debt['C3'] = '=-Assumptions!TLA_Amort'
# Cash sweep（可用现金的一定比例）
ws_debt['C4'] = '=MIN(C2-ABS(C3), MAX(0, CF!C9*Assumptions!SweepPct))'
# EndingBalance
ws_debt['C5'] = '=C2+C3+C4'
# AverageBalance
ws_debt['C6'] = '=(C2+C5)/2'
# InterestExpense
ws_debt['C7'] = '=-C6*Assumptions!TLA_Rate'
```

### Revolver
```python
# Revolver 必须 MIN(capacity, MAX(0, draw-paydown))
ws_debt['B4'] = '=MIN(Assumptions!RevolverCapacity, MAX(0, CF!B9*-1))'
```

### Iterative calc（仅在代数上合理的循环）
```python
# openpyxl 启用 iterative calc
wb.calculation.iterate = True
wb.calculation.iterateCount = 100
wb.calculation.iterateDelta = 0.001
# 仅用于 Revolver ↔ Interest ↔ CF 这种循环引用
# 不是 #REF! 或 divergent 的 band-aid
```

### Write-order surgery（环形写入死锁 100% CPU 时的 3 步）
1. **de-ring**：断开循环引用（临时改公式为 hardcode 或 0）
2. **write downstream**：写入所有下游公式
3. **re-ring**：恢复循环引用公式，启用 iterative calc

### IRR
```python
# 多中年分红用 XIRR
ws_returns['B5'] = '=XIRR(cashflows_range, dates_range)'
# 5 年 entry + exit 用 IRR (数组公式)
ws_returns['B6'] = '=IRR({-SponsorEquity,0,0,0,0,ExitEquity})'
```

## 公式决策表

| 场景 | 优选 | 替代 / 备注 |
|---|---|---|
| 不规则日期现金流贴现 | `XNPV(rate, values, dates)` | 替 `NPV` |
| 不规则日期 IRR | `XIRR(values, dates)` | 替 `IRR` |
| 现金流 2+ 符号变化 | `MIRR(values, financeRate, reinvestRate)` | 替 `IRR` |
| Lookup | `INDEX(range, MATCH(lookup, key, 0))` | 替 `VLOOKUP` |
| 条件求和 | `SUMIFS(sumRange, criteriaRange1, criterion1, ...)` | 替 `SUMPRODUCT((...))` |
| 除法 | `IFERROR(x/y, 0)` 或 `IF(y=0, 0, x/y)` | **必须**守每个 `/` |
| 数组 distinct count | `SUMPRODUCT(1/COUNTIF(range,range))` | |
| 跨表引用 | `'P&L'!B3`（带引号） | `&` 等特殊字符必须单引号 |
| 显式贴现（审计可读） | `SUMPRODUCT(values/(1+rate)^periods)` | 与 `NPV` 等价 |
| Sensitivity grid 单元格 | self-contained 公式复制 | **不能用 Excel Data Tables** |
| Scenario switch | `INDEX(Base:Downside, MATCH(Dropdown, ScenLabels, 0))` | dropdown 驱动 |

## 图表选择决策表

| 数据模式 | Chart type | 注意 |
|---|---|---|
| 趋势（时间，单 series） | `line` | 加 trendline |
| 趋势（时间，多 component） | `line`（多 series）或 `columnStacked` | |
| 类别比较（时间顺序） | `column` | **不要 `bar`** |
| Part-of-whole 分解 | `doughnut` | **替 `pie`**（pie 在 LibreOffice 有空白渲染回归） |
| Budget vs actual | `combo` + `combosplit=1` | 首 series bar，其余 line |
| 相关性 | `scatter` | |
| 营收+EBITDA 趋势 | `column` | financial-model Summary 标配 |
| 利润率趋势 | `line` | financial-model Summary 标配 |
| 期末现金轨迹 | `area` | financial-model Summary 标配 |
| Valuation 方法对比 | `bar` stacked（invisible first series + visible width series） | "Football field" |
| LP/GP 回报瀑布 | `waterfall` | total bar 用 `colors=` 约定 |
| Pipeline/conversion | `funnel` | cx extended chart |
| 层次结构 | `treemap` 或 `sunburst` | cx extended |
| 统计分布 | `boxWhisker` 或 `histogram` | cx extended |
| Sensitivity grid 配色 | 不画 chart，用 `colorscale` CF | green=upside, red=downside |

## financial-model 增量 Gate 4-6

### Gate 4 — statement integrity（3-statement & LBO）
```python
def gate_4_statement_integrity(path):
    wb = load_workbook(path)
    failures = []

    # BS Balance Check
    if 'Balance Sheet' in wb.sheetnames:
        ws = wb['Balance Sheet']
        for r in range(1, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == 'Balance Check':
                for col in range(2, 6):
                    v = ws.cell(row=r, column=col).value
                    if isinstance(v, str) and 'IMBALANCED' in v:
                        failures.append(f'BS Y{col-1} {v}')

    # CF Cash Recon
    if 'Cash Flow' in wb.sheetnames:
        ws = wb['Cash Flow']
        for r in range(1, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == 'Cash Recon':
                for col in range(2, 6):
                    v = ws.cell(row=r, column=col).value
                    if isinstance(v, str) and '!=' in v:
                        failures.append(f'CF Y{col-1} {v}')

    # S&U Balance (LBO)
    if 'S&U' in wb.sheetnames:
        ws = wb['S&U']
        for r in range(1, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == 'Check':
                v = ws.cell(row=r, column=2).value
                if isinstance(v, str) and 'IMBALANCE' in v:
                    failures.append(f'S&U {v}')

    assert not failures, f'Gate 4 FAIL: {failures}'
    print('✓ Gate 4 statement integrity pass')
```

### Gate 5 — cached-value sanity on valuation cells
```python
def gate_5_cached_values(path):
    wb = load_workbook(path, data_only=True)   # 读 cached value
    if 'DCF' in wb.sheetnames:
        ws = wb['DCF']
        for r in range(1, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                v = ws.cell(row=r, column=c).value
                if v is None:
                    continue
                if isinstance(v, str) and v.startswith('#'):
                    print(f'ERROR: DCF!{ws.cell(row=r,column=c).coordinate} = {v}')
                # cachedValue 非空、非 #OCLI_NOTEVAL
```

### Gate 6 — hardcode / zone discipline
- Calc zone 0 hardcodes（除 label）
- named range ≥3 个，每个被 ≥1 公式引用

```python
def gate_6_zone_discipline(path):
    wb = load_workbook(path)
    calc_sheets = ['P&L', 'Balance Sheet', 'Cash Flow', 'DCF', 'Debt']
    for sn in calc_sheets:
        if sn not in wb.sheetnames:
            continue
        ws = wb[sn]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)):
                    print(f'WARN: Calc {sn}!{cell.coordinate} hardcode: {cell.value}')

    # named range 被引用检查
    defined = list(wb.defined_names)
    print(f'Named ranges: {len(defined)}')
    for name in defined:
        # 扫描所有公式是否引用 name
        usage_count = 0
        for sn in wb.sheetnames:
            ws = wb[sn]
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith('='):
                        if name in cell.value:
                            usage_count += 1
        if usage_count == 0:
            print(f'WARN: named range "{name}" 未被引用')
```

### Gate 5b — visual audit via HTML preview（mandatory）
- 无 `###`
- 无截断
- 无 placeholder
- balance-check 显示 `OK` / `BALANCED`
- Dashboard chart 渲染
- y 轴 = 0 在 ARR/revenue 线
- Sensitivity grid 配色 green → red

### Gate 6.1 — token / placeholder sweep
```python
import re
def gate_6_1_token_sweep(path):
    wb = load_workbook(path)
    tokens = [r'\{\{.*?\}\}', r'\$fy\$\d+', r'<TODO>', r'xxxx', r'\bTBD\b']
    for sn in wb.sheetnames:
        ws = wb[sn]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    for tk in tokens:
                        if re.search(tk, cell.value):
                            print(f'ERROR: {sn}!{cell.coordinate} 含 placeholder: {cell.value}')
```

## financial-model 特定反模式

1. **AP 符号在 COGS**：AP 公式必须 negate
   ```python
   # ❌ ws_bs['B11'] = '=P&L!B3*DPO/365'   # COGS 是负数，AP 会变负
   # ✓ ws_bs['B11'] = '=-P&L!B3*DPO/365'   # negate 后 AP 为正
   ```

2. **`#NAME?` 不被 query/validate 抓**：跨表引用 `P&L!B3` 不加引号 → runtime `#NAME?`
   ```python
   # ❌ ws['A1'] = '=P&L!B3'               # & 特殊字符触发 #NAME?
   # ✓ ws['A1'] = "='P&L'!B3"              # 加单引号
   ```

3. **Iterative calc silent non-convergence**：`iterateCount=100` 在 cap 上收敛，即使真答案是 2×
   - 解决：检查迭代后结果是否合理，对比代数估算

4. **Batch-while-resident deadlock on circular writes**：close residents，按 Write-order surgery 两遍写

5. **Cross-sheet cached value stale in `view html`**：下游非 resident re-set

6. **Sensitivity-grid build order 仍要紧**：先建 FCF+WACC+DCF，再 grid 单独非 resident batch

7. **`BS.Cash` = CF ending cash 永远（含 Y1）**
   ```python
   # ❌ ws_bs['B4'] = 100                   # Y1 Cash hardcode
   # ✓ ws_bs['B4'] = "='Cash Flow'!B22"     # 永远引用 CF
   ```

8. **Y2+ `Opening Cash` = 前期 `Ending Cash`**
   ```python
   ws_cf['C20'] = '=B22'   # Y2 Opening = Y1 Ending
   ws_cf['D20'] = '=C22'   # Y3 Opening = Y2 Ending
   ```

9. **Waterfall "total" bars 不能程序化标记**：用 `colors=` 约定（手动指定每段颜色）

10. **DCF per-share 当 `SharesOut` 是公式**：加蓝色假设 cell，把 `SharesOut` named range 指向计算 cell

11. **`calc.iterate` 不是 `#REF!` / divergent 的 band-aid**：raise `iterateCount` 隐藏 bug
    - 如果迭代后结果不合理，应检查模型代数而非调高 iterateCount

## 参考

- [openpyxl 官方文档](https://openpyxl.readthedocs.io/)
- [SKILL.md](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md)
- [basic.md](file:///<project_root>/.agents/skills/office_xlsx/references/basic.md) - 基础元素
- [conditional_formatting.md](file:///<project_root>/.agents/skills/office_xlsx/references/conditional_formatting.md) - sensitivity grid CF
- [data_dashboard.md](file:///<project_root>/.agents/skills/office_xlsx/references/data_dashboard.md) - financial model dashboard

**说明**：OfficeCli 是 .NET CLI 项目，本项目用 openpyxl 等价实现其设计哲学。OfficeCli 的 CLI 命令（如 `officecli chart add`）已翻译为 openpyxl API 调用。CFO 4-color code / Three-zone architecture / QA Delivery Gate / Incremental Execution 方法论保留自 OfficeCli 项目。
