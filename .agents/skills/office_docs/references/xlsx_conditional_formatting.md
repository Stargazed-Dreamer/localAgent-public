# Excel 条件格式参考 (conditional_formatting)

本文件是 [office_xlsx skill](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md) 的条件格式参考。openpyxl 提供 4 种条件格式 rule，本文件覆盖全部 4 种的完整规范 + 代码示例 + 应用场景 + 坑。

## 3 flavors 完整规范

| Flavor | 类 | 关键 props | 用途 |
|---|---|---|---|
| **Color scale** | `ColorScaleRule` | `start_type` / `start_color` / `mid_type` / `mid_color` / `end_type` / `end_color` | 热力图——率、增长、sensitivity grid |
| **Data bar** | `DataBarRule` | `start_type` / `end_type` / `color`，建议显式 `start_value='0'` `end_value='<plausible>'` | 量级条——销售额、支出 |
| **Formula rule** | `FormulaRule` | `formula=[...]` + `fill` + `font` | 自定义业务规则——行级条件高亮 |

## 第四种：IconSet

| Flavor | 类 | 关键 props | 用途 |
|---|---|---|---|
| **Icon set** | `IconSetRule` | `icon_style`（如 `'3Arrows'`）/ `type` / `values` | 状态指示器——3 箭头 / 红黄绿 |

## Dashboard 推荐语义色一致

| 语义 | 填充色 | 字色 |
|---|---|---|
| good / positive | `C8E6C9` | `2E7D32` |
| bad / negative | `FFCDD2` | `C62828` |
| neutral | `F5F5F5` | `666666` |
| warning / 需关注 | `FFF3E0` | `E65100` |
| info / 提示 | `E3F2FD` | `1565C0` |

## 原则：sparingly 用

**整簿每格都涂色 = 读者什么也读不出**。条件格式的作用是让"应该被注意"的 cell 跳出来，不是给所有 cell 上色。

反模式：
```python
# ❌ 给整个数据区都加 ColorScale，每行都涂色 → 读者无法识别重点
ws.conditional_formatting.add('A1:Z1000', ColorScaleRule(...))

# ✓ 只给 KPI 列 / 关键指标列加 CF
ws.conditional_formatting.add('B2:B100', ColorScaleRule(...))   # 只 Revenue 列
ws.conditional_formatting.add('E2:E100', FormulaRule(...))       # 只逾期行
```

## 完整代码示例

### 1. Color scale — 热力图

```python
from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Font, PatternFill, Alignment
import os

wb = Workbook()
ws = wb.active
ws.title = 'Sensitivity'

# 模拟 5×5 sensitivity grid（行 WACC 7.5%-11.5%，列 g 1.5%-3.5%）
ws['A1'] = 'WACC \\ g'
ws['B1'] = '1.5%'
ws['C1'] = '2.0%'
ws['D1'] = '2.5%'
ws['E1'] = '3.0%'
ws['F1'] = '3.5%'

waccs = [0.075, 0.085, 0.095, 0.105, 0.115]
gs = [0.015, 0.020, 0.025, 0.030, 0.035]

for i, w in enumerate(waccs, start=2):
    ws.cell(row=i, column=1, value=f'{w*100:.1f}%')
    for j, g in enumerate(gs, start=2):
        # 简化 DCF：每格 self-contained 公式
        # 真实场景用 =FCF_K*(1+g)/(w-g)
        ev = 100 * (1 + g) / (w - g)   # 模拟值
        c = ws.cell(row=i, column=j, value=ev)
        c.number_format = '#,##0'

# Color scale: green=upside, white=mid, red=downside
ws.conditional_formatting.add(
    'B2:F6',
    ColorScaleRule(
        start_type='min', start_color='FFCDD2',   # 红
        mid_type='percentile', mid_value=50, mid_color='FFFFFF',   # 白
        end_type='max', end_color='C8E6C9'        # 绿
    )
)

# 表头样式
header_font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF')
header_fill = PatternFill('solid', fgColor='2E7D32')
for col in range(1, 7):
    c = ws.cell(row=1, column=col)
    c.font = header_font
    c.fill = header_fill
    c.alignment = Alignment(horizontal='center')

# 列宽
ws.column_dimensions['A'].width = 14
for col in 'BCDEF':
    ws.column_dimensions[col].width = 12

os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/sensitivity_demo_20260721.xlsx')
```

### 2. Data bar — 量级条

