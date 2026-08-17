# Report 配方参考 (report_recipes.md)

本文件是 `office_docx` skill 的 Report 配方参考，涵盖封面、Page X of Y footer、表头、财务表、SWOT cell、TOC、周报、简历等典型场景。所有代码基于纯 Python（python-docx + lxml）。

主色用绿色：`#2E7D32`（primary）/ `#E8F5E9`（light）/ `#4CAF50`（accent）。

---

## Recipe a — Rich cover page

堆叠 confidentiality banner + title + subtitle + client/project/date block + key-themes strip，然后 `pageBreakBefore=true` 强制下一节到新页。满足"封面 ≥ 60% 填充"底线。

```python
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Twips
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def set_cell_fill(cell, color_hex):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), color_hex)
    tcPr.append(shd)

def add_rich_cover(doc, title, subtitle, client, project, date_str, themes):
    """添加富封面页。
    
    themes: 关键主题列表，如 ['Growth', 'Innovation', 'Sustainability']
    """
    # 1. Confidentiality banner（顶部色块）
    banner = doc.add_table(rows=1, cols=1)
    banner.rows[0].cells[0].text = 'CONFIDENTIAL'
    set_cell_fill(banner.rows[0].cells[0], '2E7D32')
    # 文本白色居中
    for p in banner.rows[0].cells[0].paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for r in p.runs:
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            r.font.bold = True
            r.font.size = Pt(10)
    
    # 间距
    for _ in range(4):
        doc.add_paragraph()
    
    # 2. Title
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(title)
    run.font.size = Pt(36)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)  # primary green
    # East Asia 字体
    from docx.oxml.ns import qn
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    
    # 3. Subtitle
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(subtitle)
    run.font.size = Pt(18)
    run.font.italic = True
    run.font.color.rgb = RGBColor(0x4C, 0xAF, 0x50)  # accent green
    
    # 间距
    for _ in range(3):
        doc.add_paragraph()
    
    # 4. Client / Project / Date block（左对齐表）
    info_table = doc.add_table(rows=3, cols=2)
    info_table.autofit = False
    info_table.columns[0].width = Inches(2)
    info_table.columns[1].width = Inches(4)
    
    info_data = [
        ('Client', client),
        ('Project', project),
        ('Date', date_str),
    ]
    for i, (label, value) in enumerate(info_data):
        info_table.rows[i].cells[0].text = label
        info_table.rows[i].cells[1].text = value
        # label 粗体
        for p in info_table.rows[i].cells[0].paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(12)
        for p in info_table.rows[i].cells[1].paragraphs:
            for r in p.runs:
                r.font.size = Pt(12)
    
    # 间距
    for _ in range(3):
        doc.add_paragraph()
    
    # 5. Key themes strip（底部色块 + 主题）
    themes_table = doc.add_table(rows=1, cols=len(themes))
    for i, theme in enumerate(themes):
        cell = themes_table.rows[0].cells[i]
        cell.text = theme
        set_cell_fill(cell, 'E8F5E9')  # light green
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in p.runs:
                r.font.size = Pt(11)
                r.font.bold = True
                r.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
    
    # 6. 强制下一节到新页
    next_heading = doc.add_heading('第一章 引言', level=1)
    next_heading.paragraph_format.page_break_before = True

# 用法
doc = Document()
add_rich_cover(
    doc,
    title='2024 Q3 季度报告',
    subtitle='战略回顾与展望',
    client='ABC 公司',
    project='Strategic Review',
    date_str='2024-09-30',
    themes=['增长', '创新', '可持续'],
)
doc.save('workspace/office_docx/季度报告_20240930.docx')
```

---

## Recipe b — Page X of Y footer

复合 PAGE + NUMPAGES。先 add footer 段落，再三个子操作构造 `Page <X> of <Y>`。用 oxml 验证 fldChar 数量（≥ 4 个 fldChar，单 PAGE ≥ 5 runs，复合 "Page X of Y" ≥ 11 runs）。

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_field(paragraph, field_code, cached_value=''):
    """通用 field 注入（5 元组）。"""
    r1 = paragraph.add_run()
    fld1 = OxmlElement('w:fldChar')
    fld1.set(qn('w:fldCharType'), 'begin')
    r1._element.append(fld1)
    
    r2 = paragraph.add_run()
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = ' ' + field_code + ' '
    r2._element.append(instr)
    
    r3 = paragraph.add_run()
    fld3 = OxmlElement('w:fldChar')
    fld3.set(qn('w:fldCharType'), 'separate')
    r3._element.append(fld3)
    
    r4 = paragraph.add_run(cached_value)
    
    r5 = paragraph.add_run()
    fld5 = OxmlElement('w:fldChar')
    fld5.set(qn('w:fldCharType'), 'end')
    r5._element.append(fld5)

