# webapp_testing - Web 应用测试

> 整合自 anthropics/skills 的 webapp-testing + LocalAgent 项目 Reconnaissance-Then-Action 实战经验。
> 适用于新项目用 Playwright + LLM 自动化测试 Web 应用的场景。

## 1. 概述

**Web 应用测试**用 Playwright + LLM 实现端到端自动化测试，覆盖：
- 视觉验证（截图 + OCR + 多模态 LLM 检查）
- 功能验证（点击 / 填表单 / 导航）
- 错误捕获（JS 错误 / console / 网络失败）
- 回归测试（新代码不破坏旧功能）

**适用场景**：
- 测试本地开发的 Web 应用（SPA / SSR / 静态页）
- 测试第三方网站交互流程
- 验证前端重构未破坏功能
- 生成测试报告（含截图 + 错误日志）

**不适用场景**：
- 单元测试（用 Jest / Vitest / Pytest）
- 性能测试（用 Lighthouse / k6）
- 跨浏览器兼容性测试（用 BrowserStack）

## 2. Reconnaissance-Then-Action 模式

来自 anthropics/skills webapp-testing + LocalAgent 强化。

### 2.1 核心原则：先侦察再行动

**铁律**：在动态 Web 应用上做任何 DOM 操作之前，必须先：
1. 导航到目标页面
2. 等待 `networkidle`
3. 截图或读 DOM 确认渲染状态
4. 从渲染状态识别选择器
5. 用发现的选择器执行操作

**为什么**：动态 SPA（React/Vue/Svelte）在 JS 执行前 DOM 是空的或骨架屏，提前找选择器会失败或找到错误元素。

### 2.2 networkidle CRITICAL

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')  # ⚠️ CRITICAL
    # 现在才能安全 inspect DOM
    content = page.content()
    buttons = page.locator('button').all()
```

**`networkidle` 含义**：等待 500ms 内没有网络请求（页面已加载完成、JS 已执行、异步请求已结束）。

**何时不用 `networkidle`**：
- 长轮询 / WebSocket 应用（永远不 idle）
- 持续刷新的页面（实时图表）
- 此时改用 `domcontentloaded` + 显式 `wait_for_selector`

### 2.3 决策树（静态 vs 动态）

来自 anthropics/skills webapp-testing：

```
用户任务 → 是静态 HTML 吗？
│
├─ 是 → 直接读 HTML 文件识别选择器
│  ├─ 成功 → 写 Playwright 脚本用选择器
│  └─ 失败/不完整 → 当成动态处理
│
└─ 否（动态 SPA）→ 服务器已运行吗？
   │
   ├─ 否 → 启动服务器，等待就绪后执行
   │
   └─ 是 → Reconnaissance-Then-Action:
      1. 导航 + wait networkidle
      2. 截图或 inspect DOM
      3. 从渲染状态识别选择器
      4. 用发现的选择器执行操作
```

### 2.4 后端运行检查

```python
import socket

def is_server_running(port: int = 5173, host: str = '127.0.0.1') -> bool:
    """检查后端是否在指定端口运行。"""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, ConnectionRefusedError):
        return False

if not is_server_running(5173):
    # 启动后端
    import subprocess
    subprocess.Popen(['npm', 'run', 'dev'], cwd='/path/to/frontend')
    # 等待就绪
    while not is_server_running(5173):
        import time
        time.sleep(0.5)
```

## 3. Playwright 基础

### 3.1 同步 vs 异步 API

```python
# 同步（推荐：简单）
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    # ...

# 异步（需要并发时用）
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # ...
asyncio.run(main())
```

**anthropic mcp-builder 推荐**：用 `sync_playwright()` 起步，需要并发再迁 async。

### 3.2 多浏览器测试

```python
browsers = [p.chromium, p.firefox, p.webkit]
for browser_type in browsers:
    browser = browser_type.launch(headless=True)
    page = browser.new_page()
    # 测试逻辑
    browser.close()
```

### 3.3 连接已运行的 Chrome（CDP）

来自 LocalAgent 实战（调试 Chrome 实例）：

```python
# 连接已运行的 Chrome（端口 9222）
browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")

# 获取已有上下文和页面
context = browser.contexts[0]
page = context.new_page()

