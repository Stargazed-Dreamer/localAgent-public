# PDF 研究报告样式参考

本文件是 [deep_research skill](file:///f:/<project_root>/.agents/skills/deep_research/SKILL.md) 的子参考，仅当 `office_pdf` skill 默认能力不满足研究报告需求时参考。

## 何时读本文件

- 默认情况：**不需要读本文件**，参考 [.agents/skills/office_pdf/references/basic.md](file:///f:/<project_root>/.agents/skills/office_pdf/references/basic.md) 用 reportlab 现场实现即可
- 需要封面页、特定配色、研究报告专用排版时：读本文件，用 `exec_python` 调 reportlab 现场实现

## office_pdf skill 默认能力（优先用这个）

参考 [.agents/skills/office_pdf/references/basic.md](file:///f:/<project_root>/.agents/skills/office_pdf/references/basic.md) 用 `exec_python` 调 reportlab 现场实现，输入 `workspace/deep_research/{研究对象}_研究报告_{YYYYMMDD}.md`，输出 `workspace/deep_research/{研究对象}_研究报告_{YYYYMMDD}.pdf`。

详见 [office_pdf references/basic.md](file:///f:/<project_root>/.agents/skills/office_pdf/references/basic.md)。

## 研究报告专用样式（需要封面页时）

如果需要封面页（标题 + 副标题 + 作者 + 日期），参考以下 reportlab Platypus 实现。脚本写到 `temp/deep_research_pdf_{日期}.py`，不要写到 skill 目录。

### 设计规范

- **页面**：A4，页边距上 25mm / 左右 20mm / 下 20mm
- **封面页**：标题（28pt 深蓝 #1a5276）+ 副标题"深度研究报告" + 元信息行（研究时间/领域/类型）+ 作者 + 装饰分隔线，首页后分页
- **配色**：H1 = #1a5276 深蓝、H2 = #1e8449 绿色、H3 = #2e86c1 浅蓝、H4 = #5b2c6f 紫色、正文 = #2c3e50 深灰
- **字体**：中英文混排，中文用系统 TTF（如 `C:\Windows\Fonts\msyh.ttc` 微软雅黑或 `simsun.ttc` 宋体），英文用 Helvetica
- **正文**：10.5pt，行距 1.75，两端对齐，孤行/寡行控制（orphans=3, widows=3）
- **引用块**：左侧 3pt 深蓝竖线 + 浅灰背景
- **表格**：全宽、深蓝表头白字、斑马纹行（nth-child(even) 浅灰背景）
- **页眉**：「报告标题 | 深度研究报告」（首页不显示）
- **页脚**：「第 X 页」（首页不显示）
- **Markdown 解析**：第一个 H1 自动提取为封面标题，正文中不重复出现

### 关键 reportlab 代码片段

```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# 注册中文字体（关键，否则中文显示为黑方块）
pdfmetrics.registerFont(TTFont('MSYH', r'C:\Windows\Fonts\msyh.ttc'))
pdfmetrics.registerFont(TTFont('MSYH-Bold', r'C:\Windows\Fonts\msyhbd.ttc'))

# 封面页样式
cover_title_style = ParagraphStyle(
    'CoverTitle', fontName='MSYH-Bold', fontSize=28, textColor=colors.HexColor('#1a5276'),
    alignment=1, spaceAfter=8*mm, leading=36
)
cover_subtitle_style = ParagraphStyle(
    'CoverSubtitle', fontName='MSYH', fontSize=14, textColor=colors.HexColor('#95a5a6'),
    alignment=1, spaceAfter=6*mm
)

# 正文样式
body_style = ParagraphStyle(
    'Body', fontName='MSYH', fontSize=10.5, leading=18,  # 行距 1.75
    textColor=colors.HexColor('#2c3e50'), alignment=4,  # justify
    spaceBefore=1.5*mm, spaceAfter=1.5*mm
)

# 页眉页脚回调
def on_page(canvas, doc):
    canvas.saveState()
    if doc.page > 1:  # 首页不显示
        canvas.setFont('MSYH', 8)
        canvas.setFillColor(colors.HexColor('#95a5a6'))
        canvas.drawCentredString(A4[0]/2, A4[1]-15*mm, f"{title}  |  深度研究报告")
        canvas.drawCentredString(A4[0]/2, 12*mm, f"第 {doc.page} 页")
    canvas.restoreState()

# 构建文档
doc = SimpleDocTemplate(output_path, pagesize=A4,
    topMargin=25*mm, leftMargin=20*mm, rightMargin=20*mm, bottomMargin=20*mm)
story = []
# 封面
story.append(Spacer(1, 120*mm))
story.append(Paragraph(title, cover_title_style))
story.append(Paragraph("深度研究报告", cover_subtitle_style))
story.append(Paragraph(meta_line, cover_subtitle_style))
story.append(PageBreak())
# 正文（markdown 库转 HTML 后再转 Paragraph，或手动解析）
# ...
doc.build(story, onFirstPage=lambda c,d: None, onLaterPages=on_page)
```

### Markdown → reportlab Flowable 的转换

用 `markdown` 库转 HTML，再用 `html.parser` 或 `feedparser` 解析 HTML 标签转成 Paragraph/Table/Spacer 等 Flowable。复杂表格建议直接用 `pdfplumber` 验证生成结果。

> 提示：如果报告里表格很多，直接手写 Flowable 转换器工作量较大。此时优先参考 `office_pdf` skill 的方法论（它已涵盖 markdown 表格→reportlab Table 的转换），本参考的封面页样式作为补充。