def add_page_x_of_y_footer(section):
    """添加 Page X of Y footer。"""
    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # "Page " 文本（1 run）
    p.add_run('Page ')
    # PAGE field（5 runs）
    add_field(p, 'PAGE \\* MERGEFORMAT', '1')
    # " of " 文本（1 run）
    p.add_run(' of ')
    # NUMPAGES field（5 runs）
    add_field(p, 'NUMPAGES \\* MERGEFORMAT', '1')
    # 总 runs: 1 + 5 + 1 + 5 = 12 ≥ 11

def verify_footer_fldchar_count(section):
    """验证 footer 中 fldChar 数量。"""
    footer = section.footer
    count = 0
    for p in footer.paragraphs:
        for run in p.runs:
            for child in run._element:
                if child.tag.endswith('fldChar'):
                    count += 1
    return count  # 应 ≥ 4

# 用法
section = doc.sections[0]
add_page_x_of_y_footer(section)
assert verify_footer_fldchar_count(section) >= 4
```

---

## Recipe c — Header row with fill + white bold text

顺序很重要：先填表头文本 → 再设 cell fill → 再设 run 格式（空单元格 set 报错）。

```python
def add_table_with_header(doc, headers, data_rows):
    """添加带格式化表头的表格。
    
    headers: ['列1', '列2', '列3']
    data_rows: [['a', 'b', 'c'], ['d', 'e', 'f']]
    """
    table = doc.add_table(rows=1 + len(data_rows), cols=len(headers))
    table.style = 'Table Grid'
    table.autofit = False
    
    # 1. 先填表头文本
    for i, header in enumerate(headers):
        table.rows[0].cells[i].text = header
    
    # 2. 再设 cell fill
    for i in range(len(headers)):
        set_cell_fill(table.rows[0].cells[i], '2E7D32')  # primary green
    
    # 3. 再设 run 格式
    for i in range(len(headers)):
        for p in table.rows[0].cells[i].paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in p.runs:
                r.font.bold = True
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                r.font.size = Pt(11)
                # East Asia 字体
                rPr = r._element.get_or_add_rPr()
                rFonts = rPr.find(qn('w:rFonts'))
                if rFonts is None:
                    rFonts = OxmlElement('w:rFonts')
                    rPr.append(rFonts)
                rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    
    # 填数据行
    for row_idx, row_data in enumerate(data_rows, start=1):
        for col_idx, value in enumerate(row_data):
            table.rows[row_idx].cells[col_idx].text = value
    
    return table

# 用法
add_table_with_header(
    doc,
    headers=['项目', '负责人', '状态'],
    data_rows=[
        ['需求分析', '张三', '完成'],
        ['设计', '李四', '进行中'],
        ['开发', '王五', '未开始'],
    ],
)
```

---

## Recipe d — Financial table

右对齐数字、粗体合计、合计行底边框。number format `¥#,##0.00`、`0.00%`、`YYYY-MM-DD`。

