---
name: client_dev
description: >
  PySide6 客户端开发：面板架构、性能规范、异步加载模式。当用户说"开发客户端"、"GUI 开发"、
  "新增面板"、"面板卡顿"、"PySide6"、"客户端性能"、"面板开发"、"widget 优化"时使用。
  触发词：开发客户端、GUI开发、新增面板、面板卡顿、PySide6、客户端性能、面板开发、widget优化。
task_type: dev.client_dev
---

# client_dev — PySide6 客户端开发

> **task_type**: `dev.client_dev`
> **触发词**: 开发客户端、GUI开发、新增面板、面板卡顿、PySide6、客户端性能、面板开发、widget优化

## 适用场景

- 新增/修改 `client/panels/` 下的 GUI 面板
- 排查面板切换卡顿、UI 冻结问题
- 涉及 QThread / Signal / SQLite 异步加载的 GUI 逻辑
- GUI 架构变更（新增面板分类、修改导航、调整 PanelRegistry）

## 架构概览

```
client/
├── main.py              # 入口：python -m client.main
├── core/
│   ├── app.py           # MainWindow + Sidebar + QStackedWidget
│   ├── panel_base.py    # PanelBase 抽象基类 + PanelMeta
│   ├── panel_registry.py # 自动发现 panels/ 下所有 PanelBase 子类
│   ├── state.py         # AppState（QSettings 持久化：窗口几何/最近面板/设置）
│   ├── services.py      # ServiceManager（后端健康轮询 + 信号广播）
│   ├── http_client.py   # 后端 HTTP 客户端
│   └── constants.py     # 路径/超时/轮询周期常量
├── panels/              # 7 个面板（Dashboard/Tools/Keys/Accounting/Monitoring/DailySummary/Settings）
├── widgets/             # 可复用组件（tool_runner/confirm_dialog/key_form/status_card/markdown_viewer）
└── resources/style.qss  # 全局 QSS 样式
```

### 面板生命周期

```
MainWindow.__init__()
  → PanelRegistry.discover()          # 扫描 panels/ 下所有 PanelBase 子类
  → for cls in panel_classes:
      panel = cls(self)                # ★ 所有面板在启动时一次性实例化
      stack.addWidget(panel)
  → switch_to_panel(last_panel)
      → old_panel.on_hide()
      → stack.setCurrentIndex(idx)
      → new_panel.on_show()            # ★ 每次切换都触发
```

**关键事实**：面板的 `__init__` 在启动时执行一次，`on_show()` 每次切换都执行。所以 `__init__` 可以做较重的初始化（构建 UI 骨架），但 `on_show()` 必须轻量。

## 性能规范（强制）

### 1. `on_show()` 不能做重活

| 操作 | 是否允许在 on_show | 替代方案 |
|------|-------------------|---------|
| 同步 DB 查询（>10ms） | ❌ | 放 QThread 异步加载 |
| 文件读取 + JSON 解析（>5KB） | ❌ | 缓存 + mtime 检测 |
| 销毁重建大量 widget（>20个） | ❌ | 首次构建后保留，刷新时才重建 |
| 更新少量 widget 文本 | ✅ | — |
| 启动后台 QThread | ✅ | — |

### 2. 异步加载模式（QThread + Signal）

DB 查询或重计算放后台线程，通过 Signal 回主线程填充 widget：

```python
class _DataLoader(QThread):
    data_ready = Signal(dict)
    error = Signal(str)

    def __init__(self, store, ...):
        super().__init__()
        self._store = store

    def run(self):  # 后台线程执行
        try:
            data = self._store.query(...)
            self.data_ready.emit(data)
        except Exception as e:
            self.error.emit(str(e))

def _load_data(self):  # 主线程
    if self._loading:
        return  # 防重复
    self._loading = True
    self._status_label.setText("加载中…")
    self._loader = _DataLoader(self._store, ...)
    self._loader.data_ready.connect(self._on_data_ready)
    self._loader.error.connect(self._on_data_error)
    self._loader.start()  # 不阻塞

def _on_data_ready(self, data):  # 主线程，填充 widget
    try:
        # 填充表格/树等
        ...
    finally:
        self._loading = False
        self._loader = None  # 释放引用
```

**SQLite 跨线程**：`AccountingStore` 用 `check_same_thread=False` 创建连接，可在 QThread 中安全读取。写操作有 `_write_lock` 保护。

