---
aliases: [知乎, zhuanlan, zhihu]
---

# 知乎 (zhihu.com)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | zhihu.com（含子域 zhuanlan.zhihu.com / www.zhihu.com） |
| **首次接触日期** | 2026-09-12 |
| **相关 skill** | web_archive |
| **登录态要求** | **需登录**（2026-09-12 实测：IP 被标记 abuseip 后，匿名访问被完全拦截） |
| **抓取频次** | 待定（探索中断，转交云 agent） |
| **文件创建日期** | 2026-09-12 |
| **最后更新** | 2026-09-12 |

## 网站概况

中文最大问答社区，两类目标页面：专栏文章（`zhuanlan.zhihu.com/p/{id}`）和问题页（`www.zhihu.com/question/{id}`）。反爬为全站最严格级别。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|------|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 待定（首次探索即被 IP 风控拦截，未走到登录步骤） | 匿名态实测不可用 |
| 采集方式 | 一次 goto 挂网络监听拿全量（见 `temp/zhihu_explore/capture_zhihu.py`） | HTML+netlog+响应体+截图一次拿全，绝不刷新 |

## 反爬/风控

**2026-09-12 实测完整链路（首次访问即触发）：**

1. `GET zhuanlan.zhihu.com/p/{id}` → **403**，返回 650 字节的 **zse-ck JS 质询页**（`<meta id="zh-zse-ck">` + `static.zhihu.com/zse-ck/v4/*.js` 计算 `__zse_ck` cookie 后自动重试）
2. 重试 → **302** → `www.zhihu.com/account/unhuman?type=U4E3Z1&need_login=true&session={32位hex}`
3. 验证页调 `GET /api/v4/anticrawl/new_captcha_appeal?session=...` → JSON：
   - `"block_level": 1`
   - `redirect_url` 指向 `unhuman?type=abuseip`，消息明示 **本地公网 IP 被检测异常流量、已暂时限制、登录后才能继续访问**
4. 页面最终只显示"请您登录后查看更多专业优质内容。登录知乎 / 意见反馈"

**要点：**
- **触发条件**：本地住宅 IP 未登录首访即触发（`block_level:1` + abuseip）；agentweb-kit 云端数据中心 IP 实测同样撞"安全验证"墙（zhuanlan）和"你似乎来到了没有知识存在的荒原"（question 404 页）。**两种 IP 都不安全，别假设换环境就没事**
- **规避方法**：未验证。推测需登录态（`z_c0` cookie）+ 慢速（2 分钟 1 次）+ 每次换不同文章 + **从搜索引擎跳转进入**（agentweb-kit `fetch.py` 有现成 `nav="serp"`：Bing 搜 `site:zhihu.com` → 真实点击结果链接，Referer=SERP；另有 `nav="home"` 和 direct→home→serp 升级链）。注意其 REPORT 10.2 负结果：**知乎的墙有 IP 信誉层，Referer 链救不了已脏的 IP**——首访撞墙≠只是访问姿势问题
- **风险阈值**：用户明确约束 **2 分钟内最多 1 次页面查看（含下载资源/刷新）**；agentweb-kit REPORT 记录知乎"首次可、复访墙"（当日多次访问后弹安全验证，早晨首访正常）
- **别硬闯**：撞墙后继续请求/刷新只会加重风控（参考百度 IP 硬封教训）

## DOM 结构与提取规则

（未探索到内容页——被风控拦截。待云 agent 探索后回填）

已知线索：
- unhuman 验证页有 `js-initialData`（9432 字符），正常内容页应也有 SSR 数据（`js-initialData` / `window.__INITIAL_STATE__`），大概率比 DOM 更稳
- 403 质询页特征：`<meta id="zh-zse-ck" ...>` + 仅 650 字节 + 正文一句"知乎，让每一次点击都充满意义"

## URL 规则与重定向

- 专栏：`zhuanlan.zhihu.com/p/{数字id}`
- 问题：`www.zhihu.com/question/{数字id}`（可能带 `/answer/{id}` 后缀定位具体回答）
- 风控重定向：内容页 → 302 → `www.zhihu.com/account/unhuman?type=U4E3Z1&need_login=true&session=...`
- question 页被拦/删除时显示"你似乎来到了没有知识存在的荒原"（标题可判别）

## 图片/资源加载

（未探索。已知图片域为 `pic*.zhimg.com`，防盗链/Referer 要求待验证）

## 评论/动态内容

（未探索）

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| 首访 403 小页面（650字节） | 当成页面坏了去刷新 | 这是 zse-ck JS 质询，浏览器执行后自动重试，等就行 | 2026-09-12 |
| 直接 goto 目标 URL（裸请求） | 拿 URL 就 goto | 无 Referer 裸请求是显著爬虫信号：先搜索引擎（Bing `site:zhihu.com`）找入口再真实点击进入。agentweb-kit `fetch(nav="serp")` 现成实现，但我打包时没读源码漏掉了它 | 2026-09-12 |
| 被重定向到 unhuman 验证页 | 继续访问其他页面硬闯 | 停止，等待或先解决登录态 | 2026-09-12 |
| 以为换 IP 就能匿名访问 | 云端数据中心 IP 重试 | agentweb-kit 云端实测同样撞墙 | 2026-09-12 |
| 判断登录态只看页面元素 | 页面被风控页替换后选择器全空 | 检查 cookie：`z_c0`=已登录，`d_c0`=设备标识（有 d_c0 无 z_c0 = 匿名） | 2026-09-12 |

## 遗留问题

- **登录态获取**：本地未登录。用户决定转交云 agent 探索（2026-09-12）
- 内容页 DOM/initialData 提取规则：全部待探索
- 评论加载机制、问题页回答分页：待探索
- `zse-ck` 质询的 JS 逻辑（若能离线分析其 JS，可判断指纹要求）：未分析

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-12 | 初始创建：首次访问风控链路全量记录（数据存 `temp/zhihu_explore/p622803268/`），探索转交云 agent |
