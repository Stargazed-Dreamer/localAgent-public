---
aliases: [东方财富, eastmoney]
---

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | eastmoney.com |
| **首次接触日期** | 2026-07-30 |
| **相关 skill** | stock_advisor |
| **登录态要求** | 匿名可访问 |
| **抓取频次** | 周期（盘后/盘中按需） |
| **文件创建日期** | 2026-07-30 |
| **最后更新** | 2026-07-30 |

## 网站概况

东方财富网数据中心，stock_advisor 在东方财富三域名（push2/82.push2/19.push2）API 断连时，用浏览器渲染网页抓取板块资金流向和个股主力资金流向数据作为终极 fallback。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 调试浏览器有界面 |
| 登录态来源 | 匿名 | 公开行情数据 |
| 反风控 | 无 | 公开页面无反爬 |

## DOM 结构与提取规则

### 关键页面

| 页面 | URL | 用途 | 已封装 |
|------|-----|------|--------|
| 行业板块资金流向 | data.eastmoney.com/bkzj/hy.html | 行业板块主力净流入排行 | BrowserSectorSource.get_industry_sectors_by_inflow |
| 概念板块资金流向 | data.eastmoney.com/bkzj/gn.html | 概念板块主力净流入排行 | BrowserSectorSource.get_concept_sectors_by_inflow |
| 个股资金流向排行 | data.eastmoney.com/zjlx/detail.html | 沪深两市主力净流入个股Top50 | 未封装（手动 Playwright） |
| 沪深A股涨幅排行 | quote.eastmoney.com/center/gridlist.html#hs_a_board | 个股涨幅排行 | 未封装（不推荐，见坑） |

### 字段提取表

**板块页（bkzj/hy.html, bkzj/gn.html）** — 由 `_fetch_sector_table_async` 抓取：

| 字段 | 列索引 | 解析 |
|------|--------|------|
| rank | 0 | int |
| sector_name | 1 | 板块名 |
| change_pct | 3 | `_parse_pct`（去%转float） |
| main_net_inflow | 4 | `_parse_amount`（亿/万转元） |
| main_net_inflow_pct | 5 | 占比% |
| net_5d_inflow | 6 | 5日净流入 |
| net_5d_inflow_pct | 7 | 5日占比% |

选择器：`table tbody tr` → `td` 列表，过滤 `row[0].isdigit()` 跳过非数据行。

**个股资金流向页（zjlx/detail.html）** — 手动 Playwright 抓取：

| 字段 | 列索引 | 说明 |
|------|--------|------|
| 序号 | 0 | |
| 代码 | 1 | |
| 名称 | 2 | |
| 最新价 | 4 | |
| 今日涨跌幅 | 5 | |
| 今日主力净流入 | 6 | 亿/万需解析 |
| 超大单净流入净额 | 8 | |
| 超大单净流入净占比 | 9 | |
| 大单净流入净额 | 10 | |
| 大单净流入净占比 | 11 | |
| 中单/小单 | 12-15 | |

选择器同上：`table tbody tr` → `td`，默认50条，按主力净流入降序。

### 通用抓取 JS 模板

```javascript
() => {
    const rows = document.querySelectorAll('table tbody tr');
    const data = [];
    for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (cells.length > 5) {
            data.push(Array.from(cells).map(c => c.innerText.trim().replace(/\s+/g, ' ')));
        }
        if (data.length >= 50) break;
    }
    return {url: location.href, title: document.title, rows: data};
}
```

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| gridlist.html 默认只加载20条 | 滚动加载 `window.scrollTo(0, body.scrollHeight)` 期望加载更多 | 滚动加载不生效，该页面分页靠点击下一页按钮；或换用 zjlx/detail.html（默认50条） | 2026-07-30 |
| gridlist.html 点击列头排序变升序 | 点击"成交额"th 期望降序，结果升序（最小在前） | 点击后需验证排序方向，或点击两次切换；不确定时用 zjlx/detail.html 替代 | 2026-07-30 |
| gridlist.html 涨幅排行前20多为新股/ST | 直接用涨幅Top选股 | 过滤新股段(3016xx/3017xx/920xxx)、ST、N开头；或用主力净流入排行选股更有参考价值 | 2026-07-30 |
| CDP 连接的 browser 不能 close | `await browser.close()` 会关闭整个调试浏览器 | 只 `await page.close()`，不 close browser | 2026-07-30 |
| 东方财富 API 断连但网页可访问 | 以为全站不可用放弃 | push2/82.push2/19.push2 三域名 RemoteDisconnected 是 API 层断连，data.eastmoney.com 网页渲染正常，浏览器抓取是有效 fallback | 2026-07-30 |
| 渲染等待不足 | `wait_until="domcontentloaded"` 后立即抓取 | 需额外 `wait_for_timeout(6000-8000)` 等 JS 动态加载表格 | 2026-07-30 |

## 反爬/风控

- **触发条件**：（无）公开行情页面无反爬
- **规避方法**：（无）
- **风险阈值**：（无）

## 遗留问题

- **板块成分股抓取**：未实现进入板块详情页抓成分股。URL 格式待确认（quote.eastmoney.com/bk/90.{code}.html 或 data.eastmoney.com/bkzj/hy-detail-{code}.html）。后续若需板块龙头股可补充。
- **gridlist.html 翻页**：未实现点击"下一页"按钮抓取超过20条数据。zjlx/detail.html 默认50条已够用，暂不需要。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-07-30 | 初始创建，汇总自 stock_advisor 浏览器兜底选股任务（东方财富API断连时用 zjlx/detail.html + bkzj/hy.html + bkzj/gn.html 抓取） |
