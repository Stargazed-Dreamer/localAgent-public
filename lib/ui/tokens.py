"""设计 token：全项目 GUI 唯一颜色/字体/间距真源。

铁律：任何 GUI 代码中禁止出现硬编码 hex 颜色、硬编码字号，
一律引用本模块常量。自检命令（在仓库根目录执行）::

    grep -rnE "#[0-9a-fA-F]{6}" client/ workspace/recorder/ --include=*.py | grep -v tokens

除本文件外不应有输出。

数值依据见 docs/ui/style-guide.md。
"""

# ---------------------------------------------------------------------------
# 背景层级（石墨色系，L0 最暗 → L3 最亮；嵌套规则：子面板比父面板亮一级）
# ---------------------------------------------------------------------------
BG_BASE = "#1F232A"        # L0 应用窗口底色
BG_PANEL = "#252B33"       # L1 侧栏 / 面板 / 工具栏 / 状态栏
BG_CARD = "#2A303A"        # L2 卡片 / 分组框 / 表头
BG_INPUT = "#2E3540"       # 输入控件底色（QLineEdit/QComboBox/按钮）
BG_HOVER = "#363D49"       # 悬停态
BG_PRESSED = "#3E4654"     # 按下态
BG_TOOLTIP = "#333A45"     # 工具提示
BG_OVERLAY = "#171A1F"     # 图片查看器等深色覆盖层

# ---------------------------------------------------------------------------
# 边框
# ---------------------------------------------------------------------------
BORDER = "#3B424E"         # 默认边框
BORDER_STRONG = "#4C5563"  # 悬停/强调边框
BORDER_FOCUS = "#42D3AD"   # 焦点边框（= ACCENT）

# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------
TEXT_PRIMARY = "#E9EDF2"    # 主文本（禁用纯白 #FFFFFF）
TEXT_SECONDARY = "#B5BDC9"  # 次要文本 / 图标默认色
TEXT_TERTIARY = "#7D8695"   # 辅助说明 / 占位符 / 时间戳
TEXT_DISABLED = "#59616E"   # 禁用
TEXT_ON_ACCENT = "#0E1B17"  # 强调色按钮上的文字

# ---------------------------------------------------------------------------
# 强调色（青绿，继承 client 原有石墨主题基因）
# ---------------------------------------------------------------------------
ACCENT = "#42D3AD"
ACCENT_HOVER = "#65DEC0"
ACCENT_PRESSED = "#35B294"
ACCENT_WASH = "#2C4741"        # 选中/勾选底色（低饱和 accent）
ACCENT_WASH_HOVER = "#35534C"
ACCENT_BORDER = "#4C7A6F"      # 选中项描边

# ---------------------------------------------------------------------------
# 语义色
# ---------------------------------------------------------------------------
DANGER = "#E5484D"         # 危险操作（按钮描边/图标）— Radix red-9，纯红不偏粉
DANGER_TEXT = "#FF6369"    # 危险文本（暗底上提高明度）— Radix red-11
DANGER_WASH = "#3A1F23"    # 危险底色 — Radix red-3
WARNING = "#E0A84B"        # 警告
WARNING_TEXT = "#F0C36A"
WARNING_WASH = "#453A26"
SUCCESS = "#4CC38A"
SUCCESS_TEXT = "#6FD6A4"
SUCCESS_WASH = "#1F3A2D"   # 成功底色（暗色 SUCCESS wash）
INFO = "#62A0EA"
INFO_TEXT = "#8AB8F0"
INFO_WASH = "#1F2D45"      # 信息底色（暗色 INFO wash，system summary 块用）
REC = "#FF6161"            # 录制指示红（仅录制状态/停止按钮，不做普通 danger）

# 图标专用色
ICON_DEFAULT = TEXT_SECONDARY
ICON_ACTIVE = ACCENT
ICON_DANGER = DANGER
ICON_WARNING = WARNING
ICON_REC = REC

# ---------------------------------------------------------------------------
# recorder 领域色：时间轴块类型（暗色主题下保证可读的调亮版本）
# ---------------------------------------------------------------------------
BLOCK_COLORS = {
    "mouse_click": "#62A0EA",
    "mouse_scroll": "#8A93A3",
    "mouse_drag": "#E0A84B",
    "keyboard": "#4CC38A",
    "focus": "#A98BE8",
    "idle": "#5A6270",
    "screenshot": "#4FC3D9",
    "stt_segment": "#E5C15C",
    "user_note": "#E0A84B",
    "chapter": "#E9EDF2",
}
BLOCK_KEY = "#F0C36A"      # 标关键标记色（星标琥珀）
BLOCK_ANOMALY = "#E5484D"  # 标异常标记色（= DANGER）
BLOCK_AUTOMABLE = "#62D0B8"  # 标可自动化标记色

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
FONT_FAMILY = '"Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", monospace'

FONT_CAPTION = 11   # 辅助说明
FONT_SMALL = 12     # 次要文本 / 状态栏
FONT_BODY = 13      # 正文基准
FONT_TITLE = 14     # 面板标题
FONT_HEADING = 16   # 对话框标题
FONT_DISPLAY = 20   # 大数字（如录制计时）

# ---------------------------------------------------------------------------
# 间距（4px 网格）与控件尺寸
# ---------------------------------------------------------------------------
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32

RADIUS_SM = 4   # 按钮 / 输入框
RADIUS_MD = 6   # 卡片 / 分组框
RADIUS_LG = 8   # 对话框 / 浮层

CTRL_HEIGHT_SM = 24   # 紧凑按钮 / 工具栏
CTRL_HEIGHT_MD = 28   # 默认按钮 / 输入框
CTRL_HEIGHT_LG = 36   # 主操作按钮

ICON_SIZE_SM = 14
ICON_SIZE_MD = 16
ICON_SIZE_LG = 20

DIALOG_MARGIN = 20        # 对话框内容边距
DIALOG_SECTION_GAP = 16   # 对话框内区块间距
DIALOG_MIN_WIDTH = 480    # 对话框最小宽度
