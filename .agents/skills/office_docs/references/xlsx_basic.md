# Excel 基础元素参考 (basic)

本文件是 [office_xlsx skill](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md) 的基础参考，覆盖 openpyxl 全部 13 个元素 + 验证清单 + Common Pitfalls。条件格式 / 财务模型 / 仪表盘场景见对应独立 references。

## Mental Model

`.xlsx` 是 ZIP + XML 的组合，解压后典型结构：

```
my_sheet.xlsx (ZIP)
├── [Content_Types].xml
├── _rels/.rels
├── xl/
│   ├── workbook.xml          # 工作簿（sheet 列表 / 定义名称）
│   ├── worksheets/sheet1.xml # 单元格数据
│   ├── styles.xml            # 字体 / 边框 / 填充 / 对齐
│   ├── sharedStrings.xml     # 字符串共享池
│   ├── charts/chart1.xml     # 图表
│   └── drawings/drawing1.xml # 绘图对象
└── docProps/{app,core}.xml
```

openpyxl 高层 API：`Workbook` → `Worksheet` → `Cell` → `Row/Column` → `Style` → `Formula` → `Chart`。底层 XML 通过 `ws._charts` / `cell._style` 等下划线属性访问，慎用。

**公式不要写前导 `=`**：openpyxl 写公式时直接用 `"SUM(B2:B9)"` 或 `"=SUM(B2:B9)"`——openpyxl 会自动加 `=`，有些情况会双剥变成错公式。本 skill 一律用 `"=SUM(B2:B9)"` 风格（前导 `=` 是惯例，openpyxl 兼容），避免歧义。

**hex 颜色不带 `#`**：`FF0000` 不是 `#FF0000`。openpyxl `Font(color="FF0000")` / `PatternFill(fgColor="FF0000")`。

## Element 1: Cell

### 路径
```python
ws['A1']                       # 单格
ws['A1:D10']                   # 区域（返回 tuple of tuples）
ws.cell(row=1, column=1)       # 行列数字索引
ws.cell(row=1, column=1, value='Hello')  # 设值
```

### 设值与格式一次写入
```python
cell = ws['B2']
cell.value = '=SUM(B3:B9)'          # 公式（前导 = 是惯例，openpyxl 兼容）
cell.number_format = '¥#,##0.00'    # 数字格式
cell.font = Font(name='Microsoft YaHei', size=11, color='000000')
cell.fill = PatternFill('solid', fgColor='E8F5E9')
cell.alignment = Alignment(horizontal='right', vertical='center')
```

### 关键约束
- **公式前导 `=`**：openpyxl 自动添加，本 skill 一律写 `"=SUM(...)"` 风格避免歧义
- **hex 颜色不带 `#`**：`FF0000` 而非 `#FF0000`
- **`cell.value = None`** 不等于删除（用 `del cell.value` 或 `cell.value = ''`）

### 反模式
```python
# ❌ 错：cell 上设 color 歧义（text vs bg）
ws['A1'].color = 'FF0000'   # openpyxl 无此属性，会 AttributeError

# ✓ 对：用 font.color（字色）或 fill（背景）
ws['A1'].font = Font(color='FF0000')                          # 字色红
ws['A1'].fill = PatternFill('solid', fgColor='FFFF00')        # 背景黄
```

## Element 2: Sheet

### 三态可见性
```python
ws.sheet_state = 'visible'      # 默认
ws.sheet_state = 'hidden'       # 隐藏，用户可右键取消隐藏
ws.sheet_state = 'veryHidden'   # 深度隐藏，只能 VBA 切换（用户无法 UI 取消）
```

### 冻结窗格
```python
ws.freeze_panes = 'A2'   # 冻结首行
ws.freeze_panes = 'B2'   # 冻结首列 + 首行
ws.freeze_panes = 'B3'   # 冻结前 2 行 + 前 1 列
ws.freeze_panes = None   # 取消冻结
```

