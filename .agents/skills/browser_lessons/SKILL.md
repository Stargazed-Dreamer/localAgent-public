---
name: browser_lessons
description: >
  分网站记录浏览器操作经验。任何涉及浏览器的任务开始前，agent 必须按 URL 主域查
  sites/<domain>.md 获取踩坑经验；任务收尾时把非显然行为（DOM 特性/反爬/登录态/防盗链等）
  写入对应网站文件。触发词：浏览器踩坑、网站经验、browser lessons、网站坑、DOM 坑。
  适用范围：任何调用 browser_* MCP 工具或 Playwright 的任务（详见正文完整列表）。
task_type: adhoc.browser_automation
---

# 分网站浏览器操作经验（browser_lessons）

## 核心理念

**浏览器操作的踩坑是按网站维度积累的，不是按 skill 维度。** 同一网站（如 `xiaoheihe.cn`）可能被多个 skill 操作；同一 skill（如 `web_archive`）也可能操作多个网站。按网站维度组织经验，让跨 skill 的踩坑得以汇总，避免"同样一个坑，第二个 skill 又踩一遍"。

## 与现有 `key_pitfalls` 的关系

| 类型 | 存放位置 | 例子 |
|------|---------|------|
| **网站通用踩坑**（DOM 结构、反爬、URL 规则、登录态、防盗链） | `sites/<domain>.md` | "小黑盒标题从 URL redirect_data 解码" |
| **skill 特定踩坑**（与该 skill 工作流强绑定，换网站就不适用） | `agent_guide.py` 的 `key_pitfalls` | "LLM 评分连续 2 批 ≤2 停止滚动" |
| **跨网站通用浏览器踩坑**（Playwright/CDP 通用问题） | AGENTS.md "API 常见陷阱" + 本 skill SKILL.md 末尾 | "必须用 connect_over_cdp，禁止无头" |

## 工作流

### 任务开始前（必做）

任何涉及浏览器的任务，在 `agent_guide` 返回指导后、第一次 `browser_session_create` / `browser_navigate`（或 Playwright `page.goto`）之前：

1. **识别目标网站主域**：从用户给的 URL 或任务描述提取。主域 = eTLD+1（如 `https://www.xiaoheihe.cn/community/123` → 主域 `xiaoheihe.cn`）
2. **查站点经验**（两种方式，推荐第一种）：
   - **推荐**：调 `browser_match_site(domain="xiaoheihe.cn")` MCP 工具，一键返回命中文件的正文
   - **手动**：查 `references/site_index.md` 看该主域是否有已记录的 sites 文件，有则 `Read sites/<domain>.md`
3. **若有命中**：把"已知坑"和"DOM 结构"段读入上下文
4. **若无命中**：任务中遇到非显然行为时按 `sites/_template.md` 新建（见下）

### 任务收尾时（强制规则）

`task_closure` 的 step 5（文档自查）必须增加一项检查：

> 本次浏览器操作是否遇到非显然行为？
> - **是** → 写入 `sites/<domain>.md`（已有则追加新坑段；无则按 `sites/_template.md` 创建）
> - **否** → 跳过

**"非显然行为"的判定标准**（满足任一即记录）：
- DOM 选择器与"看起来对"的不一致（如小黑盒作者应该是 `.info-box__username` 但实际取到评论者，正确是 `.link-user__username`）
- URL 有特殊参数携带真实数据（如小黑盒 `redirect_data` 含标题 JSON）
- 同一网站有多种页面布局（如小黑盒图文帖 vs 文章帖）
- 反爬/风控机制（如微信图片防盗链需 Referer）
- 登录态特殊处理（如必须用调试浏览器而非无头）
- 懒加载/异步渲染导致选择器失效（如微信图片 `data-src` 而非 `src`）
- 已知遗留问题（如微信评论 Vue 异步未渲染，需调 API）

**不需要记录的**：
- 通用 Playwright/CDP 用法（如 `wait_for_load_state('networkidle')`）
- 一次性 bug（已修复，不会再复现）
- skill 工作流内部决策（如"按点赞倒序排序"——这是 skill 选择，不是网站特性）

## 网站识别规则

