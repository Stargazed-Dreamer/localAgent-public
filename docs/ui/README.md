# LocalAgent UI 设计系统（docs/ui/）

> **一句话**：本项目所有 PySide6 GUI 的唯一视觉/交互标准。写任何界面代码之前，先读本目录。
> 配套代码实现在 `lib/ui/`（tokens / theme / icons），测试在 `tests/test_ui_theme.py`。

## 为什么存在

历史上项目有四套并存风格（client 石墨青绿、录制器暗绿灰、编辑器浅色、配置对话框未样式化），
emoji 图标在 Windows 渲染不一致，固定宽度导致 CJK 文本被裁剪。
本设计系统于 2026-07-31 建立，目标是：**任何 agent（包括无 VL 视觉能力的）照着文档数值
就能做出风格统一、不裁剪、不难看的 GUI。**

## 阅读顺序

| 文档 | 内容 | 何时读 |
|------|------|--------|
| `style-guide.md` | 设计原则 + 全部 token（色板/字体/间距/尺寸） | 写界面**前**必读 |
| `components.md` | 每个 Qt 组件的规范：尺寸、kind/textRole 钩子、代码片段 | 用哪个组件查哪个 |
| `icons.md` | 图标用法 + 46 个内置图标清单 + 新增图标规范 | 需要图标时 |
| `patterns.md` | 交互模式库：幂等开关/软删除/禁用解释/空状态/图片查看器/JSON 规则… | 设计交互逻辑时 |
| `agent-cookbook.md` | 无 VL agent 实操手册：新窗口 5 步 + 验收清单 + 常见坑 | 动手时 + 完工前自查 |

## 快速开始（5 行接入）

```python
from lib.ui import apply_theme, icon, tokens
from lib.ui.theme import set_kind, set_text_role

app = QApplication(sys.argv)
apply_theme(app)                      # 全局深色工程主题（含字体/Fusion 风格）

btn = QPushButton("开始录制")
set_kind(btn, "primary")              # 主操作按钮
btn.setIcon(icon("record", tokens.ICON_REC))   # 16px SVG 图标，主题色着色
```

子进程独立 GUI（如 recorder）没有共享 QApplication 时，对顶层窗口调用
`apply_theme(main_window)` 即可，效果相同。

## 十条铁律（详细依据见各分篇）

1. **禁止硬编码颜色/字号**——只用 `lib/ui/tokens.py` 常量。自检：`grep -rnE "#[0-9a-fA-F]{6}" <你的代码>` 应无输出（tokens.py 除外）。
2. **禁止 emoji 当图标**——用 `lib.ui.icon()`，Windows 下 emoji 渲染为单色符号且风格不一。
3. **禁止对含文本控件 setFixedWidth**——用布局 + `setMinimumWidth(字体度量×1.25)`，防 CJK/125% DPI 裁剪。
4. **标记类操作必须幂等**——可再点一次取消；用 checkable 按钮并回显当前状态。
5. **禁用控件必须解释原因**——`setStatusTip()` / `setToolTip()` 写明"为什么不可用"。
6. **禁止向用户展示裸 JSON**——结构化字段优先，原始数据折叠只读（patterns.md §8）。
7. **对话框按钮全中文**——主操作在右，文案是动词（"开始录制"不是"OK"）。
8. **每个交互控件必须有 tooltip**——图标按钮 tooltip 必须含快捷键，格式"说明 (Ctrl+Z)"。
9. **长文本必须 elide + tooltip 兜底**——`Qt.ElideMiddle` / `ElideRight`。
10. **完工前过 `agent-cookbook.md` 验收清单**——全部勾完才算完成。

## 主题速览

- **深色工程风**：石墨底色 `#1F232A`（L0）→ `#252B33`（L1）→ `#2A303A`（L2）逐级提亮；
  强调色青绿 `#42D3AD`；语义色 danger/warning/success/info 全套暗色适配。
- **无阴影、无渐变**：工程工具靠边框与色阶层级表达纵深。
- **字号基准 13px**，等宽字体仅用于计时/数值/代码。
- token 全表：`lib/ui/tokens.py`（每个常量带注释），解读见 `style-guide.md`。
- **视觉参考**：`theme-showcase.png`（主题控件陈列室渲染图，有读图能力的 agent 可对照自查；
  重新生成方法见 `agent-cookbook.md` §二自检渲染脚本）。

## 迁移现状

| 模块 | 状态 |
|------|------|
| `lib/ui/` 基础设施 + 本文档 | ✅ 已落地（2026-07-31） |
| recorder（悬浮条/配置对话框/监控面板/编辑器） | 📐 重设计文档：`temp/sdd/recorder-gui-redesign/`，待实施 |
| client/ | 沿用 `client/resources/style.qss`，迁移路径见 `agent-cookbook.md` §迁移 |
