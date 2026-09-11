# 设计原则参考 (design_principles.md)

本文件是 [office_pptx skill](file:///<project_root>/.agents/skills/office_pptx/SKILL.md) 的子参考，专注 anti-AI-slop 设计原则、Visual Floor、12-column grid、字体配对、调色板。

## 目录

1. [Visual Floor 完整规范](#1-visual-floor-完整规范)
2. [12-Column Grid 规范](#2-12-column-grid-规范)
3. [Safe Zone / Visual Floor](#3-safe-zone--visual-floor)
4. [字体配对表 8 种](#4-字体配对表-8-种)
5. [调色板 10 种](#5-调色板-10-种)
6. [图表选择决策表](#6-图表选择决策表)
7. [6 设计原则完整说明](#7-6-设计原则完整说明)
8. [Copy reads human, not AI](#8-copy-reads-human-not-ai)

---

## 1. Visual Floor 完整规范

### 字号下限表

| Element | Minimum | Typical | Min shape height |
|---|---|---|---|
| Slide title | **≥ 36pt** bold | 36–44pt | ≥ 2cm |
| Section / subtitle | ≥ 20pt | 20–24pt | ≥ 1.2cm |
| Body text | **≥ 18pt** | 18–22pt | ≥ 1cm |
| Caption / axis label | ≥ 10pt muted | 10–12pt | ≥ 0.6cm |

### Rule of thumb

`min shape height ≈ font_pt × 0.05cm`

例：18pt 字在 0.8cm 高的 box 会溢出（应 ≥ 0.9cm）；36pt 字需要 ≥ 1.8cm 的 box。

### 硬规则

1. **Title 必须 ≥ 2× body size**（36pt title + 18pt body = OK；24pt title + 18pt body = 违规）
2. **Body ≥ 18pt**，4 个合法例外：
   - chart axis labels
   - legends
   - footer page number
   - ≤5-word KPI sublabels
3. **Left-align body**；center 仅用于 titles 和 hero numbers
4. **"Cards won't fit" → drop cards，不缩字号**

### 字号 wrap rule（pitch-deck 4-stat row）

60pt + 7cm 宽度下：

- `$9.4M` 会换行（6 chars）
- 安全：`$9M` / `$96B` / `$4K` / `340%` / `4.2x` / `12.3`（≤5 chars）
- ≥6 chars 必换行

```python
# 用 PIL 模拟 PowerPoint wrap 行为
from PIL import ImageFont

def estimate_wrap(text, font_size_pt, box_width_inches):
    """粗略估算文本是否会换行"""
    # PowerPoint 默认 DPI = 96
    font_size_px = font_size_pt * 96 / 72
    box_width_px = box_width_inches * 96
    # 估算每个字符宽度 ≈ 0.6 × font_size（粗略）
    char_width = font_size_px * 0.6
    max_chars_per_line = int(box_width_px / char_width)
    return len(text) > max_chars_per_line

# 用法
if estimate_wrap('$9.4M', 60, 7/2.54):
    print("会换行，改用 $9M 或加宽 box")
```

### python-pptx 设置字号

```python
from pptx.util import Pt, Cm
from pptx.dml.color import RGBColor

# 标题
title_p = title_tf.paragraphs[0]
title_p.font.size = Pt(36)  # Visual Floor minimum
title_p.font.bold = True
title_p.font.color.rgb = PALETTE['primary']
title_p.font.name = 'Microsoft YaHei'

# 正文
for p in body_tf.paragraphs:
    p.font.size = Pt(18)  # Visual Floor minimum
    p.font.name = 'Microsoft YaHei'
    p.alignment = PP_ALIGN.LEFT  # 硬规则
```

---

## 2. 12-Column Grid 规范

### 标准 widescreen

**33.87 × 19.05cm**（13.333" × 7.5"）

```python
from pptx.util import Inches, Cm

# 标准 16:9
prs.slide_width = Cm(33.87)   # 或 Inches(13.333)
prs.slide_height = Cm(19.05)  # 或 Inches(7.5)
```

### 边距与间距

- **Edge margin** ≥ 1.27cm（0.5"）四边
- **Inter-block gap** ≥ 0.76cm（0.3"）
- ≥ 20% negative space per slide

### Card grid 公式

```python
def card_grid(n_cards, slide_width=33.87, margin=1.27, gap=0.76):
    """
    n_cards: 卡片数
    返回：每张卡的 width 和 left 坐标列表（cm）
    """
    usable = slide_width - 2 * margin - (n_cards - 1) * gap
    col_width = usable / n_cards
    lefts = [margin + i * (col_width + gap) for i in range(n_cards)]
    return col_width, lefts

# 常见 grid math 结果
for n in [3, 4, 5]:
    w, lefts = card_grid(n)
    print(f"{n} cards: width={w:.2f}cm, lefts={[f'{l:.2f}' for l in lefts]}")
```

输出：

```
3 cards: width=10.21cm, lefts=['1.27', '12.24', '23.21']
4 cards: width=7.51cm,  lefts=['1.27', '9.54', '17.81', '26.08']
5 cards: width=5.89cm,  lefts=['1.27', '7.92', '14.57', '21.22', '27.87']
```

**不要手挑 x 坐标**——用 grid 公式算。

### Vertical grid

```python
def vertical_zones(slide_height=19.05, margin=1.27, gap=0.76):
    """
    返回 3 个垂直区域（header / body / footer）的 top + height
    """
    usable = slide_height - 2 * margin
    header_h = 2.5  # 标题区
    footer_h = 1.0  # 页脚区
    body_h = usable - header_h - footer_h - 2 * gap
    return {
        'header': (margin, header_h),
        'body':   (margin + header_h + gap, body_h),
        'footer': (margin + header_h + gap + body_h + gap, footer_h),
    }
```

---

## 3. Safe Zone / Visual Floor

### 必过项

- **No placeholder tokens rendered**：扫文本不含 `{{name}}` `$fy$24` `<TODO>` `lorem` `xxxx` `()` `[]`
- **No overflow off-edge / no clipped text**：shape 不超 slide 边界
- **Cover carries orienting elements**：封面有公司名 / tagline / round / date
- **Contrast**：fill brightness <30% 时所有 body run / card body / chart series / icon 必须 `FFFFFF` 或 brightness >80%

### Brightness 计算公式

```python
def brightness(rgb):
    """RGBColor → 0-100 brightness（W3C 公式）"""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return (r * 299 + g * 587 + b * 114) / 1000 / 255 * 100

# 用法
from pptx.dml.color import RGBColor
dark_fill = RGBColor(0x1A, 0x3D, 0x1B)  # brightness ≈ 22%
text_color = RGBColor(0xFF, 0xFF, 0xFF)  # brightness = 100%
assert brightness(dark_fill) < 30 and brightness(text_color) > 80  # OK
```

### 验证函数

```python
def check_contrast(slide):
    """检查 slide 中所有 shape 的 contrast"""
    issues = []
    for shape in slide.shapes:
        if not hasattr(shape, 'fill'):
            continue
        if shape.fill.type is None:
            continue
        try:
            fill_rgb = shape.fill.fore_color.rgb
        except:
            continue
        if brightness(fill_rgb) >= 30:
            continue  # 浅背景，text 可深可浅
        # 深背景：text 必须 brightness > 80
        if not shape.has_text_frame:
            continue
        for p in shape.text_frame.paragraphs:
            for run in p.runs:
                if run.font.color and run.font.color.rgb:
                    if brightness(run.font.color.rgb) <= 80:
                        issues.append(
                            f"Dark-on-dark: shape {shape.name}, "
                            f"fill={fill_rgb}, text={run.font.color.rgb}"
                        )
    return issues
```

---

## 4. 字体配对表 8 种

| Header | Body | Best For |
|---|---|---|
| Georgia | Calibri | Formal business, finance, executive reports |
| Arial Black | Arial | Bold marketing, product launches |
| Calibri | Calibri Light | Clean corporate, minimal design |
| Cambria | Calibri | Traditional professional, legal, academic |
| Trebuchet MS | Calibri | Friendly tech, startups, SaaS |
| Impact | Arial | Bold headlines, event decks, keynotes |
| Palatino | Garamond | Elegant editorial, luxury, nonprofit |
| Consolas | Calibri | Developer tools, technical / engineering |

### 中文字体

本项目所有示例显式设置 `Microsoft YaHei`：

```python
def set_font(run, header_font='Microsoft YaHei', body_font='Microsoft YaHei'):
    """统一设置字体（含 eastAsia）"""
    run.font.name = body_font
    set_font_eastasia(run, body_font)  # 见 basic.md Element 3
```

### 规则

- **最多 2 个字体**（heading + body）；每个 shape 显式设两个字体，不靠 theme inheritance
- 中文场景统一用 `Microsoft YaHei`（heading + body），不混西文衬线 + 中文无衬线
- 等宽场景用 `Consolas`（西文）+ `Microsoft YaHei`（中文）

### 反模式

- 一个 slide 用 3+ 字体
- 不显式设 font.name（依赖 theme，跨设备不可控）
- 中文字符用 `Calibri`（不支持，会回退到系统默认）

---

## 5. 调色板 10 种

### 完整 10 palette 表

| Palette | Primary | Secondary | Accent | Text | Muted |
|---|---|---|---|---|---|
| **Coral Energy** | `E64A4A` | `F4A261` | `FFF4E6` | `2D2D2D` | `8B5A5A` |
| **Midnight Executive** | `1A2B4A` | `3D5A80` | `F0F4F8` | `1A1A1A` | `5A6A8A` |
| **Forest & Moss** ⭐ | `2C5F2D` | `97BC62` | `F5F5F5` | `2D2D2D` | `6B8E6B` |
| **Charcoal Minimal** | `2D2D2D` | `808080` | `F5F5F5` | `1A1A1A` | `999999` |
| **Warm Terracotta** | `C65D3B` | `E8A87C` | `FFF8F0` | `3D2817` | `8B6F4E` |
| **Berry & Cream** | `6B2D5C` | `C99FC4` | `F8F0F5` | `2D1A2D` | `8B6B82` |
| **Ocean Gradient** | `0A4D8C` | `4A90E2` | `E6F2FF` | `0D2D4D` | `5A7A9A` |
| **Teal Trust** | `2D7A7A` | `7AC4C4` | `E6F5F5` | `1A3D3D` | `5A8A8A` |
| **Sage Calm** | `7A8B5C` | `B5C49C` | `F0F5E6` | `2D3D1A` | `8B9A6B` |
| **Cherry Bold** | `B83227` | `E8746C` | `FFF0EE` | `2D0D0A` | `8B4D47` |

### python-pptx 调色板代码

```python
from pptx.dml.color import RGBColor

PALETTES = {
    'Coral Energy':         {'primary': 'E64A4A', 'secondary': 'F4A261', 'accent': 'FFF4E6', 'text': '2D2D2D', 'muted': '8B5A5A'},
    'Midnight Executive':   {'primary': '1A2B4A', 'secondary': '3D5A80', 'accent': 'F0F4F8', 'text': '1A1A1A', 'muted': '5A6A8A'},
    'Forest & Moss':        {'primary': '2C5F2D', 'secondary': '97BC62', 'accent': 'F5F5F5', 'text': '2D2D2D', 'muted': '6B8E6B'},
    'Charcoal Minimal':     {'primary': '2D2D2D', 'secondary': '808080', 'accent': 'F5F5F5', 'text': '1A1A1A', 'muted': '999999'},
    'Warm Terracotta':      {'primary': 'C65D3B', 'secondary': 'E8A87C', 'accent': 'FFF8F0', 'text': '3D2817', 'muted': '8B6F4E'},
    'Berry & Cream':        {'primary': '6B2D5C', 'secondary': 'C99FC4', 'accent': 'F8F0F5', 'text': '2D1A2D', 'muted': '8B6B82'},
    'Ocean Gradient':       {'primary': '0A4D8C', 'secondary': '4A90E2', 'accent': 'E6F2FF', 'text': '0D2D4D', 'muted': '5A7A9A'},
    'Teal Trust':           {'primary': '2D7A7A', 'secondary': '7AC4C4', 'accent': 'E6F5F5', 'text': '1A3D3D', 'muted': '5A8A8A'},
    'Sage Calm':            {'primary': '7A8B5C', 'secondary': 'B5C49C', 'accent': 'F0F5E6', 'text': '2D3D1A', 'muted': '8B9A6B'},
    'Cherry Bold':          {'primary': 'B83227', 'secondary': 'E8746C', 'accent': 'FFF0EE', 'text': '2D0D0A', 'muted': '8B4D47'},
}

def hex_to_rgb(hex_str):
    """hex string → RGBColor"""
    return RGBColor(int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))

def get_palette(name='Forest & Moss'):
    """返回 RGBColor palette dict"""
    p = PALETTES[name]
    return {k: hex_to_rgb(v) for k, v in p.items()}
```

### 规则

1. **One dominant brand color** (60–70% weight) + one supporting + one accent
2. **body 中绝不混 4+ 色**
3. **本项目主色用 Forest & Moss**（`2C5F2D` primary, `97BC62` secondary, `F5F5F5` accent, `2D2D2D` text, `6B8E6B` muted），不用紫色

### 应用比例

```python
# 一张 slide 的色彩比例：
# - 主色（绿色）60-70%：标题、表头、强调色块、chart 主系列
# - 中性色（白/灰）20-30%：背景、正文、分隔线
# - 辅色 1（深绿/浅绿）10%：次要强调、隔行变色
# - 辅色 2（暖色如橙色）<5%：警告/重点强调

# 应用示例
slide.background.fill.solid()
slide.background.fill.fore_color.rgb = PALETTE['white']           # 中性 30%

# 左侧 1/3 主色块（主色 30%）
block = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
    0, 0, Cm(11.29), Cm(19.05))  # 1/3 width
block.fill.solid()
block.fill.fore_color.rgb = PALETTE['primary']                    # 主色 30%
block.line.fill.background()

# 标题文字（主色）
title_tf.paragraphs[0].font.color.rgb = PALETTE['primary']        # 主色

# 正文（中性色 text）
body_tf.paragraphs[0].font.color.rgb = PALETTE['text']            # 中性 30%

# 强调数字（辅色 2 - 暖色）
hero_tf.paragraphs[0].font.color.rgb = RGBColor(0xFF, 0x70, 0x43) # 辅色 <5%
```

### 反模式

- 主色用紫色（`7C3AED` 类）= AI slop 标志
- 一个 slide 用 4+ 颜色填充不同 shape
- 不设主色，每个 shape 都用不同颜色

---

## 6. 图表选择决策表

| Data shape | Use | Avoid |
|---|---|---|
| Category comparison (A vs B vs C) | `column`（垂直）/ `bar`（≥6 类，水平） | pie、line |
| Time series, 1–3 series | `line` | area、bar |
| Part-of-whole, 2–5 slices | `pie` / `doughnut` | pie with 8+ slices |
| Correlation / distribution | `scatter` | line |
| Multiple categories × metrics, dense | stacked `column` or heatmap | one chart per metric |
| KPI snapshot (single big number) | **Large-text shape**（60–72pt），NOT a chart | gauge chart、tiny bar |

### Rule of thumb

if >3 series AND >8 categories → 拆成两个图表或换 table

### python-pptx 图表代码

```python
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

# 1. Category comparison
chart_data = CategoryChartData()
chart_data.categories = ['Product A', 'Product B', 'Product C']
chart_data.add_series('Revenue ($M)', (3.2, 5.1, 2.8))
slide.shapes.add_chart(
    XL_CHART_TYPE.COLUMN_CLUSTERED,
    Inches(0.8), Inches(2), Inches(7), Inches(5),
    chart_data
)

# 2. Time series (line, y-axis from 0)
chart_data = CategoryChartData()
chart_data.categories = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun']
chart_data.add_series('ARR', (1.2, 1.8, 2.5, 3.4, 4.8, 6.1))
chart_shape = slide.shapes.add_chart(
    XL_CHART_TYPE.LINE_MARKERS,
    Inches(0.8), Inches(2), Inches(11), Inches(5),
    chart_data
)
chart = chart_shape.chart
chart.value_axis.minimum_scale = 0  # ⚠️ load-bearing
chart.value_axis.has_major_gridlines = True

# 3. Part-of-whole (pie)
chart_data = CategoryChartData()
chart_data.categories = ['Eng', 'GTM', 'G&A', 'Reserve']
chart_data.add_series('Use of Funds', (40, 35, 10, 15))
slide.shapes.add_chart(
    XL_CHART_TYPE.PIE,
    Inches(0.8), Inches(2), Inches(7), Inches(5),
    chart_data
)

# 4. KPI snapshot — 用 shape，不用 chart
kpi_box = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE,
    Inches(0.8), Inches(2), Inches(4), Inches(3)
)
kpi_box.name = 'KpiHero'
kpi_box.fill.solid()
kpi_box.fill.fore_color.rgb = PALETTE['primary']
kpi_box.line.fill.background()
tf = kpi_box.text_frame
tf.text = '$9M'
tf.paragraphs[0].font.size = Pt(72)
tf.paragraphs[0].font.bold = True
tf.paragraphs[0].font.color.rgb = PALETTE['white']
tf.paragraphs[0].font.name = 'Microsoft YaHei'
```

---

## 7. 6 设计原则完整说明

### G.1 Every slide carries a non-text visual

**含义**：每张 slide 至少一个 Shape / chart / icon / gradient band，**carries meaning, not decoration**。

```python
# ❌ 反模式：bullet-only slide
slide.shapes.add_textbox(...).text_frame.text = "• Point 1\n• Point 2"

# ✓ 推荐：bullet + 视觉元素
slide.shapes.add_textbox(...)  # bullets
# 加一个 icon-in-circle
icon = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(1), Inches(2), Inches(1), Inches(1))
icon.fill.solid()
icon.fill.fore_color.rgb = PALETTE['primary']
icon.text_frame.text = '1'
icon.text_frame.paragraphs[0].font.size = Pt(24)
icon.text_frame.paragraphs[0].font.color.rgb = PALETTE['white']
icon.text_frame.paragraphs[0].font.bold = True
```

### G.2 Dominance over equality（60-70% + 10% + 10% + <5%）

One dominant brand color + one supporting + one accent；body 中**绝不混 4+ 色**。

```python
# ✓ 推荐：1 主色（绿）+ 1 辅色（浅绿）+ 1 强调（橙）
colors_used = [
    PALETTE['primary'],    # 60-70% 主色
    PALETTE['secondary'],  # 10% 辅色
    PALETTE['white'],      # 20-30% 中性
    RGBColor(0xFF, 0x70, 0x43),  # <5% 强调（橙色）
]
assert len(set(colors_used)) <= 4  # 含中性白
```

### G.3 Compose, don't web-center（避免过度居中）

Whitespace is structural；Intentional asymmetry 比 centering everything 读起来更 designed。

```python
# ❌ 反模式：所有元素居中
slide.shapes.add_textbox(Inches(4), Inches(3), Inches(5), Inches(2))
# alignment = PP_ALIGN.CENTER

# ✓ 推荐：1/3 - 2/3 不对称布局
left_block = slide.shapes.add_shape(
    MSO_SHAPE.RECTANGLE,
    0, 0, Inches(4.4), Inches(7.5)  # 左 1/3 色块
)
right_content = slide.shapes.add_textbox(
    Inches(5), Inches(1), Inches(8), Inches(5)  # 右 2/3 内容
)
right_content.text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
```

### G.4 Vary layout across slides

5 layout patterns building blocks；**永不连续两 slide 用同一 pattern**。

5 个 layout patterns：

1. **Title/Cover** — 暗渐变 + 居中标题 + 底部 meta + brand band
2. **Stat callout row** — 3-4 大数字卡
3. **Chart + Context** — chart 左 55% + callouts 右
4. **Icon-in-circle grid** — 3 行 circle + title + description
5. **Feature grid 2×2** — 4 卡 roundRect

```python
# 检查连续两 slide 是否用同一 pattern
def check_pattern_variation(slides):
    """检测连续 slide 是否用同一 layout pattern"""
    issues = []
    for i in range(1, len(slides)):
        prev_pattern = detect_pattern(slides[i-1])
        curr_pattern = detect_pattern(slides[i])
        if prev_pattern == curr_pattern:
            issues.append(f"Slide {i} 和 {i+1} 都用 {prev_pattern}，违反 G.4")
    return issues
```

### G.5 No emoji as iconography

Use a shape or a real icon asset.

```python
# ❌ 反模式：emoji 当 icon
# tf.text = "🚀 项目启动 🎯 目标设定"

# ✓ 推荐：用 shape（circle + 数字）
def make_numbered_icon(slide, left, top, number, color=None):
    """画一个圆形 + 数字 icon"""
    circle = slide.shapes.add_shape(
        MSO_SHAPE.OVAL, left, top, Inches(1), Inches(1)
    )
    circle.name = f'Icon{number}'
    circle.fill.solid()
    circle.fill.fore_color.rgb = color or PALETTE['primary']
    circle.line.fill.background()
    tf = circle.text_frame
    tf.text = str(number)
    tf.paragraphs[0].font.size = Pt(28)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = PALETTE['white']
    tf.paragraphs[0].font.name = 'Microsoft YaHei'
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
    return circle
```

### G.6 Visual motif commitment

Pick ONE distinctive element and carry it to **every slide**。

```python
# 例：每张 slide 右上角都有一个绿色小圆点 + 公司首字母
def add_motif(slide, label='A'):
    """加 brand motif 到每张 slide"""
    motif = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Inches(12.5), Inches(0.2), Inches(0.5), Inches(0.5)
    )
    motif.name = 'BrandMotif'
    motif.fill.solid()
    motif.fill.fore_color.rgb = PALETTE['primary']
    motif.line.fill.background()
    tf = motif.text_frame
    tf.text = label
    tf.paragraphs[0].font.size = Pt(14)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = PALETTE['white']
    tf.paragraphs[0].font.name = 'Microsoft YaHei'
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
```

---

## 8. Copy reads human, not AI

### AI-slop 短语黑名单

| Avoid | Use |
|---|---|
| "leverage synergies" | "use what we have" |
| "drive impactful outcomes" | "ship revenue" |
| "holistic solution" | "full stack" |
| "paradigm shift" | "change in approach" |
| "disruptive innovation" | "10x cheaper / faster" |
| "robust scalability" | "scales to 10M users" |
| "seamless integration" | "3-line API" |
| "cutting-edge" | "newer than 2024" |
| "world-class" | "top 1% by metric" |
| "best-in-class" | "ranked #1 by X" |

### 检测函数

```python
import re

AI_SLOP_PATTERNS = [
    r'\bleverage\b', r'\bsynerg', r'\bholistic\b', r'\bparadigm\b',
    r'\bdisrupt', r'\brobust\b', r'\bseamless\b', r'\bcutting-edge\b',
    r'\bworld-class\b', r'\bbest-in-class\b', r'\bimpactful\b',
    r'\bgame-changer\b', r'\bthought leader', r'\brockstar\b',
]

def detect_ai_slop(text):
    """返回匹配的 AI-slop 短语"""
    found = []
    for pattern in AI_SLOP_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        found.extend(matches)
    return found

# 用法
for slide in prs.slides:
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        slop = detect_ai_slop(shape.text_frame.text)
        if slop:
            print(f"Slide {slide.slide_id}: AI slop detected: {slop}")
```

### 数字必须具体

- ❌ `"significant growth"` → ✓ `"340% MoM"`
- ❌ `"many customers"` → ✓ `"127 paying customers"`
- ❌ `"strong retention"` → ✓ `"NRR 118%, GRR 95%"`

---

## 参考

- [office_pptx SKILL.md](file:///<project_root>/.agents/skills/office_pptx/SKILL.md)
- [references/basic.md](file:///<project_root>/.agents/skills/office_pptx/references/basic.md)
- [references/pitch_deck.md](file:///<project_root>/.agents/skills/office_pptx/references/pitch_deck.md)
- [python-pptx 官方文档](https://python-pptx.readthedocs.io/)