### Tab color
```python
ws.sheet_properties.tabColor = 'FFC000'   # 黄色（Assumptions 标识）
ws.sheet_properties.tabColor = '4472C4'   # 蓝色（Calc 标识）
ws.sheet_properties.tabColor = '70AD47'   # 绿色（Outputs 标识）
```

### 重命名（级联！）
```python
ws.title = 'Revenue'   # 重命名会级联到：
# - named-range（指向此 sheet 的定义名）
# - formula（'Sheet1'!A1 → 'Revenue'!A1）
# - 内部 hyperlink
# - sparkline 引用
# - data validation list source
# - conditional formatting formula
```

### 反模式：sheet 被 dependency 引用时直接 remove 会失败
```python
# ❌ 错：被 validation/CF/sparkline/hyperlink/named range 跨表引用时
del wb['Sheet1']   # openpyxl 允许但生成的文件 Excel 打开会报修复提示

# ✓ 对：先删依赖，再删 sheet
for dv in ws.data_validations.dataValidation:
    if 'Sheet1' in (dv.formula1 or ''):
        ws.data_validations.dataValidation.remove(dv)
del wb['Sheet1']
```

## Element 3: Row / Column

### 列宽 / 行高
```python
ws.column_dimensions['A'].width = 20       # 字符单位
ws.column_dimensions['B'].width = 12
ws.row_dimensions[1].height = 22           # 磅单位
ws.row_dimensions[2].height = None         # 恢复默认

# 隐藏
ws.column_dimensions['D'].hidden = True
ws.row_dimensions[3].hidden = True
```

### 列级 numFmt
openpyxl 3.x 支持在 `column_dimensions` 上设 `style`，会写入 column-level style：
```python
from openpyxl.styles import NamedStyle
money_style = NamedStyle(name='money', number_format='¥#,##0.00')
wb.add_named_style(money_style)
ws.column_dimensions['C'].style = 'money'   # C 列默认货币格式
```

### 行/列插入（自动 rewrite 公式 ref）
```python
ws.insert_rows(idx=2, amount=1)    # 在第 2 行前插 1 行，公式 ref 自动重写
ws.insert_cols(idx=2, amount=1)
ws.delete_rows(idx=2, amount=1)
ws.delete_cols(idx=2, amount=1)
```

### 反模式：默认 width=8.43 必然 `###`
```python
# ❌ 错：金额列不设宽，默认 8.43 → 显示 "###"
ws['B2'] = 1234567.89   # 用户看到 ###

# ✓ 对：任何用户会读的列必须显式 width
ws.column_dimensions['B'].width = 16   # 显示 1,234,567.89
ws['B2'] = 1234567.89
ws['B2'].number_format = '¥#,##0.00'
```

**默认列宽参考**：
| 列内容 | 推荐宽度 |
|---|---|
| 日期 YYYY-MM-DD | 14 |
| 金额 ¥1,234.56 | 14-16 |
| 百分比 12.34% | 10-12 |
| 中文姓名 (4 字) | 12-14 |
| 中文描述 (10-20 字) | 30-40 |
| 长文本备注 | 50+ |

## Element 4: Formula

### 内置函数
openpyxl 支持 150+ 内置函数自动求值并缓存（**但 openpyxl 本身不计算公式！需要 Excel 打开重算**）。

```python
ws['B10'] = '=SUM(B2:B9)'
ws['B11'] = '=AVERAGE(B2:B9)'
ws['B12'] = '=IF(B2>4, "高", "低")'
ws['B13'] = '=VLOOKUP(A2, $H$2:$I$10, 2, FALSE)'
ws['B14'] = '=INDEX(Revenue!B2:B10, MATCH(A2, Revenue!A2:A10, 0))'
ws['B15'] = '=SUMIFS(C2:C10, A2:A10, "2026-07", B2:B10, "餐饮")'
```

### dynamic-array 函数需 `_xlfn.` 前缀
```python
ws['D2'] = '=_xlfn.UNIQUE(A2:A100)'
ws['D3'] = '=_xlfn.FILTER(B2:B100, C2:C100="active")'
ws['D4'] = '=_xlfn.XLOOKUP(A2, E2:E100, F2:F100)'
```

