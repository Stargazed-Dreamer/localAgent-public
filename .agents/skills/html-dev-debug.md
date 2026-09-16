---
name: "html-dev-debug"
description: "编写和调试 HTML 工具页面。当用户要求创建或修改 HTML 页面（如可视化 Dashboard、工具页面）时触发。核心方法：浏览器控制 + 截图 + OCR 闭环验证。"
task_type: dev.html_debug
---

# HTML 工具开发与调试

编写项目内 HTML 工具页面（如可视化 Dashboard、监控面板、审核页面），并通过浏览器控制 + 截图 + OCR 的闭环方法进行实时检查和调试。

## 适用场景

- 创建新的 HTML 工具页面（放入 `workspace/_shared/` 或组件目录）
- 修改现有 HTML 页面（workspace/_shared/gacha/gacha.html 等）
- 任何需要"看到渲染效果才能判断对错"的前端开发任务

## 核心方法论：浏览器控制 + 截图 + OCR 闭环

**原则：HTML 页面的正确性只能通过实际渲染来验证，不能仅靠代码审查。**

### 闭环流程

```
编写/修改 HTML → 浏览器打开 → 等待渲染 → 截图 → OCR 识别 → 分析问题 → 修改代码 → 重新验证
```

每轮循环应解决 1-2 个问题，避免一次性改多处导致无法定位回归。

### Reconnaissance-Then-Action 模式（动态页面调试必备）

调试动态页面（含 JS 渲染、异步数据加载）时，**先侦察再行动**，避免盲目操作：

1. **Navigate + 等待 networkidle**（**CRITICAL**：JS 未执行完前 inspect DOM 会得到不完整或错误的结构）
   ```python
   await page.goto(url)
   await page.wait_for_load_state('networkidle')  # 必须等
   await asyncio.sleep(2)  # 额外等待动画/数据加载
   ```
2. **Inspect rendered DOM**（不要在 networkidle 前做）
   ```python
   # 截图全页
   await page.screenshot(path='temp/inspect.png', full_page=True)
   # 获取页面内容
   content = await page.content()
   # 列出所有按钮/链接/输入框
   buttons = await page.locator('button').all()
   links = await page.locator('a').all()
   inputs = await page.locator('input').all()
   ```
3. **Identify selectors**（从 inspect 结果中提取稳定的选择器）
   - 优先级：`role=` > `text=` > `id` > `data-testid` > CSS class
   - 避免依赖动态生成的 class（如 `css-1abc2d`）
4. **Execute actions**（用发现的选择器操作，操作后截图验证）

**决策树**：
```
是否静态 HTML？
 ├─ 是 → 直接 Read 文件识别选择器 → 写 Playwright 脚本
 └─ 否（动态）→ 后端是否已运行？
     ├─ 否 → 启动后端（start.bat 或 uv run python -m server.main）
     └─ 是 → Reconnaissance-Then-Action（上述 4 步）
```

### 详细步骤

#### 1. 编写/修改 HTML 代码

- 使用 Write/Edit 工具直接修改文件
- 项目内 HTML 页面放在 `workspace/_shared/` 或组件目录（`server/static/` 已移除）
- 单文件架构（HTML + CSS + JS 内联），与现有页面风格一致
- **注意模板字面量语法**：JS 中 `${}` 内避免嵌套反引号、引号冲突、未定义变量

#### 2. 浏览器打开页面

```python
# 使用 MCP 工具
mcp_localagent_browser_open(urls=["http://127.0.0.1:8766/workspace/_shared/your_page.html"])

# 或用 Playwright（需要更多控制时）
mcp_localagent_exec_python(code="""
import asyncio
from playwright.async_api import async_playwright

async def open_page():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto('http://127.0.0.1:8766/workspace/_shared/your_page.html')
        await asyncio.sleep(5)  # 等待数据加载和动画
        # ... 后续操作

asyncio.run(open_page())
""")
```

**关键点**：
- 确保 FastAPI 后端正在运行（否则页面无法加载）
- 新增的静态文件路由需要重启后端才能生效
- 添加 `?v=N` 参数绕过浏览器缓存

#### 3. 等待渲染完成

- `await page.wait_for_load_state('networkidle')` 等待网络请求完成
- 额外 `await asyncio.sleep(3~8)` 等待动画和数据加载
- 如果有加载界面（loading screen），检查其是否已隐藏：
  ```python
  hidden = await page.evaluate("""() => {
      const ls = document.getElementById('loadingScreen');
      return ls ? ls.classList.contains('hidden') : 'no-elem';
  }""")
  ```

#### 4. 截图保存

```python
# 方式一：Playwright 截图（推荐，可截全页）
await page.screenshot(path='temp/debug_screenshot.png', full_page=True)

# 方式二：MCP 截图（只能截当前视口）
mcp_localagent_capture_screen(format="info")  # 注意：绝不传 format="base64"
```

