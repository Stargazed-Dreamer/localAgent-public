# 通用配方参考 (recipes.md)

本文件是 [office_pptx skill](file:///f:/<project_root>/.agents/skills/office_pptx/SKILL.md) 的子参考，专注通用 pptx 配方（cover / divider / content / chart / table / thank-you / project report / 复习 PPT）。

## 目录

1. [Recipe a — Rich cover slide](#recipe-a--rich-cover-slide)
2. [Recipe b — Section divider](#recipe-b--section-divider)
3. [Recipe c — Content slide with bullets](#recipe-c--content-slide-with-bullets)
4. [Recipe d — Chart slide](#recipe-d--chart-slide)
5. [Recipe e — Table slide](#recipe-e--table-slide)
6. [Recipe f — Thank-you slide](#recipe-f--thank-you-slide)
7. [Recipe g — Project report 完整示例](#recipe-g--project-report-完整示例)
8. [Recipe h — 复习 PPT 示例](#recipe-h--复习-ppt-示例)

---

## 通用导入与调色板

```python
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Cm
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.chart.data import CategoryChartData
from pptx.oxml.ns import qn

PALETTE = {
    'primary':   RGBColor(0x2C, 0x5F, 0x2D),  # Forest
    'secondary': RGBColor(0x97, 0xBC, 0x62),  # Moss
    'accent':    RGBColor(0xF5, 0xF5, 0xF5),  # Light neutral
    'text':      RGBColor(0x2D, 0x2D, 0x2D),
    'muted':     RGBColor(0x6B, 0x8E, 0x6B),
    'white':     RGBColor(0xFF, 0xFF, 0xFF),
    'warning':   RGBColor(0xFF, 0x70, 0x43),  # Deep orange for emphasis
}

def set_font_eastasia(run, font_name='Microsoft YaHei'):
    """设置 run 的中文字体（eastAsia）"""
    rPr = run._r.get_or_add_rPr()
    for ea in rPr.findall(qn('a:ea')):
        rPr.remove(ea)
    ea = rPr.makeelement(qn('a:ea'), {'typeface': font_name})
    rPr.append(ea)

def style_run(run, size=18, bold=False, color=None, font='Microsoft YaHei'):
    """统一设置 run 字体样式"""
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run.font.name = font
    set_font_eastasia(run, font)

def new_widescreen_prs():
    """新建 16:9 widescreen Presentation"""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs
```

---

## Recipe a — Rich cover slide

堆叠 confidentiality banner + title + subtitle + presenter/date block，满足"封面 ≥ 60% 填充"底线。

```python
def recipe_a_cover_slide(prs, title, subtitle, presenter, date_str,
                          confidential=True):
    """Rich cover slide — 4 个堆叠层 + 左侧 brand band"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # Layer 1: 左侧 brand band（33% width，主色填充）
    band = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        0, 0, Inches(4.4), Inches(7.5)
    )
    band.name = 'CoverBrandBand'
    band.fill.solid()
    band.fill.fore_color.rgb = PALETTE['primary']
    band.line.fill.background()

    # Band 上的 brand mark（circle + 字母）
    mark = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Inches(0.8), Inches(0.8), Inches(1), Inches(1)
    )
    mark.name = 'BrandMark'
    mark.fill.solid()
    mark.fill.fore_color.rgb = PALETTE['secondary']
    mark.line.fill.background()
    tf = mark.text_frame
    tf.text = 'G'
    style_run(tf.paragraphs[0].runs[0] if tf.paragraphs[0].runs
              else tf.paragraphs[0].add_run(),
              size=36, bold=True, color=PALETTE['white'])
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # Layer 2: confidentiality banner（顶部，warning 色）
    if confidential:
        banner = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(4.4), 0, Inches(8.933), Inches(0.4)
        )
        banner.name = 'ConfidentialBanner'
        banner.fill.solid()
        banner.fill.fore_color.rgb = PALETTE['warning']
        banner.line.fill.background()
        btf = banner.text_frame
        btf.text = 'CONFIDENTIAL — DO NOT DISTRIBUTE'
        btf.paragraphs[0].alignment = PP_ALIGN.CENTER
        if btf.paragraphs[0].runs:
            style_run(btf.paragraphs[0].runs[0], size=10, bold=True,
                      color=PALETTE['white'])

    # Layer 3: 主标题（右侧居中偏上）
    title_tb = slide.shapes.add_textbox(
        Inches(5), Inches(2), Inches(8), Inches(2)
    )
    title_tb.name = 'CoverTitle'
    ttf = title_tb.text_frame
    ttf.word_wrap = True
    ttf.text = title
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=44, bold=True, color=PALETTE['primary'])

    # Layer 4: 副标题
    sub_tb = slide.shapes.add_textbox(
        Inches(5), Inches(4), Inches(8), Inches(1)
    )
    sub_tb.name = 'CoverSubtitle'
    stf = sub_tb.text_frame
    stf.word_wrap = True
    stf.text = subtitle
    style_run(stf.paragraphs[0].runs[0] if stf.paragraphs[0].runs
              else stf.paragraphs[0].add_run(),
              size=20, color=PALETTE['muted'])

    # Layer 5: presenter / date block（底部）
    meta_tb = slide.shapes.add_textbox(
        Inches(5), Inches(6), Inches(8), Inches(1)
    )
    meta_tb.name = 'CoverMeta'
    mtf = meta_tb.text_frame
    mtf.text = f"{presenter}  ·  {date_str}"
    style_run(mtf.paragraphs[0].runs[0] if mtf.paragraphs[0].runs
              else mtf.paragraphs[0].add_run(),
              size=14, color=PALETTE['text'])

    # notes
    slide.notes_slide.notes_text_frame.text = (
        f"Cover slide. Title: {title}. Subtitle: {subtitle}. "
        f"Presenter: {presenter}, date: {date_str}."
    )
    return slide
```

**验证**：
- 5 个 shape 都有 name
- z-order: band → mark → banner → title → subtitle → meta（后加在上层）
- 字号：title 44pt bold / subtitle 20pt / meta 14pt（≥ Visual Floor）
- 视觉元素：左侧 brand band + brand mark circle + 顶部 banner（3 个非文本视觉元素）

---

## Recipe b — Section divider

巨型 translucent 数字 `size=120 opacity=0.15`。

```python
def recipe_b_section_divider(prs, section_num, section_title, section_desc=''):
    """Section divider — 巨型 translucent 数字 + 标题"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 背景：浅色填充
    bg = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        0, 0, prs.slide_width, prs.slide_height
    )
    bg.name = 'DividerBg'
    bg.fill.solid()
    bg.fill.fore_color.rgb = PALETTE['accent']
    bg.line.fill.background()

    # 巨型 translucent 数字（背景装饰）
    # python-pptx 不直接支持 opacity，用浅色模拟
    big_num = slide.shapes.add_textbox(
        Inches(0.5), Inches(0.5), Inches(6), Inches(6.5)
    )
    big_num.name = 'DividerBigNum'
    btf = big_num.text_frame
    btf.text = f"{section_num:02d}"  # 01, 02, 03...
    # 用 muted 色模拟 opacity 0.15
    style_run(btf.paragraphs[0].runs[0] if btf.paragraphs[0].runs
              else btf.paragraphs[0].add_run(),
              size=300, bold=True,
              color=RGBColor(0xE8, 0xEE, 0xE8))  # 浅 1 度的 muted

    # 标题（右侧或下方）
    title_tb = slide.shapes.add_textbox(
        Inches(7), Inches(3), Inches(6), Inches(2)
    )
    title_tb.name = 'DividerTitle'
    ttf = title_tb.text_frame
    ttf.word_wrap = True
    ttf.text = section_title
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=48, bold=True, color=PALETTE['primary'])

    # 描述（可选）
    if section_desc:
        desc_tb = slide.shapes.add_textbox(
            Inches(7), Inches(5), Inches(6), Inches(1.5)
        )
        desc_tb.name = 'DividerDesc'
        dtf = desc_tb.text_frame
        dtf.word_wrap = True
        dtf.text = section_desc
        style_run(dtf.paragraphs[0].runs[0] if dtf.paragraphs[0].runs
                  else dtf.paragraphs[0].add_run(),
                  size=18, color=PALETTE['muted'])

    slide.notes_slide.notes_text_frame.text = (
        f"Section divider for: {section_title}. "
        f"Transition: 'Now let's move to {section_title}.'"
    )
    return slide
```

---

## Recipe c — Content slide with bullets

标题 + 3-5 bullets + 视觉元素（icon/shape/chart）。字号规范（title 36-44pt，body 18-24pt）。max 5 bullets。

```python
def recipe_c_content_slide(prs, title, bullets, icon_letter='i'):
    """
    Content slide with bullets + icon visual
    bullets: list of str, max 5
    """
    assert len(bullets) <= 5, "max 5 bullets — split to multi-slide"
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # Layer 1: 标题（顶部，左对齐）
    title_tb = slide.shapes.add_textbox(
        Inches(0.8), Inches(0.5), Inches(11.5), Inches(1)
    )
    title_tb.name = 'ContentTitle'
    ttf = title_tb.text_frame
    ttf.text = title
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=40, bold=True, color=PALETTE['primary'])
    ttf.paragraphs[0].alignment = PP_ALIGN.LEFT

    # Layer 2: 视觉元素（左上 circle + icon letter）
    icon = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Inches(0.8), Inches(2), Inches(1.5), Inches(1.5)
    )
    icon.name = 'ContentIcon'
    icon.fill.solid()
    icon.fill.fore_color.rgb = PALETTE['primary']
    icon.line.fill.background()
    itf = icon.text_frame
    itf.text = icon_letter
    style_run(itf.paragraphs[0].runs[0] if itf.paragraphs[0].runs
              else itf.paragraphs[0].add_run(),
              size=48, bold=True, color=PALETTE['white'])
    itf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # Layer 3: bullets（右侧 2/3）
    body_tb = slide.shapes.add_textbox(
        Inches(3), Inches(2), Inches(9.5), Inches(5)
    )
    body_tb.name = 'ContentBody'
    btf = body_tb.text_frame
    btf.word_wrap = True

    for i, bullet in enumerate(bullets):
        p = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
        p.text = f"•  {bullet}"
        if p.runs:
            style_run(p.runs[0], size=20, color=PALETTE['text'])
        p.line_spacing = 1.5
        p.space_after = Pt(12)

    # notes
    slide.notes_slide.notes_text_frame.text = (
        f"Content slide: {title}. Bullets: {', '.join(bullets)}"
    )
    return slide
```

---

## Recipe d — Chart slide

chart 占左 55% + 2-3 stacked callouts 右。axismin=0。

```python
def recipe_d_chart_slide(prs, title, categories, series_data, callouts):
    """
    Chart slide — 左 chart + 右 callouts
    series_data: list of (name, values) tuples
    callouts: list of (number, label) tuples, len=2-3
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 标题
    title_tb = slide.shapes.add_textbox(
        Inches(0.8), Inches(0.5), Inches(11.5), Inches(1)
    )
    title_tb.name = 'ChartTitle'
    ttf = title_tb.text_frame
    ttf.text = title
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=40, bold=True, color=PALETTE['primary'])

    # Chart（左 55%）
    chart_data = CategoryChartData()
    chart_data.categories = categories
    for name, values in series_data:
        chart_data.add_series(name, values)

    chart_shape = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(0.8), Inches(2), Inches(6.5), Inches(5),
        chart_data
    )
    chart_shape.name = 'ChartMain'
    chart = chart_shape.chart
    chart.has_title = False
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False

    # ⚠️ axismin=0 load-bearing（防止 hockey-stick）
    chart.value_axis.minimum_scale = 0
    chart.value_axis.has_major_gridlines = True

    # 系列颜色
    plot = chart.plots[0]
    series_colors = [PALETTE['primary'], PALETTE['secondary'],
                     PALETTE['muted']]
    for idx, series in enumerate(plot.series):
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = series_colors[idx % len(series_colors)]

    # Callouts（右 45%）
    n_callouts = len(callouts)
    callout_height = 1.5
    callout_gap = 0.3
    total_h = n_callouts * callout_height + (n_callouts - 1) * callout_gap
    start_top = 2 + (5 - total_h) / 2  # 垂直居中

    for i, (number, label) in enumerate(callouts):
        top = Inches(start_top + i * (callout_height + callout_gap))
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(8), top, Inches(4.5), Inches(callout_height)
        )
        card.name = f'Callout{i+1}'
        card.fill.solid()
        card.fill.fore_color.rgb = PALETTE['accent']
        card.line.color.rgb = PALETTE['secondary']
        card.line.width = Pt(1)

        ctf = card.text_frame
        ctf.text = number
        style_run(ctf.paragraphs[0].runs[0] if ctf.paragraphs[0].runs
                  else ctf.paragraphs[0].add_run(),
                  size=36, bold=True, color=PALETTE['primary'])
        ctf.paragraphs[0].alignment = PP_ALIGN.CENTER

        p2 = ctf.add_paragraph()
        p2.text = label
        if p2.runs:
            style_run(p2.runs[0], size=14, color=PALETTE['muted'])
        p2.alignment = PP_ALIGN.CENTER

    # notes
    slide.notes_slide.notes_text_frame.text = (
        f"Chart slide: {title}. Y-axis from 0. "
        f"Callouts: {callouts}"
    )
    return slide
```

---

## Recipe e — Table slide

表头 fill + white bold text。财务表（右对齐数字、粗体合计、合计行底边框）。

```python
def recipe_e_table_slide(prs, title, headers, rows, totals_row=None):
    """
    Table slide — 表头主色填充 + 白字
    rows: list of list (不含表头)
    totals_row: list or None（合计行，加粗 + 底边框）
    """
    n_rows = len(rows) + 1 + (1 if totals_row else 0)
    n_cols = len(headers)

    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 标题
    title_tb = slide.shapes.add_textbox(
        Inches(0.8), Inches(0.5), Inches(11.5), Inches(1)
    )
    title_tb.name = 'TableTitle'
    ttf = title_tb.text_frame
    ttf.text = title
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=40, bold=True, color=PALETTE['primary'])

    # 表格
    table_shape = slide.shapes.add_table(
        n_rows, n_cols,
        Inches(0.8), Inches(2), Inches(11.7), Inches(4.5)
    )
    table_shape.name = 'TableMain'
    table = table_shape.table

    # 列宽（均分，可按需调整）
    col_w = 11.7 / n_cols
    for c in range(n_cols):
        table.columns[c].width = Inches(col_w)

    # 先填所有行
    # 表头
    for c, h in enumerate(headers):
        cell = table.cell(0, c)
        cell.text = h
        p = cell.text_frame.paragraphs[0]
        if p.runs:
            style_run(p.runs[0], size=14, bold=True, color=PALETTE['white'])
        p.alignment = PP_ALIGN.CENTER

    # 数据行
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row):
            cell = table.cell(r_idx, c_idx)
            cell.text = str(val)
            p = cell.text_frame.paragraphs[0]
            if p.runs:
                style_run(p.runs[0], size=14, color=PALETTE['text'])
            # 数字右对齐（从第 2 列起）
            if c_idx > 0 and str(val).replace('.', '').replace('-', '').isdigit():
                p.alignment = PP_ALIGN.RIGHT
            else:
                p.alignment = PP_ALIGN.LEFT

    # 合计行
    if totals_row:
        r_idx = n_rows - 1
        for c_idx, val in enumerate(totals_row):
            cell = table.cell(r_idx, c_idx)
            cell.text = str(val)
            p = cell.text_frame.paragraphs[0]
            if p.runs:
                style_run(p.runs[0], size=14, bold=True, color=PALETTE['primary'])
            if c_idx > 0:
                p.alignment = PP_ALIGN.RIGHT
            else:
                p.alignment = PP_ALIGN.LEFT

    # 后设样式：表头填充 + 数据行 zebra + 合计行底边框
    # 表头
    for c in range(n_cols):
        cell = table.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = PALETTE['primary']

    # 数据行 zebra
    for r_idx in range(1, len(rows) + 1):
        for c in range(n_cols):
            cell = table.cell(r_idx, c)
            cell.fill.solid()
            if r_idx % 2 == 0:
                cell.fill.fore_color.rgb = RGBColor(0xF1, 0xF8, 0xE9)
            else:
                cell.fill.fore_color.rgb = PALETTE['white']

    # 合计行填充 + 底边框
    if totals_row:
        r_idx = n_rows - 1
        for c in range(n_cols):
            cell = table.cell(r_idx, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = PALETTE['accent']

    # notes
    slide.notes_slide.notes_text_frame.text = (
        f"Table slide: {title}. Rows: {len(rows)}. "
        f"Totals row: {'yes' if totals_row else 'no'}."
    )
    return slide
```

---

## Recipe f — Thank-you slide

大字"Thank You" + 联系方式 + Q&A prompt。

```python
def recipe_f_thank_you_slide(prs, contact_email='', contact_phone='',
                              qa_prompt='Questions?'):
    """Thank-you slide — 全屏主色 + 居中大字"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 全屏主色背景
    bg = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        0, 0, prs.slide_width, prs.slide_height
    )
    bg.name = 'ThankYouBg'
    bg.fill.solid()
    bg.fill.fore_color.rgb = PALETTE['primary']
    bg.line.fill.background()

    # 大字 "Thank You"
    title_tb = slide.shapes.add_textbox(
        Inches(1), Inches(2), Inches(11.333), Inches(2)
    )
    title_tb.name = 'ThankYouTitle'
    ttf = title_tb.text_frame
    ttf.text = 'Thank You'
    style_run(ttf.paragraphs[0].runs[0] if ttf.paragraphs[0].runs
              else ttf.paragraphs[0].add_run(),
              size=72, bold=True, color=PALETTE['white'])
    ttf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # Q&A prompt
    qa_tb = slide.shapes.add_textbox(
        Inches(1), Inches(4), Inches(11.333), Inches(1)
    )
    qa_tb.name = 'ThankYouQA'
    qtf = qa_tb.text_frame
    qtf.text = qa_prompt
    style_run(qtf.paragraphs[0].runs[0] if qtf.paragraphs[0].runs
              else qtf.paragraphs[0].add_run(),
              size=24, color=PALETTE['secondary'])
    qtf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # 联系方式
    if contact_email or contact_phone:
        contact_parts = []
        if contact_email:
            contact_parts.append(f"Email: {contact_email}")
        if contact_phone:
            contact_parts.append(f"Phone: {contact_phone}")
        contact_tb = slide.shapes.add_textbox(
            Inches(1), Inches(5.5), Inches(11.333), Inches(1)
        )
        contact_tb.name = 'ThankYouContact'
        ctf = contact_tb.text_frame
        ctf.text = '  ·  '.join(contact_parts)
        style_run(ctf.paragraphs[0].runs[0] if ctf.paragraphs[0].runs
                  else ctf.paragraphs[0].add_run(),
                  size=16, color=PALETTE['accent'])
        ctf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # notes
    slide.notes_slide.notes_text_frame.text = (
        "Thank-you slide. Open Q&A. Be ready for: "
        "competition, unit econ deep-dive, team gaps."
    )
    return slide
```

---

## Recipe g — Project report 完整示例

完整 8-slide project report（约 300 行）：

1. 封面（项目 + 日期）
2. 议程
3. 项目背景
4. 完成项（带表格）
5. 数据图表
6. 风险与挑战
7. 下周计划
8. Thank you

```python
def recipe_g_project_report(output_path, project_name, date_str,
                             completed_items, chart_data_dict,
                             risks, next_week_plan):
    """Project report — 8 slides"""
    prs = new_widescreen_prs()

    # === Slide 1: 封面 ===
    recipe_a_cover_slide(
        prs,
        title=f"{project_name} 项目汇报",
        subtitle=f"阶段总结 · {date_str}",
        presenter="项目组",
        date_str=date_str,
        confidential=False
    )

    # === Slide 2: 议程 ===
    recipe_c_content_slide(
        prs,
        title="议程",
        bullets=[
            "项目背景与目标",
            "本期完成事项",
            "关键数据与进展",
            "风险与挑战",
            "下周计划",
        ],
        icon_letter="1"
    )

    # === Slide 3: 项目背景 ===
    recipe_c_content_slide(
        prs,
        title="项目背景",
        bullets=[
            f"项目名：{project_name}",
            "目标：交付完整功能并上线",
            "周期：4 周（2026-06-20 至 2026-07-18）",
            "团队：3 人（PM + 2 工程师）",
            "预算：$15K",
        ],
        icon_letter="2"
    )

    # === Slide 4: 完成项（带表格）===
    recipe_e_table_slide(
        prs,
        title="本期完成事项",
        headers=['任务', '负责人', '状态', '完成日期'],
        rows=completed_items,  # list of [task, owner, status, date]
        totals_row=['合计', '', f'{len(completed_items)} 项', '']
    )

    # === Slide 5: 数据图表 ===
    recipe_d_chart_slide(
        prs,
        title="每周任务完成数趋势",
        categories=chart_data_dict['categories'],
        series_data=chart_data_dict['series'],  # [('完成任务', [...]), ('计划任务', [...])]
        callouts=[
            (str(sum(chart_data_dict['series'][0][1])), '总完成数'),
            (f"{chart_data_dict['series'][0][1][-1]}", '本周完成'),
            ('95%', '按时率'),
        ]
    )

    # === Slide 6: 风险与挑战 ===
    recipe_c_content_slide(
        prs,
        title="风险与挑战",
        bullets=risks,  # list of str
        icon_letter="!"
    )

    # === Slide 7: 下周计划 ===
    recipe_c_content_slide(
        prs,
        title="下周计划",
        bullets=next_week_plan,  # list of str
        icon_letter="→"
    )

    # === Slide 8: Thank you ===
    recipe_f_thank_you_slide(
        prs,
        contact_email='team@example.com',
        qa_prompt='Questions & Discussion'
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    prs.save(output_path)

    # 验证
    prs2 = Presentation(output_path)
    assert len(prs2.slides) == 8, f"Expected 8 slides, got {len(prs2.slides)}"
    print(f"✓ Generated: {output_path}")
    print(f"  Slides: 8")
    print(f"  Size: {os.path.getsize(output_path)} bytes")
    return output_path

# 用法
if __name__ == '__main__':
    recipe_g_project_report(
        output_path='workspace/office_pptx/项目汇报_20260718.pptx',
        project_name='office_pptx skill',
        date_str='2026-07-18',
        completed_items=[
            ['创建 SKILL.md', 'Alice', '已完成', '2026-07-15'],
            ['创建 basic.md', 'Bob', '已完成', '2026-07-16'],
            ['创建 design_principles.md', 'Alice', '已完成', '2026-07-17'],
            ['创建 pitch_deck.md', 'Cara', '进行中', '2026-07-18'],
            ['创建 recipes.md', 'Bob', '进行中', '2026-07-18'],
        ],
        chart_data_dict={
            'categories': ['W1', 'W2', 'W3', 'W4'],
            'series': [('完成任务', [3, 4, 5, 2]), ('计划任务', [4, 4, 5, 5])],
        },
        risks=[
            'pitch_deck recipe 完整代码尚未填充',
            '需验证 python-pptx animation oxml 在 PowerPoint 实际渲染',
            '中文字体在某些 viewer 可能回退',
        ],
        next_week_plan=[
            '补全 pitch_deck recipe 完整代码',
            '在 PowerPoint 中实测 5 个示例文件',
            '更新 _index.md 与 GUIDE_REGISTRY',
            '写 evals/evals.json 测试用例',
        ],
    )
```

---

## Recipe h — 复习 PPT 示例

包含：
- 章节标题 slide
- 知识点 slide（带 bullets + icon）
- 例题 slide（带表格）

```python
def recipe_h_review_ppt(output_path, subject, exam_date,
                         chapters, examples):
    """
    复习 PPT — 章节 + 知识点 + 例题
    chapters: list of {title, points: list of str}
    examples: list of {title, problem: str, solution: str}
    """
    prs = new_widescreen_prs()

    # === Slide 1: 封面 ===
    recipe_a_cover_slide(
        prs,
        title=f"{subject} 复习",
        subtitle=f"考试日期：{exam_date}",
        presenter="复习小组",
        date_str=exam_date,
        confidential=False
    )

    # === Slides 2+: 每章一节 + 知识点 ===
    for idx, chapter in enumerate(chapters, start=1):
        # 章节分隔页
        recipe_b_section_divider(
            prs,
            section_num=idx,
            section_title=chapter['title'],
            section_desc=f"共 {len(chapter['points'])} 个知识点"
        )
        # 知识点 slide
        recipe_c_content_slide(
            prs,
            title=chapter['title'],
            bullets=chapter['points'],
            icon_letter=str(idx)
        )

    # === 例题 slides ===
    for ex in examples:
        # 例题：用 table slide（2 行：题目 / 解答）
        recipe_e_table_slide(
            prs,
            title=f"例题：{ex['title']}",
            headers=['项目', '内容'],
            rows=[
                ['题目', ex['problem']],
                ['解答', ex['solution']],
            ]
        )

    # === Thank you ===
    recipe_f_thank_you_slide(
        prs,
        qa_prompt='加油复习！'
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    prs.save(output_path)

    # 验证
    prs2 = Presentation(output_path)
    expected_slides = 1 + sum(2 for _ in chapters) + len(examples) + 1
    assert len(prs2.slides) == expected_slides, \
        f"Expected {expected_slides} slides, got {len(prs2.slides)}"
    print(f"✓ Generated: {output_path}")
    print(f"  Slides: {expected_slides}")
    return output_path

# 用法
if __name__ == '__main__':
    recipe_h_review_ppt(
        output_path='workspace/office_pptx/数据结构复习_20260718.pptx',
        subject='数据结构',
        exam_date='2026-07-25',
        chapters=[
            {'title': '栈与队列',
             'points': ['栈：LIFO（后进先出）',
                        '队列：FIFO（先进先出）',
                        '用 Python list 实现栈：append + pop',
                        '用 collections.deque 实现队列：popleft',
                        '应用：括号匹配、表达式求值']},
            {'title': '树与二叉树',
             'points': ['二叉树：每个节点最多 2 个子节点',
                        '遍历：前序 / 中序 / 后序 / 层序',
                        'BST：左 < 根 < 右',
                        'AVL：自平衡 BST',
                        '应用：字典、索引、堆']},
        ],
        examples=[
            {'title': '有效括号',
             'problem': '给定字符串 s，判断是否有效括号匹配',
             'solution': '用栈：遇左括号 push，遇右括号 pop 检查匹配'},
            {'title': '二叉树中序遍历',
             'problem': '返回二叉树的中序遍历结果',
             'solution': '递归：左 → 根 → 右；或用栈迭代'},
        ],
    )
```

---

## 通用验证函数

```python
import os
import re
from pptx import Presentation

LEFTOVER_PATTERNS = [
    r'xxxx', r'lorem', r'ipsum', r'<TODO>', r'placeholder', r'TBD',
    r'\(\s*\)', r'\[\s*\]', r'\{\{[^}]+\}\}',
]

def validate_pptx(path, min_slides=1, expected_keywords=None):
    """完整验证 pptx — Gate 1 + Gate 2b"""
    # Gate 1: schema（重新打开不报错）
    assert os.path.exists(path), f"文件不存在: {path}"
    size = os.path.getsize(path)
    assert size > 0, f"文件为空: {path}"
    print(f"✓ Gate 1 schema: 文件 {size/1024:.1f} KB")

    prs = Presentation(path)
    slide_count = len(prs.slides)
    assert slide_count >= min_slides, \
        f"Slide 数 {slide_count} < 期望 {min_slides}"
    print(f"✓ Slide 数: {slide_count}")

    # Gate 2b: leftover placeholders
    issues = []
    for slide_idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text
            for pattern in LEFTOVER_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    issues.append(
                        f"Slide {slide_idx+1} shape '{shape.name}': "
                        f"leftover '{pattern}'"
                    )
    if issues:
        print("✗ Gate 2b 检测到 leftover:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    print("✓ Gate 2b: 无 leftover placeholders")

    # 关键字检查
    if expected_keywords:
        full_text = ''
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    full_text += shape.text_frame.text + ' '
        for kw in expected_keywords:
            if kw not in full_text:
                print(f"✗ 缺少关键字: {kw}")
                return False
        print(f"✓ 关键字检查通过")

    return True

# 用法
# validate_pptx(
#     'workspace/office_pptx/项目汇报_20260718.pptx',
#     min_slides=8,
#     expected_keywords=['项目汇报', '议程', '完成事项']
# )
```

---

## 参考

- [office_pptx SKILL.md](file:///f:/<project_root>/.agents/skills/office_pptx/SKILL.md)
- [references/basic.md](file:///f:/<project_root>/.agents/skills/office_pptx/references/basic.md)
- [references/design_principles.md](file:///f:/<project_root>/.agents/skills/office_pptx/references/design_principles.md)
- [references/pitch_deck.md](file:///f:/<project_root>/.agents/skills/office_pptx/references/pitch_deck.md)
- [python-pptx 官方文档](https://python-pptx.readthedocs.io/)