- **主域 = eTLD+1**：用 `tldextract` 或手动判断。例如：
  - `https://www.xiaoheihe.cn/community/123` → `xiaoheihe.cn`
  - `https://mp.weixin.qq.com/s/abc` → `mp.weixin.qq.com`（微信主域）
  - `https://t.bilibili.com/123` 和 `https://space.bilibili.com/456` → 都归 `bilibili.com`
  - `https://bwiki.rs/arknights/...` → `bwiki.rs`
- **文件命名**：`sites/<主域点改横线>.md`，如 `xiaoheihe.cn` → `xiaoheihe.md`，`mp.weixin.qq.com` → `weixin_mp.md`
- **跨子域归并**：同一注册域下不同子域视为同一网站（`t.bilibili.com` 和 `space.bilibili.com` 都是 `bilibili.com`），除非不同子域有显著不同的 DOM 结构（此时分文件并在 `site_index.md` 注明）

## sites/ 文件结构

每个 `sites/<domain>.md` 文件按 `sites/_template.md` 的结构组织：

```markdown
# <网站名> (<主域>)

## 元信息
- 主域、首次接触日期、相关 skill、登录态要求

## 网站概况
（一句话定位）

## 浏览器方案
（连接方式、登录态、无头/有头）

## 反爬/风控
（如有）

## DOM 结构与提取规则
（按"字段 → 选择器 → 注意事项"表格组织）

## URL 规则与重定向
（如有特殊参数）

## 图片/资源加载
（懒加载、防盗链等）

## 评论/动态内容
（展开机制、加载方式）

## 已知坑（与正常行为区分）
（踩坑总结表）

## 遗留问题
（已知未解决的）
```