### 3. 避免每次切换都重建

**模式：首次加载标志 + 手动刷新**

```python
def __init__(self, parent=None):
    ...
    self._data_loaded = False

def on_show(self):  # 每次切换触发
    if self._data_loaded:
        return  # 已加载，直接显示
    self._data_loaded = True
    self._load_data()

def on_refresh(self):  # 用户点刷新按钮
    self._load_data()  # 强制重新加载
```

**模式：文件 mtime 检测（适用于配置文件驱动的面板）**

```python
def __init__(self, parent=None):
    ...
    self._manifest_mtime = self._read_mtime()

def on_show(self):
    current_mtime = self._read_mtime()
    if current_mtime != self._manifest_mtime:
        self._reload()
        self._manifest_mtime = current_mtime
```

### 4. 避免 N+1 查询

循环填充表格时，不要每行单独查 DB：

```python
# ❌ 错误：512 行 = 512 次 DB 查询
for tx in txs:
    descriptions = store.get_descriptions_for_category(tx["category"])
    ...

# ✅ 正确：一次性预取
all_categories = set(tx["category"] for tx in txs if tx["category"])
descriptions_map = {cat: store.get_descriptions_for_category(cat) for cat in all_categories}
for tx in txs:
    descriptions = descriptions_map.get(tx["category"], [])
    ...
```

### 5. 重控件懒加载

| 控件 | 开销 | 建议 |
|------|------|------|
| `QCalendarWidget` | 很重（100ms+） | 延迟到 tab 首次切换时创建 |
| `QComboBox`（作为 cellWidget） | 中等（每行 1-3ms） | 大量行时考虑分页或懒加载 |
| `QTextEdit` | 中重 | 不要在循环中创建大量实例 |
| `QTableWidgetItem` | 轻量 | 可安全用于大表格 |

## 新增面板流程

1. 在 `client/panels/` 下创建 `my_panel.py`
2. 继承 `PanelBase`，设置 `PANEL_META`：
   ```python
   class MyPanel(PanelBase):
       PANEL_META = PanelMeta(
           id="my_panel",
           title="我的面板",
           icon="📋",
           order=40,
           category="main",  # main / monitor / advanced
           requires_backend=True,
       )
   ```
3. `PanelRegistry.discover()` 会自动发现并注册（无需手动改 app.py）
4. 实现 `on_show()` / `on_hide()` / `on_refresh()` / `on_backend_status_change()` 钩子
5. 如果面板需要后端数据，在 `on_show()` 中用异步加载模式
6. 更新 `.agents/skills/_index.md` 的面板列表

## 调试技巧

- **启动客户端**：`.venv\Scripts\python.exe -m client.main`
- **查看面板实例化错误**：启动时的 `__init__` 异常会打印到 stderr
- **查看 on_show 错误**：`on_show()` 中的异常不会崩溃但会打印到 stdout
- **性能测量**：在 `on_show` 前后加 `time.time()` 计时，超过 50ms 说明需要优化

## 常见踩坑

1. **`on_show` 每次都重建** → 加首次加载标志或 mtime 检测
2. **主线程同步 DB 查询** → 放 QThread
3. **N+1 查询** → 一次性预取
4. **QThread 被 GC 回收** → 将 loader 引用存为实例属性 `self._loader`
5. **Signal 跨线程** → QThread 通过 `Signal.emit` 自动排队到主线程，安全
6. **SQLite 跨线程** → 需要 `check_same_thread=False`（AccountingStore 已配置）
7. **快速切换过滤导致多个 loader** → 用 `_loading` 标志拦截
8. **QSS 样式不生效** → 检查 `client/resources/style.qss` 是否被 `load_stylesheet` 加载

## 相关文件

| 文件 | 用途 |
|------|------|
| [panel_base.py](file:///f:/<project_root>/client/core/panel_base.py) | PanelBase 抽象基类 + PanelMeta |
| [panel_registry.py](file:///f:/<project_root>/client/core/panel_registry.py) | 自动发现机制 |
| [app.py](file:///f:/<project_root>/client/core/app.py) | MainWindow + 面板切换逻辑 |
| [accounting.py](file:///f:/<project_root>/workspace/accounting/panel.py) | 异步加载参考实现（_ReviewDataLoader） |
| [tools.py](file:///f:/<project_root>/client/panels/tools.py) | mtime 检测参考实现 |
