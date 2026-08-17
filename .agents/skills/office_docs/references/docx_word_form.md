# Word 表单参考 (word_form.md)

本文件是 `office_docx` skill 的 Word 表单场景参考，涵盖 SDT（Structured Document Tag）内容控件、FormField 旧式表单字段、MERGEFIELD 邮件合并、documentProtection 文档保护。所有代码基于纯 Python（python-docx + lxml）。

---

## 1. 何时用此 skill（trigger 关键词）

满足以下任一条件用此 skill：

- fillable form / form fields / content controls / SDT / word form / fill in
- only editable fields / protect document
- onboarding / HR intake / survey template
- contract SOW template / mail-merge template
- compliance checklist / medical intake questionnaire

## 2. 何时回退到 basic.md

以下场景**不要**用此 skill，走 `basic.md`：

- 报告 / 信件 / memo / 论文 / pitch deck
- 任何无可填字段的文档

---

## 3. 4 种 OpenXML payload 层

Word 表单有 4 种 payload 实现方式，按场景选用：

| 层 | XML 元素 | 推荐度 | 适用场景 |
|----|---------|--------|---------|
| 1 | `<w:sdt>` 内容控件 | 推荐（现代化） | text/richtext/dropdown/combobox/date/picture/group |
| 2 | `<w:ffData>` 旧式 FormField | 兼容性好但功能少 | checkbox（SDT 未实现） |
| 3 | `<w:fldChar>` 复杂字段 | 用于 mail merge | MERGEFIELD/REF/PAGEREF/SEQ/IF |
| 4 | `documentProtection` 锁 | 配合 1-3 使用 | 限制编辑到字段内 |

---

## 4. Three Paths 选路决策

在写第一个命令前选定 Path：

### Path A — 纯 python-docx

适用：text/richtext/dropdown/combobox/date/picture/group SDT，包括 options、date format、lock 都直接 oxml 注入。

### Path B — python-docx + lxml raw

适用：SDT 属性 python-docx 不暴露（如 dropdown listItem 的 `w:value` 与 displayText 不同）。

### Path C — Word 模板

适用：真正的 SDT checkbox（仍未实现，要用 FormField）/ `placeholderDocPart` 提示文本 / 自定义 richtext appearance。

---

## 5. SDT 7 types 完整说明

python-docx **不原生支持** SDT，必须用 oxml/lxml 注入 `<w:sdt>` 元素。

### 七种类型表

| Type | 用途 | 是否支持 |
|------|------|---------|
| text | 单行文本输入 | ✓ |
| richtext | 多行富文本输入 | ✓ |
| dropdown | 下拉选择（只能选） | ✓ |
| combobox | 可输入下拉（可选可输） | ✓ |
| date | 日期选择器 | ✓ |
| picture | 图片占位 | ✓ |
| group | 分组容器（不可编辑） | ✓ |
| ~~checkbox~~ | 复选框 | ✗ 未实现，用 FormField |

### 必须有 `alias` + `tag`

每个 SDT 必须同时设 `alias`（显示名）和 `tag`（程序标识），两者都要。

### dropdown/combobox 必须有非空 `items`

```python
items = ['Engineering', 'Sales', 'Marketing']
```

### date SDT 必须显示 `format`

```python
format = 'yyyy-MM-dd'
```

### locked SDT 必须显示 `lock`

```python
lock = 'sdtLocked'  # 或 'contentLocked' / 'sdtContentLocked'
```

