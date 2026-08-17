---
name: web_access
description: >
  统一的联网工具选择决策表。当 agent 需要从网络获取信息时，先按决策表选择最合适的工具路径
  （WebSearch / WebFetch / CDP / curl / 本地书签检索 / Jina Reader），避免选错工具浪费时间或拿到空内容。
  触发词：联网、上网查、搜一下、读网页、读链接、抓网页、web访问、搜索、fetch、爬取。
  适用范围：任何需要从网络获取信息的任务（详见正文）。
---

# 联网工具选择决策表 (web_access)

## 核心理念

**联网任务的第一步不是"打开浏览器"，而是"选对工具"。** 不同工具的适用场景差异巨大，选错会导致浪费时间（静态页上 CDP）或拿到空内容（登录态页面用 WebFetch）。本 skill 提供一张决策表，agent 在任何联网任务前先据此选择路径。

## 联网工具选择决策表

按目标特征从上到下匹配，命中即用：

| # | 目标特征 | 推荐路径 | 本项目对应工具 | 理由 |
|---|---------|---------|---------------|------|
| 1 | 只需摘要/概念/最新进展（不知道具体 URL） | **WebSearch** | `web_search` MCP 工具，或 `exec_python` + `requests` 抓搜索结果页 | 最快，不打开浏览器 |
| 2 | URL 明确 + 公开静态页（无需登录、无 JS 渲染关键内容） | **WebFetch** | `web_fetch(url)` MCP 工具，或 `exec_python` + `requests.get(url)` | 比 CDP 快 10 倍，无浏览器开销 |
| 3 | URL 明确 + 需登录态 / JS 渲染 / 反爬强 / 需交互 | **CDP** | `browser_open` + Playwright `connect_over_cdp("http://127.0.0.1:9222")` | 带登录态，能执行 JS，能交互 |
| 4 | 找用户访问过但没公开收藏的页面（内部系统、历史访问） | **本地书签/历史检索** | `browser_find_url(keyword, scope, limit)` MCP 工具 | 公网搜不到的页面，查本地浏览器数据 |
| 5 | 需要搜索引擎结果页的原始 HTML（SERP 结构分析） | **curl 抓 SERP** | `exec_python` + `requests.get(search_engine_url)` | WebSearch 只返回摘要，要原始结果页用 curl |
| 6 | 搜索结果被反爬 / 需 token / 要转 Markdown | **Jina Reader** | `exec_python` + `requests.get("https://r.jina.ai/" + url)` | 绕过反爬，输出干净 Markdown，免费 20 RPM |

## 决策流程图

```
要联网获取信息
  │
  ├─ 知道具体 URL 吗？
  │   ├─ 否 → 需要搜索
  │   │   ├─ 只要摘要/概念 → WebSearch（路径 1）
  │   │   └─ 要 SERP 原始 HTML → curl 抓搜索结果页（路径 5）
  │   │
  │   └─ 是 → 页面有什么特征？
  │       ├─ 公开 + 静态 + 无登录 → WebFetch（路径 2）
  │       ├─ 需登录 / JS 渲染 / 反爬 → CDP（路径 3）
  │       ├─ 被反爬挡住 / 要 Markdown → Jina Reader（路径 6）
  │       └─ 不确定页面是否需登录 → 先 WebFetch 试一次，空内容再降级 CDP
  │
  └─ 找的是"用户访问过但没收藏的页面"吗？
      └─ 是 → 本地书签/历史检索（路径 4）
```

## 各工具详解

### 路径 1：WebSearch（搜索摘要）

**适用**：找概念、找最新进展、不知道具体 URL
**工具**：`web_search` MCP 工具（如可用），或 `exec_python` 调搜索 API
**返回**：搜索结果摘要列表（标题 + 摘要 + URL）
**不适用**：需要页面完整正文、需要登录态页面

### 路径 2：WebFetch（抓取静态页）

**适用**：URL 明确、页面公开、关键内容在 HTML 源码中（非 JS 渲染）
**工具**：`web_fetch(url)` MCP 工具，或 `exec_python` + `requests.get(url)`
**返回**：页面正文（通常已转 Markdown）
**不适用**：
- 需要登录才能看内容的页面（WebFetch 不带 cookie，拿到登录页）
- JS 渲染的 SPA 页面（WebFetch 不执行 JS，拿到空壳）
- 反爬严格的页面（WebFetch 无浏览器指纹，可能被挡）

**降级信号**：WebFetch 返回内容明显过短（<100 字）、或包含"请登录"、"403"、"Access Denied" → 降级到 CDP（路径 3）

### 路径 3：CDP（调试浏览器 + Playwright）