**注意**：
- 优先用 Playwright 截图，支持 `full_page=True`
- MCP 的 `capture_screen` 只能截全屏，包含其他窗口干扰
- 截图保存到 `temp/` 目录，调试完成后清理

#### 5. OCR 识别截图内容

```python
mcp_localagent_ocr_file(file="temp/debug_screenshot.png")
```

**OCR 的作用**：
- 验证文字内容是否正确渲染
- 检查数据是否正确显示（数字、百分比、名称）
- 发现布局异常（文字重叠、截断、错位）

**OCR 的局限**：
- 无法识别颜色（需要代码审查或 DOM 检查）
- 对小字/装饰性文字识别率低
- 可能误读特殊符号

#### 6. 分析问题

**常见问题类型及定位方法**：

| 问题类型 | 症状 | 定位方法 |
|---------|------|---------|
| JS 语法错误 | 页面空白/loading 不消失 | `page.on('pageerror', ...)` 捕获错误 |
| 模板字面量错误 | "Missing } in template expression" | 检查 `${}` 内的引号、反引号嵌套 |
| 数据加载失败 | 某些区域为空 | `page.evaluate` 检查 fetch 返回值 |
| Canvas 绘制错误 | 图表不显示/控制台报错 | 检查 Canvas 尺寸、半径等参数 |
| CSS 布局问题 | 元素重叠/错位 | OCR 识别文字位置 + 代码审查 |
| 路径 404 | 数据不加载 | `page.evaluate` 测试 fetch URL |

**错误捕获代码模板**：

```python
errs = []
page.on('pageerror', lambda exc: errs.append(str(exc)))

# ... 操作页面 ...

# 检查 JS 错误
if errs:
    print(f"JS errors: {errs}")

# 检查 DOM 状态
info = await page.evaluate("""() => {
    return {
        title: document.title,
        bodyGame: document.body.dataset.game,
        loadingHidden: document.getElementById('loadingScreen')?.classList.contains('hidden'),
    };
}""")
```

**JS console 捕获标准片段**（调试必备，同时捕获 pageerror + console message + request failure）：

```python
# 完整的 JS 诊断套件
js_errors = []
console_msgs = []
failed_requests = []

page.on('pageerror', lambda exc: js_errors.append(str(exc)))
page.on('console', lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
page.on('requestfailed', lambda req: failed_requests.append(
    f"{req.url} -> {req.failure}"
))

# ... 操作页面 ...

# 汇总诊断
if js_errors:
    print(f"❌ JS errors ({len(js_errors)}):")
    for e in js_errors[:10]:
        print(f"  - {e}")
if console_msgs:
    print(f"📋 Console ({len(console_msgs)}):")
    for m in console_msgs[:20]:
        print(f"  {m}")
if failed_requests:
    print(f"🌐 Failed requests ({len(failed_requests)}):")
    for r in failed_requests[:10]:
        print(f"  - {r}")
```

> 黑盒脚本原则：调用 `tools/` 下的现成脚本时，**先用 `--help` 看用法，不要直接读源码**。这些脚本通常很大，读源码会污染上下文。只在 `--help` 不满足需求时才考虑定制。

#### 7. 修改代码并重新验证

- 每次只修 1-2 个问题
- 修改后用 `page.reload()` 或新开 Tab 刷新
- 添加缓存破坏参数：`page.goto('...?v=N')`
- 重新截图 → OCR → 验证

## 常见陷阱与解决方案

### 模板字面量（Template Literal）陷阱

**这是 HTML+JS 单文件开发中最常见的 bug 来源。**

