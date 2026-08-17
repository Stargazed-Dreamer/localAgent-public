# 图标系统（icons.md）

> 54 个内置 SVG 图标，Lucide 风格自绘（24×24 网格 / stroke 2px / 圆角端点 / currentColor），
> 按主题色动态着色。实现：`lib/ui/icons.py`，资源：`lib/ui/resources/icons/`。

## 为什么不用 emoji

Windows 下 emoji 渲染为 Segoe UI Emoji 单色/彩色符号，风格与控件不统一、尺寸难控、
部分符号缺失显示为豆腐块。SVG 图标在任何平台渲染一致，且能随主题换色。

## 用法

```python
from lib.ui import icon, icon_pixmap, available_icons, tokens

action.setIcon(icon("star"))                          # 默认 secondary 色 16px
btn.setIcon(icon("record", tokens.ICON_REC))          # 语义着色
btn.setIcon(icon("save", tokens.ICON_DEFAULT, 20))    # 指定尺寸
label.setPixmap(icon_pixmap("image", tokens.TEXT_TERTIARY, 48))  # 空状态大图
available_icons()                                     # 全部可用名字
```

**着色规则**：

| 场景 | 颜色 token |
|------|-----------|
| 普通工具栏/按钮图标 | `ICON_DEFAULT`（= secondary） |
| 激活/选中/已标记态 | `ICON_ACTIVE`（= ACCENT） |
| 危险操作 | `ICON_DANGER` |
| 警告 | `ICON_WARNING` |
| 录制中/停止 | `ICON_REC` |
| 空状态/占位装饰 | `TEXT_TERTIARY` |

checked 按钮切换图标颜色需手动：`btn.toggled.connect(
lambda on: btn.setIcon(icon("star", tokens.ICON_ACTIVE if on else tokens.ICON_DEFAULT)))`

## 图标清单（按用途分组）

### 录制控制
| 名 | 形状 | 推荐用途 |
|----|------|---------|
| `record` | 实心圆 | 开始录制（配 ICON_REC） |
| `square` | 圆角方块 | 停止 |
| `pause` | 双竖条 | 暂停 |
| `play` | 三角 | 继续 / 播放 |
| `mic` / `mic-off` | 麦克风 | 音频开关 |
| `save` | 软盘 | 保存并结束 |
| `trash` | 垃圾桶 | 丢弃录制（danger） |
| `bookmark` | 书签 | 打点 / 标记章节 |

### 编辑器工具栏
| 名 | 推荐用途 |
|----|---------|
| `eye-off` | **移除块**（软删除，原"裁剪"） |
| `eye` | 恢复块 |
| `merge` | 合并到前一块 |
| `scissors` | 拆分块 |
| `star` | 标关键（checkable，配 BLOCK_KEY 色亦可） |
| `alert-triangle` | 标异常（配 ICON_WARNING） |
| `bot` | 标可自动化 |
| `plus` | 插入块 / 新建 |
| `minus` | 减少 / 折叠 |
| `undo` / `redo` | 撤销 / 重做 |
| `sliders` | 调参面板（音频调参等） |
| `check-circle` | 定稿 / 就绪 / 完成态 |
| `check` | 通用确认、勾选指示 |

### 图片/查看器
| 名 | 推荐用途 |
|----|---------|
| `image` | 截图占位、图片入口 |
| `camera` | 截图动作 |
| `zoom-in` / `zoom-out` | 缩放 |
| `maximize` | 适应窗口 / 实际大小切换 |
| `chevron-left` / `chevron-right` | 上一帧 / 下一帧 |
| `x` | 关闭 / 清除 |

### 导航与容器
| 名 | 推荐用途 |
|----|---------|
| `monitor` | 整个屏幕（范围选择）；终端面板 |
| `app-window` | 窗口（范围选择）、单窗口 |
| `list` | 列表视图、章节列表；待办面板 |
| `folder` | 打开目录 / 输出位置 |
| `file-text` | 文件、导出文本 |
| `message-square` | 备注 / STT 文本；对话面板 |
| `pencil` | 重命名 / 编辑 |
| `copy` | 复制 |
| `home` | 概览 / Dashboard 首页 |
| `inbox` | 收件箱 |
| `wrench` | 工具面板、WIP 任务面板 |

### 状态与杂项
| 名 | 推荐用途 |
|----|---------|
| `info` | 帮助"?"按钮、提示 |
| `clock` | 时间、耗时；到期任务面板 |
| `refresh` | 刷新列表；Loop 任务调度 |
| `grip-vertical` | 拖拽把手 |
| `keyboard` | 键盘事件类型 |
| `mouse-pointer` | 鼠标事件类型 |
| `music` | 音频、BGM |
| `alert-triangle` | 异常/警告通用 |
| `bar-chart` | 监控 / 统计图表 |
| `brain` | 记忆系统 / AI |
| `key` | 密钥 / 凭证管理 |
| `settings` | 设置面板（齿轮） |
| `calendar` | 日程 / 日总结 |
| `bot` | 机器人 / LLM 模型池 |
| `sliders` | 系统工具 / 参数调整 |

## 新增图标规范

1. 从 Lucide/Feather 找对应概念，**自绘**为：24×24 viewBox、`fill="none"`、
   `stroke="currentColor"`、`stroke-width="2"`、`stroke-linecap/linejoin="round"`。
2. 文件放 `lib/ui/resources/icons/<kebab-name>.svg`。
3. 在本文档清单登记（名字 + 推荐用途）。
4. 跑 `pytest tests/test_ui_theme.py`（含 SVG 有效性与 currentColor 检查）。
5. 需要的概念没有合适图标时，宁可用文本按钮，不要硬凑 emoji。