# 用完不要 close()——会关闭 Chrome
# 只 close page
page.close()
```

**何时用 CDP**：
- 复用登录态（已登录的 Chrome 实例）
- 调试已运行实例
- LocalAgent 项目场景：用户有独立调试 Chrome，不影响工作浏览器

### 3.4 启动参数常用配置

```python
browser = p.chromium.launch(
    headless=True,  # 测试用 headless
    args=[
        '--no-sandbox',  # 容器环境
        '--disable-dev-shm-usage',  # 限制共享内存
        '--window-size=1920,1080',  # 固定窗口大小
    ]
)

context = browser.new_context(
    viewport={'width': 1280, 'height': 720},
    user_agent='Mozilla/5.0 ...',  # 自定义 UA
    locale='zh-CN',
    timezone_id='Asia/Shanghai',
)
```

## 4. 元素定位策略

### 4.1 优先级（来自最佳实践）

```
role > text > id > data-testid > CSS class
```

| 策略 | 例子 | 何时用 |
|------|------|-------|
| **role** | `page.get_by_role('button', name='提交')` | **首选**：语义化、抗样式变化 |
| **text** | `page.get_by_text('登录')` | 链接 / 按钮 / 标签 |
| **id** | `page.locator('#submit-btn')` | 稳定的 ID |
| **data-testid** | `page.locator('[data-testid="submit"]')` | 测试专用 |
| **CSS class** | `page.locator('.submit-button')` | **最后选择**：易变 |

### 4.2 各策略代码

```python
# role（推荐）
btn = page.get_by_role('button', name='提交')
input = page.get_by_role('textbox', name='邮箱')

# text
link = page.get_by_text('忘记密码')

# label
input = page.get_by_label('用户名')

# placeholder
input = page.get_by_placeholder('请输入邮箱')

# testid（测试专用）
btn = page.locator('[data-testid="submit-btn"]')

# CSS（最后选）
btn = page.locator('.btn-primary')
```

### 4.3 多元素处理

```python
# 所有按钮
buttons = page.locator('button').all()
for btn in buttons:
    print(await btn.text_content())

# 第一个 / 最后一个
page.locator('button').first.click()
page.locator('button').last.click()

# 第 N 个（0-indexed）
page.locator('button').nth(2).click()

# 过滤
page.locator('button', has_text='提交').click()
page.locator('li', has=page.locator('.active')).click()
```

## 5. JS 错误捕获套件

来自 LocalAgent 项目强化。

### 5.1 三类错误监听

```python
errors = []
console_messages = []
request_failures = []

page.on('pageerror', lambda err: errors.append(str(err)))
page.on('console', lambda msg: console_messages.append({
    'type': msg.type,
    'text': msg.text,
    'location': msg.location,
}))
page.on('requestfailed', lambda req: request_failures.append({
    'url': req.url,
    'method': req.method,
    'failure': req.failure,
}))

# 执行操作
page.goto('http://localhost:5173')
page.wait_for_load_state('networkidle')
page.click('button#submit')

# 检查捕获的错误
if errors:
    print(f"❌ 发现 {len(errors)} 个 JS 错误")
    for err in errors:
        print(f"  - {err}")

if request_failures:
    print(f"❌ 发现 {len(request_failures)} 个请求失败")
    for f in request_failures:
        print(f"  - {f['method']} {f['url']}: {f['failure']}")

# console 中 error / warning 也提示
errors_in_console = [m for m in console_messages if m['type'] in ('error', 'warning')]
if errors_in_console:
    print(f"⚠️ Console 有 {len(errors_in_console)} 条 error/warning")