```python
from openpyxl import Workbook
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Font, PatternFill, Alignment
import os

wb = Workbook()
ws = wb.active
ws.title = 'Sales'

# 表头
ws.append(['Region', 'Q1', 'Q2', 'Q3', 'Q4', 'Total'])
header_font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF')
header_fill = PatternFill('solid', fgColor='2E7D32')
for col in range(1, 7):
    c = ws.cell(row=1, column=col)
    c.font = header_font
    c.fill = header_fill
    c.alignment = Alignment(horizontal='center')

# 数据
data = [
    ['华北', 120, 135, 142, 158, None],
    ['华东', 280, 295, 310, 325, None],
    ['华南', 195, 210, 225, 240, None],
    ['西部', 85, 92, 98, 105, None],
    ['东北', 65, 68, 72, 75, None],
]
for i, row in enumerate(data, start=2):
    for j, v in enumerate(row, start=1):
        if j == 1:
            ws.cell(row=i, column=j, value=v)
        elif j == 6:
            ws.cell(row=i, column=j, value=f'=SUM(B{i}:E{i})')
        else:
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=j).number_format = '#,##0' if j > 1 else '@'

# Data bar：每季度列加量级条
for col_letter in ['B', 'C', 'D', 'E']:
    ws.conditional_formatting.add(
        f'{col_letter}2:{col_letter}6',
        DataBarRule(
            start_type='min',
            end_type='max',
            color='4472C4',   # 蓝
            showValue=True
        )
    )

# Total 列加绿色 data bar
ws.conditional_formatting.add(
    'F2:F6',
    DataBarRule(
        start_type='min',
        end_type='max',
        color='2E7D32',   # 绿
        showValue=True
    )
)

# 列宽
ws.column_dimensions['A'].width = 12
for col in 'BCDEF':
    ws.column_dimensions[col].width = 14

ws.freeze_panes = 'B2'

os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/sales_databar_20260721.xlsx')
```

### 3. Formula rule — 行级条件高亮

```python
from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Font, PatternFill, Alignment
import os
from datetime import datetime, timedelta

wb = Workbook()
ws = wb.active
ws.title = 'Tasks'

# 表头
ws.append(['Task', 'Owner', 'Due Date', 'Status', 'Days Overdue'])
header_font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF')
header_fill = PatternFill('solid', fgColor='2E7D32')
for col in range(1, 6):
    c = ws.cell(row=1, column=col)
    c.font = header_font
    c.fill = header_fill
    c.alignment = Alignment(horizontal='center')

# 数据
today = datetime(2026, 7, 21)
tasks = [
    ['Design review', 'Alice', today - timedelta(days=3), 'Done', 0],
    ['API implementation', 'Bob', today - timedelta(days=1), 'In Progress', 0],
    ['QA testing', 'Carol', today + timedelta(days=2), 'Not Started', 0],
    ['Deploy to staging', 'Dave', today - timedelta(days=5), 'Blocked', 0],
    ['User training', 'Eve', today + timedelta(days=7), 'Not Started', 0],
    ['Final release', 'Frank', today - timedelta(days=10), 'In Progress', 0],
]
for i, t in enumerate(tasks, start=2):
    ws.cell(row=i, column=1, value=t[0])
    ws.cell(row=i, column=2, value=t[1])
    ws.cell(row=i, column=3, value=t[2]).number_format = 'YYYY-MM-DD'
    ws.cell(row=i, column=4, value=t[3])
    # Days Overdue 公式
    ws.cell(row=i, column=5, value=f'=IF(AND(D{i}<>"Done", C{i}<TODAY()), TODAY()-C{i}, 0)')

# Formula rule 1：逾期行标红（行级）
red_fill = PatternFill('solid', fgColor='FFCDD2')
red_font = Font(name='Microsoft YaHei', color='C62828', bold=True)
ws.conditional_formatting.add(
    'A2:E7',
    FormulaRule(
        formula=['AND($D2<>"Done", $C2<TODAY())'],
        fill=red_fill,
        font=red_font
    )
)

# Formula rule 2：Done 状态行标绿
green_fill = PatternFill('solid', fgColor='C8E6C9')
ws.conditional_formatting.add(
    'A2:E7',
    FormulaRule(
        formula=['$D2="Done"'],
        fill=green_fill
    )
)

# Formula rule 3：Blocked 状态行标橙
orange_fill = PatternFill('solid', fgColor='FFF3E0')
ws.conditional_formatting.add(
    'A2:E7',
    FormulaRule(
        formula=['$D2="Blocked"'],
        fill=orange_fill
    )
)

# 列宽
ws.column_dimensions['A'].width = 22
ws.column_dimensions['B'].width = 12
ws.column_dimensions['C'].width = 14
ws.column_dimensions['D'].width = 14
ws.column_dimensions['E'].width = 14

ws.freeze_panes = 'A2'

os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/task_overdue_20260721.xlsx')
```

