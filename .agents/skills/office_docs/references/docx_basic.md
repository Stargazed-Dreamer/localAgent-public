# 基础元素参考 (basic.md)

本文件是 `office_docx` skill 的基础元素参考，涵盖 .docx 生成中常用的 python-docx + oxml 模式。所有代码示例均基于纯 Python（python-docx + lxml），不依赖 JS 库或 LibreOffice。

---

## 1. Mental Model

.docx 文件本质是 ZIP 压缩包，包含一组 XML 文件：

| XML 文件 | 用途 |
|---------|------|
| `word/document.xml` | 主文档正文 |
| `word/styles.xml` | 段落/字符/表格样式定义 |
| `word/numbering.xml` | 列表编号定义（abstractNum + num） |
| `word/header1.xml` `header2.xml` ... | 不同 section 的页眉 |
| `word/footer1.xml` `footer2.xml` ... | 不同 section 的页脚 |
| `word/comments.xml` | 批注 |
| `word/footnotes.xml` | 脚注 |
| `word/endnotes.xml` | 尾注 |
| `word/_rels/document.xml.rels` | 关系（图片、超链接、header/footer 引用） |
| `[Content_Types].xml` | MIME 类型声明 |

### 三层逃生舱

python-docx 提供 3 个抽象层级，按需下钻：

- **L1 — 高层 API**：`Document` / `Paragraph` / `Run` / `Table` / `Section` / `InlineShape` 等对象，覆盖 80% 常见场景。
- **L2 — dotted-attr / oxml**：通过 `paragraph._element` / `run._element` / `cell._tc` 等拿到 lxml Element，使用 `docx.oxml.ns.qn()` 解析 OOXML 标签名进行操作。覆盖 East Asia 字体、fields、SDT、custom XML 等。
- **L3 — lxml raw XML**：直接用 `lxml.etree` 构造或修改 XML 节点，用于 L2 也不暴露的边界场景（如自定义 `w:rPr` 子元素顺序、namespaces 操作）。

**原则**：先用 L1；L1 不可达时下钻到 L2；L2 也不可达时再用 L3。每下一层，验证成本翻倍。

---

## 2. Chinese Font Pattern（关键）

python-docx 的 `run.font.name = 'Microsoft YaHei'` **只设置** `w:rFonts` 的 `w:ascii` / `w:hAnsi` 属性，**不设置** `w:eastAsia`。结果：中文字符在 Word 中可能用默认字体（如 SimSun 或 Calibri）渲染，与设置不符。

必须用 oxml 显式设置 `w:eastAsia`：

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def set_run_fonts(run, ascii_font='Microsoft YaHei', east_asia_font='Microsoft YaHei'):
    """同时设置 ascii / hAnsi / eastAsia 字体，确保中英文都用指定字体渲染。"""
    run.font.name = ascii_font  # 设置 w:ascii 和 w:hAnsi
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), east_asia_font)
    # 可选：设置 hint
    rFonts.set(qn('w:hint'), 'eastAsia')

# 用法
doc = Document()
p = doc.add_paragraph()
run = p.add_run('中文 English 混排')
set_run_fonts(run, 'Microsoft YaHei', 'Microsoft YaHei')
```

### 验证字体设置

```python
def verify_east_asia_font(run, expected='Microsoft YaHei'):
    rPr = run._element.find(qn('w:rPr'))
    if rPr is None:
        return False
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        return False
    return rFonts.get(qn('w:eastAsia')) == expected
```

### 整文档字体设置（Default Style）

```python
def set_default_east_asia_font(doc, font_name='Microsoft YaHei'):
    """修改 Normal 样式，使整文档默认应用 eastAsia 字体。"""
    style = doc.styles['Normal']
    style.font.name = font_name
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), font_name)
```

---

## 3. Element 1: Paragraph / Run / Style

### 字号规范

| 元素 | 字号 | 备注 |
|------|------|------|
| H1 | ≥ 18pt（长报告 20pt） | 主章节标题 |
| H2 | 14pt 粗体 | 二级标题 |
| H3 | 12pt 粗体 | 三级标题 |
| Body | 11-12pt | 正文 |
| 行距 | 1.15-1.5x | `WD_LINE_SPACING.MULTIPLE` |

### 缩进规范

```python
from docx.shared import Pt, Twips

# 首行缩进 0.25" (360 twips)
para.paragraph_format.first_line_indent = Twips(360)