详细模板见 [sites/_template.md](file:///<project_root>/.agents/skills/browser_lessons/sites/_template.md)。

## 跨网站通用浏览器踩坑（项目级）

这些不归于任何具体网站，统一记录在此：

### 三种点击方式的区分（重要！）

Playwright 提供三种点击机制，适用场景不同，选错会导致反复失败：

| 方式 | API | 机制 | 适用场景 |
|------|-----|------|---------|
| **常规点击** | `page.click(selector)` / `locator.click()` | 滚动到可见 + 等待可点击 + JS click | 常规可见按钮，最常用 |
| **JS 派发点击** | `locator.dispatch_event("click")` | 直接 `el.dispatchEvent(new MouseEvent('click'))` | 有浮层/弹窗遮挡、元素不可见但存在；不受遮挡影响 |
| **真实鼠标点击** | `page.mouse.move(x, y)` + `page.mouse.click(x, y)` | CDP `Input.dispatchMouseEvent`（真实鼠标事件） | 需要触发真实 hover/focus 效果（如菜单展开）、需要真实用户行为特征 |
| **文件上传** | `page.set_input_files(selector, [paths])` | `DOM.setFileInputFiles` | 文件上传，绕过可见可点击要求，不要试图模拟点击触发文件选择器 |

**点击失败降级链**（遇到 click 失败时按此顺序尝试）：

```
page.click(selector)  失败（超时/遮挡）
  ↓
locator.dispatch_event("click")  （JS 层面，不受遮挡影响）
  ↓
page.mouse.move(x, y) + page.mouse.click(x, y)  （真实鼠标，需先获取元素坐标）
  ↓
截图人工确认  （可能选择器本身错了，不是点击方式的问题）
```

### 通用踩坑表

| 坑 | 规避 |
|----|------|
| Playwright 无头模式在本环境报 `Executable doesn't exist at chromium_headless_shell` | 必须用调试浏览器 + `connect_over_cdp("http://127.0.0.1:9222")` |
| 用户要求禁止无头（怕封 IP/反爬） | 同上 |
| 调试浏览器必须独立 `user-data-dir`（默认 `chrome_debug/`） | `tools/browser/start_debug_browser.py` 自动处理（支持 `--user-data-dir` 自定义） |
| `scroll_into_view_if_needed` 对 `display:none` 元素 3 秒超时 | 改用 `page.evaluate("el.scrollIntoView()")`，JS 层面不检查可见性 |
| `page.click()` 被浮层/弹窗遮挡报错 | 改用 `locator.dispatch_event("click")`；或先关弹窗再点 |
| 需要触发真实 hover/focus 效果（菜单展开） | 用 `page.mouse.move(x,y)` + `page.mouse.click(x,y)`，不用 `dispatch_event` |
| 文件上传 | `page.set_input_files(selector, [paths])`，不要模拟点击触发文件选择器 |
| Python 写 JS 的 `split('\n')` 转义层级易错 | `"split('\\n')"` → JS `split('\n')`；改 JS 后单样本必跑 |
| `innerText` 跳过 `display:none`，`innerHTML` 含它们 | 不要 clone 容器再删元素，而是只拼接需要的元素 |
| 动态页面 `networkidle` 前查 DOM 得到不完整结构 | 用 `wait_for_load_state('networkidle')` **或** `domcontentloaded` + 固定等待；⚠ **有长连接/长轮询的站点（如 js.design）`networkidle` 永不触发**（实测 `goto(wait_until="networkidle")` 45s 超时）→ 这类站点一律用 `domcontentloaded` + `wait_for_timeout(3000~6000)` |
| 跨子域同名组件 DOM 结构可能不同 | 永远加回退：`document.querySelector('.A') \|\| document.querySelector('.B')` |
| **误判"CDP 浏览器还活着"** | 后端 `/browser/tabs` 秒回是**缓存列表，不算证据**。判活必须做**真实往返**（`/browser/evaluate` 或 playwright `evaluate`） |
| 浏览器进程持端口但 `tasklist`/`wmic` 查不到、`taskkill` 报权限不足（rc=128） | 见下方「调试浏览器 CDP 假死：诊断与恢复」 |
| 用 `file:///…svg` 直接开 SVG 时注入脚本报 `Cannot read properties of null (reading 'style')` | SVG 当文档打开时**根节点就是 `<svg>`，没有 `<body>`**。套一层 HTML，用 `<img src="file://…svg">` 承载再操作 |

### 调试浏览器 CDP 假死：诊断与恢复（2026-09-19 实测）

**症状**（三者同时出现即确诊"命令面假死"，不是压力问题也不是脚本问题）：

| 探针 | 假死时的表现 |
|---|---|
| `curl http://127.0.0.1:9222/json/version` | ✅ **正常返回**（HTTP 层还活着，会误导人） |
| playwright `connect_over_cdp(...)` | `ws connecting` → `ws connected` 之后 **180s 超时**（浏览器端不回 handshake） |
| 后端 `POST /browser/evaluate` | `BROWSER_DISCONNECTED`（约 10 s 后失败） |
| 后端 `GET /browser/tabs` | ⚠️ **秒回**——但那是后端缓存的标签列表，**不能当证据** |

**恢复步骤（顺序不能错）**：

1. **先精确定位根进程**：`netstat -ano | grep ":9222"` 拿 LISTENING 的 pid
   （不要靠 `tasklist`/`wmic` 找 `chrome.exe`——实测进程名可能查不到，而端口确实被它占着）。
   再用 `wmic process get Name,ProcessId,ParentProcessId,CommandLine /format:csv`
   确认**只有这一个调试实例**（按 `--user-data-dir` 分组，避免误伤用户的日常浏览器）。
   调试浏览器的父进程应当是后端 pid（被后端托管拉起）。
2. **杀树要交给后端做**：Bash 工具通道的 `taskkill /T /F /PID <pid>` **权限不够**
   （实测 rc=128，只杀掉子进程，根进程仍占着端口）。
   必须走 `POST /terminals/spawn`，`cmd_list=["taskkill","/T","/F","/PID","<pid>"]` —— 后端有管理员权限。
3. **确认端口已释放**再拉新实例：`netstat -ano | grep ":9222"` 应只剩 `TIME_WAIT` 或空。
   ⚠ **端口没释放就拉 = 白拉**：Chrome 走**单实例交接**，新进程 0.18 s 就以 exit_code 0 退出，
   你以为起了、其实还是那个假死的在顶。
4. **重新托管拉起**：

   ```
   POST /terminals/spawn
   {"cmd_list": ["<chrome.exe 绝对路径>",
                 "--remote-debugging-port=9222",
                 "--remote-allow-origins=*",
                 "--user-data-dir=<chrome_debug 绝对路径>",
                 "--no-first-run", "--no-default-browser-check",
                 "<要开的 URL 1>", "<URL 2>"],
    "label": "debug-browser-9222"}
   ```

   配方与 `tools/browser/start_debug_browser.py` 一致（后者用 `subprocess.Popen` 起，
   在工具通道里会被回收——**托管必须走后端**）。
5. **验收**：`/json/version` 就绪 ≠ 修好。必须做一次**真实往返**才算通过：
   playwright `connect_over_cdp` 应 < 1 s，`evaluate("() => 2+2")` 应 < 0.1 s。实测修复后 connect 0.3 s、往返 0.03 s。

**登录态不受影响**：cookie 存在 `chrome_debug` profile 里，同机同账户 DPAPI 可解 →
重启后砺儒云等站点**仍是登录态**（2026-09-19 实测活过重启）。只有换机器/换用户才失效。

### 浏览器操作通用原则（像人一样思考）

**目标驱动而非步骤驱动**——讲清 tradeoff 让 AI 自己选，不替它推理。

1. **先看再操作**：任何交互前先 `page.evaluate` 确认当前 URL/标题/DOM 状态，避免在错误页面操作
   ```python
   info = await page.evaluate("""() => ({
       url: location.href, title: document.title,
       ready: document.readyState
   })""")
   ```
2. **读快写慢**：
   - **纯读取**（抓内容）：用 `page.evaluate` / `locator.all_text_contents()` 一步到位，快速
   - **写操作**（点击/输入/提交）：操作前截图确认，尤其涉及登录态/付费/发帖等不可逆操作
3. **不确定时用截图验证**：涉及登录态、付费、发帖等不可逆操作，用 `page.screenshot(path=...)` 看一眼再操作；纯读取不用截图
4. **降级链思维**：`click()` 失败 → `dispatch_event("click")` → `mouse.click(x,y)` → 截图人工确认（见上方降级链）

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 调试浏览器 | `tools/browser/start_debug_browser.py` | 启动 CDP 9222 独立实例（支持 Chromium 内核：Chrome/Edge/Brave/Vivaldi） |
| 浏览器库 | Playwright `connect_over_cdp` | 连接调试浏览器 |
| 网站索引 | `references/site_index.md` | 已记录网站清单 + 主域 → 文件映射（人类可读汇总，agent 查询优先用 `browser_match_site`） |
| 站点经验查询 | `browser_match_site(domain=...)` MCP 工具 / `tools/browser/match_site.py` | 按域名一键查询站点经验，替代手动读 site_index + sites 两步 |
| 本地书签/历史检索 | `browser_find_url(keyword=..., scope=...)` MCP 工具 / `tools/browser/find_url.py` | 查调试浏览器的书签/历史，找公网搜不到的页面 |
| 文件模板 | `sites/_template.md` | 新网站文件模板（含 aliases frontmatter） |

## 与 AGENTS.md 的硬性规则配合

AGENTS.md "API 常见陷阱" 章节已加入硬性规则：

> **任何浏览器操作任务（含 `browser_*` 工具调用、Playwright、`connect_over_cdp`）在 task_closure 时必须检查：本次是否遇到非显然行为？若是，写入 `.agents/skills/browser_lessons/sites/<domain>.md`。无对应文件时按 `_template.md` 创建。**

agent_guide 中所有浏览器相关 task_type 的 `key_pitfalls` 第一条已加索引指向：

> "先读 `.agents/skills/browser_lessons/sites/<domain>.md`（按目标 URL 主域查 `references/site_index.md`）"

## 维护

- **新建网站文件**：按 `sites/_template.md` 创建，文件名为主域（点改横线），同步更新 `references/site_index.md`
- **追加踩坑**：在对应 `sites/<domain>.md` 的"已知坑"段追加，注明发现日期
- **过时清理**：网站改版后旧选择器失效时，在原条目加 `> 已失效（YYYY-MM-DD）`，不要直接删除——保留历史信息供回滚参考
- **跨网站汇总**：每年或在大量踩坑后，用 `neat-freak` 检查是否有重复模式可提取到 SKILL.md 的"跨网站通用踩坑"段
