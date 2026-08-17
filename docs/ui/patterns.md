# 交互模式库（patterns.md）

> 视觉之外的行为规范：**什么情况用什么交互**。每条含规则、反例、正例。
> 编辑器/录制器重设计的具体应用见 `temp/sdd/recorder-gui-redesign/`。

## §1 幂等开关（标记类操作）

**规则**："标记/加星/置顶/启用"这类表达状态的操作，必须可再次点击取消（toggle），
并实时回显当前状态。禁止"只能标不能清，只能用撤销清"。

实现：
- 工具栏按钮/菜单项 `setCheckable(True)`；选中块变化时同步 checked 状态。
- checked 视觉 = accent wash + accent 图标（QSS 已内置）。
- 数据层用**追加式反向操作**（如 `unmark_key`）而非修改历史记录，保持操作日志 append-only。

```python
def sync_mark_button(self, block):
    marked = block.get("status", {}).get("marked_key", False)
    self.act_mark_key.setChecked(marked)
    self.act_mark_key.setIcon(icon("star", tokens.ICON_ACTIVE if marked else tokens.ICON_DEFAULT))
    self.act_mark_key.setStatusTip("取消关键标记" if marked else "标记为关键步骤")

def on_mark_key(self):
    target = self.selected_block_id()
    marked = self.act_mark_key.isChecked()   # 目标状态
    action = Action.UNMARK_KEY if not marked else Action.MARK_KEY
    self._add_annotation(action, target)
```

反例（历史 bug）：recorder 编辑器 `mark_key` 数据层只写 `status=True`，GUI 无 unmark 入口，
用户标错只能 Ctrl+Z 撤销——而撤销会连带撤掉之后的其他编辑。

## §2 软删除 / 恢复

**规则**：可恢复的删除，命名用「移除 / 恢复」，**禁止用「裁剪」**（裁剪在中文语境
强烈暗示图像/音频切割）。视觉：移除项保留在列表中，删除线 + 50% 透明 + 状态标签，
可重新选中执行恢复。

- 快捷键：`Delete` = 移除选中，`Shift+Delete` = 恢复选中。
- 图标：eye-off / eye（"在最终视图中隐藏/显示"的语义）。
- 彻底删除（不可恢复）才允许用「删除」+ danger 按钮 + 确认对话框。

## §3 危险操作确认

**仅对不可逆操作弹确认**：丢弃录制、彻底删除、覆盖已有文件。
可逆操作（移除块、修改标注）不弹确认，靠撤销兜底。

确认对话框规范：
- 标题 = 操作名（"丢弃本次录制？"），正文 = 后果一句话 + 影响范围（"已录 12 分钟，丢弃后不可恢复"）。
- 按钮：危险动作放 danger 按钮、文案复述动作（"丢弃"），**取消为默认按钮**（回车=安全）。
- 禁止"不再提示"勾选框，除非有对应设置项可以找回。

## §4 禁用必须解释

**规则**：任何 disabled 控件必须能通过 statusTip/tooltip 回答"为什么不能用"。

```python
self.act_merge.setEnabled(can_merge)
self.act_merge.setStatusTip("合并到前一个块" if can_merge else
                            "需选中一个可合并块（首个块无法向前合并）")
```

反例：按钮灰着，用户不知道是没选块、状态不对还是 bug。

## §5 主行动唯一

一屏/一对话框内 `primary` 按钮最多一个。其他操作：默认按钮 / ghost / 图标按钮。
用户视线应能瞬间定位"下一步点哪"。

## §6 异步与加载

- > 300ms 的操作：禁用触发按钮 + 鼠标 wait cursor 或按钮文本变"处理中…"。
- > 2s：QProgressBar（可测进度）或 indeterminate + 状态栏说明当前步骤。
- > 10s：必须可取消，或转后台线程 + 完成通知。
- **禁止在 GUI 线程跑耗时任务**（界面冻结是"卡死"的第一来源）。

## §7 图片查看器模式

截图/帧图片是 recorder 的核心内容物，查看体验必须完整：

| 能力 | 交互 |
|------|------|
| 打开 | 单击缩略图 / 详情区截图 |
| 缩放 | Ctrl+滚轮 或 工具栏 +/-，范围 10%–800%，以光标为中心 |
| 平移 | 左键拖拽；超出视口时允许 |
| 适配 | 双击 = 适应窗口 ↔ 100% 切换；工具栏 maximize 按钮 |
| 导航 | ←/→ 或 chevron 按钮切换前帧/后帧；标题栏显示 `3/12 · frame_0042.png` |
| 关闭 | Esc / × 按钮 |

缩略图规则：列表内 40×28（QTableWidget icon）或 64×36，保持宽高比，
后台线程加载 + 占位图（`image` 图标 tertiary 色），点击打开查看器。
**禁止把大图直接 scaled 到小尺寸当唯一查看方式**（历史问题：320×240 看不清文字）。

## §8 原始数据（JSON）展示规则

**禁止把 dict/list 直接 `json.dumps` 糊到文本框给用户看**（历史问题：编辑器详情面板
底部大量 JSON 原文，无法定位、无法编辑）。

分层规则：
1. **结构化字段优先**：已知字段（type/timestamp/window/坐标/文本…）→ 表单或键值表格，
   可编辑的字段给输入控件。
2. **补充数据折叠区**：未知/次要字段进"原始数据"折叠面板（QToolButton 折叠或
   QGroupBox checkable），QPlainTextEdit 只读 + 等宽字体 + 语法着色可选，
   超过 200 行截断并注明。
3. **可编辑 JSON 仅给高级模式**：默认只读；要编辑需显式切换"专家模式"并做校验
   （解析失败 `set_invalid` + 错误行号提示）。

## §9 表单校验

- 失焦/提交时校验；失败：`set_invalid(widget, True)` 红框 + 紧邻下方 danger 色错误文本。
- 提交按钮在存在校验错误时禁用，statusTip 说明第一个错误。
- 范围类错误（数值超限）优先用控件自身 range 限制，让用户无法输错。

## §10 技术细节分层

**用户文本与技术细节分离**：
- 主文本放用户可读内容（窗口标题："TRAEditor — main.py"）。
- 技术细节（hwnd、pid、完整路径）放 tooltip 或次级小字（tertiary）。

反例：窗口下拉框显示 `TrafficMonitor [hwnd=66552, pid=15640, TrafficMonitor.exe]`。

## §11 键盘可达性

- 所有工具栏动作必须有快捷键（对照表写进各功能文档），tooltip 中标注。
- 对话框：`Esc` 取消、`Ctrl+Return` 主操作（当主操作不是默认按钮时）。
- 列表：↑↓ 移动、F2 重命名、Delete 移除、Space 选中切换。
- Tab 顺序按视觉流；禁止 Tab 跳到隐藏控件。

## §12 文本溢出

- 任何可能超宽的文本：elide（路径 `ElideMiddle`、标题 `ElideRight`）+ tooltip 全文。
- 多行文本区：限制行数 + "展开全部"链接，或滚动区最大高度约束。
- 表格单元格：`setTextElideMode` + ToolTipRole。

## §13 状态反馈闭环

用户每个动作都应有反馈：状态栏瞬时消息（"已标记关键步骤"，3s）、按钮状态变化、
或列表项视觉更新。**静默成功 = 用户以为没点上**；静默失败 = 灾难（必须错误对话框
或内联错误，danger 色）。
