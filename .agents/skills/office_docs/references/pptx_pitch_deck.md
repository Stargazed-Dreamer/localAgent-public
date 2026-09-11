# 融资路演参考 (pitch_deck.md)

本文件是 [office_pptx skill](file:///<project_root>/.agents/skills/office_pptx/SKILL.md) 的子参考，专注融资路演（pitch deck）场景。

## 目录

1. [何时用此 skill](#1-何时用此-skill)
2. [何时回退到 basic.md](#2-何时回退到-basicmd)
3. [输出契约](#3-输出契约)
4. [10 必备 slides（固定顺序）](#4-10-必备-slides固定顺序)
5. [Stage diagnosis 矩阵](#5-stage-diagnosis-矩阵)
6. [5 赛道 arc 模板](#6-5-赛道-arc-模板)
7. [6 项 layout canon 模式](#7-6-项-layout-canon-模式)
8. [pitch-deck 扩展 Gate](#8-pitch-deck-扩展-gate)
9. [典型 recipe — Seed round pitch deck](#9-典型-recipe--seed-round-pitch-deck)

---

## 1. 何时用此 skill

**Trigger**（满足以下场景才用此 skill）：

- 用户明确提到 round（seed / Series A/B/C / SAFE / bridge）
- VC meeting 上下文（investor deck / fundraising）
- ≥4 项 {problem, traction, team with credentials, Use-of-Funds, stage-appropriate unit econ, financial projections}

**不满足以上条件**：路由到 [basic.md](file:///<project_root>/.agents/skills/office_pptx/references/basic.md) 或 [recipes.md](file:///<project_root>/.agents/skills/office_pptx/references/recipes.md)。

---

## 2. 何时回退到 basic.md

下列场景**不是** pitch deck，应该用 [basic.md](file:///<project_root>/.agents/skills/office_pptx/references/basic.md) 基础元素 + [recipes.md](file:///<project_root>/.agents/skills/office_pptx/references/recipes.md) 通用配方：

- **Board review**：内部董事会，无 round + ask
- **Sales deck**：销售演示，无 financial projection
- **Training deck**：培训材料，无 traction
- **Demo day**：产品演示日，无 investor 视角

判断依据：是否含 6 deltas（stage / narrative arc / 数字契约 / team credentials / y-axis from 0 / ask as slide）。无 → 不是 pitch deck。

---

## 3. 输出契约

**单个 .pptx**，遵循 pptx 全部硬规则 + pitch 6 deltas：

1. **stage 决定一切**：Seed / Series A/B/C / Bridge 的 slide 数和 narrative 完全不同
2. **narrative arc 优先**：每张 slide 必须服务于一个连贯故事
3. **数字是契约**：traction / financial / unit econ 数字必须可验证、有 footnote
4. **team 携带 prior companies**：每张 team card 必须有 prior-company 或 prior-achievement line
5. **traction y 轴从 0 起**：`value_axis.minimum_scale = 0`，load-bearing
6. **ask 是 slide 不是脚注**：单独 1 张 slide，hero 数字 + 4-bucket pie + runway

输出路径：`workspace/office_pptx/{公司名}_{round}_{YYYYMMDD}.pptx`

---

## 4. 10 必备 slides（固定顺序）

### Slide 1: Cover

公司 · tagline · round · $ · date

```python
def add_cover_slide(prs, company, tagline, round_name, ask_amount, date_str):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 暗渐变背景
    bg = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height
    )
    bg.name = 'CoverBg'
    bg.fill.gradient()
    bg.fill.gradient_stops[0].color.rgb = PALETTE['primary']
    bg.fill.gradient_stops[1].color.rgb = RGBColor(0x1A, 0x3D, 0x1B)
    bg.line.fill.background()

    # 居中标题
    title = slide.shapes.add_textbox(
        Inches(1), Inches(2.5), Inches(11.333), Inches(2)
    )
    title.name = 'CoverTitle'
    tf = title.text_frame
    tf.text = company
    p = tf.paragraphs[0]
    p.font.size = Pt(54)
    p.font.bold = True
    p.font.color.rgb = PALETTE['white']
    p.font.name = 'Microsoft YaHei'
    p.alignment = PP_ALIGN.CENTER

    # tagline
    tag_tb = slide.shapes.add_textbox(
        Inches(2), Inches(4.2), Inches(9.333), Inches(1)
    )
    tag_tb.name = 'CoverTagline'
    tag_tb.text_frame.text = tagline
    tag_tb.text_frame.paragraphs[0].font.size = Pt(22)
    tag_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['secondary']
    tag_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'
    tag_tb.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    # 底部 meta：round · $ · date
    meta_tb = slide.shapes.add_textbox(
        Inches(2), Inches(6), Inches(9.333), Inches(0.8)
    )
    meta_tb.name = 'CoverMeta'
    meta_tb.text_frame.text = f"{round_name} · {ask_amount} · {date_str}"
    meta_tb.text_frame.paragraphs[0].font.size = Pt(16)
    meta_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['accent']
    meta_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'
    meta_tb.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    # notes
    slide.notes_slide.notes_text_frame.text = f"""
    Cover slide.
    - Company: {company}
    - Round: {round_name}, raising {ask_amount}
    - Open with: "We are {company}, {tagline}. Today we're raising our {round_name}."
    """
    return slide
```

### Slide 2: Problem

1 句痛点 + 3 数据卡

```python
def add_problem_slide(prs, problem_statement, stats):
    """stats: list of (number, label) tuples, len=3"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 标题
    title_tb = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11), Inches(1))
    title_tb.name = 'ProblemTitle'
    title_tb.text_frame.text = "The Problem"
    title_tb.text_frame.paragraphs[0].font.size = Pt(40)
    title_tb.text_frame.paragraphs[0].font.bold = True
    title_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['primary']
    title_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'

    # 1 句痛点
    stmt_tb = slide.shapes.add_textbox(Inches(0.8), Inches(2), Inches(11), Inches(1.5))
    stmt_tb.name = 'ProblemStatement'
    stmt_tb.text_frame.text = problem_statement
    stmt_tb.text_frame.paragraphs[0].font.size = Pt(22)
    stmt_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['text']
    stmt_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'
    stmt_tb.text_frame.word_wrap = True

    # 3 数据卡（grid）
    card_w, lefts = card_grid(3, slide_width=13.333, margin=0.8, gap=0.4)
    for i, (number, label) in enumerate(stats):
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(lefts[i]), Inches(4), Inches(card_w), Inches(2.5)
        )
        card.name = f'StatCard{i+1}'
        card.fill.solid()
        card.fill.fore_color.rgb = PALETTE['accent']
        card.line.fill.background()

        tf = card.text_frame
        tf.text = number
        p1 = tf.paragraphs[0]
        p1.font.size = Pt(48)
        p1.font.bold = True
        p1.font.color.rgb = PALETTE['primary']
        p1.font.name = 'Microsoft YaHei'
        p1.alignment = PP_ALIGN.CENTER

        p2 = tf.add_paragraph()
        p2.text = label
        p2.font.size = Pt(14)
        p2.font.color.rgb = PALETTE['muted']
        p2.font.name = 'Microsoft YaHei'
        p2.alignment = PP_ALIGN.CENTER

    slide.notes_slide.notes_text_frame.text = "Problem slide. Lead with the pain stat..."
    return slide
```

### Slide 3: Solution

1 句产品模式 + 3 步 "how it works"

```python
def add_solution_slide(prs, solution_stmt, steps):
    """steps: list of (title, desc) tuples, len=3"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 标题
    # ...（同 Problem 标题样式）
    # 1 句产品模式（顶部）
    # 3 步流程（横向 3 卡 + 箭头连接）
    return slide
```

### Slide 4: Market

TAM/SAM/SOM nested columns + 方法论脚注

```python
def add_market_slide(prs, tam, sam, som, methodology):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 3 nested column（TAM 外、SAM 中、SOM 内）
    # 每列用 ROUNDED_RECTANGLE，宽度递减
    # 底部小字 methodology footnote
    return slide
```

### Slide 5: Product

截图 + 3 bullets 或 3 卡 feature grid

### Slide 6: Business Model

按赛道选：
- **SaaS**: CAC/LTV/Payback/GM
- **Consumer**: AOV/repeat/contribution
- **Marketplace**: GMV/take-rate/liquidity
- **Bio**: license/milestone/royalty

### Slide 7: Traction

ARR 曲线 `axismin=0` + 右侧 callout

```python
def add_traction_slide(prs, months, arr_values, callouts):
    """ARR 折线图，y 轴必须从 0"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 标题
    # chart 占左 55%
    chart_data = CategoryChartData()
    chart_data.categories = months
    chart_data.add_series('ARR ($M)', arr_values)
    chart_shape = slide.shapes.add_chart(
        XL_CHART_TYPE.LINE_MARKERS,
        Inches(0.8), Inches(2), Inches(6.5), Inches(5),
        chart_data
    )
    chart = chart_shape.chart
    chart.value_axis.minimum_scale = 0  # ⚠️ load-bearing
    chart.has_legend = False

    # 右侧 callouts
    # ...
    return slide
```

### Slide 8: Team

avatars + names + **prior companies + 一行成就**

```python
def add_team_slide(prs, members):
    """members: list of {name, role, prior, achievement, avatar_path}"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 4 卡 grid（每卡：avatar + name + role + prior company + achievement line）
    # ⚠️ 反模式：仅 {headshot + name + role} = VC credibility fail
    return slide
```

### Slide 9: Financials

4 年 plan + assumptions panel（**load-bearing**）

```python
def add_financials_slide(prs, years, revenue, ebitda, assumptions):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # 左：4 年 stacked bar (revenue + ebitda)
    # 右：assumptions panel（CAC, LTV, GM, growth rate）
    # 底部 footnote：数据来源 / 假设依据
    return slide
```

### Slide 10: Ask

`$XX M` hero + 4-bucket pie (Eng/GTM/G&A/Reserve) + runway

```python
def add_ask_slide(prs, ask_amount, runway_months, buckets):
    """buckets: dict {Eng: 40, GTM: 35, G&A: 10, Reserve: 15}"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 左：$XX M hero number
    hero_tb = slide.shapes.add_textbox(Inches(0.8), Inches(2), Inches(5), Inches(3))
    hero_tb.name = 'AskHero'
    tf = hero_tb.text_frame
    tf.text = ask_amount
    p = tf.paragraphs[0]
    p.font.size = Pt(96)
    p.font.bold = True
    p.font.color.rgb = PALETTE['primary']
    p.font.name = 'Microsoft YaHei'
    p.alignment = PP_ALIGN.LEFT

    # 右：4-bucket pie
    chart_data = CategoryChartData()
    chart_data.categories = list(buckets.keys())
    chart_data.add_series('Use of Funds', list(buckets.values()))
    slide.shapes.add_chart(
        XL_CHART_TYPE.PIE,
        Inches(6.5), Inches(1.5), Inches(6), Inches(5),
        chart_data
    )

    # 底部：runway
    runway_tb = slide.shapes.add_textbox(Inches(0.8), Inches(5.5), Inches(5), Inches(1))
    runway_tb.text_frame.text = f"Runway: {runway_months} months"
    runway_tb.text_frame.paragraphs[0].font.size = Pt(18)
    runway_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['text']
    runway_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'

    slide.notes_slide.notes_text_frame.text = "Ask slide. The ask is a slide, not a footnote."
    return slide
```

---

## 5. Stage diagnosis 矩阵

| Stage | Revenue | Slides | Dominant narrative |
|---|---|---|---|
| **Seed** | $0–1M ARR | 10–12 | Problem 30% + Solution 25% |
| **Series A** | $1–5M ARR | 12–16 | Traction 20% + Market "why now" 15% |
| **Series B** | $5–30M ARR | 18–22 | Traction + Unit econ 30% |
| **Series C** | $30M+ ARR | 20–24 | Financials + Scale + Moat 40% |
| **Bridge/SAFE** | any | 8–10 | Bridge reason + runway + commitments |

### Stage 选 narrative 权重

```python
def stage_narrative_weights(stage):
    """返回 narrative section → 占比"""
    weights = {
        'seed':    {'problem': 0.30, 'solution': 0.25, 'market': 0.15,
                    'traction': 0.10, 'team': 0.10, 'ask': 0.10},
        'series_a':{'problem': 0.10, 'solution': 0.10, 'market': 0.15,
                    'traction': 0.20, 'team': 0.10, 'unit_econ': 0.15, 'ask': 0.10},
        'series_b':{'problem': 0.05, 'solution': 0.05, 'market': 0.10,
                    'traction': 0.20, 'unit_econ': 0.15, 'financials': 0.15,
                    'team': 0.10, 'ask': 0.10},
        'series_c':{'traction': 0.15, 'unit_econ': 0.10, 'financials': 0.25,
                    'scale': 0.15, 'moat': 0.10, 'team': 0.10, 'ask': 0.10},
        'bridge':  {'bridge_reason': 0.30, 'runway': 0.25, 'commitments': 0.20,
                    'traction': 0.15, 'ask': 0.10},
    }
    return weights.get(stage, weights['seed'])
```

---

## 6. 5 赛道 arc 模板

### B2B SaaS

**Must-have**：unit econ (CAC/LTV/Payback) + logo wall

- Slide 6 Business Model: SaaS metrics
- Slide 7 Traction: ARR + logo wall (right side)
- Slide 8 Team: must include prior SaaS experience
- Slide 9 Financials: 4-year ARR projection

### Consumer

**Must-have**：retention curve + AOV growth

- Slide 6: AOV / repeat rate / contribution margin
- Slide 7: DAU/MAU + retention curve
- Slide 9: 4-year revenue + CAC payback

### Deep Tech

**Must-have**：tech moat + IP / patent

- Slide 3 Solution: tech architecture
- Slide 5 Product: demo + IP
- Slide 8 Team: PhD credentials + prior research
- Slide 9 Financials: longer runway (24-36 months)

### Marketplace

**Must-have**：liquidity + GMV growth + take-rate

- Slide 6: GMV / take-rate / liquidity
- Slide 7: supply/demand growth
- Slide 9: 4-year GMV projection

### Bio

**Must-have**：pipeline chart + 临床数据

- Slide 3 Solution: pipeline chart (Phase 1/2/3)
- Slide 5 Product: clinical data
- Slide 7 Traction: trial milestones
- Slide 8 Team: PI credentials
- Slide 9 Financials: longer horizon (5-7 years)

---

## 7. 6 项 layout canon 模式

### C.1 Title/Cover

暗渐变 + 居中标题 + 底部 meta + brand band

```python
# 见 Slide 1: Cover 示例
```

### C.2 3-Stat callout row

3 大数字卡，问题/Why-Now/Traction 默认

```python
# 见 Slide 2: Problem 示例（3 数据卡）
```

### C.3 4-Stat callout row

同 C.2 但 4 列，**wrap warning**：60pt + 7cm 宽度下 `$9.4M` 会换行

```python
def add_4_stat_row(slide, stats, top=Inches(2)):
    """4 列 stat row"""
    card_w, lefts = card_grid(4, slide_width=13.333, margin=0.8, gap=0.4)
    for i, (number, label) in enumerate(stats):
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(lefts[i]), top, Inches(card_w), Inches(2.5)
        )
        card.name = f'Stat4_{i+1}'
        card.fill.solid()
        card.fill.fore_color.rgb = PALETTE['accent']
        card.line.fill.background()
        tf = card.text_frame
        # ⚠️ 60pt + 7cm 宽度下，$9.4M 会换行；安全：$9M / $96B / 340% / 4.2x
        tf.text = number
        tf.paragraphs[0].font.size = Pt(48)  # 降到 48pt 避免 wrap
        tf.paragraphs[0].font.bold = True
        tf.paragraphs[0].font.color.rgb = PALETTE['primary']
        tf.paragraphs[0].font.name = 'Microsoft YaHei'
        tf.paragraphs[0].alignment = PP_ALIGN.CENTER

        p2 = tf.add_paragraph()
        p2.text = label
        p2.font.size = Pt(12)
        p2.font.color.rgb = PALETTE['muted']
        p2.font.name = 'Microsoft YaHei'
        p2.alignment = PP_ALIGN.CENTER
```

### C.4 Chart + Context

chart 左 55% + 2-3 stacked callouts 右

```python
# 见 Slide 7: Traction 示例
```

### C.5 Icon-in-circle grid

3 行垂直，每行 circle + title + description

```python
def add_icon_circle_grid(slide, items, top=Inches(2)):
    """items: list of (icon_letter, title, desc), len=3"""
    for i, (icon, title, desc) in enumerate(items):
        y = top + Inches(i * 1.6)
        # 左：circle + icon letter
        circle = slide.shapes.add_shape(
            MSO_SHAPE.OVAL,
            Inches(0.8), y, Inches(1.2), Inches(1.2)
        )
        circle.name = f'IconCircle_{i+1}'
        circle.fill.solid()
        circle.fill.fore_color.rgb = PALETTE['primary']
        circle.line.fill.background()
        tf = circle.text_frame
        tf.text = icon
        tf.paragraphs[0].font.size = Pt(36)
        tf.paragraphs[0].font.bold = True
        tf.paragraphs[0].font.color.rgb = PALETTE['white']
        tf.paragraphs[0].font.name = 'Microsoft YaHei'
        tf.paragraphs[0].alignment = PP_ALIGN.CENTER

        # 右：title + desc
        text_tb = slide.shapes.add_textbox(
            Inches(2.5), y, Inches(10), Inches(1.4)
        )
        text_tb.name = f'IconText_{i+1}'
        tf2 = text_tb.text_frame
        tf2.text = title
        tf2.paragraphs[0].font.size = Pt(22)
        tf2.paragraphs[0].font.bold = True
        tf2.paragraphs[0].font.color.rgb = PALETTE['primary']
        tf2.paragraphs[0].font.name = 'Microsoft YaHei'

        p2 = tf2.add_paragraph()
        p2.text = desc
        p2.font.size = Pt(16)
        p2.font.color.rgb = PALETTE['text']
        p2.font.name = 'Microsoft YaHei'
```

### C.5b 2×2 Feature grid

4 卡 roundRect，z-order canon: bg → ellipse → title → body 严格顺序

```python
def add_2x2_feature_grid(slide, features, top=Inches(2)):
    """features: list of (title, body), len=4"""
    positions = [
        (Inches(0.8), top),
        (Inches(6.9), top),
        (Inches(0.8), top + Inches(2.5)),
        (Inches(6.9), top + Inches(2.5)),
    ]
    for i, ((title, body), (left, top_pos)) in enumerate(zip(features, positions)):
        # 1. bg (roundRect)
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            left, top_pos, Inches(5.6), Inches(2.2)
        )
        card.name = f'FeatureBg_{i+1}'
        card.fill.solid()
        card.fill.fore_color.rgb = PALETTE['accent']
        card.line.fill.background()

        # 2. ellipse (icon)
        ellipse = slide.shapes.add_shape(
            MSO_SHAPE.OVAL,
            left + Inches(0.3), top_pos + Inches(0.3),
            Inches(0.8), Inches(0.8)
        )
        ellipse.name = f'FeatureIcon_{i+1}'
        ellipse.fill.solid()
        ellipse.fill.fore_color.rgb = PALETTE['primary']
        ellipse.line.fill.background()
        ellipse.text_frame.text = str(i+1)
        ellipse.text_frame.paragraphs[0].font.size = Pt(24)
        ellipse.text_frame.paragraphs[0].font.bold = True
        ellipse.text_frame.paragraphs[0].font.color.rgb = PALETTE['white']
        ellipse.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'
        ellipse.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

        # 3. title
        title_tb = slide.shapes.add_textbox(
            left + Inches(1.3), top_pos + Inches(0.3),
            Inches(4), Inches(0.5)
        )
        title_tb.name = f'FeatureTitle_{i+1}'
        title_tb.text_frame.text = title
        title_tb.text_frame.paragraphs[0].font.size = Pt(20)
        title_tb.text_frame.paragraphs[0].font.bold = True
        title_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['primary']
        title_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'

        # 4. body
        body_tb = slide.shapes.add_textbox(
            left + Inches(1.3), top_pos + Inches(1),
            Inches(4), Inches(1)
        )
        body_tb.name = f'FeatureBody_{i+1}'
        body_tb.text_frame.text = body
        body_tb.text_frame.paragraphs[0].font.size = Pt(14)
        body_tb.text_frame.paragraphs[0].font.color.rgb = PALETTE['text']
        body_tb.text_frame.paragraphs[0].font.name = 'Microsoft YaHei'
        body_tb.text_frame.word_wrap = True
```

**z-order canon**：必须按 bg → ellipse → title → body 顺序加，否则 ellipse 会被 title 遮挡。

---

## 8. pitch-deck 扩展 Gate

### Gate 5a — dark-on-dark contrast

```python
def gate_5a_check(prs):
    """检查所有 slide 的 dark-on-dark"""
    issues = []
    for slide_idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if not hasattr(shape, 'fill') or shape.fill.type is None:
                continue
            try:
                fill_rgb = shape.fill.fore_color.rgb
            except:
                continue
            if brightness(fill_rgb) >= 30:
                continue
            if not shape.has_text_frame:
                continue
            for p in shape.text_frame.paragraphs:
                for run in p.runs:
                    if run.font.color and run.font.color.rgb:
                        if brightness(run.font.color.rgb) <= 80:
                            issues.append(
                                f"Slide {slide_idx+1}: dark-on-dark "
                                f"shape '{shape.name}'"
                            )
    return issues
```

### Gate 2b — pitch-specific shell-strip signatures

扫描以下字面量：

- `xxxx` / `lorem` / `ipsum`
- `<TODO>` / `placeholder` / `TBD`
- `"this slide layout"`
- 空 `()` / `[]`
- `{{name}}` / `$fy$24`（template tokens）

```python
import re

LEFTOVER_PATTERNS = [
    r'xxxx', r'lorem', r'ipsum', r'<TODO>', r'placeholder', r'TBD',
    r'this slide layout', r'\(\s*\)', r'\[\s*\]',
    r'\{\{[^}]+\}\}', r'\$fy\$\d+',
]

def gate_2b_check(prs):
    """扫描所有 slide 的 leftover placeholders"""
    issues = []
    for slide_idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text
            for pattern in LEFTOVER_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    issues.append(
                        f"Slide {slide_idx+1}: leftover placeholder "
                        f"'{pattern}' in shape '{shape.name}'"
                    )
    return issues
```

### Gate 5b — Visual audit via HTML preview（MANDATORY）

**OfficeCli 无此内容，本项目独立设计**：python-pptx 没有 native preview，必须把 .pptx 渲染成 HTML 截图审查。

简化做法：用 `python-pptx` 提取每张 slide 的 shape 元数据，输出结构化 JSON 让人/AI 审查：

```python
def audit_to_json(prs, output_path):
    """导出 slide 审查 JSON"""
    import json
    audit = []
    for slide_idx, slide in enumerate(prs.slides):
        slide_info = {
            'slide': slide_idx + 1,
            'shapes': [],
            'issues': [],
        }
        for shape in slide.shapes:
            shape_info = {
                'name': shape.name,
                'type': str(shape.shape_type),
                'left': shape.left / 914400 if shape.left else None,
                'top': shape.top / 914400 if shape.top else None,
                'width': shape.width / 914400 if shape.width else None,
                'height': shape.height / 914400 if shape.height else None,
                'text': shape.text_frame.text if shape.has_text_frame else None,
            }
            slide_info['shapes'].append(shape_info)
            # 检查 overflow
            if shape_info['left'] and shape_info['width']:
                if shape_info['left'] + shape_info['width'] > 13.333:
                    slide_info['issues'].append(
                        f"Shape '{shape.name}' overflows right edge"
                    )
        audit.append(slide_info)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)
```

### Gate 6 — Pitch narrative sanity（executable）

#### 6.1 Cover 有 round + ask

```python
def check_6_1_cover(prs):
    cover = prs.slides[0]
    text = ''
    for shape in cover.shapes:
        if shape.has_text_frame:
            text += shape.text_frame.text + ' '
    # 检查 round 关键词
    round_keywords = ['seed', 'series a', 'series b', 'series c', 'safe', 'bridge']
    has_round = any(kw in text.lower() for kw in round_keywords)
    # 检查 $ amount
    has_amount = bool(re.search(r'\$[\d.]+[MBK]', text))
    return has_round and has_amount
```

#### 6.2 Problem 有 ≥1 数据点

#### 6.3 Traction y 轴 = 0

```python
def check_6_3_traction_y_axis(prs):
    """检查 traction chart 的 y 轴从 0 起"""
    for slide in prs.slides[5:8]:  # 大致 traction 位置
        for shape in slide.shapes:
            if not shape.has_chart:
                continue
            chart = shape.chart
            try:
                if chart.value_axis.minimum_scale != 0:
                    return False
            except:
                continue
    return True
```

#### 6.4 Team 每卡有 prior company

#### 6.5 Ask 是独立 slide（不在 cover 或 closing）

#### 6.6 Narrative 顺序匹配 10 slides

---

## 9. 典型 recipe — Seed round pitch deck

完整示例约 500 行 python-pptx 代码，覆盖 10 slides。本节给出骨架，详细 shape 代码见 [recipes.md](file:///<project_root>/.agents/skills/office_pptx/references/recipes.md)。

```python
"""Seed round pitch deck generator (skeleton).

公司假设：GreenOps — SaaS 帮 SMB 自动对账，节省财务 2 小时/周
Round: Seed $1.5M
"""
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Cm
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.chart import XL_CHART_TYPE
from pptx.chart.data import CategoryChartData

PALETTE = {
    'primary':   RGBColor(0x2C, 0x5F, 0x2D),
    'secondary': RGBColor(0x97, 0xBC, 0x62),
    'accent':    RGBColor(0xF5, 0xF5, 0xF5),
    'text':      RGBColor(0x2D, 0x2D, 0x2D),
    'muted':     RGBColor(0x6B, 0x8E, 0x6B),
    'white':     RGBColor(0xFF, 0xFF, 0xFF),
}

def card_grid(n_cards, slide_width=13.333, margin=0.8, gap=0.4):
    usable = slide_width - 2 * margin - (n_cards - 1) * gap
    col_width = usable / n_cards
    lefts = [margin + i * (col_width + gap) for i in range(n_cards)]
    return col_width, lefts


def build_seed_pitch_deck(output_path):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Slide 1: Cover
    add_cover_slide(prs, 'GreenOps',
                    'Automated reconciliation for SMB finance teams',
                    'Seed', '$1.5M', '2026-07-18')

    # Slide 2: Problem
    add_problem_slide(prs,
        '73% of SMB finance teams waste 2+ hours/week on manual reconciliation.',
        [('73%', 'of SMB finance teams'), ('2h+', 'wasted per week'),
         ('$180K', 'avg annual cost')])

    # Slide 3: Solution
    add_solution_slide(prs,
        'GreenOps auto-reconciles transactions in 3 steps.',
        [('Import', 'Connect banks + accounting'),
         ('Match', 'AI matches transactions'),
         ('Export', 'One-click close')])

    # Slide 4: Market
    add_market_slide(prs, '$4.2B TAM', '$840M SAM', '$84M SOM',
                     'Top-down: Gartner SMB finance software report 2025')

    # Slide 5: Product
    add_product_slide(prs, screenshot_path='workspace/office_pptx/screenshot.png')

    # Slide 6: Business Model (SaaS)
    add_business_model_saas(prs, cac=1200, ltv=18000, payback=8, gm=85)

    # Slide 7: Traction
    add_traction_slide(prs,
        months=['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
        arr_values=[0.1, 0.2, 0.4, 0.7, 1.1, 1.6],
        callouts=[('127', 'paying customers'), ('118%', 'NRR'),
                  ('1.2%', 'monthly churn')])

    # Slide 8: Team
    add_team_slide(prs, [
        {'name': 'Alice Chen', 'role': 'CEO',
         'prior': 'Ex-Stripe', 'achievement': 'Built Atlas to $50M ARR'},
        {'name': 'Bob Zhang', 'role': 'CTO',
         'prior': 'Ex-Stripe', 'achievement': 'Scaled API to 99.99% SLA'},
        {'name': 'Cara Liu', 'role': 'CPO',
         'prior': 'Ex-Notion', 'achievement': 'Led 0→1 mobile launch'},
    ])

    # Slide 9: Financials
    add_financials_slide(prs,
        years=['2026', '2027', '2028', '2029'],
        revenue=[1.6, 6.5, 18.2, 42.0],
        ebitda=[-1.2, -0.8, 1.5, 8.2],
        assumptions=[('CAC', '$1.2K'), ('LTV', '$18K'),
                     ('GM', '85%'), ('Growth', '300% YoY')])

    # Slide 10: Ask
    add_ask_slide(prs, '$1.5M', 18,
                  {'Eng': 40, 'GTM': 35, 'G&A': 10, 'Reserve': 15})

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    prs.save(output_path)
    return output_path

# 各 add_*_slide 函数实现见 recipes.md
```

完整 `add_cover_slide` / `add_problem_slide` / 等函数实现参考本文件第 4 节（10 必备 slides）。

---

## 参考

- [office_pptx SKILL.md](file:///<project_root>/.agents/skills/office_pptx/SKILL.md)
- [references/basic.md](file:///<project_root>/.agents/skills/office_pptx/references/basic.md)
- [references/design_principles.md](file:///<project_root>/.agents/skills/office_pptx/references/design_principles.md)
- [references/recipes.md](file:///<project_root>/.agents/skills/office_pptx/references/recipes.md)
- [python-pptx 官方文档](https://python-pptx.readthedocs.io/)
