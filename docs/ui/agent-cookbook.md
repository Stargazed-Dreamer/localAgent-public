# Agent 实操手册（agent-cookbook.md）

> 给**没有 VL 视觉能力**的 agent：照本手册的数值、模板、清单执行，就能做出符合
> 设计系统的 GUI，不需要"看一眼"。最后更新：2026-07-31。

## 一、新窗口 5 步

**第 1 步：接入主题**

```python
from lib.ui import apply_theme

app = QApplication(sys.argv)     # 或复用已有 app
apply_theme(app)                 # 独立进程 GUI 也可 apply_theme(顶层窗口)
```

**第 2 步：搭骨架**（对照 components.md §9 对话框 / 下方主窗口模板）

**第 3 步：放组件，挂钩子**
- 主按钮 `set_kind(b, "primary")`；危险 `danger`；说明文字 `set_text_role(l, "secondary")`；
  错误 `set_invalid(w, True)`；卡片 `set_kind(f, "card")`。
- 图标一律 `icon(name, tokens.XXX)`，从 `docs/ui/icons.md` 清单选。

**第 4 步：行为对照 patterns.md**
- 有标记/开关？→ §1 checkable + 状态回显
- 有删除？→ §2 软删除 or §3 危险确认
- 有禁用？→ §4 statusTip 解释
- 有截图/图片？→ §7 查看器
- 有 dict 数据？→ §8 禁止裸 JSON

**第 5 步：过验收清单**（本文 §三），全勾才算完。

## 二、代码模板

### 主窗口骨架

```python
class MyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("功能名")
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self.resize(1200, 800)
        self.setMinimumSize(900, 600)

    def _build_toolbar(self):
        tb = QToolBar(movable=False, floatable=False)
        act = tb.addAction(icon("star"), "标关键", self.on_mark)
        act.setCheckable(True)                       # 状态类操作必 checkable
        act.setShortcut("Ctrl+K")
        act.setToolTip("标关键 (Ctrl+K)")
        act.setStatusTip("标记为关键步骤；再次点击取消")
        tb.addSeparator()
        self.addToolBar(tb)

    def _build_body(self):
        splitter = QSplitter()
        splitter.addWidget(self.left_panel)          # 导航/列表
        splitter.addWidget(self.center_panel)        # 主内容
        splitter.setSizes([240, 960])
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    def _build_statusbar(self):
        self.statusBar().showMessage("就绪")
```

### 自检渲染脚本（无 VL 验证布局）

把下面脚本存到 `temp/_ui_check.py` 改两行运行，输出 PNG 后用 **read_file 读图自查**
（agent 有读图能力时）或交给有 VL 的 agent 复核：

```python
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"<project_root>")
from PySide6.QtWidgets import QApplication
from lib.ui import apply_theme
# from xxx import MyWindow                       # ← 改成你的窗口
app = QApplication([])
apply_theme(app)
w = MyWindow()                                    # ← 改成你的窗口
w.show(); app.processEvents()
w.grab().save(r"<project_root>/temp/_ui_check.png")
print("saved", w.size())
# DPI 检查：QSplashScreen 前加 os.environ["QT_SCALE_FACTOR"] = "1.25" 再跑一次
```

读图自查要点：文本是否被截断（"…"出现即失败，除非已配 tooltip）、
按钮是否对齐、是否有英文孤儿串、checked 态是否可见。

## 三、验收清单（完工前逐项打勾）

### 风格一致性
- [ ] `grep -rnE "#[0-9a-fA-F]{6}" <改动文件>` 无输出（颜色全走 tokens）
- [ ] 无 `setFixedWidth/setFixedSize` 作用于含文本控件（纯图标 28×28 除外）
- [ ] 无 emoji 充当图标/按钮文本
- [ ] 字体未手动 setFont（等宽场景除外，且用 tokens.FONT_MONO）
- [ ] 主题通过 `apply_theme()` 接入，无页面级自定义皮肤

### 布局与防裁剪
- [ ] 窗口只设 minimumSize，尺寸由布局 sizeHint 决定
- [ ] 所有可能超长文本已 elide + tooltip 全文
- [ ] 125% DPI 下用 §二 脚本渲染一次，无文本截断
- [ ] 对话框按钮区：主操作在右、取消在左、全中文
- [ ] 对话框高度 < 屏高 85%，超出部分已折叠/分页

### 交互完整性
- [ ] 每个可交互控件有 tooltip；图标按钮 tooltip 含快捷键
- [ ] 每个 disabled 控件有 statusTip 解释原因
- [ ] 标记类操作 checkable 且回显状态（patterns §1）
- [ ] 删除类操作区分软删除/彻底删除，命名符合 patterns §2/§3
- [ ] 无裸 JSON 展示（patterns §8）
- [ ] 空状态有图标+说明+下一步指引（components §12）
- [ ] 耗时操作有进度/禁用/异步处理（patterns §6）
- [ ] 每个动作有可见反馈（patterns §13）

### 工程质量
- [ ] `pytest tests/client_ui/test_ui_theme.py` 通过
- [ ] 新增功能涉及路由/模型/目录 → 对照 `.agents/rules/project_rules.md` 清单
- [ ] CHANGELOG.md 已更新

## 四、常见坑（PySide6 实战）

1. **动态属性不生效**：`setProperty` 后必须 `style().unpolish/polish`——
   用 `lib.ui.theme` 的 `set_kind/set_text_role/set_invalid` 已代劳。
2. **QSS 全局污染**：`apply_theme` 设的是 app 级样式表，局部 `setStyleSheet`
   会**叠加**而非覆盖——局部只写差异化属性（如 `font-size`），禁止写颜色。
3. **QTableWidget 行高**：默认行高随字体，设缩略图后必须
   `verticalHeader().setDefaultSectionSize(36)` 否则图被裁。
4. **高清屏图标发虚**：`lib.ui.icon()` 已按主屏 devicePixelRatio 超采样；
   自绘 QPixmap 时记得 `setDevicePixelRatio`。
5. **QGroupBox 标题被边框压住**：QSS 已处理（margin-top 26px + title top 7px），
   不要再给 groupbox 加 `padding-top: 0`。
6. **Windows emoji**：按钮文本里出现 ✓⚠ 等符号一律换成 `lib.ui.icon()`。
7. **offscreen 渲染崩溃**：先建 `QApplication` 再碰 `QPixmap`
   （参见 tests/client_ui/test_ui_theme.py 的 session fixture）。
8. **窗口总在最前/Tool 标志**：悬浮工具窗用 `WindowStaysOnTopHint | Tool | Frameless`，
   但**必须**提供拖动区域（空白处 mousePress/mouseMove 实现拖动）和明显关闭手段。

## 五、client/ 迁移路径（供后续 agent 参考）

1. `client/app.py` 的 `_load_stylesheet()` 改为 `apply_theme(app)`；
   删除 `client/resources/style.qss`（其 token 已并入 lib/ui）。
2. client 内 274+ 处内联 `setStyleSheet` 含色值的，按本手册逐文件替换为
   textRole/kind 钩子；优先迁移主窗口与高频对话框。
3. 每迁一个文件跑 §三 清单 + 自检渲染脚本对比。
4. 迁移期间两套并存可接受，**新代码必须直接用 lib/ui**。