### 段落整体缩进 0.5" (720 twips)
para.paragraph_format.left_indent = Twips(720)

# 悬挂缩进 0.5"（学术参考文献 hanging indent）
para.paragraph_format.left_indent = Twips(720)
para.paragraph_format.first_line_indent = Twips(-720)  # 负值实现 hanging
```

### 反模式

1. **空段落造间距**：`add_paragraph('')` × N 来增加段间空白。**正确做法**：用 `paragraph_format.space_before` / `space_after`。
2. **前导空格缩进**：`add_paragraph('    ' + text)`。**正确做法**：用 `first_line_indent`。
3. **下段落继承前 Heading 样式**：在 `add_heading()` 之后未显式指定 `add_paragraph(text, style='Normal')`，可能继承 Heading 样式。**正确做法**：显式 `style='Normal'`。

### 完整段落示例

```python
from docx import Document
from docx.shared import Pt, Twips, RGBColor
from docx.enum.text import WD_LINE_SPACING

doc = Document()

# 设置 Normal 样式
normal = doc.styles['Normal']
normal.font.name = 'Microsoft YaHei'
normal.font.size = Pt(11)
# East Asia 字体
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
rPr = normal.element.get_or_add_rPr()
rFonts = rPr.find(qn('w:rFonts'))
if rFonts is None:
    rFonts = OxmlElement('w:rFonts')
    rPr.append(rFonts)
rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')

# 正文段落
p = doc.add_paragraph(style='Normal')
p.paragraph_format.space_before = Pt(0)
p.paragraph_format.space_after = Pt(6)
p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
p.paragraph_format.line_spacing = 1.15
p.paragraph_format.first_line_indent = Twips(360)  # 0.25"
run = p.add_run('这是一段正文。中英文混排：Hello World。')
```

---

## 4. Element 2: Tables

### 基础创建

```python
from docx.shared import Inches

table = doc.add_table(rows=3, cols=3)
table.style = 'Table Grid'
table.autofit = False
table.width = Inches(6)

# 单元格填充
for row in table.rows:
    for cell in row.cells:
        cell.text = '...'
        # 或：cell.paragraphs[0].add_run('...')
```

### 填充表头单元格顺序（关键）

**顺序**：先填文本 → 再设 fill → 再设 run 格式（空单元格 set fill 可能 schema error）

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import RGBColor, Pt

def set_cell_fill(cell, color_hex):
    """设置单元格背景色。color_hex 形如 '2E7D32'（不带 #）。"""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), color_hex)
    tcPr.append(shd)

# 正确顺序：先文本，再 fill，再 run 格式
header_cell = table.rows[0].cells[0]
header_cell.text = '项目名称'  # 1. 先文本
set_cell_fill(header_cell, '2E7D32')  # 2. 再 fill
# 3. 再设置 run 格式
for paragraph in header_cell.paragraphs:
    for run in paragraph.runs:
        run.font.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        run.font.size = Pt(11)
```

### 反模式

1. **单行表格做水平分割线**：`add_table(rows=1, cols=1)` 模拟 `<hr>`。**正确做法**：用 `paragraph_format.border_bottom`。
2. **单元格格式设在行级**：`row.cells[0].text = ...` 设置后想改 run 颜色却改了 `row`。**正确做法**：永远在 cell 的 paragraph/run 上设。
3. **`c1="a\nb"` 单段落换行**：`cell.text = "line1\nline2"` 只会渲染为单段落内的换行符。**正确做法**：`cell.add_paragraph('line2')` 添加多段落。

---

## 5. Element 3: Lists / Numbering

### 单层 bullet

```python
doc.add_paragraph('第一项', style='List Bullet')
doc.add_paragraph('第二项', style='List Bullet')
```

### 单层 number

```python
doc.add_paragraph('第一项', style='List Number')
doc.add_paragraph('第二项', style='List Number')
```

### 多层编号（1 / 1.1 / 1.1.1）

