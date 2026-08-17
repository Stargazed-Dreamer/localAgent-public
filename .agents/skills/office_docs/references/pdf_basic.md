# PDF 基础元素参考（创建 + 操作 + 提取）

本文件是 [office_pdf skill](file:///f:/<project_root>/.agents/skills/office_pdf/SKILL.md) 的子参考，专注 PDF 文档（.pdf）的完整工作流：创建新 PDF、操作现有 PDF、提取 PDF 内容。

> **重要**：OfficeCli 项目没有 PDF skill，本文件由本项目独立设计。基于原 `office_export/references/pdf.md` 内容并扩充。

## 目录

1. [Mental Model](#1-mental-model)
2. [创建 PDF 的两种模式](#2-创建-pdf-的两种模式)
3. [5 步工作流（用 reportlab 创建 PDF）](#3-5-步工作流用-reportlab-创建-pdf)
4. [Element 1: Paragraphs](#4-element-1-paragraphs)
5. [Element 2: Headings](#5-element-2-headings)
6. [Element 3: Lists](#6-element-3-lists)
7. [Element 4: Tables](#7-element-4-tables)
8. [Element 5: Images](#8-element-5-images)
9. [Element 6: Page Breaks](#9-element-6-page-breaks)
10. [Element 7: Headers & Footers](#10-element-7-headers--footers)
11. [Element 8: Chinese Font Registration](#11-element-8-chinese-font-registration关键)
12. [PDF 操作（用 pypdf）](#12-pdf-操作用-pypdf)
13. [PDF 提取（用 pdfplumber）](#13-pdf-提取用-pdfplumber)
14. [6 陷阱（必须避免）](#14-6-陷阱必须避免)
15. [完整示例 — Markdown 转 PDF](#15-完整示例--markdown-转-pdf)
16. [完整示例 — 合并多个 PDF](#16-完整示例--合并多个-pdf)
17. [完整示例 — PDF 加水印](#17-完整示例--pdf-加水印)
18. [完整示例 — 提取 PDF 表格](#18-完整示例--提取-pdf-表格)
19. [Validation Checklist](#19-validation-checklist)
20. [Common Pitfalls 表](#20-common-pitfalls-表)

## 1. Mental Model

**PDF = 固定布局的页面描述语言**，与 docx 的"ZIP+XML"和"流式布局"完全不同。PDF 内部是文本对象、路径、图像的坐标集合，没有"段落自动换行"或"表格自动布局"的概念。

### 两个层次的库

| 库 | 职责 | API |
|------|-----|-----|
| **reportlab** | 创建新 PDF | Canvas（低层）+ Platypus（高层）|
| **pypdf** | 操作已有 PDF | `PdfReader` / `PdfWriter` |
| **pdfplumber** | 提取 PDF 内容 | `pdfplumber.open(path)` |

### Platypus 核心抽象

把内容抽象为 `Flowable`（可流动元素），由 `BaseDocTemplate` 自动分页、布局、换行。

```
SimpleDocTemplate
├── Story (List[Flowable])
│   ├── Paragraph         # 段落（支持 HTML-like 标记）
│   ├── Spacer            # 间距
│   ├── Table             # 表格
│   ├── Image             # 图片
│   ├── PageBreak         # 分页符
│   ├── ListFlowable      # 列表
│   └── KeepTogether      # 防止跨页拆分
├── PageTemplate
│   ├── onPage            # 页眉页脚回调
│   └── frames            # 内容区域（Frame）
└── Styles (StyleSheet1)  # 样式表
```

### PDF 是终端输出格式

PDF 与 docx/xlsx/pptx 不同——它是**终端输出格式，不是可编辑格式**。生成 PDF 时要"一次到位"，因为后期编辑困难。修改现有 PDF 通常需要重新生成，而不是原地编辑。

## 2. 创建 PDF 的两种模式

### Canvas 低层模式

直接绘制每页元素，完全控制位置/样式：

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors

c = canvas.Canvas("output.pdf", pagesize=A4)
c.drawString(2*cm, A4[1] - 2*cm, "固定位置文本")  # 绝对坐标
c.rect(2*cm, 2*cm, 5*cm, 3*cm, fill=1, stroke=0)  # 画矩形
c.showPage()  # 结束当前页
c.save()
```

**适用场景**：发票/票据、证书、海报、自定义 invoice——任何需要精确坐标控制的场景。

### Platypus 高层模式

用 Story（Paragraphs/Tables/Images/PageBreaks）让 reportlab 自动布局：

```python
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

doc = SimpleDocTemplate("output.pdf")
story = [
    Paragraph("标题", style_title),
    Spacer(1, 0.5*cm),
    Paragraph("正文内容...", style_body),
]
doc.build(story)
```

**适用场景**：标准文档（报告/简历/手册/归档）——任何流式长文档。

### 何时用哪个

| 场景 | 模式 |
|------|------|
| 标准文档（报告/简历/归档）| Platypus |
| 复杂自定义布局（证书/海报/invoice）| Canvas |
| 混合（标准文档 + 自定义页眉页脚）| Platypus + `onPage` 回调（回调内用 Canvas API）|

## 3. 5 步工作流（用 reportlab 创建 PDF）

### Step 1: 注册中文字体

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('MSYH-Bold', 'C:/Windows/Fonts/msyhbd.ttc'))
registerFontFamily('MSYH', normal='MSYH', bold='MSYH-Bold',
                   italic='MSYH', boldItalic='MSYH-Bold')
```

### Step 2: 定义样式

```python
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

styles = getSampleStyleSheet()
style_body = ParagraphStyle(
    'Body', parent=styles['Normal'],
    fontName='MSYH', fontSize=11, leading=18,
    textColor=colors.HexColor('#212121'),
    alignment=TA_LEFT,
)
```

### Step 3: 构建 Story

```python
from reportlab.platypus import Paragraph, Spacer, Table, PageBreak

story = [
    Paragraph("标题", style_title),
    Spacer(1, 0.5*cm),
    Paragraph("正文段落", style_body),
    PageBreak(),
]
```

### Step 4: Build PDF（with header/footer callback）

```python
from reportlab.platypus import SimpleDocTemplate
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

doc = SimpleDocTemplate(
    "workspace/office_pdf/报告_20260718.pdf",
    pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,
    topMargin=2.5*cm, bottomMargin=2*cm,
    title="报告", author="LocalAgent"
)
doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
```

### Step 5: 验证

```python
from pypdf import PdfReader
reader = PdfReader("workspace/office_pdf/报告_20260718.pdf")
print(f"页数: {len(reader.pages)}")
print(f"元数据: {reader.metadata}")
```

## 4. Element 1: Paragraphs

`Paragraph(text, style)`，text 支持 HTML-like 标签。

```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors

style = ParagraphStyle(
    'Body', fontName='MSYH', fontSize=11, leading=18,
    textColor=colors.HexColor('#212121'),
    firstLineIndent=24,  # 首行缩进 2 字符
)

# 简单段落
p = Paragraph("这是一段正文，包含中文字符。", style)

# HTML-like 标记（reportlab 支持 <b> <i> <font> <u> <sup> <sub> <a>）
p = Paragraph(
    "普通文本 <b>加粗</b> <i>斜体</i> <u>下划线</u> "
    "<font color='#2E7D32'>绿色强调</font> "
    "H<sub>2</sub>O 与 X<super>2</super> "
    "<a href='https://example.com' color='blue'>链接</a>",
    style
)
```

### 支持的 HTML-like 标记

| 标记 | 说明 | 示例 |
|------|------|------|
| `<b>` | 加粗 | `<b>加粗</b>` |
| `<i>` | 斜体 | `<i>斜体</i>` |
| `<u>` | 下划线 | `<u>下划线</u>` |
| `<font>` | 字体/颜色/字号 | `<font name='MSYH' color='red' size='14'>文本</font>` |
| `<sup>` | 上标 | `X<sup>2</sup>` |
| `<sub>` | 下标 | `H<sub>2</sub>O` |
| `<a>` | 超链接 | `<a href='https://...'>链接</a>` |
| `<br/>` | 换行 | `第一行<br/>第二行` |

### XML 转义（关键陷阱）

`<` `>` `&` 必须用 `&lt;` `&gt;` `&amp;`，否则会被解析为标签：

```python
from xml.sax.saxutils import escape

text = "x < 5 且 y > 3 & z == 0"
p = Paragraph(escape(text), style)
# 输出: x &lt; 5 且 y &gt; 3 &amp; z == 0
```

### 反模式：Unicode 上下标

```python
# ❌ 反模式：直接用 Unicode 字符（会渲染成黑方块）
Paragraph("H₂O 和 X²", style)

# ✓ 推荐：用 <sub>/<super> 标签
Paragraph("H<sub>2</sub>O 和 X<super>2</super>", style)
```

## 5. Element 2: Headings

```python
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

# 自定义绿色标题样式（推荐，避免默认蓝色 + 中文字体问题）
GREEN_PRIMARY = colors.HexColor('#2E7D32')

h1_style = ParagraphStyle(
    'CN_H1', fontName='MSYH-Bold', fontSize=18, leading=24,
    textColor=GREEN_PRIMARY,
    spaceBefore=14, spaceAfter=8, alignment=TA_LEFT
)
h2_style = ParagraphStyle(
    'CN_H2', fontName='MSYH-Bold', fontSize=14, leading=20,
    textColor=GREEN_PRIMARY,
    spaceBefore=10, spaceAfter=6, alignment=TA_LEFT
)
h3_style = ParagraphStyle(
    'CN_H3', fontName='MSYH-Bold', fontSize=12, leading=18,
    textColor=GREEN_PRIMARY,
    spaceBefore=8, spaceAfter=4, alignment=TA_LEFT
)

story.append(Paragraph("一级标题", h1_style))
story.append(Paragraph("二级标题", h2_style))
```

### 字号规范

| 层级 | 字号 |
|------|------|
| Title（封面大标题）| 22pt |
| H1 | 18pt |
| H2 | 14pt |
| H3 | 12pt |
| Body | 11pt |
| Caption（注释/页眉页脚）| 9pt |

### 自动 outline level（PDF 书签）

`ParagraphStyle` 可设 `outlineLevel` 让 PDF 阅读器显示书签导航：

```python
h1_style = ParagraphStyle('CN_H1', ..., outlineLevel=0)  # 一级书签
h2_style = ParagraphStyle('CN_H2', ..., outlineLevel=1)  # 二级书签
```

## 6. Element 3: Lists

```python
from reportlab.platypus import ListFlowable, ListItem
from reportlab.lib import colors

# 项目符号列表
items = [
    ListItem(Paragraph("编写 office_pdf skill", style_body), value='bullet'),
    ListItem(Paragraph("修复中文字体渲染", style_body), value='bullet'),
    ListItem(Paragraph("部署远程 VL 服务", style_body), value='bullet'),
]
bullet_list = ListFlowable(
    items, bulletType='bullet', bulletFontName='MSYH',
    bulletColor=colors.HexColor('#2E7D32'), leftIndent=18
)
story.append(bullet_list)

# 编号列表（bulletType: '1'=数字, 'a'=小写字母, 'A'=大写字母）
numbered_items = [
    ListItem(Paragraph("第一步：读取 markdown", style_body)),
    ListItem(Paragraph("第二步：解析结构", style_body)),
    ListItem(Paragraph("第三步：生成 PDF", style_body)),
]
numbered_list = ListFlowable(
    numbered_items, bulletType='1',
    bulletFontName='MSYH', leftIndent=18
)
story.append(numbered_list)

# 嵌套列表：ListFlowable 套 ListFlowable
nested = ListFlowable([
    ListItem(Paragraph("外层项 1", style_body), value='bullet'),
    ListItem(ListFlowable([
        ListItem(Paragraph("内层项 1.1", style_body), value='bullet'),
        ListItem(Paragraph("内层项 1.2", style_body), value='bullet'),
    ], bulletType='bullet', leftIndent=18)),
], bulletType='bullet', leftIndent=18)
story.append(nested)
```

## 7. Element 4: Tables

```python
from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.units import cm

# 数据（首行为表头）
data = [
    ['任务', '状态', '完成时间'],
    ['编写文档', '已完成', '2026-07-17'],
    ['测试发布', '进行中', '2026-07-20'],
    ['部署上线', '待开始', '2026-07-25'],
]

# 列宽（必须显式指定，否则表格会撑爆页面）
col_widths = [5*cm, 4*cm, 4*cm]

# repeatRows=1 表头跨页重复；splitByRow=True 允许按行拆分
table = Table(data, colWidths=col_widths, repeatRows=1, splitByRow=1)

# 样式：绿色表头 + 边框 + 对齐 + 隔行变色
table.setStyle(TableStyle([
    # 表头
    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2E7D32')),  # 绿色背景
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),                # 白字
    ('FONTNAME', (0, 0), (-1, 0), 'MSYH-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 11),
    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
    # 数据行
    ('FONTNAME', (0, 1), (-1, -1), 'MSYH'),
    ('FONTSIZE', (0, 1), (-1, -1), 10),
    ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#212121')),
    ('ALIGN', (0, 1), (-1, -1), 'LEFT'),
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    # 边框
    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#BDBDBD')),
    # 隔行变色（白 + Green 50 浅）
    ('ROWBACKGROUNDS', (0, 1), (-1, -1),
     [colors.white, colors.HexColor('#F1F8E9')]),
    # 内边距
    ('LEFTPADDING', (0, 0), (-1, -1), 6),
    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ('TOPPADDING', (0, 0), (-1, -1), 6),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
]))
story.append(table)
```

### TableStyle 常用命令

| 命令 | 参数 | 说明 |
|------|------|------|
| `BACKGROUND` | `(start, end), color` | 背景色 |
| `TEXTCOLOR` | `(start, end), color` | 文字色 |
| `FONTNAME` | `(start, end), name` | 字体 |
| `FONTSIZE` | `(start, end), size` | 字号 |
| `ALIGN` | `(start, end), 'LEFT'/'CENTER'/'RIGHT'` | 水平对齐 |
| `VALIGN` | `(start, end), 'TOP'/'MIDDLE'/'BOTTOM'` | 垂直对齐 |
| `GRID` | `(start, end), width, color` | 网格线 |
| `SPAN` | `(start, end)` | 合并单元格 |
| `ROWBACKGROUNDS` | `(start, end), [color1, color2]` | 隔行变色 |

### 长表格跨页处理（关键）

```python
# ❌ 反模式：默认不重复表头，跨页断裂后第 2 页没有表头
table = Table(data, colWidths=col_widths)

# ✓ 正确：repeatRows=1 + splitByRow=1
table = Table(data, colWidths=col_widths, repeatRows=1, splitByRow=1)
```

- `repeatRows=1`：第 1 行（表头）在每页重复
- `splitByRow=1`（默认 True）：允许按行拆分，避免整块切断

## 8. Element 5: Images

```python
from reportlab.platypus import Image
from reportlab.lib.units import cm

# 按指定宽度插入（高度按比例缩放）
img = Image("workspace/screenshot.png", width=12*cm, height=8*cm)
story.append(img)

# 自动按比例缩放（kind='proportional' 保持纵横比）
img = Image("workspace/screenshot.png",
            width=14*cm, height=10*cm, kind='proportional')
story.append(img)
```

### 工具函数：按页面边界自动缩放

```python
from PIL import Image as PILImage

def scale_image(path, max_width_cm=14, max_height_cm=18):
    """按比例缩放图片到不超过 max_width_cm × max_height_cm"""
    pil_img = PILImage.open(path)
    w_px, h_px = pil_img.size
    dpi = pil_img.info.get('dpi', (96, 96))[0]
    w_cm = w_px / dpi * 2.54
    h_cm = h_px / dpi * 2.54
    scale = min(max_width_cm / w_cm, max_height_cm / h_cm, 1.0)
    return Image(path, width=w_cm*scale*cm, height=h_cm*scale*cm)

story.append(scale_image("workspace/large.png", max_width_cm=14))
```

### 反模式：图片溢出

```python
# ❌ 反模式：不指定尺寸，reportlab 用原始像素尺寸（按 72 DPI 转 pt），通常严重溢出页面
img = Image("workspace/large.png")

# ✓ 正确：始终显式指定 width/height，或用 scale_image 工具函数
img = Image("workspace/large.png", width=14*cm, height=10*cm, kind='proportional')
```

## 9. Element 6: Page Breaks

```python
from reportlab.platypus import PageBreak, CondPageBreak, KeepTogether

# 显式分页
story.append(PageBreak())

# 条件分页：剩余空间不足 2 inch 时分页
story.append(CondPageBreak(2*inch))

# 防止元素跨页拆分（如标题 + 第一段要在一起）
group = KeepTogether([
    Paragraph("重要章节", h2_style),
    Paragraph("这是该章节的第一段内容...", style_body),
])
story.append(group)
```

## 10. Element 7: Headers & Footers

用 `SimpleDocTemplate` 的 `onFirstPage` + `onLaterPages` 回调：

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
import datetime

def header_footer(canvas_obj, doc):
    """页眉页脚回调（每页都会调用）"""
    # 必须先 saveState，否则样式会泄漏到后续元素
    canvas_obj.saveState()

    # 页眉（右上角）
    canvas_obj.setFont('MSYH', 9)
    canvas_obj.setFillColor(colors.HexColor('#616161'))
    canvas_obj.drawRightString(
        A4[0] - 2*cm, A4[1] - 1*cm,
        "周报 2026-07-18"
    )
    # 页眉下划线（绿色）
    canvas_obj.setStrokeColor(colors.HexColor('#2E7D32'))
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(2*cm, A4[1] - 1.2*cm, A4[0] - 2*cm, A4[1] - 1.2*cm)

    # 页脚（居中页码）
    page_num = canvas_obj.getPageNumber()
    canvas_obj.drawCentredString(A4[0] / 2, 1*cm, f"第 {page_num} 页")

    # 页脚日期（左下角）
    canvas_obj.drawString(
        2*cm, 1*cm,
        datetime.date.today().strftime('%Y-%m-%d')
    )

    # 必须最后 restoreState
    canvas_obj.restoreState()

# 在 build 时传入
doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
```

### 反模式：忘记 saveState / restoreState

```python
# ❌ 反模式：不 saveState/restoreState，页眉的字体颜色会泄漏到正文
def bad_header(canvas_obj, doc):
    canvas_obj.setFont('MSYH', 9)        # 这会泄漏
    canvas_obj.setFillColor(colors.red)  # 这会泄漏
    canvas_obj.drawString(2*cm, A4[1]-1*cm, "Header")
    # 没有 restoreState，正文渲染会受影响

# ✓ 正确：saveState + restoreState 配对
def good_header(canvas_obj, doc):
    canvas_obj.saveState()
    # ... 绘制页眉页脚 ...
    canvas_obj.restoreState()
```

## 11. Element 8: Chinese Font Registration（关键）

reportlab 默认字体（Helvetica / Times）**完全不支持中文字符**，未注册中文字体会渲染为黑方块或丢失。

### 完整注册代码

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

# 注册中文字体
pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('MSYH-Bold', 'C:/Windows/Fonts/msyhbd.ttc'))

# 注册字体族（让 <b> 标签自动用 MSYH-Bold）
registerFontFamily(
    'MSYH',
    normal='MSYH',
    bold='MSYH-Bold',
    italic='MSYH',         # 雅黑无斜体，回退到正体
    boldItalic='MSYH-Bold'
)
```

### 字体路径（跨平台）

| 系统 | 字体路径 | 常用字体 |
|------|---------|---------|
| Windows | `C:/Windows/Fonts/` | `msyh.ttc`（雅黑）/ `simsun.ttc`（宋体）/ `simhei.ttf`（黑体）/ `msyhbd.ttc`（雅黑粗体）|
| macOS | `/System/Library/Fonts/` | `PingFang.ttc` / `STHeiti Light.ttc` |
| Linux | `/usr/share/fonts/` | `wqy-microhei.ttc` / `NotoSansCJK-Regular.ttc` |

### 使用注册后的字体

```python
# 方式 1：在 ParagraphStyle 中指定 fontName
style = ParagraphStyle('CN', fontName='MSYH', fontSize=11, leading=18)
Paragraph("中文内容", style)

# 方式 2：在 HTML-like 标记中临时切换字体
Paragraph("<font name='MSYH'>中文内容</font>", any_style)
```

### .ttc 字体集合的处理

`.ttc`（TrueType Collection）包含多个字体，reportlab 用 `subfontIndex` 指定：

```python
# simsun.ttc 包含 SimSun（index=0）和 NSimSun（index=1）
pdfmetrics.registerFont(TTFont('SimSun', 'C:/Windows/Fonts/simsun.ttc', subfontIndex=0))
pdfmetrics.registerFont(TTFont('NSimSun', 'C:/Windows/Fonts/simsun.ttc', subfontIndex=1))
```

### 反模式

```python
# ❌ 反模式 1：不注册字体，用默认 Helvetica 渲染中文
Paragraph("中文内容", styles['Normal'])  # styles['Normal'].fontName='Helvetica'
# 结果：PDF 中显示为黑方块或空白

# ❌ 反模式 2：只注册字体，不注册字体族，<b> 标签不生效
pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
Paragraph("<b>加粗中文</b>", style_cn)  # <b> 不生效，仍为正体

# ✓ 正确：注册字体 + 注册字体族
pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('MSYH-Bold', 'C:/Windows/Fonts/msyhbd.ttc'))
registerFontFamily('MSYH', normal='MSYH', bold='MSYH-Bold',
                   italic='MSYH', boldItalic='MSYH-Bold')
Paragraph("<b>加粗中文</b>", style_cn)  # <b> 正确生效
```

### 字体回退策略（推荐）

```python
import os

def first_existing_font(candidates):
    """返回第一个存在的字体 (name, path)"""
    for name, path in candidates:
        if os.path.exists(path):
            return name, path
    return None, None

name, path = first_existing_font([
    ('MSYH',   'C:/Windows/Fonts/msyh.ttc'),
    ('SimSun', 'C:/Windows/Fonts/simsun.ttc'),
    ('SimHei', 'C:/Windows/Fonts/simhei.ttf'),
])
if name:
    pdfmetrics.registerFont(TTFont(name, path))
```

## 12. PDF 操作（用 pypdf）

### 合并多个 PDF

```python
from pypdf import PdfReader, PdfWriter

# pypdf 4.x 起 PdfMerger 已弃用，PdfWriter 是统一入口
writer = PdfWriter()
for pdf_path in ["part1.pdf", "part2.pdf", "part3.pdf"]:
    reader = PdfReader(pdf_path)
    for page in reader.pages:
        writer.add_page(page)
with open("workspace/office_pdf/合集_20260718.pdf", "wb") as f:
    writer.write(f)
```

### 拆分 PDF（按页）

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("workspace/office_pdf/合集.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"workspace/office_pdf/第{i+1}页.pdf", "wb") as f:
        writer.write(f)

# 按范围拆分
def split_pdf(src_path, ranges, output_dir):
    """ranges: [(start, end, output_name), ...]，页码从 1 开始"""
    reader = PdfReader(src_path)
    for start, end, name in ranges:
        writer = PdfWriter()
        for i in range(start-1, end):
            writer.add_page(reader.pages[i])
        with open(f"{output_dir}/{name}.pdf", "wb") as f:
            writer.write(f)

split_pdf("input.pdf",
          [(1, 5, "前言"), (6, 20, "正文")],
          "workspace/office_pdf/")
```

### 加密 / 解密

```python
from pypdf import PdfReader, PdfWriter

# 加密
writer = PdfWriter()
reader = PdfReader("workspace/office_pdf/敏感报告.pdf")
for page in reader.pages:
    writer.add_page(page)
writer.encrypt(
    user_password="user123",      # 用户密码（打开需要）
    owner_password="owner456",    # 所有者密码（修改权限需要）
    use_128bit=True               # 128 位加密（兼容性更好）
)
with open("workspace/office_pdf/敏感报告_加密.pdf", "wb") as f:
    writer.write(f)

# 解密
reader = PdfReader("workspace/office_pdf/敏感报告_加密.pdf")
if reader.is_encrypted:
    reader.decrypt("user123")
print(f"页数: {len(reader.pages)}")
```

### 旋转页面

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("workspace/office_pdf/横版图.pdf")
writer = PdfWriter()
for page in reader.pages:
    # rotate(90) 顺时针 90 度；rotate(-90) 逆时针；rotate(180) 翻转
    page.rotate(90)
    writer.add_page(page)
with open("workspace/office_pdf/横版图_旋转.pdf", "wb") as f:
    writer.write(f)
```

### 添加水印

```python
from pypdf import PdfReader, PdfWriter

# 准备水印 PDF（用 reportlab 生成）
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

def create_watermark(output_path, text="LOCALAGENT INTERNAL"):
    c = canvas.Canvas(output_path, pagesize=A4)
    c.saveState()
    c.setFont('Helvetica', 40)
    c.setFillColor(colors.HexColor('#2E7D32'))
    c.setFillAlpha(0.2)  # 半透明
    c.translate(A4[0]/2, A4[1]/2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.showPage()
    c.save()

create_watermark("temp/watermark.pdf", "机密 INTERNAL")

# 把水印叠加到每页
reader = PdfReader("workspace/office_pdf/原文.pdf")
watermark = PdfReader("temp/watermark.pdf")
writer = PdfWriter()
for page in reader.pages:
    page.merge_page(watermark.pages[0])  # 叠加水印
    writer.add_page(page)
with open("workspace/office_pdf/原文_加水印.pdf", "wb") as f:
    writer.write(f)
```

### 提取页数 / 元数据

```python
from pypdf import PdfReader

reader = PdfReader("workspace/office_pdf/报告.pdf")
print(f"页数: {len(reader.pages)}")
print(f"元数据: {reader.metadata}")
print(f"是否加密: {reader.is_encrypted}")
```

### 提取文本（pypdf 有限支持）

```python
from pypdf import PdfReader

reader = PdfReader("workspace/office_pdf/报告.pdf")
text = reader.pages[0].extract_text()
# 注意：pypdf 文本提取有限支持，复杂布局用 pdfplumber（见下一节）
```

## 13. PDF 提取（用 pdfplumber）

### 提取纯文本

```python
import pdfplumber

with pdfplumber.open("workspace/office_pdf/报告.pdf") as pdf:
    print(f"总页数: {len(pdf.pages)}")

    # 提取所有页文本
    full_text = ""
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        full_text += f"\n=== 第 {i+1} 页 ===\n{text}"

    print(full_text[:500])

# 简化版：只读首页
with pdfplumber.open("workspace/office_pdf/报告.pdf") as pdf:
    first_page_text = pdf.pages[0].extract_text()
```

### 提取表格

```python
import pdfplumber

with pdfplumber.open("workspace/office_pdf/账单.pdf") as pdf:
    page = pdf.pages[0]
    tables = page.extract_tables()  # 返回 list of table（每个 table 是 list of row）
    if tables:
        for table in tables:
            print("=== 表格 ===")
            for row in table:
                print(row)
                # ['任务', '状态', '完成时间']
                # ['编写文档', '已完成', '2026-07-17']

# 自定义表格提取设置（应对无边框表格）
with pdfplumber.open("workspace/office_pdf/无边框表.pdf") as pdf:
    page = pdf.pages[0]
    tables = page.extract_tables({
        "vertical_strategy": "text",     # 按文本垂直对齐识别列
        "horizontal_strategy": "text",   # 按文本水平对齐识别行
        "snap_tolerance": 3,
    })
```

### 反模式：复杂布局表格提取不准

pdfplumber 对**有边框表格**提取准确，对**无边框表格**或**合并单元格表格**可能不准。复杂布局时调整 `table_settings` 或考虑用 OCR（参考 `ocr` skill）。

## 14. 6 陷阱（必须避免）

### 陷阱 1：默认中文渲染为黑方块

**症状**：PDF 中中文显示为黑方块、空白或乱码。

**原因**：reportlab 默认字体 `Helvetica` 不含中文字形，未注册中文字体时静默丢弃。

**解决**：见 [Element 8: Chinese Font Registration](#11-element-8-chinese-font-registration关键)。

```python
# ❌ 反模式
Paragraph("中文内容", styles['Normal'])  # Helvetica，黑方块

# ✓ 正确
pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
style_cn = ParagraphStyle('CN', fontName='MSYH', fontSize=11, leading=18)
Paragraph("中文内容", style_cn)
```

### 陷阱 2：Unicode 上下标字符

**症状**：`₀₁₂` `⁰¹²` 等 Unicode 字符在 PDF 中显示为黑方块。

**原因**：TTF 字体的 cmap 表可能不包含 U+2080-U+2089（下标）、U+2070-U+2079（上标）范围。

**解决**：用 reportlab 的 `<sub>` / `<super>` 标记替代：

```python
# ❌ 反模式
Paragraph("H₂O 和 X²", style)  # 可能渲染异常

# ✓ 推荐
Paragraph("H<sub>2</sub>O 和 X<super>2</super>", style)
```

### 陷阱 3：长表格跨页断裂

**症状**：表格行被切断在两页之间，行内容部分丢失。

**原因**：默认 Table 不会自动重复表头，且单行高度超过页面剩余空间时会被截断。

**解决**：

```python
# repeatRows=1 让表头在每页重复
# splitByRow=1 允许按行拆分
table = Table(data, colWidths=col_widths, repeatRows=1, splitByRow=1)
```

### 陷阱 4：图片溢出

**症状**：图片宽度超过页面可用宽度，被截断或缩放变形。

**解决**：始终显式指定 width/height，或用 `scale_image` 工具函数按比例缩放（见 [Element 5: Images](#8-element-5-images)）。

### 陷阱 5：Paragraph XML 转义

**症状**：Paragraph 中包含 `<` `>` `&` 等字符时渲染异常或报错。

**原因**：reportlab 的 Paragraph 解析 HTML-like 标记，`<` 被识别为标签起始。

**解决**：转义为 `&lt;` `&gt;` `&amp;`：

```python
from xml.sax.saxutils import escape
text = "x < 5 且 y > 3 & z == 0"
Paragraph(escape(text), style)
```

### 陷阱 6：Spacer 单位错误

**症状**：`Spacer(1, 10)` 看不到间距或间距过大。

**原因**：`Spacer(width, height)` 的单位是 pt（1 pt = 1/72 inch），不是像素。裸数字 `10` 表示 10 pt，约 0.35 cm，肉眼几乎看不到。

**解决**：用 `cm` 或 `mm` 显式表达：

```python
from reportlab.lib.units import cm

# ❌ 反模式：裸数字
story.append(Spacer(1, 10))  # 10 pt，几乎看不到

# ✓ 推荐：显式单位
story.append(Spacer(1, 0.5*cm))  # 0.5 cm
```

## 15. 完整示例 — Markdown 转 PDF

输入：`workspace/daily_summary/20260718.md`
输出：`workspace/office_pdf/日报_20260718.pdf`

```python
"""完整示例：从 markdown 日报到 PDF"""
import os
import datetime
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, ListFlowable, ListItem
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

# === Step 1: 注册中文字体 ===
pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('MSYH-Bold', 'C:/Windows/Fonts/msyhbd.ttc'))
registerFontFamily('MSYH', normal='MSYH', bold='MSYH-Bold',
                   italic='MSYH', boldItalic='MSYH-Bold')

# === Step 2: 定义样式 ===
GREEN_PRIMARY = colors.HexColor('#2E7D32')
TEXT_PRIMARY  = colors.HexColor('#212121')
TEXT_SECOND   = colors.HexColor('#616161')

title_style = ParagraphStyle(
    'Title', fontName='MSYH-Bold', fontSize=22, leading=30,
    textColor=GREEN_PRIMARY, alignment=TA_LEFT, spaceAfter=12
)
h1_style = ParagraphStyle(
    'H1', fontName='MSYH-Bold', fontSize=18, leading=24,
    textColor=GREEN_PRIMARY, spaceBefore=14, spaceAfter=8
)
h2_style = ParagraphStyle(
    'H2', fontName='MSYH-Bold', fontSize=14, leading=20,
    textColor=GREEN_PRIMARY, spaceBefore=10, spaceAfter=6
)
body_style = ParagraphStyle(
    'Body', fontName='MSYH', fontSize=11, leading=18,
    textColor=TEXT_PRIMARY, firstLineIndent=22, spaceAfter=4
)
bullet_style = ParagraphStyle(
    'Bullet', fontName='MSYH', fontSize=11, leading=18,
    textColor=TEXT_PRIMARY, leftIndent=18
)

# === Step 3: 页眉页脚回调 ===
def header_footer(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFont('MSYH', 9)
    canvas_obj.setFillColor(TEXT_SECOND)
    canvas_obj.drawRightString(A4[0] - 2*cm, A4[1] - 1*cm, "日报 2026-07-18")
    canvas_obj.setStrokeColor(GREEN_PRIMARY)
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(2*cm, A4[1] - 1.2*cm, A4[0] - 2*cm, A4[1] - 1.2*cm)
    page_num = canvas_obj.getPageNumber()
    canvas_obj.drawCentredString(A4[0]/2, 1*cm, f"第 {page_num} 页")
    canvas_obj.drawString(2*cm, 1*cm,
        datetime.date.today().strftime('%Y-%m-%d'))
    canvas_obj.restoreState()

# === Step 4: 解析 markdown 并构建 Story ===
def md_to_story(md_text):
    story = []
    in_code_block = False
    code_buffer = []

    for line in md_text.split('\n'):
        line = line.rstrip()
        if line.startswith('```'):
            if in_code_block:
                code_text = '<br/>'.join(escape(l) for l in code_buffer)
                story.append(Paragraph(
                    f"<font name='MSYH' size='9' color='#616161'>{code_text}</font>",
                    body_style
                ))
                code_buffer = []
                in_code_block = False
            else:
                in_code_block = True
            continue
        if in_code_block:
            code_buffer.append(line)
            continue

        if not line:
            story.append(Spacer(1, 0.3*cm))
        elif line.startswith('# '):
            story.append(Paragraph(escape(line[2:]), title_style))
        elif line.startswith('## '):
            story.append(Paragraph(escape(line[3:]), h1_style))
        elif line.startswith('### '):
            story.append(Paragraph(escape(line[4:]), h2_style))
        elif line.startswith('- ') or line.startswith('* '):
            story.append(ListFlowable(
                [ListItem(Paragraph(escape(line[2:]), bullet_style))],
                bulletType='bullet', bulletColor=GREEN_PRIMARY,
                leftIndent=18, bulletFontSize=11
            ))
        else:
            story.append(Paragraph(escape(line), body_style))
    return story

# === Step 5: Build PDF ===
def md_to_pdf(md_text, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2.5*cm, bottomMargin=2*cm,
        title="日报", author="LocalAgent"
    )
    story = md_to_story(md_text)
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    print(f"✓ 已生成: {output_path}")
    return output_path

# === Step 6: 验证 ===
def validate_pdf(path, expected_keywords=None, min_pages=1):
    import pdfplumber
    assert os.path.exists(path), f"文件不存在: {path}"
    size = os.path.getsize(path)
    assert size > 0, f"文件为空: {path}"
    print(f"✓ 文件大小: {size} bytes ({size/1024:.1f} KB)")

    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        assert page_count >= min_pages, f"页数 {page_count} < 期望 {min_pages}"
        print(f"✓ 页数: {page_count}")

        full_text = ""
        for page in pdf.pages:
            full_text += page.extract_text() or ""

        if expected_keywords:
            for kw in expected_keywords:
                ok = kw in full_text
                print(f"{'✓' if ok else '✗'} 关键字 '{kw}': {'包含' if ok else '缺失'}")
                if not ok:
                    return False
    return True

# === 用法 ===
md = """# 日报 2026-07-18

## 今日完成

- 编写 office_pdf skill
- 修复中文字体渲染问题
- 部署远程 VL 服务

## 明日计划

- 完善 daily_summary 自动化
- 测试新字体在不同系统的兼容性

## 备注

今天遇到 reportlab 中文字体注册的坑，<b> 标签不生效是因为没注册字体族。
"""

output = "workspace/office_pdf/日报_20260718.pdf"
md_to_pdf(md, output)
validate_pdf(output, expected_keywords=["日报", "今日完成", "明日计划"], min_pages=1)
```

## 16. 完整示例 — 合并多个 PDF

输入：`[part1.pdf, part2.pdf, part3.pdf]`
输出：`workspace/office_pdf/合集_20260718.pdf`

```python
"""完整示例：合并多个 PDF"""
import os
from pypdf import PdfReader, PdfWriter

def merge_pdfs(input_paths, output_path):
    """合并多个 PDF 到一个文件"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    writer = PdfWriter()
    for pdf_path in input_paths:
        assert os.path.exists(pdf_path), f"文件不存在: {pdf_path}"
        reader = PdfReader(pdf_path)
        print(f"  + {pdf_path}: {len(reader.pages)} 页")
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    # 验证
    result_reader = PdfReader(output_path)
    total_pages = len(result_reader.pages)
    file_size = os.path.getsize(output_path)
    print(f"✓ 合并完成: {output_path}")
    print(f"  总页数: {total_pages}")
    print(f"  文件大小: {file_size/1024:.1f} KB")
    return output_path

# 用法
inputs = [
    "workspace/office_pdf/第一部分.pdf",
    "workspace/office_pdf/第二部分.pdf",
    "workspace/office_pdf/第三部分.pdf",
]
output = "workspace/office_pdf/合集_20260718.pdf"
merge_pdfs(inputs, output)
```

## 17. 完整示例 — PDF 加水印

输入：`workspace/office_pdf/原文.pdf`
输出：`workspace/office_pdf/原文_加水印.pdf`

```python
"""完整示例：PDF 加水印"""
import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from pypdf import PdfReader, PdfWriter

def create_watermark_pdf(watermark_path, text="CONFIDENTIAL",
                         font_size=40, alpha=0.2, angle=45):
    """用 reportlab Canvas 创建单页水印 PDF"""
    c = canvas.Canvas(watermark_path, pagesize=A4)
    c.saveState()
    c.setFont('Helvetica', font_size)
    c.setFillColor(colors.HexColor('#2E7D32'))  # 绿色水印
    c.setFillAlpha(alpha)
    c.translate(A4[0]/2, A4[1]/2)
    c.rotate(angle)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.showPage()
    c.save()
    print(f"✓ 水印 PDF 已生成: {watermark_path}")

def add_watermark(input_path, watermark_path, output_path, text="CONFIDENTIAL"):
    """给 input_path 的每页加水印，输出到 output_path"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 1. 创建水印 PDF（如果不存在）
    if not os.path.exists(watermark_path):
        create_watermark_pdf(watermark_path, text=text)

    # 2. 把水印叠加到每页
    reader = PdfReader(input_path)
    watermark = PdfReader(watermark_path)
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        page.merge_page(watermark.pages[0])  # 叠加水印
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    # 3. 验证
    result = PdfReader(output_path)
    print(f"✓ 加水印完成: {output_path}")
    print(f"  总页数: {len(result.pages)}")
    print(f"  文件大小: {os.path.getsize(output_path)/1024:.1f} KB")
    return output_path

# 用法
input_pdf = "workspace/office_pdf/原文.pdf"
watermark_pdf = "temp/watermark_confidential.pdf"
output_pdf = "workspace/office_pdf/原文_加水印.pdf"
add_watermark(input_pdf, watermark_pdf, output_pdf, text="机密 INTERNAL")
```

## 18. 完整示例 — 提取 PDF 表格

输入：`workspace/office_pdf/账单.pdf`
输出：`workspace/office_pdf/账单表格.csv`（或 markdown 表格）

```python
"""完整示例：提取 PDF 表格并导出为 CSV"""
import os
import csv
import pdfplumber

def extract_tables_to_csv(pdf_path, output_csv_path):
    """提取 PDF 每页的表格，导出为 CSV"""
    os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)

    all_rows = []
    with pdfplumber.open(pdf_path) as pdf:
        print(f"总页数: {len(pdf.pages)}")
        for page_idx, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            print(f"  第 {page_idx+1} 页: 提取到 {len(tables)} 个表格")
            for table_idx, table in enumerate(tables):
                print(f"    表格 {table_idx+1}: {len(table)} 行")
                for row in table:
                    # 清理单元格：None 替换为空字符串
                    cleaned = [(cell or '').replace('\n', ' ').strip() for cell in row]
                    all_rows.append(cleaned)

    # 写入 CSV
    with open(output_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(all_rows)

    print(f"✓ 已导出 CSV: {output_csv_path}")
    print(f"  总行数: {len(all_rows)}")
    return output_csv_path

def extract_tables_to_markdown(pdf_path, output_md_path):
    """提取 PDF 表格，导出为 Markdown 表格"""
    os.makedirs(os.path.dirname(output_md_path), exist_ok=True)

    md_lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                # 表头
                header = table[0]
                md_lines.append('| ' + ' | '.join((c or '') for c in header) + ' |')
                md_lines.append('|' + '|'.join(['---'] * len(header)) + '|')
                # 数据行
                for row in table[1:]:
                    md_lines.append('| ' + ' | '.join((c or '').replace('\n', ' ') for c in row) + ' |')
                md_lines.append('')  # 空行分隔

    with open(output_md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))

    print(f"✓ 已导出 Markdown: {output_md_path}")
    return output_md_path

# 用法
pdf_path = "workspace/office_pdf/账单.pdf"
extract_tables_to_csv(pdf_path, "workspace/office_pdf/账单表格.csv")
extract_tables_to_markdown(pdf_path, "workspace/office_pdf/账单表格.md")
```

## 19. Validation Checklist

生成 PDF 后**必须验证**，不要"生成就完事"：

```python
import os
import pdfplumber
from pypdf import PdfReader

def validate_pdf(path, expected_keywords=None, min_pages=1):
    """验证 PDF 文件"""
    # 1. 文件存在
    assert os.path.exists(path), f"文件不存在: {path}"

    # 2. 文件大小 > 0
    size = os.path.getsize(path)
    assert size > 0, f"文件为空: {path}"
    print(f"✓ 文件大小: {size} bytes ({size/1024:.1f} KB)")

    # 3. 用 pypdf.PdfReader 重开不报错（schema 验证）
    reader = PdfReader(path)
    page_count = len(reader.pages)
    assert page_count >= min_pages, f"页数 {page_count} < 期望 {min_pages}"
    print(f"✓ 页数: {page_count}")
    print(f"✓ 元数据: {reader.metadata}")

    # 4. 用 pdfplumber 提取文本检查中文渲染
    with pdfplumber.open(path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += page.extract_text() or ""

        # 5. 关键字包含检查
        if expected_keywords:
            for kw in expected_keywords:
                ok = kw in full_text
                print(f"{'✓' if ok else '✗'} 关键字 '{kw}': {'包含' if ok else '缺失'}")
                if not ok:
                    return False

        # 6. 中文渲染检查（首页文本应含可识别中文，不是黑方块占位）
        if page_count > 0:
            first_page_text = pdf.pages[0].extract_text() or ""
            print(f"✓ 首页文本片段: {first_page_text[:80]}")

    return True

# 用法
validate_pdf(
    "workspace/office_pdf/报告_20260718.pdf",
    expected_keywords=["周报", "任务", "下周计划"],
    min_pages=2
)
```

### 验证清单（人工对照）

- [ ] `pypdf.PdfReader(path)` 重开不报错
- [ ] 页数合理（不少于 1 页）
- [ ] 抽查文本内容（用 `page.extract_text()`）
- [ ] 检查中文字体是否生效（提取文本看是否乱码 / 黑方块）
- [ ] 文件大小 > 0
- [ ] 无 placeholder token（`{{var}}` `<TODO>` `xxxx` `lorem`）泄漏
- [ ] 无 Unicode 上下标字符（`₀₁₂` `⁰¹²`）
- [ ] 无长表格跨页断裂（用阅读器翻页检查表头是否重复）
- [ ] 无图片溢出页面边界

## 20. Common Pitfalls 表

| Pitfall | 症状 | Correct approach |
|---|---|---|
| 默认中文字体不支持中文 | PDF 中中文显示为黑方块/空白 | 注册 TTFont + registerFontFamily |
| Unicode 上下标 | `₀₁₂` `⁰¹²` 渲染为黑方块 | 用 `<sub>`/`<super>` 标签 |
| 长表格跨页断裂 | 表格行被切断，第 2 页无表头 | `splitByRow=True` + `repeatRows=1` |
| 图片溢出 | 图片宽度超过页面，被截断 | `maxWidth` / `maxHeight` 缩放 或 `kind='proportional'` |
| Paragraph XML 转义 | `<` `>` `&` 渲染异常或报错 | `&lt;` `&gt;` `&amp;`（用 `xml.sax.saxutils.escape`）|
| Spacer 单位错误 | `Spacer(1, 10)` 看不到间距 | 用 `inch` 或 `cm`，不要裸数字 |
| `<b>` 标签不生效 | 加粗标签渲染为正体 | `registerFontFamily` 注册 bold 字体 |
| Header/footer 样式泄漏 | 页眉字体颜色影响正文 | `canvas.saveState()` + `canvas.restoreState()` 配对 |
| 字体路径错误 | 注册失败 `FileNotFoundError` | Windows `C:/Windows/Fonts/`，macOS `/System/Library/Fonts/`，Linux `/usr/share/fonts/` |
| pdfplumber 表格提取不准 | 复杂布局表格行列错乱 | 调整 `table_settings`（`vertical_strategy`/`horizontal_strategy`/`snap_tolerance`）|
| .ttc 字体只读第一个 | `simsun.ttc` 只能用 SimSun | 用 `subfontIndex` 指定（SimSun=0, NSimSun=1）|
| 图片不指定尺寸 | 用原始像素尺寸严重溢出 | 始终显式 `width`/`height` 或 `scale_image()` |
| `PdfMerger` 弃用警告 | pypdf 4.x 报 DeprecationWarning | 改用 `PdfWriter` + `add_page` |
| 加密后无法读取 | `PdfReader` 报 `PdfReadError` | 先 `reader.decrypt(password)` 再访问 pages |

## 设计规范（anti-AI-slop）

### 主色（绿色，不用紫色）

用户偏好绿色主色。推荐 Material Design 绿色色板：

```python
from reportlab.lib import colors

GREEN_PRIMARY = colors.HexColor('#2E7D32')   # Green 800 - 主色（标题/强调）
GREEN_DARK    = colors.HexColor('#1B5E20')   # Green 900 - 深色（背景文字）
GREEN_LIGHT   = colors.HexColor('#E8F5E9')   # Green 50  - 浅色（表头背景替代）
GREEN_PALE    = colors.HexColor('#F1F8E9')   # Green 50 浅 - 隔行变色
GREEN_ACCENT  = colors.HexColor('#4CAF50')   # Green 500 - 中色（链接/图标）
TEXT_PRIMARY  = colors.HexColor('#212121')   # Grey 900  - 正文
TEXT_SECOND   = colors.HexColor('#616161')   # Grey 700  - 次要文字
DIVIDER       = colors.HexColor('#BDBDBD')   # Grey 400  - 边框
```

### 避免过度居中

```python
# ❌ 反模式：所有内容居中
style = ParagraphStyle('Bad', alignment=TA_CENTER)
story.append(Paragraph("标题", style))
story.append(Paragraph("正文", style))  # 正文居中难读

# ✓ 推荐：标题居中或左对齐，正文左对齐
title_style = ParagraphStyle('Title', alignment=TA_LEFT, ...)
body_style = ParagraphStyle('Body', alignment=TA_LEFT, ...)
# 仅封面页或表格表头用居中
```

### 表格表头绿色背景

```python
table_style = TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2E7D32')),  # 绿色表头
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),                # 白字
    ('ROWBACKGROUNDS', (0, 1), (-1, -1),
     [colors.white, colors.HexColor('#F1F8E9')]),                  # 隔行绿白
])
```

### 页边距与字号规范

```python
# A4 页面（210mm × 297mm），常规边距
doc = SimpleDocTemplate(
    path, pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,    # 左右边距 2cm，可用宽度 17cm
    topMargin=2.5*cm, bottomMargin=2*cm,  # 上边距留出页眉空间
)

# 字号规范
TITLE_SIZE    = 22  # 封面大标题
H1_SIZE       = 18  # 一级标题
H2_SIZE       = 14  # 二级标题
H3_SIZE       = 12  # 三级标题
BODY_SIZE     = 11  # 正文
CAPTION_SIZE  = 9   # 注释/页眉页脚

# 行距 = 字号 × 1.5（中文阅读舒适）
body_leading = BODY_SIZE * 1.5
```

## 与现有 skill 集成

### 1. 精选文章归档（web_archive）

将网页存档的 markdown 合集导出为单 PDF：

- 输入：`workspace/web_archive/某主题/合集.md`
- 输出：`workspace/office_pdf/某主题合集_20260718.pdf`
- 特点：文章之间用 PageBreak 分隔；文章标题用绿色 Heading2；页眉显示合集名 + 日期；页脚显示页码 + 来源 URL

详见 [web_archive skill](file:///f:/<project_root>/.agents/skills/web_archive/SKILL.md)。

### 2. 日总结 PDF（daily_summary）

将日总结导出为 PDF 归档（适合打印或邮件附件）：

- 输入：`data/activity/hourly/汇总后的日报 md`
- 输出：`workspace/office_pdf/日报_20260718.pdf`
- 特点：标题"YYYY-MM-DD 工作日报"；按时段分章节（上午/下午/晚上）；任务表格汇总（绿色表头）；页脚加日期和页码

详见 [daily_summary skill](file:///f:/<project_root>/.agents/skills/daily_summary.md)。

### 3. 面经报告（niuke_review）

将面经评分结果导出为 PDF 报告：

- 输入：`workspace/niuke_review/面经评分.json`
- 输出：`workspace/office_pdf/面经报告_20260718.pdf`
- 特点：封面页（标题/日期/作者）；评分总览表格（绿色表头 + 隔行变色）；按题目分页，每题含答案和评分；末尾改进建议；全程使用中文字体（MSYH）

详见 [niuke_review skill](file:///f:/<project_root>/.agents/skills/niuke_review.md)。

### 4. 研究报告（deep_research）

将研究报告导出为 PDF：

- 输入：deep_research 输出的 markdown 报告
- 输出：`workspace/office_pdf/研究报告_20260718.pdf`
- 参考样式：[deep_research/references/pdf_report_style.md](file:///f:/<project_root>/.agents/skills/deep_research/references/pdf_report_style.md)
- 特点：封面 + 目录 + 多章节 + 图表；正式报告风格；替换原 office_export 的 PDF 调用

## 参考

- [reportlab 官方文档](https://docs.reportlab.com/)
- [pypdf 官方文档](https://pypdf.readthedocs.io/)
- [pdfplumber 官方文档](https://github.com/jsvine/pdfplumber)
- [office_pdf SKILL.md](file:///f:/<project_root>/.agents/skills/office_pdf/SKILL.md)
