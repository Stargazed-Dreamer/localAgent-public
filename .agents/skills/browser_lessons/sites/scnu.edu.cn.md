# 砺儒云课堂 / SCNU SSO (scnu.edu.cn)

> 平台级操作 SOP 详见 `workspace/homework/platforms/moodle-scnu.md`，本文件只记浏览器踩坑。

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | scnu.edu.cn（moodle.scnu.edu.cn / sso.scnu.edu.cn） |
| **首次接触日期** | 2026-09-10 |
| **相关 skill** | homework 流水线（workspace/homework） |
| **登录态要求** | 需登录（统一身份认证 SSO + OAuth 三连跳） |
| **抓取频次** | 周期（每次取作业要求/看截止） |
| **文件创建日期** | 2026-09-10 |
| **最后更新** | 2026-09-15 |

## 网站概况

华师 Moodle 云课堂，作业自动化项目的作业来源平台。agent 任务：登录→进课→抓要求→（不代提交）。

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| SSO 授权页「确定登录」有两个 | 点卡片主体那个 → TIMEOUT element is not visible | 点 **card-footer** `.login-check-comfirm` 里的（role=link） | 2026-09-10 |
| 首页登录表单 | 点首页 `#submit` → TIMEOUT（默认收起不可见） | 点顶部「登录」链接进 `/login/index.php` 再点 `#ssobtn` | 2026-09-10 |
| 密码 | 想代填账号密码 | **不要填**——Chrome 已存密码自动填充；snapshot 里可能回显明文，严禁落盘 | 2026-09-10 |
| SSO 自动填充不稳定（09-12 能填，09-14 没填） | 假设每次都自动填充，直接点 `#btn-password-login` | 点登录若弹「请输入您的登录账号」=本次没自动填充；点 `#account` 聚焦等 1-2s、重载页面再试；仍为空 → 请用户在调试 Chrome 手动填（agent 红线不代填），用户填完点登录后 agent 接管点「确定登录」 | 2026-09-14 |
| resource/assign 页正文被课程索引抽屉淹没 | 直接 snapshot 全页 → 全是 `#courseindex` 节点、截断 | 先点「关闭课程索引」，再 `browser_snapshot(root_selector="#region-main")` | 2026-09-10 |
| 拿不到全部选课 id | 逐个翻 dashboard 卡片 | 读 `/my/` 日历下拉 `#calendar-course-filter-1` 的全部 option value | 2026-09-10 |
| 下载 pluginfile.php 文件 | `requests` 匿名 GET → 302 到 `/enrol/index.php` 假成功 | **机制化方案已验证（2026-09-13）**：playwright `connect_over_cdp("http://127.0.0.1:9222")` → `browser.contexts[0].request.get(pluginfile_url)` 带 cookie，`resp.body()` 直存指定路径（实验一提交物 PNG 163KB 实测成功，比浏览器内下载搬文件稳） | 2026-09-13 |
| 大表静默截断（作业表 13 行只见到 1 行，四轮漏扫） | 信任 browser_snapshot 的表格行数；truncated=true 时凭"已见行"下结论 | **Moodle overview 大表一律用 playwright JS innerText**：`page.evaluate("()=>document.querySelector('#assign_overview table')?.innerText")` 一次拿全表文本；行链接用 `querySelectorAll('tbody tr')` 逐行取 a.activityname（详见 platforms/moodle-scnu.md §2.3） | 2026-09-15 |
| 截图给 VLM 读 | 用 browser_screenshot 返回的服务端路径喂 understand_image → 400/不存在 | 截图存在 localAgent 侧，IDE 不可读；提取内容走 DOM，别绕截图 | 2026-09-10 |

## URL 规则与重定向

- 登录链：`/login/index.php` → SSO `login.html`（`#btn-password-login`）→ `openapi/auth.html?sysname=砺儒云课堂`（gotoApp）→ 回跳 `/my/`
- 课程：`course/view.php?id=<N>`；活动：`mod/{resource|assign}/view.php?id=<N>`
- 文件直链：`/pluginfile.php/<file_id>/mod_{resource,assign}/...`（file_id ≠ cm id）

## 遗留问题

- ~~pluginfile 带 cookie 下载的机制化方案未定~~ → 2026-09-13 已解决（playwright CDP context request，见已知坑表）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-10 | 初始创建，汇总自砺儒云首轮实跑 |
| 2026-09-10 | 补「我的课程」顶栏按钮坑（menuitem 非 link；`.dropdown-menu.show` 快照法） |
| 2026-09-13 | pluginfile 机制化下载验证成功（playwright CDP ctx.request），遗留问题关闭 |
| 2026-09-14 | 补 SSO 自动填充不稳定坑（未填充时点登录弹「请输入您的登录账号」；处理路径=聚焦/重载→用户手动填） |
| 2026-09-15 | 补大表静默截断坑（arch 13 实验漏扫 12 个根因）；moodle-scnu.md §2.3 推荐 JS innerText 取数法 |