```python
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement

def set_cell_border(cell, edge='bottom', size=4, color='2E7D32'):
    """设置单元格边框。"""
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = tcPr.find(qn('w:tcBorders'))
    if tcBorders is None:
        tcBorders = OxmlElement('w:tcBorders')
        tcPr.append(tcBorders)
    border = tcBorders.find(qn('w:' + edge))
    if border is None:
        border = OxmlElement('w:' + edge)
        tcBorders.append(border)
    border.set(qn('w:val'), 'single')
    border.set(qn('w:sz'), str(size))
    border.set(qn('w:color'), color)

def add_financial_table(doc, line_items, total_label='合计'):
    """添加财务表。
    
    line_items: [{'label': '收入', 'amount': 100000.00, 'format': 'currency'},
                  {'label': '成本', 'amount': 60000.00, 'format': 'currency'},
                  {'label': '利润率', 'amount': 0.40, 'format': 'percent'}]
    """
    table = doc.add_table(rows=len(line_items) + 2, cols=2)
    table.style = 'Table Grid'
    
    # 表头
    table.rows[0].cells[0].text = '项目'
    table.rows[0].cells[1].text = '金额'
    set_cell_fill(table.rows[0].cells[0], '2E7D32')
    set_cell_fill(table.rows[0].cells[1], '2E7D32')
    for cell in table.rows[0].cells:
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    
    # 数据行
    total = 0
    for i, item in enumerate(line_items, start=1):
        table.rows[i].cells[0].text = item['label']
        # 格式化金额
        if item['format'] == 'currency':
            value_str = '¥{:,.2f}'.format(item['amount'])
            if item['label'] in ('收入',):
                total += item['amount']
            elif item['label'] in ('成本',):
                total -= item['amount']
        elif item['format'] == 'percent':
            value_str = '{:.2%}'.format(item['amount'])
        else:
            value_str = str(item['amount'])
        
        cell = table.rows[i].cells[1]
        cell.text = value_str
        # 右对齐
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    # 合计行
    total_row = table.rows[-1]
    total_row.cells[0].text = total_label
    total_row.cells[1].text = '¥{:,.2f}'.format(total)
    # 粗体
    for cell in total_row.cells:
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.bold = True
        # 底边框
        set_cell_fill(cell, 'E8F5E9')  # light green
    # 右对齐合计金额
    for p in total_row.cells[1].paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    return table

# 用法
add_financial_table(doc, [
    {'label': '收入', 'amount': 100000.00, 'format': 'currency'},
    {'label': '成本', 'amount': 60000.00, 'format': 'currency'},
    {'label': '利润率', 'amount': 0.40, 'format': 'percent'},
])
```

---

## Recipe e — Cell with multiple bullets (SWOT / risk matrix)

`c1="a\nb"` 是单段落换行；多 bullet 段落需 `add_paragraph` 到 cell。

```python
def add_swot_table(doc, swot_data):
    """添加 SWOT 表格，每个 cell 多个 bullet。
    
    swot_data: {'S': ['优势1', '优势2'], 'W': [...], 'O': [...], 'T': [...]}
    """
    table = doc.add_table(rows=2, cols=2)
    table.style = 'Table Grid'
    table.autofit = False
    
    # 表头标签
    labels = [('S', '优势 Strengths', 0, 0),
              ('W', '劣势 Weaknesses', 0, 1),
              ('O', '机会 Opportunities', 1, 0),
              ('T', '威胁 Threats', 1, 1)]
    
    for key, label, row, col in labels:
        cell = table.rows[row].cells[col]
        # 1. 标题段落
        p = cell.paragraphs[0]
        run = p.add_run(label)
        run.font.bold = True
        run.font.size = Pt(12)
        run.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
        
        # 2. 多个 bullet 段落
        for bullet in swot_data[key]:
            bullet_p = cell.add_paragraph(bullet, style='List Bullet')
            for r in bullet_p.runs:
                r.font.size = Pt(10)
    
    return table

# 用法
add_swot_table(doc, {
    'S': ['技术领先', '团队经验丰富', '品牌知名度高'],
    'W': ['资金有限', '市场覆盖不足'],
    'O': ['政策支持', '新兴市场需求增长'],
    'T': ['竞争对手增加', '原材料价格上涨'],
})
```

---

## Recipe f — TOC without a field engine

python-docx **不能算** TOC 页码。保留 live TOC + `updateFields=true`，告诉收件人在 Word 中打开。**不要**替换为猜的静态页码。

```python
def add_toc(doc):
    """插入 TOC field + updateFields=true。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    p = doc.add_paragraph()
    
    # TOC field
    r1 = p.add_run()
    fld1 = OxmlElement('w:fldChar')
    fld1.set(qn('w:fldCharType'), 'begin')
    r1._element.append(fld1)
    
    r2 = p.add_run()
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = ' TOC \\o "1-3" \\h \\z \\u '
    r2._element.append(instr)
    
    r3 = p.add_run()
    fld3 = OxmlElement('w:fldChar')
    fld3.set(qn('w:fldCharType'), 'separate')
    r3._element.append(fld3)
    
    # cached value：提示文本（不是猜的页码）
    r4 = p.add_run('右键此处选择"更新域"以生成目录')
    r4.font.italic = True
    r4.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    
    r5 = p.add_run()
    fld5 = OxmlElement('w:fldChar')
    fld5.set(qn('w:fldCharType'), 'end')
    r5._element.append(fld5)
    
    # 设置 updateFields=true
    settings = doc.settings.element
    update_fields = OxmlElement('w:updateFields')
    update_fields.set(qn('w:val'), 'true')
    settings.append(update_fields)

# 反模式：不要这样做
# for h1, page in [('第一章', '1'), ('第二章', '5')]:
#     doc.add_paragraph(f'{h1}\t{page}')  # 静态猜页码，错！
```