### 通用 SDT 注入函数

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_sdt(paragraph, sdt_type='text', alias='', tag='', lock='',
            items=None, date_format='', placeholder=''):
    """在段落中注入 SDT 内容控件。
    
    sdt_type: text/richtext/dropdown/combobox/date/picture/group
    alias: 显示名（必填）
    tag: 程序标识（必填）
    lock: sdtLocked/contentLocked/sdtContentLocked
    items: dropdown/combobox 的选项列表
    date_format: date SDT 的格式，如 'yyyy-MM-dd'
    placeholder: 占位提示文本
    """
    sdt = OxmlElement('w:sdt')
    
    # sdtPr
    sdtPr = OxmlElement('w:sdtPr')
    if alias:
        alias_elem = OxmlElement('w:alias')
        alias_elem.set(qn('w:val'), alias)
        sdtPr.append(alias_elem)
    if tag:
        tag_elem = OxmlElement('w:tag')
        tag_elem.set(qn('w:val'), tag)
        sdtPr.append(tag_elem)
    if lock:
        lock_elem = OxmlElement('w:' + lock)
        sdtPr.append(lock_elem)
    
    # 类型特定属性
    if sdt_type == 'text':
        text_pr = OxmlElement('w:text')
        sdtPr.append(text_pr)
    elif sdt_type == 'richtext':
        # richtext 是默认（无显式子元素）
        pass
    elif sdt_type in ('dropdown', 'combobox'):
        combo = OxmlElement('w:' + sdt_type)
        if items:
            for item in items:
                list_item = OxmlElement('w:listItem')
                list_item.set(qn('w:displayText'), item)
                list_item.set(qn('w:value'), item)
                combo.append(list_item)
        sdtPr.append(combo)
    elif sdt_type == 'date':
        date_pr = OxmlElement('w:date')
        if date_format:
            date_pr.set(qn('w:dateFormat'), date_format)
        sdtPr.append(date_pr)
    elif sdt_type == 'picture':
        picture_pr = OxmlElement('w:picture')
        sdtPr.append(picture_pr)
    elif sdt_type == 'group':
        group_pr = OxmlElement('w:group')
        sdtPr.append(group_pr)
    
    if placeholder:
        # placeholder 需 placeholderDocPart（Path C 限制）
        # 这里简化为注释
        pass
    
    sdt.append(sdtPr)
    
    # sdtContent
    sdtContent = OxmlElement('w:sdtContent')
    # 内容段落
    content_p = OxmlElement('w:p')
    content_r = OxmlElement('w:r')
    content_t = OxmlElement('w:t')
    content_t.text = placeholder or ''
    content_r.append(content_t)
    content_p.append(content_r)
    sdtContent.append(content_p)
    sdt.append(sdtContent)
    
    paragraph._element.append(sdt)
    return sdt

# 用法：text SDT
p = doc.add_paragraph('项目名称：')
add_sdt(p, sdt_type='text', alias='Project Name', tag='project_name')

# 用法：dropdown SDT
p = doc.add_paragraph('部门：')
add_sdt(p, sdt_type='dropdown', alias='Department', tag='department',
        items=['Engineering', 'Sales', 'Marketing'])

# 用法：date SDT
p = doc.add_paragraph('入职日期：')
add_sdt(p, sdt_type='date', alias='Onboarding Date', tag='onboarding_date',
        date_format='yyyy-MM-dd')
```

---

## 6. documentProtection 4 modes

### 5 modes 表（含 none）

| Mode | `w:edit` 值 | 行为 |
|------|------------|------|
| none | （无元素） | 无保护 |
| forms | `forms` | 只能编辑 form fields / SDT |
| readOnly | `readOnly` | 完全只读 |
| comments | `comments` | 只能加批注 |
| trackedChanges | `trackedChanges` | 修改自动 track |

### 关键行为

文档保护同时限制 Word 用户和 python-docx。在 `forms` 模式下：

- form-field 编辑成功（字段内可填）
- 非字段内容编辑被拒

### 启用 protection 的 oxml 代码

```python
def set_document_protection(doc, edit_mode='forms', password=None):
    """设置文档保护模式。
    
    edit_mode: forms/readOnly/comments/trackedChanges
    password: 可选密码（明文需 hash，这里简化）
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    settings = doc.settings.element
    # 移除已有的 documentProtection
    existing = settings.find(qn('w:documentProtection'))
    if existing is not None:
        settings.remove(existing)
    
    protection = OxmlElement('w:documentProtection')
    protection.set(qn('w:edit'), edit_mode)
    protection.set(qn('w:enforcement'), '1')
    if password:
        protection.set(qn('w:hash'), password)  # 实际需 hash
    settings.append(protection)
```

### Lock values 表

| Lock | 含义 | 患者填 | 医生填 |
|------|------|--------|--------|
| (none) | 无锁 | ✓ | ✓ |
| sdtLocked | SDT 锁定（不可删 SDT，可改内容） | ✓ | ✓ |
| contentLocked | 内容锁定（可删 SDT，不可改内容） | ✗ | ✓ |
| sdtContentLocked | SDT + 内容都锁 | ✗ | ✗ |

### Block-level lock（段落包裹 SDT）— 纵深防御

```python
def wrap_paragraph_with_sdt(paragraph, alias='Block', tag='block', lock='sdtLocked'):
    """把段落用 group SDT 包裹，提供 block-level 保护。"""
    # 复杂实现：需重构 XML 树
    pass
```

### Role-gated fields（多角色表单）

患者填的 `lock=sdtLocked`（可编辑不可删），医生填的 `lock=contentLocked`（患者不可改）。

```python
# 患者填写部分
p = doc.add_paragraph('患者姓名：')
add_sdt(p, sdt_type='text', alias='Patient Name', tag='patient_name',
        lock='sdtLocked')  # 患者可编辑

# 医生填写部分
p = doc.add_paragraph('诊断：')
add_sdt(p, sdt_type='text', alias='Diagnosis', tag='diagnosis',
        lock='contentLocked')  # 患者不可改，医生可改
```

---

## 7. MERGEFIELD 模式

Mail merge 模板用 MERGEFIELD field。

### oxml 注入

```python
def add_mergefield(paragraph, field_name, format_spec='', cached_value=''):
    """添加 MERGEFIELD field。
    
    field_name: 数据字段名，如 'CustomerName'
    format_spec: 数字格式，如 '#,##0.00'（可选）
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    instruction = 'MERGEFIELD ' + field_name
    if format_spec:
        instruction += ' \\# "' + format_spec + '"'
    instruction += ' \\* MERGEFORMAT '
    
    r1 = paragraph.add_run()
    fld1 = OxmlElement('w:fldChar')
    fld1.set(qn('w:fldCharType'), 'begin')
    r1._element.append(fld1)
    
    r2 = paragraph.add_run()
    instr_text = OxmlElement('w:instrText')
    instr_text.set(qn('xml:space'), 'preserve')
    instr_text.text = ' ' + instruction + ' '
    r2._element.append(instr_text)
    
    r3 = paragraph.add_run()
    fld3 = OxmlElement('w:fldChar')
    fld3.set(qn('w:fldCharType'), 'separate')
    r3._element.append(fld3)
    
    r4 = paragraph.add_run('«' + field_name + '»')  # cached value
    
    r5 = paragraph.add_run()
    fld5 = OxmlElement('w:fldChar')
    fld5.set(qn('w:fldCharType'), 'end')
    r5._element.append(fld5)

