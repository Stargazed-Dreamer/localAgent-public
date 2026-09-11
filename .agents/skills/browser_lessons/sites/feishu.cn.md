---
aliases: [飞书, 飞书云文档, feishu, 飞书知识库]
---

# 飞书云文档 (feishu.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | feishu.cn（含 `my.feishu.cn`、`{tenant}.feishu.cn`、`internal-api-drive-stream.feishu.cn`） |
| **首次接触日期** | 2026-08-29 |
| **相关 skill** | web_archive（`workspace/web_archive/extract_feishu.py`） |
| **登录态要求** | 分享链接**游客态即可读全文**；仅私有文档需登录 |
| **抓取频次** | 一次性（按需） |
| **文件创建日期** | 2026-08-29 |
| **最后更新** | 2026-08-29 |

## 网站概况

飞书云文档（`/docx/{token}`）与知识库页面（`/wiki/{token}`）。agent 在此只做一件事：把分享页存档为 Markdown（正文 + 图片 + 表格 + 代码块），无评论区。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 无头下飞书渲染管线不完整 |
| 登录态来源 | 无需登录 | 顶栏显示「登录/注册」但正文不折叠 |
| 页面就绪等待 | `goto(domcontentloaded)` + `wait_for_timeout(6500)` | 文档块是异步挂载，早于 6s 快照会拿到空容器 |

## 反爬/风控

- **触发条件**：未见频控告警；单文档一次滚动约 12–27 步，间隔 240ms，实测安全
- **规避方法**：复用真实浏览器实例 + 真实 cookie，不构造请求头
- **风险阈值**：（未知，未压测量）

## DOM 结构与提取规则

### 字段提取表

| 字段 | 选择器 | 注意事项 |
|------|--------|---------|
| 文档容器 | `.bear-web-x-container` | 唯一滚动容器，`scrollTop/scrollHeight` 都在这上面 |
| 根块 | `[data-block-type="page"]` | 其 `data-record-id` **就是 docx 的 obj_token**（wiki 链接也能直接拿到，省掉 `wiki/v2/tree/get_info`） |
| 块 | `[data-block-id]` + `data-block-type` | id 是**文档内递增整数**，可用于漏抓自检（1..max 缺口） |
| 标题 | `.note-title__input-text` | 回退 `document.title` 去掉 ` - 飞书云文档` 后缀 |
| 最近修改 | `.note-meta__desc` | 文案是"最新修改时间为08月29日"，非绝对日期 |
| 行内文本 | `.ace-line` | 含零宽字符 `\u200b`，必须清掉 |
| 图片 token | `[image-token]` | 比 `img[src]` 可靠（src 会被换成 `blob:`） |
| 代码块 | `.cm-line` | 语言在 `[data-lang]` / `.lang` / `class*='language-'` |
| 有序列表序号 | `.order` | 直接读 DOM 序号，不要自己计数（分段会重置） |
| 高亮 | `.text-highlight-background*` | → `==文字==` |
| 文档互链 | `a.mention-doc` | → `[标题](href)` |

### 页面布局变体

- **docx**：`https://{host}/docx/{token}`
- **wiki**：`https://{host}/wiki/{token}` → 渲染出的 DOM 与 docx 同构，抓取逻辑通用
- **docs（旧）**：`/docs/` 同域规则，未实测

### 选择器优先级与回退

```javascript
// 标题：note-title 优先，document.title 兜底
const t = document.querySelector('.note-title__input-text')?.innerText.trim()
       || document.title.replace(/ - 飞书云文档.*$/, '');
```

## URL 规则与重定向

- **wiki token ≠ obj_token**：URL 里的 wiki token 不是文档 token，但无需换——根块 `data-record-id` 已经是 obj_token
- **正文接口全部不可用**：`/space/api/.../blocks`、`raw_content` 在游客态 fetch 直接 `Failed to fetch`；正文由协同 OT websocket（`engine_channel` / `/space/api/rce/heartbeat`）增量下发，**不可重放**，别找 API 捷径

