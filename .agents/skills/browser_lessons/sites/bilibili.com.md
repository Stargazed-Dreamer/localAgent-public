# Bilibili (bilibili.com)

---
aliases: [<data_drive>:\<bilibili_videos>, bilibili, space.bilibili.com, t.bilibili.com]
---

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | bilibili.com |
| **首次接触日期** | 2026-06-12 |
| **相关 skill** | bilibili_gacha、official_gacha |
| **登录态要求** | 需登录（个人空间动态/关注管理） |
| **抓取频次** | 周期（抽奖清理不定期触发） |
| **文件创建日期** | 2026-09-05 |
| **最后更新** | 2026-09-05 |

## 网站概况

视频/社区平台。agent 主要在个人空间动态页做互动抽奖整理：扫描转发动态、检查开奖状态（API 拦截）、删转发动态、取关抽奖 UP 主。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `user-data-dir`（chrome_debug/） |
| 反风控 | 不注入 JS 伪装补丁 + 随机延迟 | 项目统一：CDP 连真实 Chrome，指纹天然合规；禁止 playwright-stealth（见 docs/browser-anti-detection.md） |

## 反爬/风控

- **触发条件**：批量机械操作（连续删动态/取关/滚动过快）
- **规避方法**：操作间隔 1.5-4s、UP 之间 8-15s；`page.mouse.move/click` 模拟真人（8-20 步移动+随机偏移）；滚动用 `page.mouse.wheel`；`page.evaluate` 只读 DOM 不触发操作
- **风险阈值**：单次取关 ≤3、删动态 ≤5（脚本硬上限，细水长流）

## DOM 结构与提取规则

### 字段提取表（个人空间动态页 /dynamic）

| 字段 | 选择器 | 注意事项 |
|------|--------|---------|
| 卡片容器 | `.bili-dyn-list__item` | 顺序即页面顺序 |
| 原动态作者 | `.dyn-orig-author__name` | 转发卡片的"原作者" |
| 原动态 UID | `.dyn-orig-author__face[biliscope-userid]` | 属性在头像元素上 |
| 原动态内容 | `.bili-dyn-content__orig` | |
| 互动抽奖按钮 | `a.lottery[data-type="lottery"][data-rid]` | **活跃抽奖必渲染；抽奖结束后按钮从列表页被剥离**（重要判据，见已知坑） |
| 三点菜单 | `.bili-dyn-more` | **默认 size=0x0，必须先 hover 卡片才显示** |
| 三点菜单选项 | `.bili-cascader` → `.bili-cascader-options__item` | 含"删除" |
| 删除确认 | `.bili-modal__button.confirm.red` | "确认删除" |
| 关注按钮（空间页） | `.follow-btn` | 加载后约 3-4 秒才出现；点击"已关注"直接变"关注"，无二次确认 |

### 选择器优先级与回退

```javascript
// 卡片内精确找按钮（不要用 document.querySelector 全页找第一个！）
const btn = card.querySelector(`a.lottery[data-rid="${rid}"]`);
```

## 互动抽奖开奖状态判定（lottery_notice API）

点击 `a.lottery` 按钮触发 `https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice?business_id={rid}&business_type=1`，用 `page.on("response")` 拦截（直接调用 API 需 CSRF token，返回 code=-9999）：

| data.status | 含义 | 佐证（2026-09-05 实测） |
|-------------|------|------------------------|
| 0 | 未开奖 | 弹窗显示"开奖倒计时 XX 天 XX 时"，lottery_time = 未来计划开奖时间 |
| 1 | 已开奖 | — |
| 2 | 已开奖（终态，弹窗显示"XX奖名单"+ 中奖者昵称） | 棉花大哥哥 rid=414395 实测 |

- ⚠️ **`lottery_time` 非 0 不代表已开奖**：未开奖时它是计划开奖时间戳。判定已开奖只认 `status ∈ {1, 2}`，否则会误删未开奖动态（bilibili_check_lottery.py 已改保守判定并记录 raw_status/raw_lottery_time 备查）。
- 弹窗 OCR 关键词除"已开奖/中奖名单"外还有**"XX奖名单"句式**（如"一等奖名单"），正则别漏。

## URL 规则与重定向

- **特殊参数**：个人空间 `https://space.bilibili.com/{mid}/dynamic`；关注管理 `https://space.bilibili.com/{mid}/fans/follow`（可按"关注时间"排序）

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| 抽奖结束后按钮被剥离 | 认为"卡片没有 a.lottery 按钮 = 不是抽奖" | 活跃抽奖在转发卡片和 UP 空间页列表都渲染按钮；**有抽奖文本但无按钮 ⇒ 抽奖已结束**（可作清理证据，建议再用空间页定位原动态复核） | 2026-09-05 |
| `document.querySelector('a.lottery')` 抓到错误目标 | 全页取第一个按钮就点击 | 先按 origName 定位卡片，再在**卡片内**取按钮 | 2026-09-05 |
| 点击 `.bili-dyn-content__orig` 打不开详情页 | `context.expect_page()` 等新标签（15s 超时全失败） | 绕行：去 UP 空间页 `space.bilibili.com/{uid}/dynamic` 按关键词定位原动态 → 卡片内 a.lottery 拦截 API | 2026-09-05 |
| check 脚本数据源按类型优先级取 | 固定"lottery_check 优先于 scan" | 按 **mtime 取最新**，否则旧 lottery_check 压过新 scan 白查一轮（2026-09-05 实测浪费 22 次点击） | 2026-09-05 |
| 油猴"动态管理"面板干扰 | 用油猴注入的"删除并取关"按钮（class 含 CKFOMAN） | 用 B 站原生三点菜单 → 删除 → 确认删除 | 2026-06-12 |
| `getBoundingClientRect()` 键名 | 假设 `w`/`h` | 实际是 `width`/`height` | 2026-06-12 |
| 弹窗可能在特殊渲染层 | 直接读弹窗 DOM | 截图 + OCR（POST /ocr/path/json） | 2026-06-12 |

## 遗留问题

- 空间页"近期动态无 a.lottery 按钮"还有另一种可能：抽奖动态被 UP 删除或 buried 在列表深处（本次用关键词文本匹配定位原动态卡片复核，仍无按钮才判定已结束）。
- `up.json`（插件导出）是 2026-06-12 快照，不含之后新关注的 UP——不在导出里 ≠ 未关注；取关前脚本靠 `.follow-btn` 文字兜底（已是"关注"则跳过）。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-05 | 初始创建，汇总自 bilibili_gacha/official_gacha skill 文件 + 当日抽奖清理实测（lottery API status=2、按钮剥离判据、expect_page 失败绕行、mtime 数据源） |