1. **引号冲突**：HTML 属性用 `"`，JS 字符串用 `'`，模板字面量用 `` ` ``
   ```javascript
   // 错误：onclick 属性中的引号与模板字面量冲突
   `<div onclick="switchGame('${g.key}')">`  // 如果 g.key 含引号会出错

   // 正确：提前计算，避免在模板内嵌复杂表达式
   const extraCls = hasData ? '' : ' no-data';
   return `<div class="tab ${extraCls}" data-game="${g.key}">`;
   ```

2. **嵌套反引号**：模板字面量内不能再嵌反引号
   ```javascript
   // 错误：内层反引号会提前终止外层模板
   `${widthPct>15 ? `<span>${name}</span>` : ''}`

   // 正确：用字符串拼接或提前计算
   const barLabel = widthPct>15 ? '<span>'+name+'</span>' : '';
   return `<div>${barLabel}</div>`;
   ```

3. **未定义变量**：`${}` 内引用未定义的变量直接报错
   ```javascript
   // 错误：rarity_label_map 未定义
   rarity: rarity_label_map?.[r.rarity_label] || r.rarity_label

   // 正确：直接使用已有字段
   rarity: r.rarity_label || r.rarity
   ```

### 静态文件路径问题

- `server/static/` 已移除，`/static/` 挂载不再可用
- `app.mount("/workspace", StaticFiles(directory="workspace"))` → URL: `/workspace/xxx.json`
- HTML 中 fetch 路径相对于当前页面 URL：
  - `/workspace/_shared/gacha/gacha.html` 中 fetch `/workspace/xxx.json` → 直接用绝对路径 `/workspace/xxx.json`（推荐，不依赖文件位置）
- **新增挂载后必须重启后端**

### Canvas 绘图参数

- `getBoundingClientRect()` 可能返回 0（元素未渲染/不可见）
- 半径、宽度等参数必须 `Math.max(val, min)` 保护
- 高 DPI 屏幕：`canvas.width = rect.width * dpr; ctx.scale(dpr, dpr)`

### 浏览器缓存

- 修改 HTML 后浏览器可能使用缓存的旧版本
- 解决方案：URL 添加 `?v=timestamp` 参数
- Playwright：`await page.goto('...?v=' + Date.now())`

## 设计规范（anti-AI-slop 原则）

**避免"AI 味"设计**——以下模式是 AI 生成页面的常见特征，会让页面看起来像模板而非精心设计：

| 反模式 | 为什么避免 | 正确做法 |
|--------|----------|---------|
| 过度居中布局 | 所有内容堆在中间，缺乏视觉层次 | 采用左对齐或不对称布局，关键信息靠左 |
| 紫色渐变 | AI 默认配色，缺乏辨识度 | 用项目主色（用户偏好绿色）或品牌色 |
| 统一圆角 | 所有元素相同圆角，缺乏节奏感 | 不同元素用不同圆角（卡片 8px、按钮 4px、头像圆形） |
| Inter 字体 | AI 默认字体，过于"科技感" | 用系统字体栈或更有性格的字体 |
| 过多 emoji | 装饰性 emoji 堆砌，分散注意力 | 只在功能性图标位用 emoji，标题/正文不用 |
| 均匀间距 | 所有元素相同 margin，缺乏重点 | 关键分组用大间距，组内用小间距 |
| 纯文本幻灯片/卡片 | 无视觉锚点，扫读困难 | 每个区块至少有一个视觉元素（图标/图表/色块） |

**Dominance over equality（主色优先）**：1 个主色占 60-70% + 1-2 个辅色 + 1 个强调色。不要让所有颜色平均分配。

**用户偏好**（本项目特定）：
- 主色用绿色，**不用紫色**
- 界面用 PySide6 GUI 优于 web（用户偏好原生应用自由度）
- 状态展示界面用只读组件，不含输入元素
- 任务通知要有未读数指示（红点 + 数字）
- 按钮宽度要足够，避免内容裁剪

## 与项目集成

### 文件位置

```
workspace/_shared/       # HTML 工具页面（共享资源）
  your_page.html        # 新页面
  gacha/                # 抽卡统计 Dashboard
workspace/accounting/   # 独立服务 HTML（端口 8780）
  accounting.html       # 记账审核（由 accounting_server.py 提供服务）
```

### 添加新页面后的检查清单

1. **后端路由**：如果页面需要访问 `workspace/` 数据，确认 `/workspace` 挂载存在
2. **监控面板入口**：在 `index.html` 中添加链接
3. **重启后端**：新增静态文件挂载需要重启
4. **验证**：浏览器打开 → 截图 → OCR → 确认渲染正确

### 数据访问模式

HTML 页面通过 fetch 加载 JSON 数据文件：

```javascript
// 相对路径（推荐，与页面位置无关时）
const r = await fetch('../workspace/game_gacha/summary.json');

// 绝对路径（更明确）
const r = await fetch('/workspace/game_gacha/summary.json');
```

错误处理模板：

```javascript
async function loadData(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch(e) {
    console.error(`Failed to load ${url}:`, e);
    return null;  // 返回 null，由调用方处理
  }
}
```

## 工具链速查

| 操作 | MCP 工具 | 说明 |
|------|---------|------|
| 打开页面 | `mcp_localagent_browser_open` | 在调试浏览器中打开 |
| 截图 | `mcp_localagent_capture_screen` | format="info"，绝不传 base64 |
| OCR 识别 | `mcp_localagent_ocr_file` | 识别截图中的文字 |
| 执行 JS | `mcp_localagent_exec_python` | 运行 Playwright 脚本 |
| 检查后端 | `mcp_localagent_exec_status` | 确认后端运行中 |
| 检查浏览器 | `mcp_localagent_browser_status` | 确认调试浏览器可用 |