```

### 5.2 完整测试套件封装

```python
class WebappTestSession:
    """Web 应用测试会话，封装错误捕获 + 截图 + 报告。"""

    def __init__(self, page):
        self.page = page
        self.errors = []
        self.console_messages = []
        self.request_failures = []
        self.screenshots = []

        page.on('pageerror', lambda err: self.errors.append({
            'error': str(err),
            'timestamp': page.clock.now(),
        }))
        page.on('console', lambda msg: self.console_messages.append({
            'type': msg.type,
            'text': msg.text,
        }))
        page.on('requestfailed', lambda req: self.request_failures.append({
            'url': req.url,
            'method': req.method,
            'failure': req.failure,
        }))

    def screenshot(self, name: str, full_page: bool = False):
        path = f'/tmp/test_screenshots/{name}.png'
        self.page.screenshot(path=path, full_page=full_page)
        self.screenshots.append({'name': name, 'path': path})

    def assert_no_errors(self):
        assert not self.errors, f"发现 {len(self.errors)} 个 JS 错误: {self.errors}"
        assert not self.request_failures, f"发现 {len(self.request_failures)} 个请求失败"

    def report(self) -> dict:
        return {
            'errors': self.errors,
            'console_errors': [m for m in self.console_messages if m['type'] == 'error'],
            'console_warnings': [m for m in self.console_messages if m['type'] == 'warning'],
            'request_failures': self.request_failures,
            'screenshots': self.screenshots,
        }
```

### 5.3 console.log 阈值

不是所有 console 都要 fail。建议：
- `console.error` → 必须调查
- `console.warning` → 评估是否预期
- `console.log` → 一般忽略
- `pageerror`（未捕获异常）→ **必须 fail**

## 6. 视觉验证

### 6.1 截图 + 人工/LLM 检查

```python
# 整页截图
page.screenshot(path='full_page.png', full_page=True)

# 元素截图
page.locator('.header').screenshot(path='header.png')

# 视口截图（只截可见区域）
page.screenshot(path='viewport.png')
```

### 6.2 OCR 检查文字内容

来自 LocalAgent 项目（用 `screen_ocr` MCP 工具）：

```python
# 用 LLM 检查截图
import base64
with open('screenshot.png', 'rb') as f:
    img_b64 = base64.b64encode(f.read()).decode()

# 发给多模态 LLM
response = llm.chat({
    'messages': [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': '检查这个页面：1. 是否有视觉错位？2. 是否所有按钮可读？3. 是否有错别字？'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{img_b64}'}}
        ]
    }]
})
```

**警告**：base64 数据**不要**直接进 LLM 上下文！用 MCP 工具的 `inline` 引用机制（如 `capture_screen(format="inline")`），让 MCP 协议处理，不污染文本上下文。

### 6.3 像素差异对比（回归测试）

```python
# 第一次跑：保存基准截图
page.screenshot(path='baseline.png')

# 后续跑：对比
result = page.screenshot()
expect(result).to_match_screenshot('baseline.png', max_diff_pixel_ratio=0.01)
```

Playwright 自带 `expect(page).to_have_screenshot()` 做 diff。

## 7. 测试用例设计

### 7.1 用户旅程测试（端到端）

测试用户从入口到完成目标的完整路径：

```python
def test_user_registration_flow(page):
    """测试新用户注册完整流程。"""
    session = WebappTestSession(page)

    # 1. 访问首页
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')

    # 2. 点击注册
    page.get_by_role('link', name='注册').click()
    page.wait_for_load_state('networkidle')

    # 3. 填表单
    page.get_by_label('邮箱').fill('test@example.com')
    page.get_by_label('密码').fill('SecurePass123!')
    page.get_by_label('确认密码').fill('SecurePass123!')

    # 4. 截图（人工验证）
    session.screenshot('registration_form_filled')

    # 5. 提交
    page.get_by_role('button', name='注册').click()
    page.wait_for_load_state('networkidle')

    # 6. 验证成功
    expect(page.get_by_text('注册成功')).to_be_visible()

    # 7. 断言无错误
    session.assert_no_errors()
```

### 7.2 边界情况测试

```python
def test_register_with_invalid_email(page):
    """测试无效邮箱注册。"""
    page.goto('http://localhost:5173/register')
    page.wait_for_load_state('networkidle')

    page.get_by_label('邮箱').fill('not-an-email')
    page.get_by_role('button', name='注册').click()

    # 应该有错误提示，不跳转
    expect(page.get_by_text('邮箱格式不正确')).to_be_visible()
    expect(page).to_have_url('*/register')

def test_register_with_existing_email(page):
    """测试已注册邮箱。"""
    # 预置数据
    create_user(email='existing@example.com')

    page.goto('http://localhost:5173/register')
    page.wait_for_load_state('networkidle')
    page.get_by_label('邮箱').fill('existing@example.com')
    # ...
    expect(page.get_by_text('邮箱已被注册')).to_be_visible()