### OFFSET / INDIRECT
```python
ws['B2'] = '=SUM(OFFSET(A1, 0, 0, 12, 1))'           # 动态区域
ws['B3'] = '=INDIRECT("Sheet" & A1 & "!B2")'          # 间接引用
```

### 行/列插入时公式 ref 自动 rewrite
```python
ws['B10'] = '=SUM(B2:B9)'
ws.insert_rows(idx=5, amount=1)
# openpyxl 自动把 B10 的公式改为 '=SUM(B2:B10)'（范围下移）
# 但**插入位置在公式所在行**时需手动检查
```

### 反模式
```python
# ❌ 1. 跨表 'Sheet1!A1' 在 shell 里裸写
#    shell history expansion 会吃掉 !（bash）或 ! 触发报错
subprocess.run(['echo', "=Sheet1!A1"])   # 报错或截断

# ✓ 用 python string 处理
formula = f"='{sheet_name}'!A1"   # sheet 名带空格/特殊字符必须单引号

# ❌ 2. =B5*1.05 把假设硬编进公式
ws['C5'] = '=B5*1.05'   # 5% 增长率硬编

# ✓ 改成 named range 或引用 Assumptions
ws['C5'] = '=B5*(1+Assumptions!GrowthRate)'
ws['C5'] = '=B5*(1+GrowthRate)'   # GrowthRate 是 defined name

# ❌ 3. 跨表 'P&L!B3' 不加引号
ws['A1'] = '=P&L!B3'   # & 特殊字符 → runtime #NAME?

# ✓ 加单引号
ws['A1'] = "='P&L'!B3"

# ❌ 4. Notes 列想显示文字却写公式
ws['D2'] = '=TV = FCF*(1+g)/(WACC-g)'   # 不是合法公式，会 #NAME?

# ✓ 文字直接写 value
ws['D2'] = 'TV = FCF*(1+g)/(WACC-g)'
```

## Element 5: Chart

### 路径
```python
ws._charts[0]                     # 第 0 个图表
chart = ws._charts[0]
chart.title                       # 标题
chart.y_axis                      # y 轴
chart.x_axis                      # x 轴
chart.series                      # series 列表（不可变，改 series 需 remove + add）
```

### 三种数据喂入方式

**(a) inline `data=` `categories=` —— 仅 demo**
```python
from openpyxl.chart import BarChart, Reference
chart = BarChart()
chart.add_data([10, 20, 30, 40])           # inline data（仅 demo）
chart.categories(['Q1', 'Q2', 'Q3', 'Q4'])
ws.add_chart(chart, 'E5')
```

**(b) 2D `Reference` —— 常规**
```python
from openpyxl.chart import BarChart, Reference
chart = BarChart()
chart.type = 'col'
chart.title = '季度营收'
data = Reference(ws, min_col=2, min_row=1, max_row=5, max_col=3)   # 含表头
cats = Reference(ws, min_col=1, min_row=2, max_row=5)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, 'E5')
```

**(c) per-series `Series` —— 多系列 / 非连续区**
```python
from openpyxl.chart import LineChart, Series, Reference
chart = LineChart()
s1 = Series(Reference(ws, min_col=2, min_row=2, max_row=13), title='Revenue')
s2 = Series(Reference(ws, min_col=3, min_row=2, max_row=13), title='EBITDA')
chart.append(s1)
chart.append(s2)
ws.add_chart(chart, 'E5')
```

### `data_range` 必须带 sheet 前缀
```python
# ❌ 错：裸 A17:C22 引擎拒
chart.add_data(Reference(ws, min_col=1, min_row=17, max_row=22, max_col=3))
# 但若直接写公式串必须带 sheet 前缀
# ✓ 对：'Summary!A17:C22'

# Reference 对象自动处理 sheet 前缀，无需手动拼
```

