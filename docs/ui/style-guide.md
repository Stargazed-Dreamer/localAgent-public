# 风格指南（style-guide.md）

> 设计原则 + 全部设计 token 的数值与使用规则。
> token 真源是 `lib/ui/tokens.py`（代码引用）；本文档解释**为什么这么定、怎么用**。
> 改 token 必须同时改 `lib/ui/tokens.py` 和本文档，并跑 `tests/client_ui/test_ui_theme.py`。

## 一、设计原则

1. **工程工具，不是消费品**：信息密度优先，装饰为零。无阴影、无渐变、无圆角堆砌；
   纵深靠 4 级背景色阶 + 1px 边框表达。
2. **暗色默认**：目标用户长时间盯屏，暗底降低疲劳。暗色不是"把白色变黑"，
   所有颜色为暗底重新调校（文本禁纯白、强调色降饱和）。
3. **状态可预期**：每个可交互控件有完整状态矩阵（normal/hover/pressed/checked/
   disabled/focus），且状态色全部来自 token，任何 agent 不用"发挥"。
4. **CJK 优先**：中文为主要 UI 语言，所有宽度规则按全角字符校核；禁止中英混排
   （按钮要么全中文要么全英文，本项目统一中文）。
5. **无 VL 可执行**：本系统不依赖"看一眼调一调"。所有规范是数值、枚举、清单，
   照抄即对。验收靠 `agent-cookbook.md` 的清单而非肉眼。

## 二、色彩系统

### 2.1 背景色阶（石墨系）

| token | 值 | 用途 |
|-------|----|----|
| `BG_BASE` | `#1F232A` | L0：应用窗口/对话框底色 |
| `BG_PANEL` | `#252B33` | L1：侧栏、工具栏、状态栏、列表/树/表格底色 |
| `BG_CARD` | `#2A303A` | L2：卡片、分组框（QGroupBox）、表头 |
| `BG_INPUT` | `#2E3540` | 输入控件与默认按钮底色 |
| `BG_HOVER` | `#363D49` | 悬停 |
| `BG_PRESSED` | `#3E4654` | 按下 |
| `BG_TOOLTIP` | `#333A45` | 提示框、弹出菜单、下拉列表 |
| `BG_OVERLAY` | `#171A1F` | 图片查看器等需要"更暗"的覆盖层 |

**嵌套规则**：子容器比父容器亮一级。窗口(L0) > 面板(L1) > 卡片(L2) > 输入框(BG_INPUT)。
禁止跳级（L0 上直接放 BG_INPUT 按钮是允许的——按钮属于控件层；但 L0 上放 BG_PANEL
大色块再嵌 BG_BASE 卡片是反级，禁止）。

### 2.2 边框

| token | 值 | 用途 |
|-------|----|----|
| `BORDER` | `#3B424E` | 默认 1px 边框 |
| `BORDER_STRONG` | `#4C5563` | 悬停边框 / 浮层（菜单、下拉、tooltip）边框 |
| `BORDER_FOCUS` | `#42D3AD` | 输入焦点边框（= ACCENT） |

边框是暗色主题表达结构的主要手段，**宁可多一个 1px 边框，不要少**。

### 2.3 文本

| token | 值 | 用途 | 对应 textRole |
|-------|----|----|--------------|
| `TEXT_PRIMARY` | `#E9EDF2` | 正文、标题 | （默认） |
| `TEXT_SECONDARY` | `#B5BDC9` | 次要文本、分组框标题、图标默认色 | `secondary` |
| `TEXT_TERTIARY` | `#7D8695` | 时间戳、占位符、脚注、说明 | `tertiary` / `caption` |
| `TEXT_DISABLED` | `#59616E` | 禁用 | （:disabled 自动） |
| `TEXT_ON_ACCENT` | `#0E1B17` | 强调色按钮上的文字 | （kind=primary 自动） |

