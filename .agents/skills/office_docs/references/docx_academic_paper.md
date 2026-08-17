# 学术论文参考 (academic_paper.md)

本文件是 `office_docx` skill 的学术论文场景参考，涵盖 APA / Chicago / IEEE / MLA 四种引用风格、OMML 公式、SEQ + PAGEREF 交叉引用。所有代码基于纯 Python（python-docx + lxml）。

---

## 1. 何时用此 skill（trigger）

满足以下 ≥2 个条件时用此 skill：

- 研究论文 / 期刊投稿 / 会议论文 / 论文章节
- 正式引用风格（APA / Chicago / IEEE / MLA）
- 编号公式（equation 1, 2, ...）
- 图表交叉引用（see Fig. 1, Table II）
- 脚注 / 尾注
- 参考文献列表
- 多列期刊布局（IEEE / ACM / Nature 双栏）
- Abstract / Keywords / Affiliation 三件套

## 2. 何时回退到 basic.md

以下场景**不要**用此 skill，走 `basic.md`：

- 白皮书（white paper）
- 政策简报（policy brief）
- 技术报告（technical report）
- HR 模板 / 合同模板 / SOW
- 任何无 venue / 无引用风格的文档

---

## 3. 5 步工作流

### Step 1 — 读 venue spec（一次决策，下游全跟）

确认目标 venue 的引用风格：APA 7 / Chicago 17 / IEEE / MLA 9。一旦确定，**全文档**沿用，不要混用。

### Step 2 — 规划 sections

标准学术论文章节顺序：

```
Abstract → Keywords → Introduction → Methods → Results → Discussion → Conclusion → References
```

3+ H1 时加 TOC。

### Step 3 — 先设置 styles

**在任何内容之前**设置：

- Heading1 / Heading2 / Heading3 字号、对齐、缩进
- Caption（图/表 caption）样式
- AbstractTitle 样式
- Bibliography 样式（hanging indent）

```python
from docx import Document
from docx.shared import Pt, Twips, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

def setup_academic_styles(doc, citation_style='APA'):
    """根据引用风格设置文档样式。"""
    styles = doc.styles
    
    # Normal 样式
    normal = styles['Normal']
    normal.font.name = 'Times New Roman'
    normal.font.size = Pt(12)
    # East Asia 字体（如果有中文）
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    rPr = normal.element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), 'SimSun')
    
    # APA 2x 行距 / IEEE 1.15x
    if citation_style == 'APA':
        normal.paragraph_format.line_spacing = 2.0
    elif citation_style == 'IEEE':
        normal.paragraph_format.line_spacing = 1.15
        normal.font.size = Pt(10)
    else:
        normal.paragraph_format.line_spacing = 2.0
    
    # Heading 1
    h1 = styles['Heading 1']
    h1.font.size = Pt(14)
    h1.font.bold = True
    if citation_style == 'APA':
        h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif citation_style == 'IEEE':
        h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        h1.font.size = Pt(10)
    
    # Bibliography 样式（hanging indent 0.5"）
    if 'Bibliography' not in [s.name for s in styles]:
        bib = styles.add_style('Bibliography', WD_STYLE_TYPE.PARAGRAPH)
        bib.paragraph_format.left_indent = Twips(720)
        bib.paragraph_format.first_line_indent = Twips(-720)
        bib.paragraph_format.line_spacing = 2.0
        bib.font.name = 'Times New Roman'
        bib.font.size = Pt(12)
```

### Step 4 — 按顺序构建 body

```
cover/title block → abstract → keywords → TOC → body sections → 
figures/tables with SEQ captions → bibliography → footnotes（最后按段落路径加）
```

### Step 5 — QA — Delivery Gate

继承 docx Gates 1-3，加学术 Gates 4-5（见第 8 节）。

---

## 4. 学术增量规则（在 docx 之上的 6 项）

1. **Citation style 是契约**：选定后全文档统一，不要混用。
2. **公式是 first-class content**：inline `<m:oMath>` / display `<m:oMathPara>`，不要用普通文本模拟。
3. **图表自动编号**：SEQ Figure / SEQ Table + PAGEREF 交叉引用。
4. **Bibliography 用 hanging indent**：`left_indent=720, first_line_indent=-720`。
5. **Abstract / Keywords / Affiliation 是首页三件套**：缺一不可。
6. **Multi-column 布局**（IEEE / ACM / Nature）：用 oxml 设 `w:cols`。

---

## 5. Citation 4 风格完整规范