### 扩展类型（cx extended charts）
```python
from openpyxl.chart import BarChart, LineChart, PieChart, DoughnutChart, AreaChart, ScatterChart
# 标准：bar / line / pie / doughnut / area / scatter
# cx 扩展：boxWhisker / waterfall / funnel / histogram / treemap / sunburst / pareto
# openpyxl 3.x 部分支持，需通过 oxml 注入：
# from openpyxl.chart.series import SeriesLabel
# 详细见 openpyxl 文档 charts 章节
```

### 移动 / 缩放
```python
chart.anchor = 'F5'              # 移到 F5
chart.anchor = 'F5:N25'          # 锁定到 F5:N25 区域
chart.width = 18                 # cm
chart.height = 10                # cm
```

### 反模式
```python
# ❌ 1. 单列 data_range 引擎拒
data = Reference(ws, min_col=2, min_row=2, max_row=13)   # 只有数值列
chart.add_data(data)   # Chart requires data 错误

# ✓ 必须含类别列
data = Reference(ws, min_col=2, min_row=1, max_row=13, max_col=3)
cats = Reference(ws, min_col=1, min_row=2, max_row=13)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)

# ❌ 2. 图表创建后 series 不可变
chart.series[0].values = Reference(...)   # 不生效

# ✓ 加/换 series 必须 remove + add
chart.series.pop(0)                       # 删旧
chart.append(Series(new_ref, title='新系列'))

# ❌ 3. 无 auto-fit，5-6 类别 + 2 series 默认尺寸太小
# ✓ 显式设 anchor 区域
chart.anchor = 'A5:L22'   # 12 列宽足够 5-6 类别 + 2 series
```

## Element 6: PivotTable

openpyxl 透视表支持有限（主要是读取已有透视表，创建透视表 API 不完整）。**建议用 pandas + openpyxl 组合**：pandas 计算聚合，openpyxl 写入结果。

### 推荐模式
```python
import pandas as pd
from openpyxl import Workbook

# pandas 计算透视
df = pd.read_csv('data.csv')
pivot = df.pivot_table(index='Region', columns='Month', values='Revenue', aggfunc='sum', fill_value=0)

# openpyxl 写入
wb = Workbook()
ws = wb.active
ws.title = 'Pivot'

# 写表头
ws.append(['Region'] + list(pivot.columns))
# 写数据
for idx, row in pivot.iterrows():
    ws.append([idx] + list(row.values))

# 表头样式（绿色）
from openpyxl.styles import Font, PatternFill
for col in range(1, len(pivot.columns) + 2):
    c = ws.cell(row=1, column=col)
    c.font = Font(name='Microsoft YaHei', bold=True, color='FFFFFF')
    c.fill = PatternFill('solid', fgColor='2E7D32')
```

### `ws.pivot_tables` 列表
如果加载已有含透视表的 xlsx，`ws.pivot_tables` 是透视表对象列表。但 openpyxl 不支持创建新透视表，建议用 pandas 替代。

### 反模式
```python
# ❌ 错：直接 ws.pivot_tables.append(...) 创建透视表（API 不完整）

# ✓ 先 get 确认 shape 再叠加
print(ws.max_row, ws.max_column)   # 确认数据范围
print([t for t in ws.pivot_tables])   # 确认已有透视表
```

## Element 7: Sparkline

openpyxl sparkline 支持有限，需用 oxml 注入。`openpyxl.worksheet.sparkline` 模块存在但不完整。

### type 严格枚举
```
line | column | stacked
（别名 winloss / win-loss → 实际是 stacked 的特殊渲染）
```

### highPoint + 颜色
```python
# highPoint 是 bool（不是颜色！）
sg.highPoint = True
sg.highMarkerColor = 'FF0000'   # 颜色单独属性
```

### 行高要求
```python
ws.row_dimensions[r].height = 20   # 行高 ≥ 20，否则默认 15pt 行里 sparkline 是扁平乱线
```