### 4. Icon set — 状态指示器

```python
from openpyxl import Workbook
from openpyxl.formatting.rule import IconSetRule
from openpyxl.styles import Font, PatternFill, Alignment
import os

wb = Workbook()
ws = wb.active
ws.title = 'KPI'

# 表头
ws.append(['Metric', 'Target', 'Actual', 'Achievement %', 'Status'])
header_font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF')
header_fill = PatternFill('solid', fgColor='2E7D32')
for col in range(1, 6):
    c = ws.cell(row=1, column=col)
    c.font = header_font
    c.fill = header_fill
    c.alignment = Alignment(horizontal='center')

# 数据
kpis = [
    ['Revenue ($M)', 100, 112],
    ['EBITDA Margin', 0.25, 0.28],
    ['Customer Churn', 0.05, 0.07],
    ['NPS Score', 50, 62],
    ['CAC Payback (months)', 12, 18],
]
for i, k in enumerate(kpis, start=2):
    ws.cell(row=i, column=1, value=k[0])
    ws.cell(row=i, column=2, value=k[1])
    ws.cell(row=i, column=3, value=k[2])
    ws.cell(row=i, column=4, value=f'=C{i}/B{i}')
    ws.cell(row=i, column=4).number_format = '0.0%'

# IconSetRule：3 箭头
# <80% 红，80-100% 黄，>100% 绿
ws.conditional_formatting.add(
    'D2:D6',
    IconSetRule(
        icon_style='3Arrows',
        type='percent',
        values=[0, 80, 100]
        # icon 0: 红下箭头 (0-80%)
        # icon 1: 黄横箭头 (80-100%)
        # icon 2: 绿上箭头 (>100%)
    )
)

# 列宽
ws.column_dimensions['A'].width = 22
ws.column_dimensions['B'].width = 12
ws.column_dimensions['C'].width = 12
ws.column_dimensions['D'].width = 16
ws.column_dimensions['E'].width = 12

ws.freeze_panes = 'A2'

os.makedirs('workspace/office_xlsx', exist_ok=True)
wb.save('workspace/office_xlsx/kpi_iconset_20260721.xlsx')
```

## 坑

### 坑 1：元素名 `conditionalformatting`（无下划线）
XML 里是 `conditionalformatting`（无下划线），但 openpyxl API 是 `ws.conditional_formatting`（有下划线）：
```python
# ✓ openpyxl API（有下划线）
ws.conditional_formatting.add(range_str, rule)

# ❌ 不要直接操作 XML
# ws._conditional_formatting._cf_rules.append(...)   # 内部 API，慎用
```

### 坑 2：修改 prop 后可能不对称
```python
# 修改已添加的 CF rule 的 prop，可能不生效（rule 对象内部状态不一致）
rule = ColorScaleRule(...)
ws.conditional_formatting.add('B2:B100', rule)
rule.start_color = 'FF0000'   # ❌ 可能不生效

# ✓ 重新创建 CF rule
ws.conditional_formatting._cf_rules.clear()   # 清掉旧 rule
new_rule = ColorScaleRule(start_type='min', start_color='FF0000', ...)
ws.conditional_formatting.add('B2:B100', new_rule)
```

### 坑 3：Formula rule 的相对/绝对引用
```python
# Formula rule 的 formula 字符串里，$ 决定相对/绝对
# $D2 = 列绝对、行相对（行级高亮必须这样）
ws.conditional_formatting.add(
    'A2:E100',
    FormulaRule(formula=['$D2="Done"'], ...)   # ✓ 行级高亮
)

# $D$2 = 列绝对、行绝对（只对第 2 行生效，不是行级）
ws.conditional_formatting.add(
    'A2:E100',
    FormulaRule(formula=['$D$2="Done"'], ...)   # ❌ 只第 2 行高亮
)

# D2 = 列相对、行相对（每格都判断自己的 D 列值，但 CF 是按区域应用的，行为可能不符预期）
```

### 坑 4：CF 优先级
```python
# 多个 CF 同时匹配同一 cell，按添加顺序优先（先加的优先）
# 但 Excel UI 里可以拖拽改顺序，openpyxl 不支持
# 解决：把更具体的 rule 先添加
ws.conditional_formatting.add('A2:E100', FormulaRule(formula=['$D2="Blocked"'], fill=orange_fill))   # 先加具体
ws.conditional_formatting.add('A2:E100', FormulaRule(formula=['$D2<>"Done"'], fill=red_fill))         # 后加宽泛
# "Blocked" 行只显示橙色（优先）
```