- **禁止 `#FFFFFF`**：纯白在暗底上刺眼且有"廉价感"，最亮也只到 `#E9EDF2`。
- 一行内最多两级文本层次（如 主 + secondary）；三层只在卡片/详情区允许。

### 2.4 强调色

| token | 值 | 用途 |
|-------|----|----|
| `ACCENT` | `#42D3AD` | 主按钮底、焦点边框、进度条、滑块、链接式文本 |
| `ACCENT_HOVER` | `#65DEC0` | 主按钮悬停 |
| `ACCENT_PRESSED` | `#35B294` | 主按钮按下 |
| `ACCENT_WASH` | `#2C4741` | 选中底色（列表/表格选中行、checked 按钮） |
| `ACCENT_WASH_HOVER` | `#35534C` | 选中态上的悬停 |
| `ACCENT_BORDER` | `#4C7A6F` | 选中项描边、分割器悬停 |

**选中态公式**（背下来，所有"被选中"都长这样）：
`背景 ACCENT_WASH + 边框 ACCENT_BORDER + 文本/图标 ACCENT`。
强调色只表达"主行动"与"选中/激活"，一屏内 primary 按钮**最多一个**。

### 2.5 语义色

| 语义 | 主色 | 文本色（暗底提亮） | 底色 | 用途 |
|------|------|------------------|------|------|
| danger | `DANGER #E06C75` | `DANGER_TEXT #F2999F` | `DANGER_WASH #453036` | 不可逆操作、错误 |
| warning | `WARNING #E0A84B` | `WARNING_TEXT #F0C36A` | `WARNING_WASH #453A26` | 警告、需注意 |
| success | `SUCCESS #4CC38A` | `SUCCESS_TEXT #6FD6A4` | — | 完成、就绪 |
| info | `INFO #62A0EA` | `INFO_TEXT #8AB8F0` | — | 提示、链接式信息 |
| rec | `REC #FF6161` | — | — | **仅**录制中指示与停止按钮，不得当 danger 用 |

规则：
- 语义色的"文本色"变体用于暗底上的文字/图标（明度更高）；"主色"用于边框、小色块。
- 状态指示优先用"色点 + 文本"（如 `● 录制中`），不要整行染色。

### 2.6 领域色（recorder 时间轴块类型）

`tokens.BLOCK_COLORS`：10 种块类型色，全部为暗底调亮版。
块状态标记色：`BLOCK_KEY #F0C36A`（关键/琥珀）、`BLOCK_ANOMALY #E06C75`（异常/红）、
`BLOCK_AUTOMABLE #62D0B8`（可自动化/青）。
用途仅限时间轴/事件流等"类型标签"场景，**不得**用作通用 UI 元素颜色。

## 三、字体

| 项 | 值 | 说明 |
|----|----|----|
| 主字体 | `Microsoft YaHei UI` → `Segoe UI` → `PingFang SC` → sans-serif | 由 `apply_theme` 全局设置 |
| 等宽 | `Cascadia Mono` → `Consolas` → monospace | 计时器、数值、代码/JSON、坐标 |
| 行高 | 约 1.4 倍字号 | Qt 默认即可，不要手动压缩 |

字号阶梯（只用这 6 档，`tokens.FONT_*`）：

| token | px | 用途 |
|-------|----|----|
| `FONT_CAPTION` | 11 | 脚注、辅助说明、进度条文字 |
| `FONT_SMALL` | 12 | 状态栏、次要列表项 |
| `FONT_BODY` | 13 | **正文基准**，按钮/输入框/列表默认 |
| `FONT_TITLE` | 14 | 面板标题、分组强调 |
| `FONT_HEADING` | 16 | 对话框标题 |
| `FONT_DISPLAY` | 20 | 大数字（录制计时等），配等宽字体 |

字重：正文 400；标题/表头/主按钮 600；**不用 700 以上**，不用斜体（CJK 斜体是伪斜体，丑）。
QLabel 用等宽：`set_text_role(label, "mono")`；代码/JSON 用 QPlainTextEdit + 手动设等宽字体。