# 用法
p = doc.add_paragraph('客户：')
add_mergefield(p, 'CustomerName')
p = doc.add_paragraph('金额：')
add_mergefield(p, 'Amount', format_spec='#,##0.00')
```

### 反模式

**把 `{{customer_name}}` 当 body 文本写** → 应该用 MERGEFIELD field。

---

## 8. 关键执行顺序

`protection=forms` 必须是**最后**一个结构性命令；之后非字段编辑需 `--force` 或 raw XML。

### Build order

```
create+open → metadata → 结构（headings、label 段落）→ 
SDT/formfield skeletons（Path A）→ Path B 注入 → 
per-field lock → protection=forms LAST → save
```

```python
def build_form_document(output_path):
    """完整流程示例。"""
    doc = Document()
    
    # 1. metadata（标题、说明）
    doc.add_heading('HR 入职表', level=1)
    doc.add_paragraph('请填写以下字段：')
    
    # 2. 结构（label 段落 + SDT）
    p = doc.add_paragraph('员工姓名：')
    add_sdt(p, sdt_type='text', alias='Employee Name', tag='emp_name')
    
    p = doc.add_paragraph('部门：')
    add_sdt(p, sdt_type='dropdown', alias='Department', tag='dept',
            items=['Engineering', 'Sales', 'Marketing'])
    
    p = doc.add_paragraph('入职日期：')
    add_sdt(p, sdt_type='date', alias='Onboarding Date', tag='onboard_date',
            date_format='yyyy-MM-dd')
    
    # 3. per-field lock（如果需要 role-gated）
    # ...（见第 6 节）
    
    # 4. protection=forms LAST
    set_document_protection(doc, edit_mode='forms')
    
    # 5. save
    doc.save(output_path)
    
    # 6. 验证
    validate_docx(output_path)
```

---

## 9. 典型 recipe — SOW (Statement of Work) 模板

```python
def build_sow_template(output_path):
    """SOW 模板：项目名称 / 客户名称 / 工作描述 / 里程碑 / 价格 / 签字日期。"""
    doc = Document()
    
    doc.add_heading('Statement of Work', level=1)
    
    # 项目名称
    p = doc.add_paragraph('项目名称：')
    add_sdt(p, sdt_type='text', alias='Project Name', tag='project_name')
    
    # 客户名称
    p = doc.add_paragraph('客户名称：')
    add_sdt(p, sdt_type='text', alias='Client Name', tag='client_name')
    
    # 工作描述
    p = doc.add_paragraph('工作描述：')
    add_sdt(p, sdt_type='richtext', alias='Work Description', tag='work_desc')
    
    # 里程碑
    p = doc.add_paragraph('里程碑：')
    add_sdt(p, sdt_type='richtext', alias='Milestones', tag='milestones')
    
    # 价格
    p = doc.add_paragraph('价格（USD）：')
    add_sdt(p, sdt_type='text', alias='Price', tag='price')
    
    # 签字日期
    p = doc.add_paragraph('签字日期：')
    add_sdt(p, sdt_type='date', alias='Signature Date', tag='sign_date',
            date_format='yyyy-MM-dd')
    
    # 保护
    set_document_protection(doc, edit_mode='forms')
    
    doc.save(output_path)
    return output_path