python-docx **不原生支持**多层列表的创建，必须用 oxml 在 `numbering.xml` 中定义 `abstractNum` + `num` 引用：

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def create_multi_level_numbering(doc):
    """创建多级编号定义（1. / 1.1 / 1.1.1），返回 numId。"""
    numbering = doc.part.numbering_part.numbering_definitions._numbering
    # 创建 abstractNum
    abstract_num = OxmlElement('w:abstractNum')
    abstract_num.set(qn('w:abstractNumId'), '0')
    # 多级定义
    for level in range(3):
        lvl = OxmlElement('w:lvl')
        lvl.set(qn('w:ilvl'), str(level))
        # 编号格式
        numFmt = OxmlElement('w:numFmt')
        numFmt.set(qn('w:val'), 'decimal')
        lvl.append(numFmt)
        # 显示文本：%1. / %1.%2. / %1.%2.%3.
        lvlText = OxmlElement('w:lvlText')
        pattern = '.'.join(['%{}'.format(i+1) for i in range(level+1)]) + '.'
        lvlText.set(qn('w:val'), pattern)
        lvl.append(lvlText)
        # 缩进
        pPr = OxmlElement('w:pPr')
        ind = OxmlElement('w:ind')
        ind.set(qn('w:left'), str(720 * (level + 1)))
        ind.set(qn('w:hanging'), '360')
        pPr.append(ind)
        lvl.append(pPr)
        abstract_num.append(lvl)
    numbering.append(abstract_num)
    # 创建 num 引用
    num = OxmlElement('w:num')
    num.set(qn('w:numId'), '1')
    abstract_num_id_ref = OxmlElement('w:abstractNumId')
    abstract_num_id_ref.set(qn('w:val'), '0')
    num.append(abstract_num_id_ref)
    numbering.append(num)
    return 1  # numId

def add_numbered_paragraph(doc, text, level=0, num_id=1):
    """添加多级编号段落。level=0 是 1.，level=1 是 1.1，level=2 是 1.1.1。"""
    p = doc.add_paragraph(text)
    pPr = p._element.get_or_add_pPr()
    numPr = OxmlElement('w:numPr')
    ilvl = OxmlElement('w:ilvl')
    ilvl.set(qn('w:val'), str(level))
    numPr.append(ilvl)
    numId = OxmlElement('w:numId')
    numId.set(qn('w:val'), str(num_id))
    numPr.append(numId)
    pPr.append(numPr)
    return p
```

---

## 6. Element 4: Tab Stops

签名行 / 目录页码行 / 表格内对齐数字。

```python
from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER

# 右对齐 tab + 点线 leader（目录行）
p = doc.add_paragraph()
p.paragraph_format.tab_stops.add_tab_stop(
    Inches(6),
    WD_TAB_ALIGNMENT.RIGHT,
    WD_TAB_LEADER.DOTS,
)
run = p.add_run('第一章 引言\t1')  # \t 触发 leader 渲染
```

### 反模式

**leader=dot 单独不渲染点线**：必须用 `\t` 触发。如果只设了 `tab_stops` 但段落文本没有 `\t`，点线不会出现。

---

## 7. Element 5: Fields

python-docx **没有原生 field API**，必须用 oxml 注入 `<w:fldChar>` 五元组：

```
begin → instrText → separate → cached value → end
```

### 通用 field 注入函数

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_field(paragraph, field_code, cached_value=''):
    """在段落末尾注入一个 field。
    
    field_code 例如：
      'PAGE \\* MERGEFORMAT'           当前页码
      'NUMPAGES \\* MERGEFORMAT'       总页数
      'DATE \\@ "yyyy-MM-dd"'          日期
      'MERGEFIELD CustomerName'        邮件合并字段
      'REF bookmarkName'               书签引用
      'SEQ Figure'                     Figure 自动编号
      'TOC \\o "1-3" \\h \\z \\u'      目录
    """
    run_begin = paragraph.add_run()
    fldChar_begin = OxmlElement('w:fldChar')
    fldChar_begin.set(qn('w:fldCharType'), 'begin')
    run_begin._element.append(fldChar_begin)

    run_instr = paragraph.add_run()
    instrText = OxmlElement('w:instrText')
    instrText.set(qn('xml:space'), 'preserve')
    instrText.text = ' ' + field_code + ' '
    run_instr._element.append(instrText)

    run_sep = paragraph.add_run()
    fldChar_sep = OxmlElement('w:fldChar')
    fldChar_sep.set(qn('w:fldCharType'), 'separate')
    run_sep._element.append(fldChar_sep)

    run_cached = paragraph.add_run()
    if cached_value:
        run_cached.text = cached_value
    # else: 空缓存，Word 打开后会自动重算

    run_end = paragraph.add_run()
    fldChar_end = OxmlElement('w:fldChar')
    fldChar_end.set(qn('w:fldCharType'), 'end')
    run_end._element.append(fldChar_end)

# 用法：当前页码
p = doc.add_paragraph()
add_field(p, 'PAGE \\* MERGEFORMAT', '1')
```

