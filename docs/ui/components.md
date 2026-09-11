# 组件规范（components.md）

> 每个 Qt 组件怎么用：尺寸、钩子（kind/textRole/objectName）、代码片段。
> 所有样式已由 `lib/ui/theme.py` 的全量 QSS 实现——**你负责放对钩子，样式自动生效**。
> 钩子设置辅助函数：`from lib.ui.theme import set_kind, set_text_role, set_invalid`。

## 目录
按钮 → 输入 → 选择 → 列表/树/表格 → 容器 → 工具栏 → 状态栏 → 标签 → 对话框 →
菜单/Tooltip → 进度 → 空状态

---

## 1. 按钮（QPushButton）

### 1.1 五种 kind

| kind | 用途 | 一屏上限 | 设置 |
|------|------|---------|------|
| 默认 | 普通操作 | 不限 | （不设） |
| `primary` | 当前页面的主行动 | **1 个** | `set_kind(btn, "primary")` |
| `danger` | 不可逆/破坏性操作 | 不限 | `set_kind(btn, "danger")` |
| `ghost` | 低强调辅助操作（如"跳过"） | 不限 | `set_kind(btn, "ghost")` |
| checkable | **状态开关**（如"已标记"） | 不限 | `btn.setCheckable(True)` |

```python
btn = QPushButton("开始录制")
set_kind(btn, "primary")
btn.setMinimumHeight(36)                    # 主操作用 CTRL_HEIGHT_LG
btn.setIcon(icon("record", tokens.ICON_REC, 16))
```

### 1.2 规则

- 文案是**动词或动词短语**："开始录制""保存并结束"；禁止"OK/Yes/确定"这类无信息词
  （"确定"仅在无歧义的简单确认中可用）。
- 高度：普通 28（QSS 已含），对话框主操作 36（`setMinimumHeight`）。
- 宽度：不固定；主操作可 `setMinimumWidth(120)`。
- 带图标：`icon(name, tokens.ICON_DEFAULT)`，16px；图标在左文本在右。
- disabled 时必须 `setStatusTip("为什么不可用")`（patterns.md §4）。

### 1.3 图标按钮（QToolButton，工具栏内）

```python
from lib.ui import icon_button

b = icon_button("undo", "撤销 (Ctrl+Z)")     # 28×28，默认 16px 图标
b.setStatusTip("撤销上一个标注操作")
```
- 纯图标按钮可固定尺寸：`b.setFixedSize(28, 28)`（这是 setFixedSize 唯一合法场景）。
- 优先使用 `icon_button()`，它会统一尺寸、tooltip、statusTip 和无障碍名称。
- tooltip 必写且含快捷键；statusTip 补充"做什么/为何禁用"。

---

## 2. 输入控件

### 2.1 QLineEdit / QPlainTextEdit / QTextEdit

- 占位提示用 `setPlaceholderText()`，颜色自动为 `TEXT_TERTIARY`。
- 校验失败：`set_invalid(edit, True)`（红框）+ 紧邻下方放 `textRole="danger"` 的
  错误说明标签；通过时 `set_invalid(edit, False)` 并隐藏说明。
- 只读展示字段用 QLineEdit `setReadOnly(True)`（自动降级为灰底），**不要**用
  QPlainTextEdit 展示只读短文本（像输入区，误导）。

### 2.2 QComboBox

- 选项文本 ≤ 24 字符，超长截断为"前 12…后 8"并把全文放进该项的 `Qt.ToolTipRole`。
- 技术细节（hwnd/pid/路径）**不进入选项文本**，放 tooltip（见 patterns.md §12）。
- 选项 > 12 个时考虑改用 QLineEdit + QCompleter 或列表对话框。

### 2.3 QSpinBox / QDoubleSpinBox

- 必须 `setRange()` + `setSuffix(" s" / " px")` 单位后缀 + `setSingleStep()` 合理步长。
- 与单位标签成对出现时不重复（有 suffix 就别再加"秒"标签）。

### 2.4 QSlider

- 右侧必须跟数值标签（等宽，宽度按最大值度量固定），实时更新。
- 说明超过一句话 → 正文只留一句，完整解释放"?" help 按钮的 tooltip 或 WhatsThis，
  **禁止把整段教程塞进对话框**（recorder 配置对话框曾因此 1097px 高）。