## 四、间距与尺寸

### 4.1 4px 网格

`SPACE_XS=4 / SM=8 / MD=12 / LG=16 / XL=24 / XXL=32`。所有 margin/spacing 必须取这 6 档。
常用搭配：
- 对话框内容边距：`DIALOG_MARGIN=20`（特例值，唯一允许的 20）
- 对话框内区块间距：`DIALOG_SECTION_GAP=16`
- 控件间紧凑间距：8；表单行距：12

### 4.2 控件尺寸

| token | px | 用途 |
|-------|----|----|
| `CTRL_HEIGHT_SM` | 24 | 工具栏图标按钮、紧凑控件 |
| `CTRL_HEIGHT_MD` | 28 | 默认按钮、输入框、下拉框（QSS 已内置 min-height） |
| `CTRL_HEIGHT_LG` | 36 | 对话框主操作按钮 |
| `ICON_SIZE_SM/MD/LG` | 14/16/20 | 图标尺寸，默认 16 |
| `RADIUS_SM/MD/LG` | 4/6/8 | 按钮输入框 / 卡片分组框 / 对话框浮层 |
| `DIALOG_MIN_WIDTH` | 480 | 对话框最小宽度 |

### 4.3 防裁剪铁律（本项目反复踩坑）

1. **含文本控件禁止 `setFixedWidth`**。图标按钮（纯图标）可以固定 28×28。
2. 需要最小宽度时用字体度量计算并加 25% 余量：
   ```python
   w = fm.horizontalAdvance(text) + 28   # padding 14*2；中文按全角算
   btn.setMinimumWidth(int(w * 1.25))    # 余量覆盖 125% DPI 与字号差异
   ```
3. 窗口尺寸 = 布局 `sizeHint()`，再 `resize(hint * 1.1)`；**只设 minimumSize，不设 fixedSize**
   （极少数例外：悬浮条高度可固定）。
4. 可能超长的文本（窗口标题、文件路径、用户输入）必须 elide + tooltip：
   标签 `Qt.ElideMiddle`（路径）/ `ElideRight`（标题），并 `setToolTip(全文)`。
5. 完工必须在 125% 与 150% DPI 下各开一次窗口（无 VL 时用 `agent-cookbook.md`
   的自动截图脚本量尺寸，不需要肉眼）。

## 五、状态矩阵（所有可交互控件必须齐全）

| 状态 | 视觉 | 实现 |
|------|------|------|
| normal | token 默认 | — |
| hover | 背景 → `BG_HOVER`，边框 → `BORDER_STRONG` | QSS 已内置 |
| pressed | 背景 → `BG_PRESSED` | QSS 已内置 |
| checked | `ACCENT_WASH + ACCENT_BORDER + ACCENT 文本/图标` | `setCheckable(True)`，QSS 已内置 |
| disabled | 文本 → `TEXT_DISABLED`，背景降一级 | QSS 已内置；**必须配 statusTip 解释原因**（patterns.md §4） |
| focus（键盘） | 边框 → `ACCENT`（输入类）/ `ACCENT_BORDER`（按钮类） | QSS 已内置 |

**禁用即弃疗是反模式**：控件禁用时不给原因，用户只能猜。见 patterns.md §4。

## 六、暗色主题唯一性

- 全项目**只有这一套主题**，通过 `apply_theme()` 应用；禁止页面级"自定义皮肤"。
- 新增颜色需求 → 先进 `tokens.py`（起名 + 注释用途）→ 再使用。
  自检：`grep -rnE "#[0-9a-fA-F]{6}" <新增代码>` 应无输出。
- 局部微调样式（如某标签加大字号）允许 `setStyleSheet("font-size: 14px;")`
  这种**不含颜色**的片段；含颜色的局部样式一律视为违规。
