---
aliases: [小黑盒, xiaoheihe]
---

# 小黑盒 (xiaoheihe.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | xiaoheihe.cn |
| **首次接触日期** | 2026-07-13 |
| **相关 skill** | web_archive（帖子存档）、arknights_gacha（小黑盒导入寻访数据） |
| **登录态要求** | 匿名可读帖子；写操作（点赞/评论）需登录 |
| **抓取频次** | 周期（web_archive 按需，arknights_gacha 全量历史一次性） |
| **文件创建日期** | 2026-07-18 |
| **最后更新** | 2026-07-18 |

## 网站概况

游戏社区论坛，主要内容是玩家帖子（图文帖 + 文章帖）。agent 任务主要是批量存档帖子为 Markdown（含正文+评论+图片），以及从小黑盒导出的 JSON 中导入明日方舟寻访记录。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | **禁止** | 用户明确要求（怕封 IP/反爬）+ Playwright 无头在本环境报 `Executable doesn't exist at chromium_headless_shell` |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `chrome_debug/` user-data-dir |
| 反风控 | 不主动调 API，纯 DOM 提取 | 模拟人类滚动 |

## 反爬/风控

- **触发条件**：未观察到主动反爬，但用户要求保留登录态、禁止无头
- **规避方法**：用调试浏览器（带登录态）+ Playwright DOM 提取，不调私有 API
- **风险阈值**：批量抓取 38 个链接未触发限制；单帖评论滚动 780 条未触发限制

## DOM 结构与提取规则

### 字段提取表

| 字段 | 选择器 | 注意事项 |
|------|--------|---------|
| **标题** | URL `redirect_data` 参数 → JSON 解码 `link.title` | **不从 DOM 取**——DOM 标题会取到"立即下载小黑盒APP"占位。无 `redirect_data`（如跨年帖）从 DOM `.link-section-title` 取 |
| **作者** | `.link-user__username` | **不是 `.info-box__username`**——后者在含评论的 DOM 上会取到首个评论者 |
| **正文** | 见下方"页面布局变体" | 两种布局必须都支持 |
| **发布时间/IP** | `.link-section-link-data` 的 innerText | 格式 `"07-04\n广东"`（当年）或 `"2024-04-26\n上海"`（跨年），按 `\n` 分割；当年帖自动补抓取年份 |
| **评论总数** | `.slide-tab__tab-cnt` 的 innerText | 回退到主评论数 |
| **主评论** | （见 web_archive/extract_web.py） | 按点赞倒序 |
| **子回复** | `.comment-children-item` | **单横线**，不是 `.comment-children__comment-item`（双下划线） |

### 页面布局变体（必须都支持）

- **图文帖**：根 `.hb-bbs-image-text` → 正文在 `.image-text__content`
- **文章帖**：根 `.hb-bbs-post` → 正文在 `.hb-article`（含 `p.text` + `blockquote` + `h2.main-title` + `div.img`）
- 作者选择器 `.link-user__username` 两种布局通用

### 评论展开机制

循环点击 `button.comment-children__load-all`，**每轮重查按钮**（展开后会产生新按钮）。配合 LLM 评估：每 20 条新评论触发一次 cheap 模型评估，连续 2 批评分 ≤2 则停止滚动（避免垃圾评论区无限抓取）。

### 选择器优先级与回退

```javascript
// 正文容器回退
const content = document.querySelector('.image-text__content') 
             || document.querySelector('.hb-article');
```

## URL 规则与重定向

- **`redirect_data` 参数**：URL 形如 `https://www.xiaoheihe.cn/community/{id}?redirect_data=<URL编码的JSON>`
  - JSON 含 `link.title`（真实标题）、可能含其他元信息
  - 解码方式：`urllib.parse.unquote` → `json.loads` → `['link']['title']`
- **无 `redirect_data` 的情况**：跨年帖等不带此参数，从 DOM `.link-section-title` 取标题
- **简化 URL**：存档时截断 `redirect_data` 参数，只保留 `https://www.xiaoheihe.cn/community/{id}`