### 5.1 共享默认值

所有 4 种风格共用：

- Reference-list 段落用 `indent=720 hangingIndent=720`（hanging indent 0.5"）
- 3+ H1 时加 live TOC
- `updateFields=true` 让 Word 重算页码

### 5.2 决策表

| Style | In-text shape | Reference list order | Body line spacing | Footnotes? |
|-------|---------------|---------------------|-------------------|------------|
| APA 7 | `(Smith, 2024)` | Alphabetical | 2x double | No |
| Chicago 17 Notes-Bib | Superscript `¹` | Alphabetical | 1x or 2x | Yes (footnotes) |
| IEEE | `[1]` | First-appearance order | 1.15x single | No |
| MLA 9 | `(Smith 412)` | Alphabetical | 2x double | No |

### 5.3 Section numbering convention

| Style | H1 format | H2 format |
|-------|-----------|-----------|
| APA | Unnumbered, centered, bold | Unnumbered, left, bold |
| Chicago | `1. Introduction` left-aligned | `1.1 Policy Formation` |
| IEEE | `I. INTRODUCTION` ALL CAPS Roman | `A. Datasets` title case |
| MLA | Unnumbered, left, bold | Unnumbered, left, bold |

### 5.4 APA 7 完整规范

**In-text**：

- 单作者：`(Smith, 2024)` 或 `Smith (2024)`
- 直接引语：`(Smith, 2024, p. 15)`
- 3+ 作者首引后用：`(Smith et al., 2024)`

**Reference list**：

- Alphabetical（按 author surname）
- Title caps：article 用 sentence case，journal 用 title case + italic
- Reference shape：`Author, A. A., & Co-Author, B. B. (Year). Title of article. Journal Name, Volume(Issue), pages.` DOI 优先于 URL

**Spacing**：2x 双倍

**Body first-line indent**：0.5" (`firstLineIndent=720` twips)

**H1**：unnumbered, centered, bold

**代码示例**：

```python
def add_apa_h1(doc, text):
    p = doc.add_paragraph(style='Heading 1')
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.font.bold = True
    run.font.size = Pt(14)
    return p

def add_apa_body(doc, text):
    p = doc.add_paragraph(style='Normal')
    p.paragraph_format.first_line_indent = Twips(720)  # 0.5"
    p.paragraph_format.line_spacing = 2.0
    p.add_run(text)
    return p

def add_apa_reference(doc, text):
    p = doc.add_paragraph(style='Bibliography')
    # Bibliography 样式已设 hanging indent
    p.add_run(text)
    return p

# 用法
add_apa_h1(doc, 'Introduction')
add_apa_body(doc, 'Cognitive load theory (Sweller, 1988) suggests that...')
add_apa_reference(doc, 'Sweller, J. (1988). Cognitive load during problem solving. Cognitive Science, 12(2), 257-285.')
```

### 5.5 Chicago 17 Notes-Bib 完整规范

**In-text**：superscript footnote number

**首次 footnote 完整引用**：`Timothy Brook, The Troubled Empire (Cambridge, MA: Harvard UP, 2010), 142.`

**之后 shortened form**：`Brook, Troubled Empire, 150.`

**Repeat-citation**：

- 紧邻相同来源相同页：`Ibid.`
- 紧邻相同来源不同页：`Ibid., 22.`
- 非紧邻重复：shortened form，**不用** `op. cit.`（Chicago 17 抛弃）

**Bibliography**：alphabetical by author surname

**Footnote 字号**：信任渲染器默认（约 10pt）

**Bibliography 字号**：12pt

**Primary / Secondary Sources 分组**：两个 H2 under Bibliography H1

**Book titles**：italic

**Author-Date 变体**（sciences）：fallback 到 APA 配方，只改标点 — author 和 year 之间无逗号 `(Smith 2024)`

**H1**：`1. Introduction` left-aligned

**H2**：`1.1 Policy Formation`

```python
def add_chicago_h1(doc, text):
    p = doc.add_paragraph(style='Heading 1')
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(text)
    run.font.bold = True
    run.font.size = Pt(14)
    return p

def add_chicago_footnote(doc, footnote_text):
    """添加脚注。需 oxml 操作 footnotes.xml part。"""
    # 简化：实际实现需扩展 python-docx 的 footnotes part
    # 这里展示 footnote reference 的注入
    p = doc.add_paragraph()
    run = p.add_run('主文本')
    # footnote reference
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    footnote_ref = OxmlElement('w:footnoteReference')
    footnote_ref.set(qn('w:id'), '1')  # 需要在 footnotes.xml 中定义 id=1
    run._element.append(footnote_ref)
```