### oxml 注入示例
```python
from openpyxl.xml.functions import etree
from openpyxl.xml.constants import SHEET_NS

# sparkline group XML（简化示例）
spark_xml = '''
<xm:sparklineGroup xmlns:xm="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
    type="line" displayEmptyCellsAs="gap" markers="0" high="1" low="0"
    first="0" last="0" negative="0">
    <xm:colorSeries rgb="FF2E7D32"/>
    <xm:colorNegative rgb="FFC62828"/>
    <xm:colorAxis rgb="FF000000"/>
    <xm:colorMarkers rgb="FFD32F2F"/>
    <xm:colorFirst rgb="FFD32F2F"/>
    <xm:colorLast rgb="FFD32F2F"/>
    <xm:colorHigh rgb="FFFF0000"/>
    <xm:colorLow rgb="FF2E7D32"/>
    <xm:sparklines>
        <xm:sparkline>
            <xm:f>Sheet1!B2:M2</xm:f>
            <xm:sqref>N2</xm:sqref>
        </xm:sparkline>
    </xm:sparklines>
</xm:sparklineGroup>
'''

# 注入到 worksheet XML（实际操作需用 lxml 解析 extLst）
```

### 反模式
```python
# ❌ 1. 跨部门/区域的 sparkline 无序
#    sparkline 应只用在时序行（按月/季度），跨类别对比无意义

# ❌ 2. highpoint='FF0000' 当颜色用
sg.highpoint = 'FF0000'   # 报错，highpoint 是 bool

# ✓
sg.highPoint = True
sg.highMarkerColor = 'FF0000'
```

## Element 8: Conditional Formatting