### 反模式

**把 `{{customer_name}}` 当 body 文本写**：

```python
# 错误
doc.add_paragraph('客户：{{customer_name}}')

# 正确
p = doc.add_paragraph('客户：')
add_field(p, 'MERGEFIELD CustomerName', '')
```

---

## 8. Element 6: Headers & Footers

### 基础页眉

```python
section = doc.sections[0]
header = section.header
header.is_linked_to_previous = False
header.paragraphs[0].text = '公司名称 | 项目周报'
header.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
```

### 不同首页

```python
section.different_first_page_header_footer = True
first_header = section.first_page_header
first_header.paragraphs[0].text = '封面页 - 不显示页码'
```

### Page X of Y 复合 footer

```python
def add_page_x_of_y_footer(section):
    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # "Page " 文本
    p.add_run('Page ')
    # PAGE field
    add_field(p, 'PAGE \\* MERGEFORMAT', '1')
    # " of " 文本
    p.add_run(' of ')
    # NUMPAGES field
    add_field(p, 'NUMPAGES \\* MERGEFORMAT', '1')
```

每个 `add_field` 调用注入 5 个 run（begin/instrText/separate/cached/end），所以复合 "Page X of Y" 总共 ≥ 11 个 run。

---

## 9. Element 7: TOC

### 前置条件

文档必须有真实 TOC 源（built-in `Heading 1` / `Heading 2` / `Heading 3` 样式，或带 `outlineLvl` 的自定义段落样式）。

### 反模式

没有 TOC 源时插入 TOC field → Word 打开后渲染 "Error! No table of contents entries found."

### 插入 TOC

```python
def add_toc(doc):
    """插入 TOC field，updateFields=true 让 Word 打开时重算。"""
    p = doc.add_paragraph()
    # TOC field
    add_field(p, 'TOC \\o "1-3" \\h \\z \\u', '右键点击此处选择"更新域"以生成目录')
    
    # 设置 updateFields=true（settings.xml）
    settings = doc.settings.element
    update_fields = OxmlElement('w:updateFields')
    update_fields.set(qn('w:val'), 'true')
    settings.append(update_fields)
```

---

## 10. Element 8: Images

### 基础插入

```python
doc.add_picture('image.png', width=Inches(5))
```

### Alt 文本（关键）

```python
def add_picture_with_alt(doc, image_path, width_inches, alt_text):
    """添加图片并设置 alt 文本（无障碍）。"""
    doc.add_picture(image_path, width=Inches(width_inches))
    # 拿到最后插入的 inline shape
    inline_shape = doc.inline_shapes[-1]
    inline_shape.alt_text = alt_text  # python-docx 0.8.11+ 支持
    # 兜底：直接 oxml 设 <wp:docPr> 的 descr
    if not inline_shape.alt_text:
        inline = inline_shape._inline
        docPr = inline.find(qn('wp:docPr'))
        if docPr is not None:
            docPr.set('descr', alt_text)
```

### 反模式

**缺 alt 文本**：屏幕阅读器无法识别图片内容。所有图片必须设 `alt_text`。

---

## 11. Element 9: Charts

python-docx **不原生支持** chart 创建。

### 替代方案

用 `openpyxl` 创建 chart，导出为图片，然后嵌入 docx；或用 `python-pptx` 的 chart 功能生成，再截图嵌入。

```python
# 替代方案：openpyxl 生成 chart → 截图为 PNG → 嵌入 docx
# 这是 OfficeCli 无此内容，本项目独立设计的临时方案。
# 长期方案：等待 python-docx 添加 chart 支持，或自己用 oxml 注入 <w:drawing> + <c:chart>
```

### 反模式

**用 PNG 截图代替原生 chart**：丧失数据可编辑性。如果用户需要后续修改数据，应保留源 Excel 文件。

---

## 12. Element 10: Hyperlinks & Bookmarks

### 外部超链接