## 图片/资源加载

| 项 | 选择器/规则 | 备注 |
|----|-----------|------|
| 密文下发 | `/space/api/box/file/cdn_url/` 返回 `cipher_type:"1"` + `secret` + `nonce` | 前端解密后 `img.src` 变 `blob:`，`page.request.get(blob:)` 报 `Protocol "blob:" not supported` |
| **原图下载（关键）** | `https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/all/{token}` | 仅需页面 cookie；另有 `/preview/{token}/?preview_type=16`、`/v2/cover/{token}/?fallback_source=1&policy=equal&width=1920&height=1920` |
| 尺寸陷阱 | `width=1280` 返回缩放图 | 1920 档不超原图时返回原始尺寸（实测 3214×1704 / 4724×7087 原图可取） |
| 兜底顺序 | 响应拦截字节 → 构造 URL → `locator(...).screenshot()` | 截图分辨率受渲染宽度限制，只在前三者都失败时用 |

## 评论/动态内容

无评论体系。

**虚拟滚动是这里唯一的"动态内容"难题**：飞书按**顶层块**粒度卸载屏外块。

| 项 | 规则 | 备注 |
|----|------|------|
| 累积方式 | 每滚一步 `scan()`，`data-block-id → outerHTML` 取**字节数最大**的快照 | 同一块可能先渲染骨架再补全 |
| 排序 | `getBoundingClientRect().top + el.scrollTop` 取最小 y，最后按 y 排序 | 文档顺序不能靠 DOM 顺序（块会被重挂） |
| 终止 | 触底 + `scrollHeight` 不变 + 块 id 集合不变，连续 4 次 | 单靠触底会漏尾部懒渲染 |
| 图片 | **逐 token**「定位→立刻取字节」，不能在循环外统一 `scrollIntoView` | 否则前面的操作把后面的块卸载掉（实测 10/16 张"块未挂载"） |

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| 正文只剩开头几段 | 一次取容器 `innerHTML` | 滚动增量快照（见上表） | 2026-08-29 |
| 标题下的正文整段消失 | 只渲染块自身 `.ace-line` | 子块**嵌在标题块内部** `.heading-children`，需递归渲染 | 2026-08-29 |
| 图片下不下来 | `page.request.get(img.src)` | `src` 是 `blob:`；构造 `/stream/download/all/{token}` | 2026-08-29 |
| 图片糊 | 只用 `element.screenshot()` | 构造 URL 拿原图，截图仅兜底 | 2026-08-29 |
| 「下载为 Markdown」找不到 | 翻表头菜单 | 匿名分享页 `can_export:true` 但菜单根本不渲染，放弃此路径 | 2026-08-29 |
| print 崩 `'gbk' codec can't encode '\u200b'` | 直接 print 标题 | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` | 2026-08-29 |
| 图片永远走截图兜底、无报错 | `resp.headers().get(...)` | Playwright 的 `APIResponse.headers` 是 **dict 属性**；`TypeError` 被 `except` 吞掉 | 2026-08-29 |
| 子集重跑覆盖他篇图片 | 图片名用文档序号 | 前缀用 `doc_token`，序号在 `--url`/`--limit` 下会重排 | 2026-08-29 |

## 遗留问题

- **wiki 子页面不展开**：只抓给定的那一个 wiki 节点，侧边栏子文档需要另找 `wiki/v2/tree/children` 接口，未验证游客态可用性
- **未覆盖块类型**：`bitable`（多维表格）、`sheet`（电子表格内嵌）、`view`（嵌入视图）、`synced_block`（同步块）、`add_ons` 未在样本中出现，落到"未识别块→降级正文 + warning"分支，实际效果待验证
- **附件/音频/视频块**：仅输出 `📎 名称`，未下载文件本体

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-08-29 | 初始创建，汇总自 `workspace/web_archive/extract_feishu.py` 首次开发（4 篇文档存档实测） |