### 5.6 IEEE 完整规范

**In-text**：

- `[1]`, `[2]` 风格
- 按首次出现顺序编号（**非字母序**）
- 重复引用用相同编号
- `[1, p. 15]` 页码
- `[1]-[3]` 范围

**Reference entry**：以 bracketed number 开头

```
[1] A. Smith and B. Jones, "Title," IEEE Trans. X, vol. 5, no. 3, pp. 1-10, 2024, doi: ...
```

**Authors**：initial-first（`A. Smith`）

**Journal names**：缩写按 IEEE 列表（`IEEE Trans. Neural Netw.`）

**Body 是 two-column**

**Abstract**：单列在上方，10pt，1.15x 行距，200-250 词

**Body first-line indent**：0.2" (`firstLineIndent=288` twips)

**Section headings**：ALL CAPS + Roman numerals

- H1: `I. INTRODUCTION`, `II. RELATED WORK`
- H2: `A. Datasets`, `B. Baselines` title case

**Tables 编号**：Roman（`Table I`, `Table II`）

**Figures 编号**：保持 Arabic（`Fig. 1`）

**关键限制**：`recalcFields=seq` 写 Arabic 缓存值，IEEE Roman 表要么手动 patch 缓存要么接受 Arabic

```python
def add_ieee_h1(doc, text):
    """IEEE H1: I. INTRODUCTION 全大写 + Roman"""
    p = doc.add_paragraph(style='Heading 1')
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text.upper())
    run.font.bold = True
    run.font.size = Pt(10)
    return p

def add_ieee_h2(doc, text):
    """IEEE H2: A. Datasets title case"""
    p = doc.add_paragraph(style='Heading 2')
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(text)
    run.font.bold = True
    run.font.italic = True
    run.font.size = Pt(10)
    return p

def add_ieee_body(doc, text):
    p = doc.add_paragraph(style='Normal')
    p.paragraph_format.first_line_indent = Twips(288)  # 0.2"
    p.paragraph_format.line_spacing = 1.15
    p.add_run(text)
    return p

def add_ieee_reference(doc, num, text):
    p = doc.add_paragraph(style='Bibliography')
    p.add_run(f'[{num}] {text}')
    return p

# 用法
add_ieee_h1(doc, 'Introduction')  # 渲染为 "INTRODUCTION"（全大写）
add_ieee_h2(doc, 'Datasets')
add_ieee_reference(doc, 1, 'A. Smith and B. Jones, "Title," IEEE Trans. X, vol. 5, no. 3, pp. 1-10, 2024, doi: 10.1109/X.2024.12345.')
```

### 5.7 MLA 9 完整规范

**In-text**：`(Author Page)` 无逗号

- 示例：`(Smith 412)`
- 直接引语始终带页码

**Reference section 标题**：**Works Cited**（不是 References / Bibliography）

**Entries**：

- Alphabetical by surname
- Hanging indent
- 2x 间距

**九个 "core elements"**：

```
Author. Title. Container, Other Contributors, Version, Number, Publisher, Date, Location.
```

**Book titles**：italic

**Article titles**：in quotes

**Otherwise identical to APA paragraph setup**

**H1**：unnumbered, left, bold

```python
def add_mla_works_cited_entry(doc, text):
    p = doc.add_paragraph(style='Bibliography')
    p.paragraph_format.line_spacing = 2.0
    p.add_run(text)
    return p

# 用法
add_mla_works_cited_entry(doc, 'Smith, John. "The Art of Citation." Journal of Style, vol. 12, no. 3, 2024, pp. 410-425.')
```

---

## 6. OMML 公式模式

### 6.1 两种 mode

- **display**：`<m:oMathPara>` standalone centered block（独立成行居中）
- **inline**：`<m:oMath>` 在 paragraph 内（行内）

### 6.2 LaTeX subset

python-docx 不原生支持 LaTeX 公式。需用 lxml 注入 OMML XML。

### 6.3 典型模式