```

### 7.3 回归测试

```python
# tests/test_critical_paths.py
"""关键路径回归测试：每次发版前必跑。"""

CRITICAL_PATHS = [
    ('login', test_user_login_flow),
    ('register', test_user_registration_flow),
    ('create_post', test_create_post_flow),
    ('search', test_search_function),
]

@pytest.mark.parametrize('name,test_func', CRITICAL_PATHS)
def test_critical_path(page, name, test_func):
    test_func(page)
```

## 8. 测试报告生成

### 8.1 HTML 报告

```python
import json
from datetime import datetime
from pathlib import Path

def generate_test_report(test_session: WebappTestSession, test_name: str):
    """生成 HTML 测试报告。"""
    report = {
        'test_name': test_name,
        'timestamp': datetime.now().isoformat(),
        'status': 'failed' if test_session.errors else 'passed',
        'errors': test_session.errors,
        'console_errors': [m for m in test_session.console_messages if m['type'] == 'error'],
        'request_failures': test_session.request_failures,
        'screenshots': test_session.screenshots,
    }

    # 保存 JSON
    Path('/tmp/test_reports').mkdir(exist_ok=True)
    with open(f'/tmp/test_reports/{test_name}.json', 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # 生成简单 HTML
    html = f"""<!DOCTYPE html>
<html>
<head><title>测试报告: {test_name}</title></head>
<body>
  <h1>{test_name}</h1>
  <p>状态: {'✅ 通过' if report['status'] == 'passed' else '❌ 失败'}</p>
  <p>时间: {report['timestamp']}</p>

  <h2>错误</h2>
  <pre>{json.dumps(report['errors'], indent=2, ensure_ascii=False)}</pre>

  <h2>截图</h2>
  {''.join(f'<img src="file://{s["path"]}" width="400"><br>' for s in report['screenshots'])}
</body>
</html>"""

    with open(f'/tmp/test_reports/{test_name}.html', 'w', encoding='utf-8') as f:
        f.write(html)
```

### 8.2 失败现场保留

测试失败时必须保留：
- 失败时的截图（全屏 + 视口）
- 失败时的 DOM 状态（`page.content()`）
- 失败时的 console log
- 失败时的网络请求列表
- 失败时的 URL

```python
def on_test_failure(page, test_name, error):
    """测试失败时的现场保留。"""
    Path(f'/tmp/test_failures/{test_name}').mkdir(parents=True, exist_ok=True)
    base = f'/tmp/test_failures/{test_name}'

    page.screenshot(path=f'{base}/screenshot.png', full_page=True)
    with open(f'{base}/dom.html', 'w', encoding='utf-8') as f:
        f.write(page.content())
    with open(f'{base}/url.txt', 'w') as f:
        f.write(page.url)
    with open(f'{base}/error.txt', 'w', encoding='utf-8') as f:
        f.write(str(error))
```

## 9. 与 CI 集成

### 9.1 GitHub Actions 示例

```yaml
# .github/workflows/test.yml
name: Web App Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with: { node-version: '20' }

      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }

      - name: Install backend deps
        run: pip install -r requirements.txt

      - name: Install frontend deps
        run: cd frontend && npm ci

      - name: Install Playwright
        run: |
          pip install pytest-playwright
          playwright install --with-deps chromium

      - name: Start backend
        run: |
          uvicorn main:app --port 8000 &
          sleep 3

      - name: Start frontend
        run: |
          cd frontend && npm run preview -- --port 5173 &
          sleep 5

      - name: Run tests
        run: pytest tests/e2e/ -v --html=report.html

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-reports
          path: |
            report.html
            /tmp/test_failures/
```

### 9.2 pytest-playwright 用法

```python
# conftest.py
import pytest

@pytest.fixture(scope='session')
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        'viewport': {'width': 1280, 'height': 720},
        'locale': 'zh-CN',
    }

# tests/test_homepage.py
def test_homepage_loads(page):
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')
    expect(page).to_have_title('我的应用')