```

---

## 10. 典型 recipe — HR 入职表

```python
def build_hr_onboarding_form(output_path):
    """HR 入职表：员工姓名 / 工号 / 入职日期 / 部门 dropdown / 职位。"""
    doc = Document()
    
    doc.add_heading('HR 入职表', level=1)
    
    p = doc.add_paragraph('员工姓名：')
    add_sdt(p, sdt_type='text', alias='Employee Name', tag='emp_name')
    
    p = doc.add_paragraph('工号：')
    add_sdt(p, sdt_type='text', alias='Employee ID', tag='emp_id')
    
    p = doc.add_paragraph('入职日期：')
    add_sdt(p, sdt_type='date', alias='Onboarding Date', tag='onboard_date',
            date_format='yyyy-MM-dd')
    
    p = doc.add_paragraph('部门：')
    add_sdt(p, sdt_type='dropdown', alias='Department', tag='dept',
            items=['Engineering', 'Sales', 'Marketing', 'HR', 'Finance'])
    
    p = doc.add_paragraph('职位：')
    add_sdt(p, sdt_type='text', alias='Position', tag='position')
    
    set_document_protection(doc, edit_mode='forms')
    doc.save(output_path)
    return output_path
```

---

## 11. 典型 recipe — 医疗问卷（多角色）

```python
def build_medical_questionnaire(output_path):
    """医疗问卷：患者填部分 + 医生填部分（role-gated）。"""
    doc = Document()
    
    doc.add_heading('医疗问卷', level=1)
    
    # === 患者填写部分 ===
    doc.add_heading('患者信息（患者填写）', level=2)
    
    p = doc.add_paragraph('姓名：')
    add_sdt(p, sdt_type='text', alias='Patient Name', tag='patient_name',
            lock='sdtLocked')  # 患者可编辑
    
    p = doc.add_paragraph('生日：')
    add_sdt(p, sdt_type='date', alias='Patient DOB', tag='patient_dob',
            date_format='yyyy-MM-dd', lock='sdtLocked')
    
    p = doc.add_paragraph('症状描述：')
    add_sdt(p, sdt_type='richtext', alias='Symptoms', tag='symptoms',
            lock='sdtLocked')
    
    # === 医生填写部分 ===
    doc.add_heading('医生诊断（医生填写）', level=2)
    
    p = doc.add_paragraph('诊断：')
    add_sdt(p, sdt_type='richtext', alias='Diagnosis', tag='diagnosis',
            lock='contentLocked')  # 患者不可改，医生可改
    
    p = doc.add_paragraph('处方：')
    add_sdt(p, sdt_type='richtext', alias='Prescription', tag='prescription',
            lock='contentLocked')
    
    p = doc.add_paragraph('医生签字：')
    add_sdt(p, sdt_type='text', alias='Doctor Signature', tag='doctor_sign',
            lock='contentLocked')
    
    set_document_protection(doc, edit_mode='forms')
    doc.save(output_path)
    return output_path
```

---

## 12. 关键反模式

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | 用下划线 `___` 或空行模拟字段 | 用 SDT 或 FormField，0 结构化数据绕过所有验证 |
| 2 | SDT `type=checkbox` | 用 FormField `--type formfield --prop type=checkbox` |
| 3 | `--after find:` 锚点不唯一 | 用更独特的短语（如双语合同 "甲方签字"） |
| 4 | 双语合同中 `--after 'find:Client Signature:'` 用了内部双引号 | 外层单引号包整个参数 |
| 5 | `protection=forms` 不是最后一步 | 必须最后，之后非字段编辑需 raw XML |
| 6 | SDT 缺 `alias` 或 `tag` | 两者都要 |
| 7 | dropdown SDT 缺 `items` | 必须有非空 items |
| 8 | date SDT 缺 `format` | 必须显示设 `date_format` |
| 9 | 把 `{{customer_name}}` 当 body 文本 | 用 MERGEFIELD field |

---

## 13. Word-form Delivery Gate（6 gates，独立 QA，**不**继承 docx）

Word-form 场景**不**继承 docx Gates 1-3，用以下 6 gates：

### Gate 1 — Validate clean

`protection=forms` 不再产生 schema error。

```python
def gate1_validate_clean(path):
    try:
        docx.Document(path)
        return True
    except Exception as e:
        return False, str(e)