---

## 3. 选择控件（QCheckBox / QRadioButton）

- 复选框 = 独立开关；单选框 = 互斥分组（必须放同一 QGroupBox/QButtonGroup）。
- 每个选项可配一句 `textRole="tertiary"` 的说明（缩进 24px 对齐文本）。
- 选中态样式 QSS 已内置（teal 底 + 深色勾/点），无需任何自定义。

---

## 4. 列表 / 树 / 表格

### 4.1 QListWidget / QTreeWidget

- 行高由 QSS padding 保证；树节点需要更高时 `item.setSizeHint(0, QSize(0, 28))`。
- 选中态自动为 accent wash。需要"当前项"概念（如当前章节）时用加粗 + accent 文本，
  不要再加背景色（会和选中混淆）。
- 图标用 `icon()` 生成 QIcon 直接 `item.setIcon()`，类型色从 `tokens.BLOCK_COLORS` 取。

### 4.2 QTableWidget / QTableView

- 表头自动加粗灰底；**关闭交替行色**除非行高 ≤ 24 且列 ≤ 3
  （`setAlternatingRowColors(False)`，默认我们主题的层次已够）。
- 网格线已内置；选中整行：`setSelectionBehavior(QAbstractItemView.SelectRows)`。
- 行内缩略图：`setIcon` + `setIconSize(QSize(40, 28))`，配合行高 ≥ 32。
- 列宽：内容列 `setSectionResizeMode(col, QHeaderView.Stretch)`，
  其余 `ResizeToContents`；时间戳列等宽字体。
- **长内容单元格**：`setTextElideMode(Qt.ElideRight)` + item 的 `Qt.ToolTipRole` 放全文。

---

## 5. 容器

### 5.1 QGroupBox（分组框）

- 标题即分组名（自动 600 字重、secondary 色）。
- 内边距 QSS 已含（上 16 / 其余 12）；不要再套一层带 margin 的容器。
- 对话框内分组框之间间距 `DIALOG_SECTION_GAP=16`。

### 5.2 卡片（QFrame）

```python
card = QFrame()
set_kind(card, "card")          # BG_CARD + 边框 + 6px 圆角
card.setLayout(QVBoxLayout())
```

### 5.3 分隔线

```python
line = QFrame(); line.setFrameShape(QFrame.HLine)   # QSS 已样式化为 1px BORDER
```
工具栏内分隔用 `toolbar.addSeparator()`（自动 1px 竖线）。

### 5.4 QSplitter

- 把手 2px、悬停 accent；初始比例用 `setSizes([220, 900, 340])` 这类**具体像素**，
  再用 `setStretchFactor` 指定弹性区。
- 可折叠面板 `setCollapsible(i, True)`，但状态变化后必须 `refresh()` 布局。

---

## 6. 工具栏（QToolBar）

- 高度自动（内容 24 + padding）；`setMovable(False)`、`setFloatable(False)`。
- 动作分组用 `addSeparator()` 隔开，分组顺序 = 使用频率。
- 每个 QAction：图标 + 文本 + tooltip(含快捷键) + statusTip。文本按钮
  `setToolButtonStyle(Qt.ToolButtonTextBesideIcon)`；纯图标按钮保持默认。
- **checkable 动作用于状态开关**（标记类），普通动作用于命令（patterns.md §1）。
- 溢出：动作太多时低频动作进"更多"菜单（QToolButton + setMenu + InstantPopup），
  不允许工具栏挤压主内容。

---

## 7. 状态栏（QStatusBar）

- 内容格式：`段1 | 段2 | 段3`，各段之间用 `textRole="tertiary"` 的分隔符或直接
  `showMessage("就绪 | 0:12:34 | 201 事件 / 47 帧")`。
- 计时/计数用等宽字体（`textRole="mono"`）。
- 需要引起注意的状态（未就绪/错误）用色点：`"● 未就绪"`，色点字符上
  `textRole="warning"` / `danger`。不要整栏染色。
- 瞬时反馈 `showMessage("已保存", 3000)`；常驻信息用 `addPermanentWidget`。

---