```

运行：`pytest --headed --browser chromium`

## 10. 反模式

### 10.1 依赖动态 CSS class

**反模式**：
```python
page.locator('.css-1abc23')  # Tailwind / styled-components 生成的动态 class
```

class 名是构建时生成的，下次 build 可能变化 → 测试 flaky。

**正确**：
```python
page.get_by_role('button', name='提交')
page.locator('[data-testid="submit"]')
```

### 10.2 忘记 networkidle

**反模式**：
```python
page.goto('http://localhost:5173')
page.locator('button').click()  # ❌ SPA 还没渲染
```

**正确**：
```python
page.goto('http://localhost:5173')
page.wait_for_load_state('networkidle')  # ✅
page.locator('button').click()
```

### 10.3 不捕获 JS 错误

**反模式**：只检查页面是否最终正确，忽略过程中的 JS 错误。

**后果**：JS 错误可能是潜在 bug，未捕获 → 上线后才发现。

**正确**：每个测试都挂 `pageerror` / `console` / `requestfailed` 监听，断言无错误。

### 10.4 用 sleep 等待

**反模式**：
```python
page.click('button')
import time; time.sleep(2)  # ❌ 不可靠
expect(page.locator('.result')).to_be_visible()
```

**问题**：sleep 太短 → 测试失败；太长 → 测试慢。

**正确**：
```python
page.click('button')
expect(page.locator('.result')).to_be_visible()  # Playwright 自动重试到超时
# 或显式等待
page.wait_for_selector('.result', state='visible', timeout=10000)
```

### 10.5 测试间状态泄漏

**反模式**：
```python
# 测试1：登录
page.goto('/login')
page.fill('#email', 'user@test.com')
# ...

# 测试2：访问个人资料（依赖测试1的登录态）
page.goto('/profile')  # ❌ 上次登录的 cookie 还在
```

**正确**：每个测试用独立 context（自动隔离）：
```python
@pytest.fixture
def page(browser):
    context = browser.new_context()  # 每个测试新 context
    page = context.new_page()
    yield page
    context.close()  # 测试结束自动清理
```

### 10.6 测试数据硬编码

**反模式**：
```python
page.fill('#email', 'test_user@example.com')
# 数据库中必须有这个用户，否则测试失败
```

**正确**：测试前置数据，或用 mock：
```python
@pytest.fixture
def test_user(db):
    user = create_user(email=f'test_{uuid.uuid4()}@example.com')
    yield user
    user.delete()
```

### 10.7 一个测试断言过多

**反模式**：
```python
def test_everything(page):
    page.goto('/')
    # 50 个断言全塞这里
```

**问题**：失败时不知道哪里坏了；定位慢。

**正确**：单一职责，每个测试只验证一个行为。

## 11. 测试套件检查清单

新项目构建测试套件时按此清单逐项确认：

- [ ] 选定 Playwright API（sync 起步）
- [ ] 配置多浏览器测试（chromium + firefox + webkit）
- [ ] 每个测试都挂 `pageerror` / `console` / `requestfailed` 监听
- [ ] 每个测试都在操作前 `wait_for_load_state('networkidle')`
- [ ] 用 role / text / data-testid 而非 CSS class
- [ ] 测试间用独立 context 隔离状态
- [ ] 失败时自动截图 + 保留 DOM + 错误日志
- [ ] 关键路径有回归测试
- [ ] CI 集成（GitHub Actions）
- [ ] 测试报告可读（HTML 含截图）
- [ ] 测试数据预置 + 清理
- [ ] 不用 sleep，用 expect / wait_for_*

## 12. 参考资源

- **Playwright 官方文档**：`https://playwright.dev/python/`
- **pytest-playwright**：`https://github.com/microsoft/playwright-python`
- **anthropics/skills webapp-testing**：`https://github.com/anthropics/skills/blob/main/skills/webapp-testing/SKILL.md`
- **LocalAgent html-dev-debug skill**：`f:\<project_root>\.agents\skills\html-dev-debug.md`
- **LocalAgent Chrome 调试实例启动**：`f:\<project_root>\tools\browser\start_debug_chrome.py`
- **环境兼容性约束（Playwright / Chrome）**：`f:\<project_root>\docs\environment-constraints.md`