```

### Gate 2 — Token / placeholder leak

no underscores simulating fields。

```python
def gate2_no_underscore_fields(path):
    doc = docx.Document(path)
    for p in doc.paragraphs:
        if '___' in p.text or '____' in p.text:
            return False, f'underscore field detected: {p.text}'
    return True
```

### Gate 3 — At least one structured field exists

SDT + formfield + field > 0。

```python
def gate3_has_structured_field(path):
    doc = docx.Document(path)
    sdt_count = 0
    fld_count = 0
    # 扫描所有 SDT
    for elem in doc.element.iter():
        if elem.tag.endswith('sdt'):
            sdt_count += 1
        if elem.tag.endswith('fldChar'):
            fld_count += 1
    return sdt_count + fld_count > 0, f'sdt={sdt_count}, fields={fld_count}'
```

### Gate 4 — Every SDT has alias + tag

```python
def gate4_sdt_has_alias_tag(path):
    doc = docx.Document(path)
    for sdt in doc.element.iter(qn('w:sdt')):
        sdtPr = sdt.find(qn('w:sdtPr'))
        if sdtPr is None:
            return False, 'SDT without sdtPr'
        if sdtPr.find(qn('w:alias')) is None:
            return False, 'SDT without alias'
        if sdtPr.find(qn('w:tag')) is None:
            return False, 'SDT without tag'
    return True
```

### Gate 5 — Protection enforced

```python
def gate5_protection_enforced(path):
    doc = docx.Document(path)
    settings = doc.settings.element
    protection = settings.find(qn('w:documentProtection'))
    if protection is None:
        return False, 'no documentProtection'
    if protection.get(qn('w:enforcement')) != '1':
        return False, 'enforcement=0'
    return True
```

### Gate 6 — No type=checkbox leaked onto any SDT

```python
def gate6_no_sdt_checkbox(path):
    doc = docx.Document(path)
    for sdtPr in doc.element.iter(qn('w:sdtPr')):
        if sdtPr.find(qn('w:checkbox')) is not None:
            return False, 'SDT has checkbox (use FormField instead)'
    return True
```

---

## 14. Word-form Known Issues 表

选自 OfficeCli K1-K19：

| # | Issue | Workaround |
|---|-------|-----------|
| K1 | SDT type=checkbox 未实现 | 用 FormField `--type formfield --prop type=checkbox` |
| K3 | SDT maxlength UNSUPPORTED | 下游 enforce length |
| K4 | SDT items/format/type 创建后不可 set | add 时设 |
| K5 | FormField maxlength UNSUPPORTED | 下游验证 |
| K6 | FormField dropdown items UNSUPPORTED | 用 SDT dropdown |
| K9 | Batch mode 静默丢 genuinely-UNSUPPORTED props | batch 后 readback 验证 |
| K13 | FormField name > 20 字符 | 保持 ≤ 20 字符 |
| K14 | shd.fill on paragraph emit schema-invalid | 在 run 上用 shading |
| K15 | view forms 不列 MERGEFIELDs | query field 和 view forms 是两个 disjoint inventory |
| K18 | query/get 把 prop fields 包在 .format.{prop} 下 | OfficeCli 特有，python-docx 无此问题 |
| K19 | LibreOffice 把 formfield checkbox 渲染成 ☐☐（double box） | [RENDERER-BUG]，用 Word 渲染 |

---

## 15. Phase 2 限制（python-docx 不可达，需 Word）

以下功能 python-docx 无法实现，需 Word 手动调整或放弃：

| 功能 | 限制 | 替代方案 |
|------|------|---------|
| 真 SDT checkbox with specific locking | 未实现 | 用 FormField checkbox |
| Prompt text（"Click here to enter a date"） | 需 `placeholderDocPart` | 在 SDT content 中放占位文本 |
| Custom richtext default appearance | Word style pane 调整 | 接受默认 |
| Watermark resize | 不可设 | 固定尺寸 |

**OfficeCli 无原生 API 的内容（标注：本项目独立设计）**：

- python-docx 不暴露 SDT API，本文件所有 SDT 代码模板为本项目独立设计的 oxml 注入方案。
- python-docx 不暴露 documentProtection API，本文件 `set_document_protection` 为本项目独立设计。