```python
def add_hyperlink(paragraph, url, text):
    """添加外部超链接。"""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    part = paragraph.part
    r_id = part.relate_to(url, RT.HYPERLINK, is_external=True)
    
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    # 蓝色 + 下划线
    color = OxmlElement('w:color')
    color.set(qn('w:val'), '2E7D32')  # 用主色绿代替默认蓝
    rPr.append(color)
    underline = OxmlElement('w:u')
    underline.set(qn('w:val'), 'single')
    rPr.append(underline)
    new_run.append(rPr)
    
    text_elem = OxmlElement('w:t')
    text_elem.text = text
    text_elem.set(qn('xml:space'), 'preserve')
    new_run.append(text_elem)
    
    hyperlink.append(new_run)
    paragraph._element.append(hyperlink)
```

### 内部超链接（bookmark）

```python
def add_bookmark(paragraph, bookmark_name, bookmark_id):
    """在段落上添加 bookmark。"""
    start = OxmlElement('w:bookmarkStart')
    start.set(qn('w:id'), str(bookmark_id))
    start.set(qn('w:name'), bookmark_name)
    paragraph._element.insert(0, start)
    
    end = OxmlElement('w:bookmarkEnd')
    end.set(qn('w:id'), str(bookmark_id))
    paragraph._element.append(end)

def add_internal_link(paragraph, bookmark_name, text):
    """添加指向 bookmark 的内部超链接。"""
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('w:anchor'), bookmark_name)
    
    new_run = OxmlElement('w:r')
    text_elem = OxmlElement('w:t')
    text_elem.text = text
    text_elem.set(qn('xml:space'), 'preserve')
    new_run.append(text_elem)
    hyperlink.append(new_run)
    
    paragraph._element.append(hyperlink)
```

---

## 13. Element 11: Sections / Page Setup / Page Breaks

### 页面尺寸与边距

```python
section = doc.sections[0]
section.page_width = Inches(8.5)   # Letter
section.page_height = Inches(11)
section.top_margin = Inches(1)
section.bottom_margin = Inches(1)
section.left_margin = Inches(1)
section.right_margin = Inches(1)
```

### 多列布局

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def set_columns(section, count=2, spacing_inches=0.5):
    """设置多列布局。python-docx 无原生 API，需 oxml。"""
    sectPr = section._sectPr
    cols = sectPr.find(qn('w:cols'))
    if cols is None:
        cols = OxmlElement('w:cols')
        sectPr.append(cols)
    cols.set(qn('w:num'), str(count))
    cols.set(qn('w:space'), str(int(spacing_inches * 1440)))  # twips
```

### Page breaks

```python
# 方式 1：pageBreakBefore 在 heading 上
h = doc.add_heading('第二章', level=1)
h.paragraph_format.page_break_before = True

# 方式 2：explicit pagebreak
from docx.enum.text import WD_BREAK
doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
```

### 反模式

**两种 page break 机制叠加**：在 heading 上同时设 `page_break_before = True` 又在前面插入 explicit pagebreak → 多出一页空白。**只用一种**。

---

## 14. Element 12: Equations / Footnotes / Comments / Tracked Changes / Watermark

### Equations

python-docx 无原生 API。需 oxml 注入 OMML（Office Math Markup Language），详见 `academic_paper.md`。

### Comments

```python
def add_comment(doc, paragraph, comment_text, author='Reviewer'):
    """给段落添加批注。需要操作 comments.xml part。"""
    # 这个比较复杂，需要创建 comments part + 在 document.xml 中插入 <w:commentRangeStart/End> + <w:commentReference>
    # 简化模式（实际实现需扩展 python-docx 的 part 系统）
    pass
```

完整实现需扩展 python-docx 的 part 系统。**这是 OfficeCli 无原生 API 的内容，本项目独立设计**。

### Tracked Changes

```python
def add_inserted_run(paragraph, text, author='Editor', date='2024-01-01T00:00:00Z'):
    """插入 tracked-changes 的 <w:ins> 标记 run。"""
    ins = OxmlElement('w:ins')
    ins.set(qn('w:id'), '1')
    ins.set(qn('w:author'), author)
    ins.set(qn('w:date'), date)
    
    run = OxmlElement('w:r')
    text_elem = OxmlElement('w:t')
    text_elem.text = text
    text_elem.set(qn('xml:space'), 'preserve')
    run.append(text_elem)
    ins.append(run)
    
    paragraph._element.append(ins)

def add_deleted_run(paragraph, text, author='Editor', date='2024-01-01T00:00:00Z'):
    """插入 tracked-changes 的 <w:del> 标记 run。"""
    dele = OxmlElement('w:del')
    dele.set(qn('w:id'), '2')
    dele.set(qn('w:author'), author)
    dele.set(qn('w:date'), date)
    
    run = OxmlElement('w:r')
    del_text = OxmlElement('w:delText')
    del_text.text = text
    del_text.set(qn('xml:space'), 'preserve')
    run.append(del_text)
    dele.append(run)
    
    paragraph._element.append(dele)