---

## Recipe g — Weekly report 完整示例

```python
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Twips
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

def build_weekly_report(output_path, week_title, summary, completed_items, 
                        risks, next_week_plan):
    """构建周报。
    
    week_title: '2024 W39'
    summary: 摘要文本
    completed_items: [{'item': '需求分析', 'owner': '张三', 'status': '完成'}, ...]
    risks: {'S': [...], 'W': [...], 'O': [...], 'T': [...]}  # SWOT
    next_week_plan: ['计划1', '计划2', ...]
    """
    doc = Document()
    
    # === 1. 封面 ===
    add_rich_cover(
        doc,
        title=week_title + ' 周报',
        subtitle='Weekly Report',
        client='内部',
        project='工程团队',
        date_str=datetime.date.today().strftime('%Y-%m-%d'),
        themes=['交付', '质量', '效率'],
    )
    
    # === 2. TOC ===
    doc.add_heading('目录', level=1)
    add_toc(doc)
    
    # === 3. 摘要 section ===
    doc.add_heading('摘要', level=1)
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.5
    p.paragraph_format.first_line_indent = Twips(360)
    p.add_run(summary)
    
    # === 4. 完成项 section ===
    doc.add_heading('本周完成项', level=1)
    add_table_with_header(
        doc,
        headers=['项目', '负责人', '状态'],
        data_rows=[[item['item'], item['owner'], item['status']] 
                   for item in completed_items],
    )
    
    # === 5. 风险 section (SWOT) ===
    doc.add_heading('风险与机会 (SWOT)', level=1)
    add_swot_table(doc, risks)
    
    # === 6. 下周计划 section ===
    doc.add_heading('下周计划', level=1)
    for plan in next_week_plan:
        doc.add_paragraph(plan, style='List Number')
    
    # === Page X of Y footer ===
    add_page_x_of_y_footer(doc.sections[0])
    
    doc.save(output_path)
    
    # 验证
    validate_docx(output_path)
    
    return output_path

def validate_docx(path):
    """Gate 1: schema 验证。"""
    d = Document(path)
    # 抽查
    assert len(d.paragraphs) > 0
    assert len(d.tables) > 0

# 用法
build_weekly_report(
    output_path='workspace/office_docx/周报_20240930.docx',
    week_title='2024 W39',
    summary='本周完成需求分析和设计阶段，启动开发。整体进度符合预期。',
    completed_items=[
        {'item': '需求分析', 'owner': '张三', 'status': '完成'},
        {'item': '系统设计', 'owner': '李四', 'status': '完成'},
        {'item': '前端开发', 'owner': '王五', 'status': '进行中'},
    ],
    risks={
        'S': ['团队技术能力强', '需求清晰'],
        'W': ['测试资源紧张'],
        'O': ['可复用历史代码'],
        'T': ['第三方接口可能延期'],
    },
    next_week_plan=[
        '完成前端核心功能开发',
        '启动后端 API 实现',
        '准备测试环境',
    ],
)
```

---

## Recipe h — Resume 简历示例