详见 [conditional_formatting.md](file:///<project_root>/.agents/skills/office_xlsx/references/conditional_formatting.md)。3 flavors + iconSet + 完整代码示例。

## Element 9: Data Validation

### 三种 list-source 模式

**(a) Inline list**
```python
from openpyxl.worksheet.datavalidation import DataValidation
dv = DataValidation(type='list', formula1='"Yes,No,Maybe"', allow_blank=True)
dv.add('D2:D100')
ws.add_data_validation(dv)
```

**(b) Named range**
```python
# 先定义 named range
from openpyxl.workbook.defined_name import DefinedName
wb.defined_names['StatusList'] = DefinedName('StatusList', attr_text='Lookups!$A$2:$A$4')

dv = DataValidation(type='list', formula1='=StatusList', allow_blank=True)
dv.add('D2:D100')
ws.add_data_validation(dv)
```

**(c) Direct cross-sheet range**
```python
dv = DataValidation(type='list', formula1='Lookups!$A$2:$A$4', allow_blank=True)
dv.add('D2:D100')
ws.add_data_validation(dv)
# 注意：某些 viewer（LibreOffice 旧版）对此模式有问题，用 named range 更稳
```

### 其他 type
```python
DataValidation(type='decimal', operator='between', formula1='0', formula2='100')
DataValidation(type='whole', operator='greaterThanOrEqual', formula1='0')
DataValidation(type='date', operator='greaterThan', formula1='DATE(2026,1,1)')
DataValidation(type='textLength', operator='lessThanOrEqual', formula1='50')
DataValidation(type='custom', formula1='=AND(ISNUMBER(A2),A2>0)')   # 自定义公式
```

### 反模式
```python
# ❌ 跨表 formula1 在 LibreOffice 旧版会出问题
dv = DataValidation(type='list', formula1='Lookups!$A$2:$A$4')

# ✓ 用 named range 更稳
wb.defined_names['StatusList'] = DefinedName('StatusList', attr_text='Lookups!$A$2:$A$4')
dv = DataValidation(type='list', formula1='=StatusList')
```

## Element 10: Named Range

### Workbook 级
```python
from openpyxl.workbook.defined_name import DefinedName
wb.defined_names['GrowthRate'] = DefinedName('GrowthRate', attr_text='Assumptions!$B$2')
wb.defined_names['WACC'] = DefinedName('WACC', attr_text='Assumptions!$B$3')
```

### Sheet 级
```python
ws.defined_names['LocalRate'] = DefinedName('LocalRate', attr_text='$B$5', localSheetId=wb.sheetnames.index(ws.title))
```

### ≥3 次使用的假设必命名
WACC、TaxRate、TerminalGrowth、ExitMultiple、ChurnRate、DiscountRate 等关键假设，被 ≥3 个公式引用时必须命名。这样修改假设时一处生效。

### 内置名
```python
# 打印区域
wb.defined_names['_xlnm.Print_Area'] = DefinedName('_xlnm.Print_Area', attr_text='Dashboard!$A$1:$H$36')

# 重复打印行列（每页都打印的表头行）
wb.defined_names['_xlnm.Print_Titles'] = DefinedName('_xlnm.Print_Titles', attr_text='Data!$1:$2')
```

### 反模式
```python
# ❌ 声明但不用 = 死装饰
wb.defined_names['UnusedRate'] = DefinedName('UnusedRate', attr_text='Assumptions!$Z$99')
# 任何公式都没引用 UnusedRate → 删除

# ✓ 命名后至少 1 个公式引用
ws['B5'] = '=B4*(1+GrowthRate)'   # 引用 named range GrowthRate
```

## Element 11: Table (ListObject)

```python
from openpyxl.worksheet.table import Table, TableStyleInfo

tab = Table(displayName='SalesTable', ref='A1:D100')
style = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
tab.tableStyleInfo = style
ws.add_table(tab)
```

Table 提供：
- auto-filter（自动筛选）
- structured refs（`SalesTable[Revenue]` 代替 `B2:B100`）
- 表头自动加粗
- banded rows（隔行变色）

### 反模式
```python
# ❌ Table ref 与 manual 样式冲突
ws['A1'].fill = PatternFill('solid', fgColor='2E7D32')   # 手动设表头色
ws.add_table(Table(ref='A1:D100'))   # Table 样式会覆盖手动样式

# ✓ 用 TableStyleInfo 设颜色，或不用 Table
```

## Element 12: Comment

```python
from openpyxl.comments import Comment

c = Comment('Source: Company 10-K, FY2024, Page 45, Revenue Note', 'analyst')
c.width = 300   # px
c.height = 100
ws['B2'].comment = c   # B2 是蓝色硬编输入，comment 文档化来源
```

### `comment` 不是 cell prop，是独立对象
```python
# ❌ 错：尝试设字符串
ws['B2'].comment = '这是说明'   # AttributeError

# ✓ Comment 对象
ws['B2'].comment = Comment('这是说明', 'author')
```

### 用途
文档化蓝色硬编码假设的来源：
- `Source: Company 10-K, FY2024, Page 45, Revenue Note`
- `Source: Bloomberg, 2026-05-02, AAPL US Equity`
- `Source: Management guidance, Q2 2026 earnings call`
- `Assumption: 行业平均增速 5%，参考 McKinsey 2024 报告`

## Element 13: Merge Cells

```python
# 字符串语法
ws.merge_cells('A1:D1')
ws['A1'] = '2026 年 Q3 营收汇总'

# 行列数字语法
ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)

# 取消合并
ws.unmerge_cells('A1:D1')

# 列出所有合并区域
for mr in ws.merged_cells.ranges:
    print(mr)
```

### 反模式：合并后只能写左上角
```python
ws.merge_cells('A1:D1')

# ❌ 错：写其他单元格会丢失
ws['B1'] = '会被丢弃'

# ✓ 只写左上角
ws['A1'] = '标题'
ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
```

## Validation Checklist

```python
from openpyxl import load_workbook

def validate(path):
    # 1. 重开不报错
    wb = load_workbook(path)
    print(f'✓ schema OK, sheets: {wb.sheetnames}')

    # 2. 抽 2-3 个公式 cell.value 检查公式串
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        for row in ws.iter_rows(min_row=1, max_row=min(5, ws.max_row)):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith('='):
                    print(f'  {sheet}!{cell.coordinate} = {cell.value}')

    # 3. 每数字列抽 1 cell—— % 列整 0.0% 检查
    #    （0.0% 可能是分母错或分子缓存 stale）

    # 4. 范围覆盖所有行：SUM(B2:B12) 但数据到 B13 = off-by-one
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and 'SUM(' in v:
                    # 解析 B2:B12 范围，与 ws.max_row 比对
                    pass

    # 5. 跨表公式无 \!（shell mangle 残留）
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and '\\!' in cell.value:
                    print(f'  ERROR shell-mangled: {sheet}!{cell.coordinate}')

    # 6. named range 指向其名所声称
    for name in wb.defined_names:
        dn = wb.defined_names[name]
        print(f'  {name} → {dn.attr_text}')

    # 7. 每个 / 分母被守
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and '/' in v and v.startswith('='):
                    # 检查是否有 IFERROR 或 IF(y=0,...) 守护
                    if 'IFERROR' not in v and 'IF(' not in v:
                        print(f'  WARN unguarded /: {sheet}!{cell.coordinate} = {v}')

    return True
```

### 必检项
- [ ] `load_workbook(path)` 重开不报错
- [ ] 抽 2-3 个公式 `cell.value` 检查公式串
- [ ] 每数字列抽 1 cell——`%` 列整 `0.0%` = 分母错或分子缓存 stale
- [ ] 范围覆盖所有行：`SUM(B2:B12)` 但数据到 `B13` = off-by-one
- [ ] 跨表公式无 `\!`（shell mangle 残留）
- [ ] named range 指向其名所声称
- [ ] 每个 `/` 分母被守——`IFERROR(x/y, 0)` 或 `IF(y=0, 0, x/y)`

## Common Pitfalls 表

| 症状 | 原因 | 解法 |
|------|------|------|
| 跨表 `!` 在 shell 里报错 | bash history expansion 吃 `!` | 用 python 内 string 处理，不裸写 shell |
| Chart series 改了不生效 | series 不可变 | `chart.series.pop(0)` + `chart.append(Series(...))` |
| `cell.color = 'FF0000'` AttributeError | openpyxl 无 `cell.color` 属性 | `cell.font = Font(color='FF0000')`（字色）或 `cell.fill`（背景） |
| hex 颜色 `#FF0000` 不识别 | openpyxl 不带 `#` | 去掉 `#`，用 `FF0000` |
| 公式 prop 名猜错报错 | openpyxl prop 名严格 | 查 openpyxl 文档 |
| Sheet 名带空格公式报错 | 全路径需引号 | `'Sheet Name'!A1`（单引号） |
| Year 显示 `2,026` | 默认数字格式带千分位 | `numFmt="@"`（文本）或字符串类型 |
| 文件改了不生效 | 文件在 Excel 中打开 | 先在 Excel 关闭文件 |
| 公式 cell.value 有但 cachedValue 空 | openpyxl 不计算公式 | Excel 打开时重算，或 `wb.calculation.fullCalcOnLoad = True` |
| 中文字符显示方框 | 默认字体不支持中文 | `Font(name='Microsoft YaHei')` |
| 合并后写其他 cell 数据丢失 | 合并只保留左上角 | 合并前写值，或合并后只写左上角 |
| `###` 显示 | 列宽不够 | 显式设 `ws.column_dimensions['B'].width = 16` |
| 大数字显示 `1.23E+12` | 默认 General 转科学计数法 | `numFmt='0'` 或 `'#,##0'` |
| 日期显示数字 | 日期存成 datetime 但 numFmt 没设 | `cell.number_format = 'YYYY-MM-DD'` |
| VLOOKUP 跨表 `#N/A` | 范围没绝对引用 | `=VLOOKUP(A2, Sheet!$A$2:$B$100, 2, FALSE)` |

## 参考

- [openpyxl 官方文档](https://openpyxl.readthedocs.io/)
- [SKILL.md](file:///<project_root>/.agents/skills/office_xlsx/SKILL.md)
- [conditional_formatting.md](file:///<project_root>/.agents/skills/office_xlsx/references/conditional_formatting.md) - 条件格式详解
- [financial_model.md](file:///<project_root>/.agents/skills/office_xlsx/references/financial_model.md) - 财务模型场景
- [data_dashboard.md](file:///<project_root>/.agents/skills/office_xlsx/references/data_dashboard.md) - 数据仪表盘场景