## 图片/资源加载

| 项 | 规则 | 备注 |
|----|------|------|
| 懒加载 | 把 `img.src` 显式写回 `setAttribute('src', img.src)` | 解决懒加载占位符导致 `innerHTML` 序列化丢失真实 URL |
| 孤儿图片 | 先 `setAttribute` 再序列化 | 否则图片下载了但正文未引用 |
| 正文换行 | 遍历 TextNode，把 `\n` 替换成 `<br>`，再序列化 | 直接用 `innerHTML` 序列化会丢失换行，正文挤成一段 |
| 防盗链 | 未观察到 | 小黑盒图片可直接下载 |

## 评论/动态内容

| 项 | 选择器/规则 | 备注 |
|----|-----------|------|
| 评论容器 | （见 extract_web.py） | 按点赞倒序，主评论和子回复都按赞数排序 |
| 主评论项 | （见 extract_web.py） | 高赞在前 |
| 子回复项 | `.comment-children-item` | 单横线，**非** `.comment-children__comment-item` |
| 展开机制 | 循环点击 `button.comment-children__load-all` | 每轮重查按钮，展开后会产生新按钮 |
| 评论总数 | `.slide-tab__tab-cnt` innerText | 回退到主评论数 |
| 异步加载 | 滚动加载 | 每滚一篇触发新评论加载 |
| 评分停止 | 每 20 条触发 LLM 评估，连续 2 批 ≤2 停止 | 避免垃圾评论区无限抓取 |

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| **标题乱码/取到"立即下载小黑盒APP"** | 从 DOM 取标题 | 从 URL `redirect_data` 参数 JSON 解码 `link.title`；无 `redirect_data` 时从 DOM `.link-section-title` 取 | 2026-07-13 |
| **正文混入评论/作者卡/标签/页码** | clone `.hb-cpt__scroll-list` 后删元素 | 精确定位正文容器（图文帖 `.image-text__content` / 文章帖 `.hb-article`） | 2026-07-13 |
| **作者取到评论者** | `.info-box__username` | `.link-user__username` | 2026-07-13 |
| **子回复提取为 0** | `.comment-children__comment-item` | `.comment-children-item`（单横线不是双下划线） | 2026-07-13 |
| **文章帖正文为 0** | 只支持图文帖布局 | 文章帖走第二种布局（`.hb-article`） | 2026-07-13 |
| **正文残留页码/swiper 按钮** | 整个 scroll-list innerText | 只提取正文容器内的 `img` + 文本 | 2026-07-13 |
| **图片下载了但正文未引用（孤儿图片）** | 直接用 innerHTML 序列化 | 先把 `img.src` 显式写回 `setAttribute('src', img.src)` | 2026-07-13 |
| **正文换行丢失（挤成一段）** | 直接用 innerHTML 序列化 | 遍历 TextNode，把 `\n` 替换成 `<br>`，再序列化 | 2026-07-13 |
| **untitled 链接正文为 0** | 当 bug 调试 | 正常，无效链接不入 index.json | 2026-07-13 |
| **图片帖正文只有几个字** | 当 bug 调试 | 正常，主要内容在图片里 | 2026-07-13 |
| **当年帖发布时间无年份** | 直接用 | 自动补抓取年份；跨年帖才有完整年份 | 2026-07-13 |

## 遗留问题

- **小黑盒评论 LLM 评估依赖后端 LLM 池**：后端不可用时返回 3（中性），不阻断抓取但不优化停止策略
- **`--overwrite` 不覆盖同名文件**：`dedupe_filename` 在文件已存在时仍加 `_2` 后缀（web_archive/extract_web.py 未传 overwrite 参数）。测试时需先清空目录再跑

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-07-18 | 初始创建，汇总自 `.agents/skills/web_archive/SKILL.md` 和 `server/agent_guide.py` 的 `adhoc.web_archive` key_pitfalls |