```python
# display equation
def add_display_equation(doc, latex_formula, equation_number=None):
    """添加 display equation（独立成行居中）。"""
    from docx.oxml.ns import qn, nsmap
    from docx.oxml import OxmlElement
    from lxml import etree
    
    # 这里简化为文本占位；实际需将 LaTeX 转 OMML（可用 latex2mathml + xslt 转 OMML）
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # OMML namespace
    m_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
    
    oMathPara = etree.SubElement(p._element, '{%s}oMathPara' % m_ns)
    oMath = etree.SubElement(oMathPara, '{%s}oMath' % m_ns)
    # 这里需要把 LaTeX 转 OMML 元素，简化示例
    r = etree.SubElement(oMath, '{%s}r' % m_ns)
    t = etree.SubElement(r, '{%s}t' % m_ns)
    t.text = latex_formula  # 占位，实际应是 OMML 元素
    
    # 如果有编号，加 right tab
    if equation_number is not None:
        p.paragraph_format.tab_stops.add_tab_stop(Inches(6.5), WD_TAB_ALIGNMENT.RIGHT)
        p.add_run('\t(' + str(equation_number) + ')')
```

### 6.4 Equation numbering

无原生 `\eqno`。单行公式居中 + 编号 flush-right 用两个 tab stops：

```python
def add_numbered_equation(doc, latex_formula, eq_num):
    """公式居中 + 编号右对齐。"""
    p = doc.add_paragraph()
    # 两个 tab：center tab + right tab
    p.paragraph_format.tab_stops.add_tab_stop(Inches(3.25), WD_TAB_ALIGNMENT.CENTER)
    p.paragraph_format.tab_stops.add_tab_stop(Inches(6.5), WD_TAB_ALIGNMENT.RIGHT)
    p.add_run('\t' + latex_formula + '\t(' + str(eq_num) + ')')
```

### 6.5 关键陷阱

**`\left(...\right)` 带内部 sub/superscript → parse error**

用普通 `(`, `)`, `[`, `]` — OMML 在 display mode 自动 sizing。

错误：

```
\left(x^2 + y^2\right) = z^2
```

正确：

```
(x^2 + y^2) = z^2
```

---

## 7. SEQ + PAGEREF 交叉引用完整模式

### 7.1 SEQ 自动编号

python-docx 用 oxml 注入 `<w:fldChar fieldType="seq" identifier="Figure"/>`：

```python
def add_seq_field(paragraph, identifier, cached_value=''):
    """添加 SEQ field 用于自动编号。
    
    identifier: 'Figure' / 'Table' / 'Equation'
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    # begin
    r1 = paragraph.add_run()
    fld1 = OxmlElement('w:fldChar')
    fld1.set(qn('w:fldCharType'), 'begin')
    r1._element.append(fld1)
    
    # instrText
    r2 = paragraph.add_run()
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = ' SEQ ' + identifier + ' \\* ARABIC '
    r2._element.append(instr)
    
    # separate
    r3 = paragraph.add_run()
    fld3 = OxmlElement('w:fldChar')
    fld3.set(qn('w:fldCharType'), 'separate')
    r3._element.append(fld3)
    
    # cached value
    r4 = paragraph.add_run(cached_value)
    
    # end
    r5 = paragraph.add_run()
    fld5 = OxmlElement('w:fldChar')
    fld5.set(qn('w:fldCharType'), 'end')
    r5._element.append(fld5)
```

### 7.2 Figure caption 配方（caption 在图**下方**）

```python
def add_figure_with_caption(doc, image_path, caption_text, alt_text):
    """添加图片 + caption。caption 在图下方。"""
    # 1. 图片
    doc.add_picture(image_path, width=Inches(5))
    last_shape = doc.inline_shapes[-1]
    last_shape.alt_text = alt_text
    
    # 2. caption（图下方）
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.keep_with_previous = True  # 与图保持同页
    
    # "Figure " 文本
    run = p.add_run('Figure ')
    run.font.bold = True
    run.font.size = Pt(10)
    
    # SEQ Figure field
    add_seq_field(p, 'Figure', '1')
    
    # ": " + caption text
    run2 = p.add_run(': ' + caption_text)
    run2.font.size = Pt(10)
    
    # 加 bookmark 供 PAGEREF 引用
    add_bookmark(p, 'fig_' + caption_text.replace(' ', '_')[:20], 100)
```

### 7.3 Table caption 配方（caption 在表**上方**）

```python
def add_table_with_caption(doc, rows, cols, caption_text):
    """添加表格 + caption。caption 在表上方。"""
    # 1. caption（表上方）
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.keep_with_next = True  # 与表保持同页
    
    run = p.add_run('Table ')
    run.font.bold = True
    run.font.size = Pt(10)
    
    add_seq_field(p, 'Table', '1')
    
    run2 = p.add_run(': ' + caption_text)
    run2.font.size = Pt(10)
    
    # 2. 表格
    table = doc.add_table(rows=rows, cols=cols)
    table.style = 'Table Grid'
    return table
```

