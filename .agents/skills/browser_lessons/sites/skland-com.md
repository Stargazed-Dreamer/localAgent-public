---
aliases: ['明日方舟社区', 'skland', 'skland.com', '森空岛']
---

# skland.com (skland.com)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | skland.com |
| **首次接触日期** | 2026-09-16 |
| **相关 skill** | （待补） |
| **登录态要求** | （待补） |
| **抓取频次** | （待补） |
| **文件创建日期** | 2026-09-16 |
| **最后更新** | 2026-09-16 |

## 网站概况

（待补）

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `user-data-dir` |

## 反爬/风控

（无）

## DOM 结构与提取规则

（待补）

## URL 规则与重定向

（无）

## 图片/资源加载

（无）

## 评论/动态内容

（无）

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|
| 文章页 HTML 是 SPA 空壳（~4KB），正文图片不在首屏 HTML 里 | 对 www.skland.com 直接 curl/requests 找 img | 游客态 CDP 渲染后从 DOM 取直链；正文数据接口 `zonai.skland.com/web/v2/item?id=N` 需前端生成的 `sign` 头（无 sign 返回 {"code":10000,"message":"请求异常"}），**不逆向签名**，渲染即得 | 2026-09-16 |
| 正文图/评论区图/站点装饰图混在全页 img 里 | querySelectorAll('img') 全抓 | 按容器分：正文 `[class*="ImageGallery__ImageWrap"]`；评论区 `[class*="Aggregation"]`；侧栏工具图 `toolboxstyle`（排除）。类名是 styled-components hash+语义段，**只能锚定 `__` 后语义段**，hash 段每次构建会变 | 2026-09-16 |
| 图片直链 bbs.hycdn.cn/image/...webp | 担心防盗链带一堆头 | 实测无防盗链：带不带 Referer 都 200；缩略图后缀 `?x-oss-process=style/thumbnail` 剥掉即原图。落地脚本 `workspace/web_archive/skland_images.py` | 2026-09-16 |


## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-16 | 初始创建（由 browser_write_lesson 自动生成） |
| 2026-09-16 | 追加 已知坑（来自 https://www.skland.com/article?id=6310276） |
