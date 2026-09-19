# 网站索引

已记录的网站清单与主域 → 文件映射。

> **agent 查询优先用 `browser_match_site` MCP 工具**（或 `tools/browser/match_site.py`），一键按域名/别名匹配，直接返回 sites 文件正文。本文件为人类可读的汇总表，不再作为 agent 查询的必经路径——脚本直接扫 `sites/` 目录，避免与本表不同步。维护时只需确保 `sites/` 目录文件正确，本表供 neat-freak 审查时人工对照。

## 已记录网站

| 主域 | 网站名 | 文件 | 相关 skill | 触发关键词 | 登录态 |
|------|--------|------|-----------|-----------|--------|
| `xiaoheihe.cn` | 小黑盒 | [sites/xiaoheihe.md](file:///<project_root>/.agents/skills/browser_lessons/sites/xiaoheihe.md) | web_archive、arknights_gacha（导入） | 小黑盒、xiaoheihe、社区帖子、图文帖、文章帖 | 匿名可读，写操作需登录 |
| `127.0.0.1` | LocalAgent 本地监控面板 | [sites/127-0-0-1.md](file:///<project_root>/.agents/skills/browser_lessons/sites/127-0-0-1.md) | browser_lessons、html-dev-debug | localhost、LocalAgent监控面板、终端卡片 | 本机后端运行时可访问 |
| `kurogames.com` | 库洛云游戏 | [sites/kurogames.md](file:///<project_root>/.agents/skills/browser_lessons/sites/kurogames.md) | wuwa_gacha | 鸣潮、云鸣潮、唤取记录 | 需登录库洛通行证 |
| `hypergryph.com` | 鹰角通行证与游戏记录页 | [sites/hypergryph.md](file:///<project_root>/.agents/skills/browser_lessons/sites/hypergryph.md) | arknights_gacha、endfield_gacha | 明日方舟、终末地、鹰角、寻访记录 | 需登录鹰角通行证 |
| `feishu.cn` | 飞书云文档 | [sites/feishu.cn.md](../sites/feishu.cn.md) | web_archive（extract_feishu.py） | 飞书、飞书文档、云文档、wiki、docx | 分享链接游客态可读 |
| `docs.qq.com` | 腾讯文档 | [sites/docs.qq.com.md](../sites/docs.qq.com.md) | web_archive（extract_tencent_doc.py） | 腾讯文档、腾讯、docs.qq.com、tdocs | 游客态可读全文 |
| `bilibili.com` | <data_drive>:\<bilibili_videos> | [sites/bilibili.com.md](../sites/bilibili.com.md) | bilibili_gacha、official_gacha | <data_drive>:\<bilibili_videos>、bilibili、互动抽奖、取关、删动态 | 需登录 |
| `zhihu.com` | 知乎 | [sites/zhihu.md](../sites/zhihu.md) | web_archive（探索中） | 知乎、zhihu、zhuanlan、专栏文章、问题页 | **需登录**（IP 风控后匿名完全被拦，2026-09-12 实测） |
| `scnu.edu.cn` | 砺儒云课堂（华师 Moodle） | [sites/scnu.edu.cn.md](../sites/scnu.edu.cn.md) | homework（作业流水线） | 砺儒云、moodle.scnu、作业要求、SSO 登录 | 需登录（统一身份认证 SSO） |
| `js.design` | 即时设计 | [sites/js.design.md](../sites/js.design.md) | homework（hci 作业 / jsd_research） | 即时设计、jsd、原型、画布、导入文件 | 需登录（客户端 userData 可复用） |
| `skland.com` | 森空岛 | [sites/skland-com.md](../sites/skland-com.md) | web_archive（skland_images.py） | 森空岛、skland、明日方舟社区 | 待验证（SPA + sign 接口不逆向，走 CDP 渲染取直链） |
| `wjx.cn` | 问卷星 | [sites/wjx-cn.md](../sites/wjx-cn.md) | homework（问卷建题自动化） | 问卷星、wjx、wenjuanxing、问卷调查、文本导入 | 调试浏览器已带登录态（但首页导航仍按未登录渲染） |

## 待记录网站（已知但未汇总）

以下网站已被现有 skill 操作，但 sites/ 文件尚未创建。下次操作这些网站遇到非显然行为时，用 `browser_write_lesson` MCP 工具写入（自动按 `_template.md` 9 段结构建文件、规范化域名、合并 aliases），并补到此表。

| 主域 | 网站名 | 相关 skill | 备注 |
|------|--------|-----------|------|
| `mp.weixin.qq.com` | 微信公众号 | web_archive | DOM 坑已在 web_archive/SKILL.md 中，未汇总到独立 sites 文件 |
| `*.xiaoe-tech.com` | 小鹅通鹅圈子 | community_review | 内容提取/滚动防跳帖/点赞标记坑已在 community_review.md 中 |
| `bwiki.rs` | Bwiki | arknights_gacha | 卡池数据采集 |
| `prts.wiki` | PRTS | arknights_gacha | 限定卡池数据 |
| `nowcoder.com` | 牛客 | niuke_review | 面经抓取（实际用 WebSearch + WebFetch，未必走浏览器） |
| `gryphline.com` | 鹰角国际服 | endfield_gacha | 国际服登录 |

## 网站识别规则

- **主域 = eTLD+1**：用 `tldextract` 或手动判断
- **跨子域归并**：同一注册域下不同子域视为同一网站（`t.bilibili.com` 和 `space.bilibili.com` 都是 `bilibili.com`），除非不同子域有显著不同的 DOM 结构
- **文件命名**：主域中的点改为横线（`xiaoheihe.cn` → `xiaoheihe.md`，`mp.weixin.qq.com` → `weixin_mp.md`）

## 维护

- 新建 sites 文件后必须同步更新此索引
- 网站改版导致 sites 文件大量失效时，在文件顶部加 `> ⚠️ YYYY-MM-DD 起本文件部分内容可能过时，待重新验证` 提示，不要直接删除
- neat-freak 审查时检查此索引与 `sites/` 目录实际文件的一致性