**适用**：需登录态、JS 渲染、反爬强、需交互（点击/滚动/填表）
**工具**：`browser_open` + `exec_python` 执行 Playwright `connect_over_cdp("http://127.0.0.1:9222")`
**返回**：渲染后的完整 DOM、可交互
**前置**：调试浏览器已启动（`tools/browser/start_debug_browser.py`）
**成本**：最慢（启动连接 + 渲染 + 等待 networkidle），但最可靠
**必读**：操作前先查 `browser_lessons` 的站点经验（见下"与 browser_lessons 的配合"）

### 路径 4：本地书签/历史检索

**适用**：找用户访问过但没公开收藏的页面（内部系统、历史访问过的页面）
**工具**：`browser_find_url(keyword, scope="bookmarks|history|all", limit=20)` MCP 工具
**返回**：匹配的书签/历史记录列表（title + url + 访问次数 + 最后访问时间）
**数据源**：调试浏览器的 `chrome_debug/Default/Bookmarks`（JSON）和 `chrome_debug/Default/History`（SQLite）（目录可配置）
**注意**：只查调试浏览器的数据，不碰用户工作浏览器（隔离原则）

### 路径 5：curl 抓 SERP

**适用**：需要搜索引擎结果页的原始 HTML 结构（如分析搜索结果布局、批量提取结果 URL）
**工具**：`exec_python` + `requests.get(search_engine_url, headers={"User-Agent": "..."})`
**返回**：搜索结果页原始 HTML
**注意**：需带 User-Agent，否则可能被搜索引擎拒；大量请求可能触发验证码

### 路径 6：Jina Reader（反爬/Markdown 化）

**适用**：搜索结果被反爬挡住、页面需 token、要把页面转成干净 Markdown
**工具**：`exec_python` + `requests.get("https://r.jina.ai/" + url)`
**返回**：页面正文的 Markdown 版本（去掉导航/广告/侧边栏）
**限制**：免费 20 RPM，适合少量页面；不适合批量抓取
**示例**：
```python
import requests
resp = requests.get(f"https://r.jina.ai/https://example.com")
print(resp.text)  # 干净的 Markdown 正文
```

## 与 browser_lessons 的配合

**选了路径 3（CDP）后，必须先查站点经验**：

1. 调 `browser_match_site(domain="<目标主域>")` MCP 工具，或手动读 `.agents/skills/browser_lessons/sites/<domain>.md`
2. 把"已知坑"和"DOM 结构"段读入上下文，避免重复踩坑
3. 任务收尾时，若遇到非显然行为，按 `browser_lessons` 规则写入对应 sites 文件

> 详见 `.agents/skills/browser_lessons/SKILL.md`。

## 与现有 skill 的关系

本 skill 是**联网任务的入口决策层**，不替代具体 skill 的工作流：

| skill | 角色 | 与本 skill 的关系 |
|-------|------|------------------|
| `web_access`（本 skill） | 联网工具选择决策 | **入口**：任何联网任务先来这选工具 |
| `niuke_review` | 面经抓取+评分 | 工作流内用 WebSearch+WebFetch，本 skill 解释为什么这么选 |
| `web_archive` | 网页批量存档 | 工作流内用 CDP，本 skill 解释为什么不用 WebFetch |
| `community_review` | 社群帖子抓取 | 工作流内用 CDP（需登录态），本 skill 解释选型理由 |
| `browser_lessons` | 分网站踩坑经验 | 路径 3（CDP）选中后的必读依赖 |

## 常见误用

| 误用 | 后果 | 正确做法 |
|------|------|---------|
| 对公开静态页用 CDP | 浪费 10-30 秒启动浏览器 | 先用 WebFetch，空内容再降级 |
| 对登录态页面用 WebFetch | 拿到登录页/空内容 | 直接用 CDP（带登录态） |
| 对 JS 渲染的 SPA 用 WebFetch | 拿到空壳 HTML | 用 CDP 或 Jina Reader |
| 不知道 URL 就用 CDP 去 Google 搜 | 慢且不必要 | 先 WebSearch 拿 URL，再决定路径 |
| 找用户历史访问的页面用 WebSearch | 公网搜不到内部页面 | 用本地书签/历史检索（路径 4） |

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| WebSearch | `web_search` MCP 工具 | 搜索摘要 |
| WebFetch | `web_fetch` MCP 工具 | 抓取静态页 |
| CDP | `browser_open` + Playwright `connect_over_cdp(9222)` | 调试浏览器 |
| 本地书签/历史检索 | `browser_find_url` MCP 工具 | 查调试浏览器的书签/历史 |
| curl | `exec_python` + `requests` | 抓 SERP 原始 HTML |
| Jina Reader | `exec_python` + `requests.get("https://r.jina.ai/" + url)` | 反爬/Markdown 化 |
