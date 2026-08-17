# Excel 数据仪表盘参考 (data_dashboard)

本文件是 [office_xlsx skill](file:///f:/<project_root>/.agents/skills/office_xlsx/SKILL.md) 的数据仪表盘场景参考。借鉴 OfficeCli 项目的设计哲学（QA Delivery Gate / Incremental Execution），用 openpyxl 实现 KPI 卡 + chart + sparkline + conditional formatting 的数据可视化。

## 何时用此 skill

trigger 关键词：
- `dashboard` / `KPI dashboard` / `analytics dashboard`
- `executive dashboard` / `metrics dashboard`
- `CSV to dashboard` / `数据可视化`

## 何时回退到 basic.md 或 financial_model.md

| 场景 | 路由 |
|---|---|
| 单表 CSV + 格式化 tracker（≤1 chart） | [basic.md](file:///f:/<project_root>/.agents/skills/office_xlsx/references/basic.md) |
| 3-statement / DCF / LBO | [financial_model.md](file:///f:/<project_root>/.agents/skills/office_xlsx/references/financial_model.md) |
| 周报 ≤1 chart <10 行 | basic.md |
| 多 KPI + 多 chart + sparkline + CF | **本文件** |

## 5 条 non-negotiable 原则

### 1. KPI 全部公式驱动
```python
# ❌ hardcode 计算值
ws_d['B2'] = 1234567   # Total Revenue 硬编

# ✓ 公式引用 Data/Summary sheet
ws_d['B2'] = '=SUM(Data!B2:B1000)'
ws_d['B3'] = '=AVERAGE(Summary!C2:C13)'
ws_d['B4'] = '=IFERROR((Summary!C13-Summary!C12)/Summary!C12, 0)'
```

### 2. chart series 必 cell-range 引用
```python
# ❌ inline data 仅限 5 分钟 demo
chart.add_data([10, 20, 30, 40, 50])

# ✓ cell range 引用
data = Reference(ws_summary, min_col=2, min_row=1, max_row=13, max_col=3)
chart.add_data(data, titles_from_data=True)
```

### 3. Dashboard-first 架构
- KPI label / value / chart / sparkline 全在 Dashboard sheet
- 原始数据在 upstream Data / Summary sheet
- Dashboard 不含原始数据，只引用

### 4. chart source 必须可见单元格
LibreOffice 不渲染隐藏列/表的公式，chart 会空白。
```python
# ❌ 引用隐藏列
chart.add_data(Reference(ws, min_col=5, ...))   # E 列被 hidden

# ✓ 聚合到可见 Summary，只隐藏非 chart-source 的 helper 列
ws_summary['B2'] = '=SUMIFS(Data!E:E, ...)'   # 聚合到 Summary B 列
chart.add_data(Reference(ws_summary, min_col=2, ...))   # 引用 Summary B
```

### 5. 数据量驱动复杂度

| Rows | KPIs | Charts | Sparklines | CF rules | Preset |
|---|---|---|---|---|---|
| <10 | 1-2 | 1 | 跳过 | 0-1 | `minimal` |
| 10-50 | 2-3 | 2 | 仅时序 | 1-2 | `dashboard` |
| 50-200 | 3-5 | 2-3 | 仅时序 | 2-3 | `dashboard` |
| 200+ | 3-5 | 3 | 仅时序 | 3-4 | `dashboard` |

## 必设项

### `calc.fullCalcOnLoad=true`
```python
# ✓ 通过 wb.calculation 属性
wb.calculation.fullCalcOnLoad = True

# ❌ 不要 raw-set <calcPr> XML（会重复元素 validate 失败）
```

### `activeTab=N`（0-based，必须最后设）
```python
# 必须在所有 sheet 存在后再设
wb.active = wb.sheetnames.index('Dashboard')   # 0-based index
# 或
wb['Dashboard'].sheet_view.tabSelected = True
```

### 每次上游编辑后刷新下游 cachedValue
```python
# fullCalcOnLoad 只 schedule runtime recalc，不刷新 build-time cache
# 上游 cell 改后，下游公式 cachedValue stale
# 解决：re-touch 每个 downstream cell
def refresh_downstream(wb, sheet_name, cells):
    ws = wb[sheet_name]
    for coord in cells:
        c = ws[coord]
        v = c.value
        c.value = None
        c.value = v   # re-touch
```

## Layout 三种 pattern

### Pattern 1: executive summary
```
A1:H4   KPI 条（4-6 KPI 卡横排）
A6:    Chart 从 row 6 堆叠
```
适用：董事会 / 投资者 / 一页报告

### Pattern 2: ops console
```
A:B     KPI 列（label/value 垂直堆）
C:L     Chart 占据右侧
```
适用：运营监控 / 实时数据 / 仪表盘墙

### Pattern 3: scorecard
```
2×3 grid KPI 卡（≥6 KPI）
每卡：label / value / sparkline
```
适用：BSC / OKR / 多维度评分

## 复杂度缩放表

| Rows | KPIs | Charts | Sparklines | CF rules | Preset |
|---|---|---|---|---|---|
| <10 | 1-2 | 1 | 跳过 | 0-1 | `minimal` |
| 10-50 | 2-3 | 2 | 仅时序 | 1-2 | `dashboard` |
| 50-200 | 3-5 | 2-3 | 仅时序 | 2-3 | `dashboard` |
| 200+ | 3-5 | 3 | 仅时序 | 3-4 | `dashboard` |

## KPI 卡 anatomy

### 结构
```
[label]   ← 小灰（size=9, color=666666, bold）
[value]   ← 大粗（size=24, bold, numFmt, color 表色调）
```

### 一行浅 fill 模拟"卡"
```python
from openpyxl.styles import Font, PatternFill, Alignment

def kpi_card(ws, row, col, label, value_formula, num_fmt='#,##0', value_color='2E7D32'):
    # Label
    lc = ws.cell(row=row, column=col, value=label)
    lc.font = Font(name='Microsoft YaHei', size=9, color='666666', bold=True)
    lc.alignment = Alignment(horizontal='left', vertical='center')
    lc.fill = PatternFill('solid', fgColor='F0F4FF')

    # Value
    vc = ws.cell(row=row + 1, column=col, value=value_formula)
    vc.font = Font(name='Microsoft YaHei', size=24, bold=True, color=value_color)
    vc.number_format = num_fmt
    vc.alignment = Alignment(horizontal='left', vertical='center')
    vc.fill = PatternFill('solid', fgColor='F0F4FF')

    # Row heights
    ws.row_dimensions[row].height = 18
    ws.row_dimensions[row + 1].height = 36
```

### value 列宽按 cachedValue 定
| Value 位数 | 列宽 |
|---|---|
| 4-6 位 | 22-24 |
| 7-9 位（百万） | 26-30 |
| 10+ 位（亿/十亿） | 32-36 |
| 百亿+ 货币 + landscape | **40-44** |

公式起点：`ceil((visible_chars + 2) * 1.3)`

### Sparkline 列 12
```python
ws.column_dimensions['C'].width = 12   # sparkline 专用列
```

## Chart title 宽度预算

```
chart.width ≥ ceil(title.length × 0.18)
```
- 35 字标题需 width ≥ 7，安全 10-12
- 太窄标题会截断

## Print-ready delivery（board/investor/一页/董事会）

### Print_Area scoped to Dashboard
```python
from openpyxl.workbook.defined_name import DefinedName
wb.defined_names['_xlnm.Print_Area'] = DefinedName(
    '_xlnm.Print_Area',
    attr_text='Dashboard!$A$1:$H$36'
)
```

### pageSetUpPr fitToPage
```python
# openpyxl 通过 ws.sheet_properties.pageSetUpPr
from openpyxl.worksheet.properties import PageSetupProperties
ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
```

### pageSetup landscape + fitToWidth/Height
```python
ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
ws.page_setup.paperSize = ws.PAPERSIZE_A4   # 9
ws.page_setup.fitToWidth = 1
ws.page_setup.fitToHeight = 1
ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
```

### 隐藏所有非 Dashboard sheet
```python
# 单 Print_Area scope 不够，必须隐藏非 Dashboard sheet
for sn in wb.sheetnames:
    if sn != 'Dashboard':
        wb[sn].sheet_state = 'hidden'
```

## 典型 recipe — 销售 KPI dashboard

```python
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import DataBarRule, ColorScaleRule

wb = Workbook()

# === Sheet 1: Data (原始数据) ===
ws_data = wb.active
ws_data.title = 'Data'
ws_data.append(['Date', 'Region', 'Product', 'Revenue', 'Units'])
# 模拟 12 个月数据
import random
from datetime import datetime, timedelta
regions = ['华北', '华东', '华南', '西部']
products = ['A', 'B', 'C']
for i in range(120):
    d = datetime(2026, 1, 1) + timedelta(days=i * 3)
    r = random.choice(regions)
    p = random.choice(products)
    rev = random.randint(1000, 5000)
    units = random.randint(10, 50)
    ws_data.append([d, r, p, rev, units])

# === Sheet 2: Summary (聚合) ===
ws_sum = wb.create_sheet('Summary')
ws_sum.append(['Month', 'Total Revenue', 'Total Units', 'Avg Order Value'])
for m in range(1, 13):
    month_start = datetime(2026, m, 1)
    if m == 12:
        month_end = datetime(2026, 12, 31)
    else:
        month_end = datetime(2026, m + 1, 1) - timedelta(days=1)
    ws_sum.append([
        month_start,
        f'=SUMIFS(Data!D:D, Data!A:A, ">="&DATE({month_start.year},{month_start.month},1), Data!A:A, "<"&DATE({month_start.year + (1 if m == 12 else month_start.month + 1)},{1 if m == 12 else month_start.month + 1},1))',
        f'=SUMIFS(Data!E:E, Data!A:A, ">="&DATE({month_start.year},{month_start.month},1), Data!A:A, "<"&DATE({month_start.year + (1 if m == 12 else month_start.month + 1)},{1 if m == 12 else month_start.month + 1},1))',
        f'=IFERROR(B{m+1}/C{m+1}, 0)'
    ])
    ws_sum.cell(row=m + 1, column=4).number_format = '¥#,##0.00'

# === Sheet 3: Dashboard (KPI + chart + CF) ===
ws_d = wb.create_sheet('Dashboard')
ws_d.sheet_view.tabSelected = True

# Title
ws_d['A1'] = 'Sales KPI Dashboard - 2026'
ws_d['A1'].font = Font(name='Microsoft YaHei', size=18, bold=True, color='2E7D32')
ws_d.merge_cells('A1:H1')

# KPI 卡（4 个横排）
def kpi_card(ws, row, col, label, formula, num_fmt='#,##0', color='2E7D32'):
    lc = ws.cell(row=row, column=col, value=label)
    lc.font = Font(name='Microsoft YaHei', size=9, color='666666', bold=True)
    lc.fill = PatternFill('solid', fgColor='F0F4FF')
    vc = ws.cell(row=row + 1, column=col, value=formula)
    vc.font = Font(name='Microsoft YaHei', size=20, bold=True, color=color)
    vc.number_format = num_fmt
    vc.fill = PatternFill('solid', fgColor='F0F4FF')

kpi_card(ws_d, 3, 1, 'Total Revenue ($)', '=SUM(Summary!B2:B13)', '¥#,##0', '2E7D32')
kpi_card(ws_d, 3, 3, 'Total Units', '=SUM(Summary!C2:C13)', '#,##0', '4472C4')
kpi_card(ws_d, 3, 5, 'Avg Order Value', '=IFERROR(B4/D4, 0)', '¥#,##0.00', '4CAF50')
kpi_card(ws_d, 3, 7, 'Peak Month Rev', '=MAX(Summary!B2:B13)', '¥#,##0', 'E65100')

# 列宽
for col_letter, w in [('A', 18), ('B', 16), ('C', 18), ('D', 14),
                       ('E', 18), ('F', 16), ('G', 18), ('H', 16)]:
    ws_d.column_dimensions[col_letter].width = w

# Chart 1: 月度营收趋势 (line)
chart1 = LineChart()
chart1.title = 'Monthly Revenue Trend'
chart1.y_axis.title = 'Revenue'
chart1.x_axis.title = 'Month'
data1 = Reference(ws_sum, min_col=2, min_row=1, max_row=13)
cats1 = Reference(ws_sum, min_col=1, min_row=2, max_row=13)
chart1.add_data(data1, titles_from_data=True)
chart1.set_categories(cats1)
chart1.anchor = 'A7'
chart1.width = 18
chart1.height = 10
ws_d.add_chart(chart1)

# Chart 2: 月度单位销量 (bar)
chart2 = BarChart()
chart2.type = 'col'
chart2.title = 'Monthly Units Sold'
data2 = Reference(ws_sum, min_col=3, min_row=1, max_row=13)
chart2.add_data(data2, titles_from_data=True)
chart2.set_categories(cats1)
chart2.anchor = 'A24'
chart2.width = 18
chart2.height = 10
ws_d.add_chart(chart2)

# CF: Summary Revenue 列加 data bar
ws_sum.conditional_formatting.add(
    'B2:B13',
    DataBarRule(start_type='min', end_type='max', color='2E7D32', showValue=True)
)

# CF: Summary AOV 列加 color scale
ws_sum.conditional_formatting.add(
    'D2:D13',
    ColorScaleRule(
        start_type='min', start_color='FFCDD2',
        mid_type='percentile', mid_value=50, mid_color='FFFFFF',
        end_type='max', end_color='C8E6C9'
    )
)

# freeze panes
ws_d.freeze_panes = 'A3'

# fullCalcOnLoad + activeTab 最后设
wb.calculation.fullCalcOnLoad = True
wb.active = wb.sheetnames.index('Dashboard')

os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/sales_dashboard_20260721.xlsx')
```

## 典型 recipe — 运营 ops console

```python
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, Reference

wb = Workbook()

# Data sheet
ws_data = wb.active
ws_data.title = 'Data'
ws_data.append(['Time', 'Server', 'CPU %', 'Memory %', 'Latency (ms)'])
# 模拟数据...

# Dashboard sheet (ops console)
ws_d = wb.create_sheet('Dashboard')
ws_d['A1'] = 'Ops Console'
ws_d['A1'].font = Font(name='Microsoft YaHei', size=16, bold=True, color='2E7D32')
ws_d.merge_cells('A1:L1')

# KPI 列 (A:B)
kpis = [
    ('Avg CPU %', '=AVERAGE(Data!C:C)', '0.0%', '4472C4'),
    ('Peak CPU %', '=MAX(Data!C:C)', '0.0%', 'C62828'),
    ('Avg Memory %', '=AVERAGE(Data!D:D)', '0.0%', '4472C4'),
    ('Peak Latency (ms)', '=MAX(Data!E:E)', '#,##0', 'C62828'),
    ('Total Records', '=COUNTA(Data!A:A)-1', '#,##0', '2E7D32'),
]
for i, (label, formula, fmt, color) in enumerate(kpis, start=3):
    ws_d.cell(row=i, column=1, value=label).font = Font(name='Microsoft YaHei', size=9, color='666666', bold=True)
    ws_d.cell(row=i, column=2, value=formula).font = Font(name='Microsoft YaHei', size=16, bold=True, color=color)
    ws_d.cell(row=i, column=2).number_format = fmt

# Chart 占 C:L
chart = BarChart()
chart.type = 'col'
chart.title = 'CPU Trend'
chart.anchor = 'C3'
chart.width = 24
chart.height = 12
ws_d.add_chart(chart)

# 列宽
ws_d.column_dimensions['A'].width = 22
ws_d.column_dimensions['B'].width = 16
for col in 'CDEFGHIJKL':
    ws_d.column_dimensions[col].width = 10

wb.calculation.fullCalcOnLoad = True
wb.active = wb.sheetnames.index('Dashboard')
wb.save('workspace/office_xlsx/ops_console_20260721.xlsx')
```

## data-dashboard Gate 1-8

### Gate 1 — KPI formula coverage
```python
def gate_1_kpi_formula(wb, plan_kpi_count):
    ws = wb['Dashboard']
    formula_count = 0
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith('='):
                formula_count += 1
    assert formula_count >= plan_kpi_count, f'Gate 1 FAIL: {formula_count} formulas < {plan_kpi_count}'
    print(f'✓ Gate 1 KPI formula: {formula_count} >= {plan_kpi_count}')
```

### Gate 2 — Chart count + data + plausible title width
```python
def gate_2_charts(wb, plan_chart_count):
    ws = wb['Dashboard']
    charts = ws._charts
    assert len(charts) >= plan_chart_count, f'Gate 2 FAIL: {len(charts)} charts < {plan_chart_count}'
    for chart in charts:
        # 检查 title
        if chart.title is None:
            print(f'WARN: chart 无 title')
        # 检查 series 有 data
        if not chart.series:
            print(f'WARN: chart 无 series')
    print(f'✓ Gate 2 charts: {len(charts)} >= {plan_chart_count}')
```

### Gate 3 — Chart series names populated
```python
def gate_3_series_names(wb):
    ws = wb['Dashboard']
    for i, chart in enumerate(ws._charts):
        for j, s in enumerate(chart.series):
            if s.title is None:
                print(f'WARN: chart {i} series {j} 无 name')
```

### Gate 4 — CF rules on Data sheet（10+ rows）
```python
def gate_4_cf(wb):
    if 'Data' not in wb.sheetnames:
        return
    ws = wb['Data']
    if ws.max_row < 10:
        return
    cf_count = len(list(ws.conditional_formatting._cf_rules))
    assert cf_count >= 1, 'Gate 4 FAIL: Data sheet 10+ rows 但无 CF'
```

### Gate 5 — activeTab and fullCalcOnLoad set
```python
def gate_5_settings(wb):
    assert wb.calculation.fullCalcOnLoad == True, 'Gate 5: fullCalcOnLoad 未设'
    dashboard_idx = wb.sheetnames.index('Dashboard')
    # activeTab 检查
    print(f'✓ Gate 5 fullCalcOnLoad + active sheet: {wb.active.title}')
```

### Gate 6 — Placeholder sweep
```python
import re
def gate_6_placeholder(wb):
    tokens = [r'\{\{.*?\}\}', r'<TODO>', r'xxxx', r'\bTBD\b']
    for sn in wb.sheetnames:
        ws = wb[sn]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    for tk in tokens:
                        if re.search(tk, cell.value):
                            print(f'ERROR: {sn}!{cell.coordinate} placeholder: {cell.value}')
```

### Gate 7 — Visual delivery floor
- `view html` 走查（或 openpyxl 抽查）
- 无 `###`（列宽不够）
- 无截断 title/label
- chart 渲染（series 非空）

### Gate 8 — Formula sanity
```python
def gate_8_formula_sanity(path):
    wb = load_workbook(path, data_only=True)   # 读 cachedValue
    ws = wb['Dashboard']
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith('#'):
                print(f'ERROR: Dashboard!{cell.coordinate} = {cell.value}')
            # cachedValue real, not stale/error
```

## data-dashboard 特定反模式 D-1..D-17

### D-1: `combosplit` 是 DeferredAddKey
仅 add 时设，后续修改不生效。Combo chart 拆分要在 `add_data` 时指定。

### D-2: `referenceline` 格式 `value:color:label:dash`
参考线格式严格，用冒号分隔 4 段：`100:FF0000:Target:Solid`。

### D-3: Scatter 不接受 `series1.xValues`
Scatter chart 的 x 值通过 `Reference` 传给 `chart.x_axis.title` 或 `set_categories`，不接受 series-level xValues。

### D-5: Dashboard 列宽默认 8.43，24pt bold KPI 显示 `###`
```python
# ❌ 不设列宽
ws_d['B2'] = 12345678   # 24pt bold → ###

# ✓ 显式设宽
ws_d.column_dimensions['B'].width = 28
ws_d['B2'] = 12345678
ws_d['B2'].font = Font(name='Microsoft YaHei', size=24, bold=True)
```

### D-6: `raw-set activeTab` 必须是最后一次 mutation
```python
# ❌ 先设 activeTab 再加 sheet
wb.active = 0
ws_new = wb.create_sheet('NewSheet')   # activeTab 可能被重置

# ✓ 所有 sheet 创建完后再设
ws_d = wb.create_sheet('Dashboard')
# ... 所有 sheet 操作完成
wb.active = wb.sheetnames.index('Dashboard')
wb.save(path)
```

### D-7: `calc.fullCalcOnLoad` via raw-set 创建重复 `<calcPr>`
```python
# ❌ raw-set XML
ws._xml_set('<calcPr fullCalcOnLoad="1"/>')   # 重复元素 validate 失败

# ✓ 通过 wb.calculation 属性
wb.calculation.fullCalcOnLoad = True
```

### D-8: LibreOffice 不渲染隐藏列/表公式 → chart 空白
```python
# ❌ 引用隐藏列
ws_data.column_dimensions['E'].hidden = True
chart.add_data(Reference(ws_data, min_col=5, ...))   # LibreOffice chart 空白

# ✓ 聚合到可见 Summary
ws_sum['B2'] = '=SUMIFS(Data!E:E, ...)'
chart.add_data(Reference(ws_sum, min_col=2, ...))
```

### D-9: `chartType=pie` 在 LibreOffice 空白渲染
```python
# ❌ pie chart
chart = PieChart()

# ✓ doughnut chart（替 pie）
from openpyxl.chart import DoughnutChart
chart = DoughnutChart()
```

### D-10: `SUMIFS`/`AVERAGEIFS` 带日期条件是字符串时 silent fail
```python
# ❌ 日期条件作为字符串
ws['B2'] = '=SUMIFS(Data!D:D, Data!A:A, ">=2026-01-01")'   # silent fail

# ✓ 用 DATE() 或 DATEVALUE() 包
ws['B2'] = '=SUMIFS(Data!D:D, Data!A:A, ">="&DATE(2026,1,1))'
ws['B2'] = '=SUMIFS(Data!D:D, Data!A:A, ">="&DATEVALUE("2026-01-01"))'
```

### D-11: Summary sheet 百分比公式显示原始小数
```python
# ❌ 公式设了但 numFmt 没设
ws['B2'] = '=C2/D2'   # 显示 0.1234 而非 12.34%

# ✓ 公式同 set 调用设 numFmt
ws['B2'] = '=C2/D2'
ws['B2'].number_format = '0.0%'
```

### D-12: `import --header` 设 freeze + AutoFilter 但不设列宽
```python
# 从 CSV import 时自动设 freeze + AutoFilter，但列宽默认 8.43
# 必须手设
ws.column_dimensions['A'].width = 18
ws.column_dimensions['B'].width = 14
```

### D-13: Sparkline `highpoint=FF0000` 当颜色
```python
# ❌ highpoint 当颜色用
sg.highpoint = 'FF0000'   # 报错

# ✓ highPoint 是 bool，颜色单独属性
sg.highPoint = True
sg.highMarkerColor = 'FF0000'
```

### D-14: Sparkline 跨部门/区域（无序）无意义
```python
# ❌ 跨部门 sparkline（顺序无意义）
sparkline for ['Sales', 'Engineering', 'HR']   # 无序对比

# ✓ 只在时序行用 sparkline
sparkline for ['Jan', 'Feb', 'Mar', ..., 'Dec']   # 时序有意义
```

### D-15: 空 chart `add` 被拒 `Chart requires a 'data' property`
```python
# ❌ 空 chart 直接 add
chart = BarChart()
ws.add_chart(chart)   # 报错

# ✓ 先 add_data 再 add_chart
chart = BarChart()
chart.add_data(Reference(...))
ws.add_chart(chart, 'A5')
```

### D-16: `fullCalcOnLoad=true` 只 schedule runtime recalc，不刷 build-time cachedValue
```python
# fullCalcOnLoad 只在 Excel 打开时触发重算
# build-time cachedValue（openpyxl 写入时）不刷新
# 解决：手动 re-touch downstream cell
def refresh_cell(ws, coord):
    c = ws[coord]
    v = c.value
    c.value = None
    c.value = v
```

### D-17: 内置 calc 引擎不求值 `SUMPRODUCT((A2:A97=X)*C2:C97*D2:D97)` array-predicate 形式
```python
# ❌ openpyxl/Excel calc 引擎可能不求值此 array-predicate 形式
ws['B2'] = '=SUMPRODUCT((Data!A2:A97="2026-07")*Data!C2:C97*Data!D2:D97)'

# ✓ 改写为 helper column + SUMIF
# Data!F2 = =IF(A2="2026-07", C2*D2, 0)
# Summary!B2 = =SUM(Data!F2:F97)
```

## 与其他元素协同

### Dashboard + CF
- KPI value 用 ColorScaleRule（高=绿，低=红）
- Chart 不受 CF 影响

### Dashboard + Sparkline
- Sparkline 列宽 12
- 行高 ≥ 20
- 只用时序数据

### Dashboard + Chart
- chart anchor 区域显式设
- chart series 不可变，改 series 需 remove + add

## 参考

- [openpyxl 官方文档](https://openpyxl.readthedocs.io/)
- [SKILL.md](file:///f:/<project_root>/.agents/skills/office_xlsx/SKILL.md)
- [basic.md](file:///f:/<project_root>/.agents/skills/office_xlsx/references/basic.md) - 基础元素
- [conditional_formatting.md](file:///f:/<project_root>/.agents/skills/office_xlsx/references/conditional_formatting.md) - KPI 状态指示器
- [financial_model.md](file:///f:/<project_root>/.agents/skills/office_xlsx/references/financial_model.md) - financial model dashboard

**说明**：OfficeCli 是 .NET CLI 项目，本项目用 openpyxl 等价实现其设计哲学。OfficeCli 的 CLI 命令已翻译为 openpyxl API 调用。QA Delivery Gate / Incremental Execution / 反模式 D-1..D-17 方法论保留自 OfficeCli 项目。
