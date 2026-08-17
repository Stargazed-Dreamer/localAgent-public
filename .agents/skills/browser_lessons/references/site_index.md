# 网站索引

已记录的网站清单与主域 → 文件映射。

> **agent 查询优先用 `browser_match_site` MCP 工具**（或 `tools/browser/match_site.py`），一键按域名/别名匹配，直接返回 sites 文件正文。本文件为人类可读的汇总表，不再作为 agent 查询的必经路径——脚本直接扫 `sites/` 目录，避免与本表不同步。维护时只需确保 `sites/` 目录文件正确，本表供 neat-freak 审查时人工对照。

## 已记录网站

| 主域 | 网站名 | 文件 | 相关 skill | 触发关键词 | 登录态 |
|------|--------|------|-----------|-----------|--------|
| `xiaoheihe.cn` | 小黑盒 | [sites/xiaoheihe.md](file:///f:/<project_root>/.agents/skills/browser_lessons/sites/xiaoheihe.md) | web_archive、arknights_gacha（导入） | 小黑盒、xiaoheihe、社区帖子、图文帖、文章帖 | 匿名可读，写操作需登录 |
| `127.0.0.1` | LocalAgent 本地监控面板 | [sites/127-0-0-1.md](file:///f:/<project_root>/.agents/skills/browser_lessons/sites/127-0-0-1.md) | browser_lessons、html-dev-debug | localhost、LocalAgent监控面板、终端卡片 | 本机后端运行时可访问 |
| `kurogames.com` | 库洛云游戏 | [sites/kurogames.md](file:///f:/<project_root>/.agents/skills/browser_lessons/sites/kurogames.md) | wuwa_gacha | 鸣潮、云鸣潮、唤取记录 | 需登录库洛通行证 |
| `hypergryph.com` | 鹰角通行证与游戏记录页 | [sites/hypergryph.md](file:///f:/<project_root>/.agents/skills/browser_lessons/sites/hypergryph.md) | arknights_gacha、endfield_gacha | 明日方舟、终末地、鹰角、寻访记录 | 需登录鹰角通行证 |

## 待记录网站（已知但未汇总）

以下网站已被现有 skill 操作，但 sites/ 文件尚未创建。下次操作这些网站遇到非显然行为时按 `sites/_template.md` 创建并补到此表。

| 主域 | 网站名 | 相关 skill | 备注 |
|------|--------|-----------|------|
| `mp.weixin.qq.com` | 微信公众号 | web_archive | DOM 坑已在 web_archive/SKILL.md 中，未汇总到独立 sites 文件 |
| `*.xiaoe-tech.com` | 小鹅通鹅圈子 | community_review | 内容提取/滚动防跳帖/点赞标记坑已在 community_review.md 中 |
| `bilibili.com` | <data_drive>:\<bilibili_videos> | bilibili_gacha、official_gacha | 互动抽奖/动态/反风控坑散落在两个 skill 文件中 |
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