## 8. 标签（QLabel）

textRole 速查：

| role | 效果 | 场景 |
|------|------|------|
| `secondary` | 灰 | 次要说明、字段说明 |
| `tertiary` | 更灰 | 时间戳、占位 |
| `caption` | 11px 灰 | 脚注 |
| `title` | 14px 600 | 面板标题 |
| `heading` | 16px 600 | 对话框标题 |
| `danger` / `warning` / `success` / `accent` | 语义色文本 | 状态/校验 |
| `mono` | 等宽 | 计时、数值 |

- 标签默认透明背景，可放任何容器上。
- 展示路径/长标题：`label.setTextInteractionFlags(Qt.TextSelectableByMouse)` 允许复制。

---

## 9. 对话框（QDialog）

```python
dlg = QDialog(parent)
dlg.setWindowTitle("新建录制")               # 名词短语，动词放主按钮
lay = QVBoxLayout(dlg)
lay.setContentsMargins(20, 20, 20, 16)       # DIALOG_MARGIN / 底部 16
lay.setSpacing(16)                           # DIALOG_SECTION_GAP
# ... 内容区 ...
btns = QDialogButtonBox()
ok = btns.addButton("开始录制", QDialogButtonBox.AcceptRole)
set_kind(ok, "primary")
cancel = btns.addButton("取消", QDialogButtonBox.RejectRole)
lay.addWidget(btns)
dlg.setMinimumWidth(560)                     # ≥ DIALOG_MIN_WIDTH
dlg.adjustSize()                             # 高度由内容决定
```

规则：
- 按钮顺序：主操作在右，取消在其左；**全中文**（QDialogButtonBox 标准按钮默认英文，
  必须像上面一样用 addButton 显式给中文文本）。
- 危险操作按钮不做默认按钮（回车不应触发不可逆操作）。
- 模态对话框任务完成后给用户明确出口：成功即关闭 + 状态栏反馈；失败保留现场 + 错误说明。
- 对话框高度上限：主屏可用高度 × 0.85；超过就分页/折叠，不允许无限变高。

## 10. 菜单 / Tooltip / WhatsThis

- 菜单项：动词短语 + 快捷键显示在右侧（Qt 自动）；危险项与前一组用分隔线隔开。
- Tooltip：单行，`<b>名称</b> 说明 (快捷键)`；纯图标按钮必写。
- 长解释（> 2 句）：控件旁放 `?` 小按钮（ghost，icon="info"），
  `setWhatsThis()` 放完整说明，或点击弹 QMessageBox.information。

## 11. 进度（QProgressBar）

-  determinate：`setRange(0, 100)` + 百分比；indeterminate：`setRange(0, 0)`。
- 耗时 > 2s 的操作必须给进度或"处理中…"提示（patterns.md §6）。

## 12. 空状态（EmptyState）

列表/详情无内容时，不允许大白板。结构：

```python
empty = QWidget(objectName="EmptyState")     # QSS 钩子：整体 tertiary 色
lay = QVBoxLayout(empty)
icon_lab = QLabel()
icon_lab.setPixmap(icon_pixmap("image", tokens.TEXT_TERTIARY, 48))
icon_lab.setAlignment(Qt.AlignCenter)
text = QLabel("尚无标注")
text.setAlignment(Qt.AlignCenter)
hint = QLabel("选中时间轴中的块，点击工具栏标记按钮")   # 告诉用户下一步
hint.setAlignment(Qt.AlignCenter)
lay.addStretch(1); lay.addWidget(icon_lab); lay.addWidget(text)
lay.addWidget(hint); lay.addStretch(1)
```

三要素：**图标 + 一句话现状 + 下一步指引**（或主行动按钮）。

> 2026-09 起有组件化实现，优先直接用：
>
> ```python
> from lib.ui import EmptyState
> empty = EmptyState("clock", "没有到期任务", hint="到期任务由 Loop 周期推送")
> layout.addWidget(empty)   # 与列表区域互斥 setVisible
> ```
>
> 内部已按本节配方组装（objectName="EmptyState" + icon_pixmap 48px tertiary + 居中排版）。
> 列表区与空状态互斥显示时，同时切换 `list.setVisible(not empty_flag)`。