### 7.4 PAGEREF 交叉引用配方

```python
def add_pageref(paragraph, bookmark_name, cached_value=''):
    """添加 PAGEREF field 交叉引用书签的页码。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    r1 = paragraph.add_run()
    fld1 = OxmlElement('w:fldChar')
    fld1.set(qn('w:fldCharType'), 'begin')
    r1._element.append(fld1)
    
    r2 = paragraph.add_run()
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = ' PAGEREF ' + bookmark_name + ' \\h '
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

# 用法：see Fig. 1 (page X)
p = doc.add_paragraph()
p.add_run('As shown in Fig. ')
add_seq_field(p, 'Figure', '1')  # 或直接硬编码 Fig. 1
p.add_run(' on page ')
add_pageref(p, 'fig_architecture', '5')
```

### 7.5 验证

```python
def verify_seq_pageref(doc):
    """扫描 document.xml 找 SEQ / PAGEREF instrText。"""
    seq_count = 0
    pageref_count = 0
    for p in doc.paragraphs:
        for run in p.runs:
            for child in run._element:
                if child.tag.endswith('instrText'):
                    text = child.text or ''
                    if 'SEQ' in text:
                        seq_count += 1
                    if 'PAGEREF' in text:
                        pageref_count += 1
    return {'seq': seq_count, 'pageref': pageref_count}
```

### 7.6 缓存值

freshly-added SEQ field **无 evaluated cached number**，必须让 Word 重算（python-docx 不能算）。设置 `updateFields=true` 让 Word 打开时自动重算。

---

## 8. Academic 增量 Gates 4-5

继承 docx Gates 1-3（schema / token leak / visual audit），加：

### Gate 4 — citation round-trip

in-text citation 数 ≤ reference list 条目数。

```python
def check_citation_round_trip(doc, in_text_pattern, reference_count):
    """简单验证：扫描 in-text citation 模式数应 ≤ reference 条目数。"""
    import re
    in_text_count = 0
    for p in doc.paragraphs:
        in_text_count += len(re.findall(in_text_pattern, p.text))
    return in_text_count <= reference_count, f'in_text={in_text_count}, refs={reference_count}'

# APA: r'\([A-Z][a-z]+, \d{4}\}'
# IEEE: r'\[\d+\]'
```

### Gate 5a — SEQ presence + cached numbers distinct

```python
def check_seq_distinct(doc):
    """验证 SEQ Figure / SEQ Table 各自的 cached value 不重复。"""
    # 见 7.5 验证函数
    # 然后从 separate 后的 cached run 中提取数字，检查 distinct
    pass
```

### Gate 5b — Visual audit via HTML preview（MANDATORY）

人工检查（或转 HTML 后自动检查）：

- 页面顺序正确
- Abstract 恰好出现一次
- Figure N 编号 distinct
- 公式渲染为 math 非 plain-text LaTeX
- IEEE 论文 section 标题 ALL CAPS + Roman
- APA 论文 H1 centered bold unnumbered
- in-text "see Fig. N" 能解析到实际 figure
- Heading 层级视觉可分

---

## 9. Academic-specific pitfalls

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | `\left(...\right)` 带内部 sub/superscript | 用普通 `(...)`, OMML 在 display mode 自动 sizing |
| 2 | Section break 后多 1 paragraph offset | 注意 section break 段落本身不渲染内容 |
| 3 | Multi-column 不自动 revert | 后续 section 显式设 `cols.num=1` |
| 4 | `--type equation` on table cell 被拒 | 公式放在 cell 内的 paragraph，不要直接 on cell |
| 5 | SEQ caption numbers 由 `recalcFields=seq` 填充 | 设 `updateFields=true`，让 Word 重算 |
| 6 | Hanging-indent 写成 `ind.firstLine=-720` | Canonical form：`indent=720 hangingIndent=720`（python-docx 中是 `left_indent=720, first_line_indent=-720`） |
| 7 | Footnote reference runs 在 `view annotated` 显示为空字符串 | 这是渲染器问题，不影响 schema |
| 8 | Caption placement — Table caption 在表下方 | Table caption 在表**上方**；Figure caption 在图**下方** |
| 9 | 引用风格混用 | 全文档统一一种 |
| 10 | Abstract 缺失或不在首页 | Abstract / Keywords / Affiliation 是首页三件套 |