```

### Watermark

```python
def add_watermark(section, text='CONFIDENTIAL'):
    """在 section 的 header 中注入水印（<v:shape>）。"""
    header = section.header
    p = header.paragraphs[0]
    
    run = p.add_run()
    pict = OxmlElement('w:pict')
    shape = OxmlElement('v:shape')
    shape.set('id', 'PowerPlusWaterMarkObject')
    shape.set('o:spid', '_x0000_s2049')
    shape.set('type', '#_x0000_t136')
    shape.set('style', 'position:absolute;margin-left:0;margin-top:0;width:400pt;height:200pt;z-index:-251654144;mso-wrap-edited:f;mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;mso-position-vertical-relative:margin')
    shape.set('o:allowincell', 'f')
    shape.set('fillcolor', '#BFBFBF')
    shape.set('stroked', 'f')
    
    textpath = OxmlElement('v:textpath')
    textpath.set('style', 'font-family:"Calibri";font-size:1pt;v-text-kern:t')
    textpath.set('trim', 't')
    textpath.set('fitpath', 't')
    textpath.set('string', text)
    shape.append(textpath)
    
    pict.append(shape)
    run._element.append(pict)
```

---

## 15. Validation Checklist

```python
import docx

def validate_docx(path):
    """Gate 1: schema 验证。重开不报错。"""
    try:
        d = docx.Document(path)
    except Exception as e:
        return False, f'schema error: {e}'
    
    # 抽 2-3 个段落
    for i, p in enumerate(d.paragraphs[:3]):
        if not p.text and not p.runs:
            return False, f'段落 {i} 为空且无 runs'
    
    # 检查中文字体
    for p in d.paragraphs:
        for run in p.runs:
            rPr = run._element.find(qn('w:rPr'))
            if rPr is not None:
                rFonts = rPr.find(qn('w:rFonts'))
                if rFonts is not None:
                    east_asia = rFonts.get(qn('w:eastAsia'))
                    if east_asia and east_asia not in ['Microsoft YaHei', 'SimSun', 'SimHei']:
                        # 字体不在白名单不一定是错，但应该警告
                        pass
    
    # 检查 fields 有 fldChar 子元素
    field_count = 0
    for p in d.paragraphs:
        for run in p.runs:
            fldChars = run._element.findall(qn('w:fldChar'))
            field_count += len(fldChars)
    
    return True, f'OK, fields={field_count}'
```

---

## 16. Common Pitfalls 表

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | `--index` vs `[N]` 索引混乱（python-docx 用 0-based） | 永远用 0-based 索引：`table.rows[0].cells[0]` |
| 2 | 空段落造间距 | `paragraph_format.space_before` / `space_after` |
| 3 | 行级 set 设单元格格式 | 设在 cell 的 paragraph/run |
| 4 | `listStyle` 设在 run 上 | 设在 paragraph：`doc.add_paragraph(text, style='List Bullet')` |
| 5 | 用前导空格缩进 | `paragraph_format.first_line_indent` |
| 6 | Cover 页码抑制失败 | `section.different_first_page_header_footer = True` + `first_page_footer` 留空 |
| 7 | 单 cell 多 bullet 段落 | 多 `cell.add_paragraph(text, style='List Bullet')` 到 cell |
| 8 | `cell.text = "a\nb"` 期望多段落 | 实际单段落单换行；要多段落用 `cell.add_paragraph()` |
| 9 | 中文字体不生效 | 用 oxml 设 `w:rFonts` 的 `w:eastAsia` |
| 10 | `{{var}}` 当 body 文本写 | 用 MERGEFIELD field |
| 11 | 两种 page break 机制叠加 | 只用 `pageBreakBefore` OR explicit pagebreak |
| 12 | Table caption 放表下方 | Table caption 在表**上方**，Figure caption 在图**下方** |
| 13 | leader=dot 但无 `\t` | tab_stops + `\t` 触发点线 |
| 14 | TOC 源不存在却插 TOC | 先确保有 Heading 样式段落 |
| 15 | `different_first_page_header_footer = True` 但忘了 first_page_footer | 显式设置 first_page_header / first_page_footer |
