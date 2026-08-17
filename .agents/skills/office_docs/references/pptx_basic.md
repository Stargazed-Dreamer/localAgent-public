# 基础元素参考 (basic.md)

本文件是 [office_pptx skill](file:///f:/<project_root>/.agents/skills/office_pptx/SKILL.md) 的子参考，专注 python-pptx 基础元素的 API、用法、反模式。

## 目录

1. [Mental Model](#mental-model)
2. [Element 1: slide（幻灯片）](#element-1-slide)
3. [Element 2: shape（文本/图形）](#element-2-shape)
4. [Element 3: paragraph / run（文本内嵌样式）](#element-3-paragraph--run)
5. [Element 4: chart（图表）](#element-4-chart)
6. [Element 5: picture（图片）](#element-5-picture)
7. [Element 6: connector（连接线）](#element-6-connector)
8. [Element 7: table（表格）](#element-7-table)
9. [Element 8: placeholder（占位符）](#element-8-placeholder)
10. [Element 9: group（组合）](#element-9-group)
11. [Element 10: animation（动画）](#element-10-animation)
12. [Element 11: transition（过渡）](#element-11-transition)
13. [Element 12: notes（演讲者备注）](#element-12-notes)
14. [Element 13: master / layouts / smartart / equation / video / audio / zoom / comment / model3d / diagram](#element-13-misc)

---

## Mental Model

.pptx 是 ZIP + XML 的组合。python-pptx 给你高层 API：

```
Presentation
├── Slides（Slide 集合）
│   └── Slide
│       ├── Shapes（Shape 集合）
│       │   ├── TextBox      → TextFrame（Paragraphs → Runs）
│       │   ├── Placeholder  → 标题/正文/图片占位符
│       │   ├── Picture
│       │   ├── Table        → Rows → Cells → TextFrame
│       │   ├── Shape（自选图形：矩形/圆/箭头）
│       │   ├── Chart        → 柱状/饼/折线/散点
│       │   └── Connector    → 直线/折线/曲线
│       └── Layout（继承自 SlideLayout）
└── SlideMasters / SlideLayouts
```

**关键概念**：
- **Slide Layout（版式）**：预定义的内容区域（标题+正文、空白、双栏等），新 Slide 基于版式创建
- **Placeholder（占位符）**：版式上预留的位置（标题、正文、图片），用 `placeholders[i]` 访问
- **Shape**：所有可视元素的基类，文本框/图片/形状/表格都是 Shape 的子类
- **TextFrame**：文本容器，含多段落（Paragraph），每段含多个 Run（同段不同样式）

**shapes 必须**显式定位**（无 layout engine，自己算 grid）**。16:9 widescreen 默认 33.87 × 19.05cm（13.333" × 7.5"）。

## 基础导入

```python
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu, Cm
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.oxml.ns import qn
```

## Forest & Moss 调色板（本项目主色）

```python
PALETTE = {
    'primary':   RGBColor(0x2C, 0x5F, 0x2D),  # Forest green
    'secondary': RGBColor(0x97, 0xBC, 0x62),  # Moss
    'accent':    RGBColor(0xF5, 0xF5, 0xF5),  # Light neutral
    'text':      RGBColor(0x2D, 0x2D, 0x2D),  # Dark neutral
    'muted':     RGBColor(0x6B, 0x8E, 0x6B),  # Sage
    'white':     RGBColor(0xFF, 0xFF, 0xFF),
}
```

---

## Element 1: slide

### 创建

```python
# Blank layout（最灵活，推荐用于自定义设计）
slide = prs.slides.add_slide(prs.slide_layouts[6])

# Title Slide（封面）
slide = prs.slides.add_slide(prs.slide_layouts[0])

# Title and Content（标题 + 正文占位符）
slide = prs.slides.add_slide(prs.slide_layouts[1])
```

python-pptx 内置 11 种版式（索引 0-10），常用：

| 索引 | 名称 | 用途 |
|------|------|------|
| 0 | Title Slide | 封面（标题 + 副标题） |
| 1 | Title and Content | 标题 + 正文占位符 |
| 5 | Title Only | 仅标题 |
| 6 | Blank | **空白（最灵活，推荐）** |
| 7 | Content with Caption | 内容 + 说明 |

### Background 三种形式

```python
# 1. Solid hex
bg = slide.background
bg.fill.solid()
bg.fill.fore_color.rgb = RGBColor(0x2C, 0x5F, 0x2D)

# 2. Gradient (start-end-angle) — python-pptx 不直接支持渐变，需 oxml 注入
# 等价做法：用大矩形 shape 模拟渐变背景
from pptx.enum.shapes import MSO_SHAPE
gradient_rect = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height
)
gradient_rect.fill.gradient()  # python-pptx 支持 gradient fill
gradient_rect.fill.gradient_stops[0].color.rgb = RGBColor(0x2C, 0x5F, 0x2D)
gradient_rect.fill.gradient_stops[1].color.rgb = RGBColor(0x1A, 0x3D, 0x1B)
gradient_rect.line.fill.background()
gradient_rect.name = 'BgGradient'

# 3. Image background
slide.shapes.add_picture(
    '/path/to/hero.jpg', 0, 0, prs.slide_width, prs.slide_height
)
```

### 反模式

- 依赖 theme 默认布局；不显式设字号
- 用 `slide_layouts[6]` 但忘记设 16:9 尺寸（默认 4:3 10×7.5"）

---

## Element 2: shape

### 显式定位 + 命名（load-bearing）

```python
# 必须显式 x/y/width/height（无 layout engine，自己算 grid）
shape = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE,
    Inches(1), Inches(2), Inches(4), Inches(2)
)
# 创建时即命名（positional [N] 在 reorder 后会漂移）
shape.name = 'HeroTitle'

shape.fill.solid()
shape.fill.fore_color.rgb = PALETTE['primary']
shape.line.fill.background()  # 无描边

tf = shape.text_frame
tf.text = 'Hello'
tf.paragraphs[0].font.size = Pt(36)
tf.paragraphs[0].font.bold = True
tf.paragraphs[0].font.color.rgb = PALETTE['white']
```

### Preset 几何

`MSO_SHAPE` 选几何：`RECTANGLE` / `ROUNDED_RECTANGLE` / `OVAL` / `CHEVRON` / `PENTAGON` / `DIAMOND` / `HEXAGON` / `RIGHT_ARROW` / `LEFT_ARROW` / `STAR_5_POINT` 等。**自定义 `M...Z` 路径不支持**（OfficeCli 的 custom path 在 python-pptx 中无 API，需 oxml 注入）。

### Z-order canon

后加的在上层。背景装饰先加，标题后加：

```python
# 1. 背景装饰先加
bg_band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, ...)
bg_band.name = 'BgBand'

# 2. 标题后加（在 bg_band 上层）
title = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, ...)
title.name = 'HeroTitle'
```

修复 z-order 用 oxml（python-pptx 无 `shape.zorder` API，需操作 `spTree`）：

```python
def move_shape_to_front(shape):
    """把 shape 移到最上层"""
    spTree = shape._element.getparent()
    spTree.remove(shape._element)
    spTree.append(shape._element)

def move_shape_to_back(shape):
    """把 shape 移到最下层"""
    spTree = shape._element.getparent()
    spTree.remove(shape._element)
    spTree.insert(2, shape._element)  # 插到 nvGrpSpPr/grpSpPr 之后
```

### 用 name 寻址

```python
def find_shape_by_name(slide, name):
    """按 name 查找 shape（避免 positional 索引漂移）"""
    for shape in slide.shapes:
        if shape.name == name:
            return shape
    return None

hero = find_shape_by_name(slide, 'HeroTitle')
```

### 反模式

- 未命名 shape，事后用 positional 寻址；positional `[N]` 在 reorder 后会漂移
- 不显式定位，依赖 layout engine（python-pptx 没有）
- `add_shape` 后忘设 `fill`，默认有蓝色 fill + 黑色边框（AI slop 来源）

---

## Element 3: paragraph / run

### 单行 vs 多段 vs 多 run

```python
# 单行文本用 shape.text_frame.text 即可
shape.text_frame.text = 'Hello World'

# \n 在 python-pptx 中是 <a:br/> 单段落换行（不是字面量）
# 即同段内软换行
shape.text_frame.text = 'Line 1\nLine 2'  # 单段落，两行

# 真正的多段落用 add_paragraph
tf = shape.text_frame
p1 = tf.paragraphs[0]
p1.text = 'Paragraph 1'

p2 = tf.add_paragraph()
p2.text = 'Paragraph 2'

# 同行内混合样式用 paragraph.add_run
p3 = tf.add_paragraph()
run1 = p3.add_run()
run1.text = 'Normal '
run1.font.size = Pt(18)
run1.font.name = 'Microsoft YaHei'

run2 = p3.add_run()
run2.text = 'Bold green'
run2.font.size = Pt(18)
run2.font.bold = True
run2.font.color.rgb = PALETTE['primary']
run2.font.name = 'Microsoft YaHei'
```

### 中文字体设置（eastAsia）

python-pptx 的 `font.name` 只设 ASCII 字体，中文字体需通过 oxml 设 `eastAsia`：

```python
def set_font_eastasia(run, font_name='Microsoft YaHei'):
    """设置 run 的中文字体（eastAsia）"""
    rPr = run._r.get_or_add_rPr()
    for ea in rPr.findall(qn('a:ea')):
        rPr.remove(ea)
    ea = rPr.makeelement(qn('a:ea'), {'typeface': font_name})
    rPr.append(ea)

# 用法
run = p.add_run()
run.text = "中文文本"
run.font.name = 'Microsoft YaHei'
set_font_eastasia(run, 'Microsoft YaHei')
run.font.size = Pt(18)
```

### 段落属性

```python
p.alignment = PP_ALIGN.LEFT        # LEFT / CENTER / RIGHT / JUSTIFY
p.level = 0                        # 0-8 缩进级别
p.line_spacing = 1.5               # 行距（倍数或 Pt）
p.space_before = Pt(12)            # 段前距
p.space_after = Pt(8)              # 段后距
```

### 反模式

- 用 `<a:br/>` 字面量当换行（M-6 bug：会以 7 个字面字符存储）。用 `add_paragraph()` 或 `\n`
- 一个 paragraph 内放多段不同字号（应该用多 paragraph）

---

## Element 4: chart

### 创建

```python
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

chart_data = CategoryChartData()
chart_data.categories = ['Q1', 'Q2', 'Q3', 'Q4']
chart_data.add_series('ARR ($M)', (1.2, 2.8, 5.1, 9.4))

chart_shape = slide.shapes.add_chart(
    XL_CHART_TYPE.COLUMN_CLUSTERED,
    Inches(0.8), Inches(2), Inches(7), Inches(5),
    chart_data
)
chart_shape.name = 'TractionChart'
chart = chart_shape.chart

# 标题
chart.has_title = True
chart.chart_title.text_frame.text = 'ARR Growth'
chart.chart_title.text_frame.paragraphs[0].font.size = Pt(16)
chart.chart_title.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'

# 图例
chart.has_legend = True
chart.legend.position = XL_LEGEND_POSITION.BOTTOM
chart.legend.include_in_layout = False

# 数据标签
plot = chart.plots[0]
plot.has_data_labels = True

# 系列颜色
series = plot.series[0]
series.format.fill.solid()
series.format.fill.fore_color.rgb = PALETTE['primary']

# axismin=0 — load-bearing（防止 traction 图 y 轴造假）
value_axis = chart.value_axis
value_axis.minimum_scale = 0
value_axis.has_major_gridlines = True
```

### chartType 选择

| `XL_CHART_TYPE` | 用途 |
|-----------------|------|
| `COLUMN_CLUSTERED` | 类别比较（垂直柱） |
| `BAR_CLUSTERED` | 类别比较（≥6 类用水平条） |
| `LINE` / `LINE_MARKERS` | 时间序列 1-3 series |
| `PIE` | Part-of-whole 2-5 slices |
| `DOUGHNUT` | 同 pie 但中心可放总数 |
| `XY_SCATTER` | 相关性 / 分布 |
| `AREA` | 累积量随时间 |

### 多 series

```python
chart_data.add_series('ARR', (1.2, 2.8, 5.1, 9.4))
chart_data.add_series('Net Revenue', (0.9, 2.1, 4.0, 7.6))

# 分别染色
plot = chart.plots[0]
colors = [PALETTE['primary'], PALETTE['secondary']]
for idx, series in enumerate(plot.series):
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = colors[idx]
```

### 反模式

- 图表标题含 `()` `[]` `TBD`（以字面量 ship）— 必须 placeholder-free
- chart colors 在某些 viewer 会被归一到 theme（python-pptx 显式设 `series.format.fill` 可覆盖）
- `value_axis.minimum_scale` 不设 0 → hockey-stick y 轴 = VC visual lie

---

## Element 5: picture

### 添加图片

```python
from PIL import Image

img_path = '/path/to/hero.jpg'
pil_img = Image.open(img_path)
w_px, h_px = pil_img.size
target_w_inches = 8
target_h_inches = h_px / w_px * target_w_inches  # 保持纵横比

pic = slide.shapes.add_picture(
    img_path,
    Inches((13.333 - target_w_inches) / 2),  # 水平居中
    Inches(2),
    width=Inches(target_w_inches),
    height=Inches(target_h_inches)
)
pic.name = 'HeroImage'
```

### 必须设 alt 文本

用 oxml 设 `<wp:docPr>` 的 `descr` 属性：

```python
def set_picture_alt(pic, alt_text):
    """设置图片 alt 文本（accessibility）"""
    docPr = pic._element._nvXxPr.cNvPr
    docPr.set('descr', alt_text)

set_picture_alt(pic, 'Screenshot of dashboard showing 340% MoM growth')
```

### 反模式

- 透明 / fit 图片直接放在白底上漂浮（应放彩色矩形衬底）
- 拉伸图片破坏纵横比（必须用 PIL 算等比尺寸）
- 在 busy 截图上叠文字（应加半透明遮罩）
- 不指定 width/height → 按原始像素插入（严重溢出）

---

## Element 6: connector

### 创建带箭头的连接线

```python
from pptx.enum.shapes import MSO_CONNECTOR

connector = slide.shapes.add_connector(
    MSO_CONNECTOR.STRAIGHT,
    Inches(2), Inches(3),    # begin_x, begin_y
    Inches(6), Inches(3)     # end_x, end_y
)
connector.name = 'FlowArrow1'

# 设线条样式
connector.line.color.rgb = PALETTE['text']
connector.line.width = Pt(2)

# 必须加 arrowhead（每个 flow connector 都要有）
def add_arrowhead(connector, head='triangle', tail='none'):
    """给 connector 加箭头（python-pptx 不直接支持，需 oxml）"""
    from pptx.oxml.ns import qn
    line = connector.line._get_or_add_ln()
    # 头部箭头
    head_end = line.find(qn('a:headEnd'))
    if head_end is None:
        head_end = line.makeelement(qn('a:headEnd'), {})
        line.append(head_end)
    head_end.set('type', head)
    # 尾部箭头
    tail_end = line.find(qn('a:tailEnd'))
    if tail_end is None:
        tail_end = line.makeelement(qn('a:tailEnd'), {})
        line.append(tail_end)
    tail_end.set('type', tail)

add_arrowhead(connector, head='none', tail='triangle')
```

### 反模式

- bare shape 作为 from/to（被拒），必须用全路径 `add_connector(type, begin_x, begin_y, end_x, end_y)`
- flow connector 是 plain lines 不加 arrowhead（VC 看不出流向）

---

## Element 7: table

### 创建 + 样式

```python
rows, cols = 4, 3
table_shape = slide.shapes.add_table(
    rows, cols,
    Inches(0.8), Inches(2), Inches(11.5), Inches(4)
)
table_shape.name = 'TeamTable'
table = table_shape.table

# 列宽（必须显式）
table.columns[0].width = Inches(4)
table.columns[1].width = Inches(4)
table.columns[2].width = Inches(3.5)

# 先填行再设表级字体（row ops 会重置字体 cascade）
data = [
    ['Name', 'Role', 'Prior Company'],
    ['Alice', 'CEO', 'Ex-Stripe, built Atlas to $50M ARR'],
    ['Bob',   'CTO', 'Ex-Stripe, scaled API to 99.99% SLA'],
    ['Cara',  'CPO', 'Ex-Notion, led 0→1 mobile launch'],
]
for r_idx, row_data in enumerate(data):
    for c_idx, val in enumerate(row_data):
        cell = table.cell(r_idx, c_idx)
        cell.text = val
        p = cell.text_frame.paragraphs[0]
        p.font.name = 'Microsoft YaHei'

# 表级字体（在所有行填完后设）
# Header-row 样式是表级
for c_idx in range(cols):
    cell = table.cell(0, c_idx)
    cell.fill.solid()
    cell.fill.fore_color.rgb = PALETTE['primary']
    p = cell.text_frame.paragraphs[0]
    p.font.bold = True
    p.font.size = Pt(14)
    p.font.color.rgb = PALETTE['white']

# 数据行：zebra striping
for r_idx in range(1, rows):
    for c_idx in range(cols):
        cell = table.cell(r_idx, c_idx)
        cell.fill.solid()
        if r_idx % 2 == 0:
            cell.fill.fore_color.rgb = RGBColor(0xF1, 0xF8, 0xE9)
        else:
            cell.fill.fore_color.rgb = PALETTE['white']
        p = cell.text_frame.paragraphs[0]
        p.font.size = Pt(12)
        p.font.color.rgb = PALETTE['text']
```

### 反模式

- 先设表级字体再填行（row ops 会重置字体 cascade）；应该**先填行再设表级字体**
- 不设列宽（默认均分，可能不适合数据）

---

## Element 8: placeholder

```python
# 仅当 slide 使用带 placeholder 的 layout 时可用（非 blank）
slide = prs.slides.add_slide(prs.slide_layouts[1])

title_ph = slide.placeholders[0]   # 标题
title_ph.text = "本周进展"

body_ph = slide.placeholders[1]    # 正文
tf = body_ph.text_frame
tf.text = "完成 office_pptx skill"
p2 = tf.add_paragraph()
p2.text = "修复 traction 图 y 轴"

# 段落级别（影响缩进和项目符号）
tf.paragraphs[0].level = 0   # 一级
tf.paragraphs[1].level = 1   # 二级（缩进更深）
```

> 注：blank layout (slide_layouts[6]) 没有 placeholder，必须用 `add_textbox` 替代。

---

## Element 9: group

```python
# python-pptx 支持 add_group_shape（v0.6.19+）
from pptx.enum.shapes import MSO_SHAPE

group = slide.shapes.add_group_shape()
group.name = 'StatCardGroup'

# 在 group 内添加 shapes（需要用 group.shapes.add_*）
card1 = group.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
    Inches(0), Inches(0), Inches(3), Inches(2))
card1.name = 'StatCard1'

card2 = group.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
    Inches(3.2), Inches(0), Inches(3), Inches(2))
card2.name = 'StatCard2'

# 整组定位
group.left = Inches(1)
group.top = Inches(2)
```

> 注：group 比单 shape 加 `shape.name` 后再用 name 寻址更稳，可整体移动 / 缩放。

---

## Element 10: animation

python-pptx **不原生支持** animation API，需用 oxml 注入 `<p:timing>` 元素：

```python
def add_fade_entrance(slide, shape, duration_ms=400, delay_ms=0):
    """给 shape 加 fade entrance 动画（python-pptx 无原生 API，用 oxml）"""
    from pptx.oxml.ns import qn
    import lxml.etree as etree

    # 构造 <p:timing> XML
    timing_xml = f'''
    <p:timing xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:tnLst>
        <p:par>
          <p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">
            <p:childTnLst>
              <p:seq concurrent="1" nextAc="seek">
                <p:cTn id="2" dur="indefinite" nodeType="mainSeq">
                  <p:childTnLst>
                    <p:par>
                      <p:cTn id="3" fill="hold">
                        <p:stCondLst>
                          <p:cond delay="indefinite"/>
                        </p:stCondLst>
                        <p:childTnLst>
                          <p:par>
                            <p:cTn id="4" fill="hold">
                              <p:stCondLst>
                                <p:cond delay="0"/>
                              </p:stCondLst>
                              <p:childTnLst>
                                <p:par>
                                  <p:cTn id="5" presetID="10" presetClass="entr"
                                         presetSubtype="0" fill="hold" grpId="0"
                                         nodeType="clickEffect">
                                    <p:stCondLst>
                                      <p:cond delay="{delay_ms}"/>
                                    </p:stCondLst>
                                    <p:childTnLst>
                                      <p:set>
                                        <p:cBhvr>
                                          <p:cTn id="6" dur="1" fill="hold">
                                            <p:stCondLst>
                                              <p:cond delay="0"/>
                                            </p:stCondLst>
                                          </p:cTn>
                                          <p:tgtEl>
                                            <p:spTgt spid="{shape.shape_id}"/>
                                          </p:tgtEl>
                                          <p:attrNameLst>
                                            <p:attrName>style.visibility</p:attrName>
                                          </p:attrNameLst>
                                        </p:cBhvr>
                                        <p:to>
                                          <p:strVal val="visible"/>
                                        </p:to>
                                      </p:set>
                                      <p:anim calcmode="lin" valueType="num">
                                        <p:cBhvr additive="base">
                                          <p:cTn id="7" dur="{duration_ms}" fill="hold"/>
                                          <p:tgtEl>
                                            <p:spTgt spid="{shape.shape_id}"/>
                                          </p:tgtEl>
                                          <p:attrNameLst>
                                            <p:attrName>style.opacity</p:attrName>
                                          </p:attrNameLst>
                                        </p:cBhvr>
                                        <p:tavLst>
                                          <p:tav tm="0">
                                            <p:val>
                                              <p:fltVal val="0"/>
                                            </p:val>
                                          </p:tav>
                                          <p:tav tm="100000">
                                            <p:val>
                                              <p:fltVal val="1"/>
                                            </p:val>
                                          </p:tav>
                                        </p:tavLst>
                                      </p:anim>
                                    </p:childTnLst>
                                  </p:cTn>
                                </p:childTnLst>
                              </p:cTn>
                            </p:par>
                          </p:childTnLst>
                        </p:cTn>
                      </p:par>
                    </p:childTnLst>
                  </p:cTn>
                </p:childTnLst>
              </p:seq>
            </p:childTnLst>
          </p:cTn>
        </p:par>
      </p:tnLst>
    </p:timing>
    '''
    timing = etree.fromstring(timing_xml)
    slide._element.append(timing)

# 用法：fade-entrance-400
add_fade_entrance(slide, shape, duration_ms=400, delay_ms=0)

# animation=none 清除
def clear_animation(slide):
    from pptx.oxml.ns import qn
    for timing in slide._element.findall(qn('p:timing')):
        slide._element.remove(timing)
```

### 反模式

- `bounce` / `swivel` / `spin` / `fly-from-edge` 在商务场景读起来业余（只用 `fade` / `wipe` / `appear`）
- 编造 `morph.duration=` / `transition.delay=` 作为独立 prop（这些不是 python-pptx API）

---

## Element 11: transition

python-pptx **不原生支持** transition API，需用 oxml 注入 `<p:transition>` 元素：

```python
def add_morph_transition(slide, duration_ms=2000):
    """Morph transition：morph-slow / -fast / morph-<DUR_MS>"""
    from pptx.oxml.ns import qn
    import lxml.etree as etree

    # 移除已有 transition
    for tr in slide._element.findall(qn('p:transition')):
        slide._element.remove(tr)

    transition_xml = f'''
    <p:transition xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:p159="http://schemas.microsoft.com/office/powerpoint/2015/09/main"
                  spd="slow" p159:dur="{duration_ms}">
      <p159:morph option="byObject"/>
    </p:transition>
    '''
    transition = etree.fromstring(transition_xml)
    slide._element.append(transition)

# 用法
add_morph_transition(slide, duration_ms=2000)  # morph-slow
add_morph_transition(slide, duration_ms=800)   # morph-fast
```

### 反模式

- 编造 `morph.duration=` / `transition.delay=` 作为独立 prop
- Morph 在 PowerPoint 2019 之前版本不支持（会降级为 fade）

---

## Element 12: notes

**每个 content slide 必须**有 notes（H7 hard rule）：

```python
slide.notes_slide.notes_text_frame.text = """
Key talking points:
- Lead with the pain: 73% of teams waste 2+ hours/week on manual reconciliation
- Tie to VC thesis: SaaS automation is a $4B market growing 18% CAGR
- Demo the 3-step workflow: import → match → export
- Cite case study: Acme Co saved $180K/year, payback in 4 months
"""
```

### 反模式

- 内容 slide 无 notes（讲者拿不到 script）
- notes 写成 "TODO" / placeholder

---

## Element 13: misc

### master / layouts

```python
# 访问 slide master
master = prs.slide_masters[0]

# 访问 layouts
for layout in prs.slide_layouts:
    print(layout.name)

# typed add/set/remove（python-pptx 部分支持）
# 改 master 背景
master.background.fill.solid()
master.background.fill.fore_color.rgb = PALETTE['white']
```

### smartart

python-pptx **不原生支持** smartart 创建，需 oxml round-trip via add-part：

```python
# 不可用 API，需用 oxml 注入 SmartArt part
# 实际场景：用 chart / shape / table 替代 SmartArt 即可
# SmartArt 优势是 layout engine，但本项目用 12-column grid 自己算
```

### equation

python-pptx **不原生支持**，需 oxml 注入 `<a:math>` 元素。**OfficeCli 无此内容，本项目独立设计**：用图片（截图自 LaTeX 渲染）或 OMML XML 注入。

### video / audio

python-pptx 支持 `add_movie`：

```python
slide.shapes.add_movie(
    '/path/to/video.mp4',
    Inches(2), Inches(2), Inches(6), Inches(4),
    poster_frame_image='/path/to/poster.jpg',
    mime_type='video/mp4'
)
```

属性：`loop=True/False`、`auto_start=True/False` 通过 oxml 设 `<p:nvPr>` 下的 `<p:ph>` 属性。

### zoom

`--prop target=N`（一链接一目标），python-pptx 需用 oxml 创建 `<p:graphicFrame>` 下的 zoom element：

```python
# 简化做法：用 hyperlink 替代
from pptx.enum.text import MSO_HYPERLINK
shape.click_action.target_slide = prs.slides[idx]
```

### comment

legacy + p188 现代线程化，python-pptx 不直接支持，需 oxml。**OfficeCli 无此内容，本项目独立设计**：notes 是首选替代。

### model3d

`rotation=ax/ay/az`，python-pptx **不原生支持**，需 oxml 注入 `<p:graphicFrame>` 下的 `<p164:model3d>`。**OfficeCli 无此内容，本项目独立设计**：用 2D 截图替代。

### diagram

add-only mermaid → native shapes 或 rendered image：

```python
# 推荐：把 mermaid 渲染成 image，再 add_picture
# 不可用时用 shape + connector 自己画流程图
```

---

## 参考

- [python-pptx 官方文档](https://python-pptx.readthedocs.io/)
- [python-pptx GitHub](https://github.com/scanny/python-pptx)
- [office_pptx SKILL.md](file:///f:/<project_root>/.agents/skills/office_pptx/SKILL.md)
- [references/design_principles.md](file:///f:/<project_root>/.agents/skills/office_pptx/references/design_principles.md)
- [references/recipes.md](file:///f:/<project_root>/.agents/skills/office_pptx/references/recipes.md)
