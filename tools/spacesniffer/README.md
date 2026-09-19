# SpaceSniffer 快照浏览器

读取 SpaceSniffer 导出的 `.sns` 快照并用 **squarified treemap** 可视化。
复刻原版的全部**分析类**操作（导航 / 过滤 / 标记 / 详情 / 导出），
不做扫描 —— 它是**快照浏览器**，不是扫描器。

## 启动

```bat
:: 双击打开空窗口
tools\spacesniffer\start.bat

:: 直接加载指定快照
tools\spacesniffer\start.bat "D:\快照.sns"

:: 不开图形界面，只打印摘要（排查 / 脚本化用）
.venv\Scripts\python.exe tools\spacesniffer\main.py --info "D:\快照.sns"
```

也可以直接把 `.sns` 文件拖到窗口里（支持拖放），或用菜单 `文件 → 打开快照`。

## 界面

```
菜单栏 / 工具栏
面包屑路径栏
┌─ Treemap ───────────────────┬─ 详情面板 ──────┐
│  面积 ∝ 体积的方块图         │  选中项信息      │
│  单击选 / 双击缩放或打开     │  内容列表        │
│                             │  类型分布        │
└─────────────────────────────┴─────────────────┘
状态栏：悬停项 | 卷容量 | 文件/目录计数 | 过滤与标记
```

## 操作对照（原版 → 本工具）

| SpaceSniffer 操作 | 本工具 | 快捷键 |
|---|---|---|
| 单击选中方块 | ✅ | 单击 |
| 双击进入目录 | ✅ | 双击目录 |
| 双击打开文件 | ✅ | 双击文件 |
| 返回上一层 | ✅ | `Backspace` / `Esc` / `Alt+↑` |
| 回到根目录 | ✅ | `Alt+Home` |
| 面包屑跳转 | ✅ | 点路径段 |
| 过滤框（掩码 / 大小 / 时间 / 属性） | ✅ | `Ctrl+F` 聚焦，回车应用 |
| 四色标记 | ✅ | `Ctrl+1` ~ `Ctrl+4`（再按取消） |
| 标记着色开关 | ✅ | `Ctrl+T` |
| 按类型着色 + 图例 | ✅ | — |
| 右键菜单（打开 / 定位 / 属性 / 复制路径 / 删除） | ✅ | 右键方块 |
| 删除到回收站 | ✅ | 右键 → 删除到回收站 |
| 导出报告 | ✅ | `Ctrl+E` 文本 / `Ctrl+Shift+E` CSV |
| 重新加载快照 | ✅ | `F5` |
| 悬停 tooltip、最大文件/目录排行 | ✅ | — |
| 实时扫描 / 扫描进度 | ❌ | 快照是离线结果，无扫描过程 |
| 文件系统事件同步（变动闪烁） | ❌ | 只对实时扫描有意义 |
| NTFS ADS（交换数据流）扫描 | ❌ | 快照里没有 ADS 记录 |

## 过滤框语法

分号分隔多个条件，条件之间是**且**；`|` 前缀表示排除。

| 写法 | 含义 |
|---|---|
| `*.jpg` | 只看 jpg 文件 |
| `*.jpg;*.png` | 只看 jpg 或 png（同组掩码之间是**或**） |
| `jpg` | 只写扩展名，等价于 `*.jpg` |
| `*.tar.gz` | 多段后缀可写，走通配匹配 |
| `\|*.dll` | 排除 dll |
| `>500mb` / `<1gb` | 大小范围，单位 `b/kb/mb/gb/tb` |
| `>2years` / `<3months` | 修改时间早于两年前 / 近三个月内，单位 `day/week/month/year` |
| `:red` | 只看红色标记（`:yellow` `:green` `:blue` `:all`） |
| `\|:yellow` | 排除黄色标记 |
| `N:download` | 名称包含 download |
| `A:hidden` | 带隐藏属性（`readonly/hidden/system/archive/compressed/sparse`） |

组合示例：`*.jpg;>1mb;<3months;|:yellow`

几点语义说明：

- 过滤**主要作用在文件上**。目录只要子树里有命中内容就会被保留，并按命中内容的
  合计大小重新计量（tree map 的面积随之收缩）；目录名本身也参与掩码与 `N:` 匹配。
- 时间过滤对目录无效 —— 快照里目录的时间戳全为 0。
- `>500mb` 这类下界会**整枝剪枝**：父目录小于下界时子树必然全不命中，直接跳过，
  所以大小过滤比类型过滤还快。
- 应用过滤后**保持当前层级**：停在同一路径上重新定位，不会把你踢回根目录。

## 导出

- **文本报告**（`Ctrl+E`）：卷统计 + 子目录 TOP + 本级大文件 + 全树最大文件/目录 +
  类型分布，可直接贴进笔记。
- **CSV 明细**（`Ctrl+Shift+E`）：当前视图范围内逐条 `完整路径/名称/类型/类别/大小/
  修改时间/属性/标记/是否目录`。写的是 `utf-8-sig`，Excel 双击打开不乱码。

## 模块结构

| 文件 | 职责 |
|---|---|
| `sns_model.py` | `.sns` 解析 + `Entry`/`Snapshot` 树模型。**自包含**，不依赖 `workspace/` |
| `treemap.py` | squarified 布局引擎（纯几何，只依赖 `QRectF`，可进工作线程） |
| `filtering.py` | 过滤 DSL 解析 + 视图树构建 |
| `filetypes.py` | 扩展名 → 类型类别 → 配色 |
| `tagging.py` | 四色标记 |
| `report.py` | 文本报告 / CSV / 类型分布 / Top N |
| `viewer.py` | `QMainWindow`：画布、面包屑、过滤栏、详情面板、状态栏、右键菜单 |
| `main.py` | CLI 入口 |
| `start.bat` | 双击启动（GBK + CRLF 编码，勿存成 UTF-8） |

配色 token 统一定义在 `lib/ui/tokens.py`（`FILE_TYPE_COLORS`），设计规范见
`docs/ui/style-guide.md`。

## `.sns` 格式

字段布局与栈语义的逆向笔记在 `workspace/disk_manager/references/sns_format.md`。

本目录的解析器是**独立实现**，刻意不复用 `workspace/disk_manager/scripts/parse_sns.py`：
`workspace/**` 属于发布产物，`tools/` 是开发期工具，反向依赖会破坏分发边界。
两份实现的一致性由交叉校验测试守护 —— 改任一边而另一边没跟上，测试就会红：

```bat
.venv\Scripts\python.exe -m pytest tests\files_tools\test_spacesniffer.py -q
```

## 性能

以本机 67.7 MB 全盘快照（852,241 条记录 / 706,340 文件 / 145,898 目录）实测：

| 环节 | 耗时 |
|---|---|
| 解析 + 建树 | ~5.8 s |
| 布局（1400×900 视口） | ~100 ms |
| 命中测试 | ~7.5 µs / 次 |
| 类型过滤（`*.jpg`） | ~0.3 s |
| 大小过滤（`>500mb`） | ~7 ms（整枝剪枝生效） |

解析与布局都在后台线程跑，界面不卡。