### 坑 5：DataBar 颜色不要用浅色
```python
# ❌ 浅色 data bar 在白底上几乎看不见
DataBarRule(color='E8F5E9', ...)   # 浅绿，看不见

# ✓ 用饱和色
DataBarRule(color='2E7D32', ...)   # 深绿
DataBarRule(color='4472C4', ...)   # 蓝
```

## 典型应用场景

### 1. Sensitivity grid 配色
```python
# green=upside, red=downside
ws.conditional_formatting.add(
    'B2:F6',
    ColorScaleRule(
        start_type='min', start_color='FFCDD2',
        mid_type='percentile', mid_value=50, mid_color='FFFFFF',
        end_type='max', end_color='C8E6C9'
    )
)
```

### 2. 销售额数据条
```python
ws.conditional_formatting.add(
    'B2:B100',
    DataBarRule(start_type='min', end_type='max', color='2E7D32', showValue=True)
)
```

### 3. 行级条件高亮（逾期行标红）
```python
ws.conditional_formatting.add(
    'A2:E100',
    FormulaRule(
        formula=['AND($D2<>"Done", $C2<TODAY())'],
        fill=PatternFill('solid', fgColor='FFCDD2'),
        font=Font(color='C62828', bold=True)
    )
)
```

### 4. KPI 状态指示器（3 箭头）
```python
ws.conditional_formatting.add(
    'D2:D10',
    IconSetRule(icon_style='3Arrows', type='percent', values=[0, 80, 100])
)
```

### 5. 热力图（增长率）
```python
ws.conditional_formatting.add(
    'C2:C100',
    ColorScaleRule(
        start_type='num', start_value=-0.1, start_color='FFCDD2',   # 负增长红
        mid_type='num', mid_value=0, mid_color='FFFFFF',             # 0 白
        end_type='num', end_value=0.2, end_color='C8E6C9'            # 高增长绿
    )
)
```

### 6. 突出 Top N
```python
# 高亮前 3 名
ws.conditional_formatting.add(
    'B2:B100',
    FormulaRule(
        formula=['B2>=LARGE($B$2:$B$100, 3)'],
        fill=PatternFill('solid', fgColor='C8E6C9'),
        font=Font(color='2E7D32', bold=True)
    )
)
```

### 7. 重复值标记
```python
ws.conditional_formatting.add(
    'A2:A100',
    FormulaRule(
        formula=['COUNTIF($A$2:$A$100, A2)>1'],
        fill=PatternFill('solid', fgColor='FFF3E0')
    )
)
```

## 与其他元素协同

### CF + Table
```python
# Table 自带 banded rows，加 CF 时注意不要冲突
# Table 样式优先级低于 CF，CF 会覆盖 Table 样式
```

### CF + Data Validation
```python
# CF 可以根据 DV 选择的值高亮
# 如：DV 选 "Done" 后整行变绿（用 FormulaRule formula=['$D2="Done"']）
```

### CF + Sparkline
```python
# Sparkline 是独立对象，不受 CF 影响
# 若要 sparkline 颜色随状态变，用 highPoint / lowPoint / negativePoint
```

## 多规则组合（Advanced patterns）

### Pattern 1：KPI 三色阈值
```python
# <80% 红，80-100% 黄，>100% 绿
from openpyxl.formatting.rule import CellIsRule

# 先加 ≥100% 绿
ws.conditional_formatting.add(
    'D2:D100',
    CellIsRule(
        operator='greaterThanOrEqual',
        formula=['1'],
        fill=PatternFill('solid', fgColor='C8E6C9'),
        font=Font(color='2E7D32', bold=True)
    )
)
# 再加 80%-100% 黄
ws.conditional_formatting.add(
    'D2:D100',
    CellIsRule(
        operator='between',
        formula=['0.8', '0.9999'],
        fill=PatternFill('solid', fgColor='FFF3E0'),
        font=Font(color='E65100')
    )
)
# 最后 <80% 红
ws.conditional_formatting.add(
    'D2:D100',
    CellIsRule(
        operator='lessThan',
        formula=['0.8'],
        fill=PatternFill('solid', fgColor='FFCDD2'),
        font=Font(color='C62828', bold=True)
    )
)
```