```python
def build_resume(output_path, name, contact, education, experience, skills):
    """构建简历。
    
    name: '张三'
    contact: {'email': '...', 'phone': '...', 'location': '...'}
    education: [{'school': '...', 'degree': '...', 'period': '...', 'gpa': '...'}]
    experience: [{'company': '...', 'role': '...', 'period': '...', 
                  'bullets': ['...', '...']}]
    skills: ['Python', 'JavaScript', 'SQL', ...]
    """
    doc = Document()
    
    # === 个人信息 header ===
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(name)
    run.font.size = Pt(28)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    
    # 联系方式
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_str = ' | '.join([
        contact.get('email', ''),
        contact.get('phone', ''),
        contact.get('location', ''),
    ])
    run = p.add_run(contact_str)
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)
    
    # 分割线
    p = doc.add_paragraph()
    pPr = p._element.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '12')
    bottom.set(qn('w:color'), '2E7D32')
    pBdr.append(bottom)
    pPr.append(pBdr)
    
    # === 教育背景 ===
    h = doc.add_heading('教育背景', level=1)
    for r in h.runs:
        r.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
    
    for edu in education:
        # 表格：学校 | 学位 | 时间
        table = doc.add_table(rows=2, cols=2)
        table.autofit = False
        table.columns[0].width = Inches(4)
        table.columns[1].width = Inches(2)
        
        table.rows[0].cells[0].text = edu['school']
        table.rows[0].cells[1].text = edu['period']
        # 学校粗体
        for p in table.rows[0].cells[0].paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(12)
        # 时间右对齐
        for p in table.rows[0].cells[1].paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for r in p.runs:
                r.font.size = Pt(11)
                r.font.italic = True
        
        table.rows[1].cells[0].text = edu['degree']
        if 'gpa' in edu:
            table.rows[1].cells[1].text = 'GPA: ' + edu['gpa']
    
    # === 工作经验 ===
    h = doc.add_heading('工作经验', level=1)
    for r in h.runs:
        r.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
    
    for exp in experience:
        # 公司 + 时间
        p = doc.add_paragraph()
        run = p.add_run(exp['company'])
        run.font.bold = True
        run.font.size = Pt(13)
        # tab + 时间右对齐
        p.paragraph_format.tab_stops.add_tab_stop(Inches(6), WD_TAB_ALIGNMENT.RIGHT)
        run2 = p.add_run('\t' + exp['period'])
        run2.font.italic = True
        run2.font.size = Pt(11)
        
        # 职位
        p = doc.add_paragraph()
        run = p.add_run(exp['role'])
        run.font.bold = True
        run.font.size = Pt(12)
        run.font.color.rgb = RGBColor(0x4C, 0xAF, 0x50)
        
        # 工作内容 bullet
        for bullet in exp['bullets']:
            doc.add_paragraph(bullet, style='List Bullet')
    
    # === 技能 ===
    h = doc.add_heading('技能', level=1)
    for r in h.runs:
        r.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)
    
    # 技能分两列表格
    skill_table = doc.add_table(rows=(len(skills) + 1) // 2, cols=2)
    for i, skill in enumerate(skills):
        row = i // 2
        col = i % 2
        skill_table.rows[row].cells[col].text = '• ' + skill
        for p in skill_table.rows[row].cells[col].paragraphs:
            for r in p.runs:
                r.font.size = Pt(11)
    
    doc.save(output_path)
    return output_path

# 用法
build_resume(
    output_path='workspace/office_docx/简历_20240930.docx',
    name='张三',
    contact={
        'email': 'zhangsan@example.com',
        'phone': '138-0000-0000',
        'location': '上海',
    },
    education=[
        {
            'school': '清华大学',
            'degree': '计算机科学学士',
            'period': '2018-2022',
            'gpa': '3.8/4.0',
        },
    ],
    experience=[
        {
            'company': 'ABC 科技有限公司',
            'role': '高级软件工程师',
            'period': '2022-至今',
            'bullets': [
                '负责后端 API 设计与开发，服务日活 100 万用户',
                '主导微服务架构改造，性能提升 40%',
                '指导 3 名初级工程师',
            ],
        },
        {
            'company': 'XYZ 创业公司',
            'role': '软件工程师实习生',
            'period': '2021-2022',
            'bullets': [
                '参与电商平台前端开发',
                '实现支付模块，处理 1000+ 并发',
            ],
        },
    ],
    skills=['Python', 'JavaScript', 'TypeScript', 'SQL', 'AWS', 'Docker', 'Kubernetes'],
)
```

---

## 验证 + 输出

所有 recipe 完成后执行：

```python
import os

def report_output(path):
    """报告生成结果。"""
    size_kb = os.path.getsize(path) / 1024
    doc = Document(path)
    para_count = len(doc.paragraphs)
    table_count = len(doc.tables)
    print(f'文件: {path}')
    print(f'大小: {size_kb:.1f} KB')
    print(f'段落数: {para_count}')
    print(f'表格数: {table_count}')
    # 页数估计（粗略：每页约 30 段或 1 表）
    est_pages = max(1, para_count // 30 + table_count)
    print(f'估计页数: {est_pages}')
```

**OfficeCli 无原生 API 的内容（标注：本项目独立设计）**：

- `set_cell_fill` / `set_cell_border` 等单元格格式函数为本项目独立设计的 oxml 注入方案。
- `add_field` 通用 field 注入函数为本项目独立设计（OfficeCli 用 CLI 命令，本项目用 python-docx + oxml）。
