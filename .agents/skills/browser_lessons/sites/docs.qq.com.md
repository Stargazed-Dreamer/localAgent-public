---
aliases: [腾讯文档, 腾讯, docs.qq.com, tencent doc]
---

# 腾讯文档 (docs.qq.com)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | docs.qq.com（含 `docimgN.docs.qq.com` 图片直链） |
| **首次接触日期** | 2026-09-02（VM 探索）/ 2026-09-05（真机合并） |
| **相关 skill** | web_archive（`workspace/web_archive/extract_tencent_doc.py`） |
| **登录态要求** | **游客态即可读全文**（顶栏「登录」不挡正文） |
| **抓取频次** | 一次性（按需） |
| **最后更新** | 2026-09-05 |

## 网站概况

腾讯文档 `doc/{id}`（tdocs/melo 渲染器）。agent 在此只做一件事：把文档存档为 Markdown（正文 + 表格 + 代码块 + 图片）。**正文文字不在 DOM**（canvas 绘制，DOM `span`=0），必须走客户端数据接口。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|------|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 登录态来源 | 无需登录，游客态 | 私有文档另说 |
| 数据接口 | `dop-api/get/doc?padId=300000000$xxx&commandsFormat=1&start_index=N&count=10&revision_version=RRRR&doc_chunk_version=3` | padId 带 `300000000$` 前缀；必须带 `revision_version`/`doc_chunk_version`（从页面真实请求里抓） |
| 正文双通道 | 小文档（≈3 万字以下）**不发 get/doc**，正文内嵌在 opendoc JSONP `clientVars.collab_client_vars.initialAttributedText.text[]`；大文档走 get/doc 分片 | **两通道都取、取更长者**；"没捕到 get/doc"≠失败 |
| **每篇必须新 context** | `browser.new_context()` + 结束 `ctx.close()` | 文档数据缓存 IndexedDB/localStorage，复用 context 二次访问不再发 dop-api → 拿不到 revision_version 整篇误判失败 |

## 核心坑（详表见 workspace/web_archive/SKILL.md 腾讯文档章节）

| 问题 | 正确做法 |
|------|---------|
| 正文在 canvas 上，DOM 无文字 | 别走 DOM。走 `dop-api` 数据接口（返回 chunk 的 `command` 是 base64 protobuf，无 schema 通用遍历取最长字符串，长度精确等于 `gcp_end-gcp_start`） |
| 小文档整篇判失败 | 按大小分流，opendoc JSONP 先剥壳（`clientVarsCallback({...})`）再 `json.loads` |
| 二次访问同文档失败 | 不是缓存问题，是 IndexedDB/localStorage 应用层缓存 → 每篇新 context |
| `\x08` 占位符 | 是通用"内嵌对象"占位（图片/代码卡片/公式共用），分类靠分片级 f215 计数；带行号代码卡片真实内容在首分片"内容池"（连续 `\x1c..`1d` 相邻链），**必须锚点匹配回填，不能顺序 1:1**（池可能缺块） |
| 控制符语义 | `\r` 换行、`\x1a/\x06/\x07` 表格、`\x1c/\x1d` 代码块、`\x13..\x15` 超链接、`\x05` 等残留 C0 剥掉 |
| 图片下载 | `docimgN.docs.qq.com` 直链（无 blob: 问题），带 `Referer: https://docs.qq.com/` requests 直接下 |
| 真实滚动容器 | `.scrollable--soyAp`（body 不滚）；tdocs/melo 按"物理页"虚拟挂载，屏外页整体卸载，扫图必须慢滚全程累积 |
| 大纲抽屉 | 虚拟列表且项 id 重挂载会重生成 → 按规范化文本去重；合成 click 不触发滚动，需 Playwright 真实点击 |
| 清理旧图被拦截 | 项目安全删除钩子拦截 bulk-delete。哈希命名重名即同图，只覆盖不批删 |

## 已知限制

- 部分超链接在 protobuf 里是锚文本而非真实外链（HYPERLINK href 待核）
- 个别 `\x08` 占位在游客数据里无内容（文档侧数据缺失，非脚本 bug）