### Pattern 2：双轴对比（actual vs target）
```python
# Actual 列相对 Target 列的达成率配色
# B 列 = Target，C 列 = Actual
ws.conditional_formatting.add(
    'C2:C100',
    FormulaRule(
        formula=['$C2/$B2>=1'],   # 达成 ≥100% 绿
        fill=PatternFill('solid', fgColor='C8E6C9')
    )
)
ws.conditional_formatting.add(
    'C2:C100',
    FormulaRule(
        formula=['AND($C2/$B2<1, $C2/$B2>=0.8)'],   # 80-100% 黄
        fill=PatternFill('solid', fgColor='FFF3E0')
    )
)
ws.conditional_formatting.add(
    'C2:C100',
    FormulaRule(
        formula=['$C2/$B2<0.8'],   # <80% 红
        fill=PatternFill('solid', fgColor='FFCDD2')
    )
)
```

### Pattern 3：行级动态配色（基于另一列值）
```python
# 根据 Status 列（D 列）值动态配色整行
status_colors = [
    ('Done', 'C8E6C9', '2E7D32'),       # 绿
    ('In Progress', 'E3F2FD', '1565C0'), # 蓝
    ('Blocked', 'FFCDD2', 'C62828'),     # 红
    ('Not Started', 'F5F5F5', '666666'), # 灰
]
for status, fill_color, font_color in status_colors:
    ws.conditional_formatting.add(
        'A2:E100',
        FormulaRule(
            formula=[f'$D2="{status}"'],
            fill=PatternFill('solid', fgColor=fill_color),
            font=Font(color=font_color)
        )
    )
```

### Pattern 4：周末行高亮
```python
# A 列是日期，高亮周六周日行
ws.conditional_formatting.add(
    'A2:E100',
    FormulaRule(
        formula=['WEEKDAY($A2, 2)>5'],   # 2=Monday-based, >5 = Sat/Sun
        fill=PatternFill('solid', fgColor='F5F5F5'),
        font=Font(color='999999', italic=True)
    )
)
```

### Pattern 5：duplicate 检测 + 唯一值标记
```python
# 重复值标橙
ws.conditional_formatting.add(
    'A2:A100',
    FormulaRule(
        formula=['COUNTIF($A$2:$A$100, A2)>1'],
        fill=PatternFill('solid', fgColor='FFF3E0')
    )
)

# 唯一值标绿
ws.conditional_formatting.add(
    'A2:A100',
    FormulaRule(
        formula=['COUNTIF($A$2:$A$100, A2)=1'],
        fill=PatternFill('solid', fgColor='C8E6C9')
    )
)
```

## CF rule 类型完整速查表

| Rule 类 | 用途 | 关键参数 |
|---|---|---|
| `CellIsRule` | 简单数值比较 | `operator` (`greaterThan` / `lessThan` / `between` / `equal` / ...) + `formula` |
| `ColorScaleRule` | 热力图渐变 | `start_type/value/color` / `mid_type/value/color` / `end_type/value/color` |
| `DataBarRule` | 量级条 | `start_type` / `end_type` / `color` / `showValue` |
| `FormulaRule` | 自定义公式 | `formula=[...]` + `fill` + `font` + `border` |
| `IconSetRule` | 图标集 | `icon_style` (`3Arrows` / `3TrafficLights1` / `4Arrows` / `5Quarters` ...) + `type` + `values` |

### operator 完整枚举（CellIsRule）
```
between | notBetween
equal | notEqual
greaterThan | lessThan
greaterThanOrEqual | lessThanOrEqual
containsText | notContains | beginsWith | endsWith
containsErrors | notContainsErrors
containsBlanks | notContainsBlanks
```

### icon_style 完整枚举（IconSetRule）
```
3Arrows | 3ArrowsGray | 3Flags | 3TrafficLights1 | 3TrafficLights2 | 3Symbols | 3Symbols2
4Arrows | 4ArrowsGray | 4Rating | 4RedToBlack | 4TrafficLights
5Arrows | 5ArrowsGray | 5Quarters | 5Rating
```

### start_type / end_type 完整枚举
```
min | max | num | percent | percentile | formula
```

## 参考

- [openpyxl 条件格式文档](https://openpyxl.readthedocs.io/en/stable/api/openpyxl.formatting.rule.html)
- [SKILL.md](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md)
- [basic.md](file:///<project_root>/.agents/skills/office_xlsx/references/basic.md) - 基础元素
- [financial_model.md](file:///<project_root>/.agents/skills/office_xlsx/references/financial_model.md) - sensitivity grid 应用
- [data_dashboard.md](file:///<project_root>/.agents/skills/office_xlsx/references/data_dashboard.md) - KPI 状态指示器应用
