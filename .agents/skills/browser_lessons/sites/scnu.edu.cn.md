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
| **最后更新** | 2026-09-19 |

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
| 分组限定作业在列表页"隐形" | 只抓 `a[href*="/mod/"]` 来列作业 → 别班/未发布的条目整批无链接被静默漏掉，误判成"老师还没布置" | 枚举 `li.activity` 全部行、用其 `id="module-<cmid>"` 拿 cm id；**有无 `<a>` 只当"本班可访问"判据**，不当存在性判据（受限文案写在行内：「不能使用除非 你属于 X」）。详见 `platforms/moodle-scnu.md` §2.4 | 2026-09-19 |
| 未加入班级 choicegroup 时作业页打不开 | 以为是权限 bug 或链接失效 | assign 页 302 到 `/course/cms/<cmid>/restricted` = 分组限定；先在课程 choicegroup 加入本班（"保存我的选择"是平台保存类操作，**代点前先请示用户**），保存后 URL 带 `notify=choicegroupsaved` | 2026-09-16 |
| 跨进程脚本拿错标签页 | 复用旧脚本时 `pages[0]` / 找不到就 `new_page()` → 实际操作 about:blank，evaluate 返回空被当成"页面没数据" | 按 URL 关键字取**最新**匹配标签（`reversed(pages)`）；结果为空先打印当前所有标签 URL 复核，再怀疑网站 | 2026-09-19 |
| resource 页 `#region-main` 可能不存在 | 写死 `links "#region-main"`，空输出误判"该资源没有附件" | 退回 `links body` + grep `pluginfile`；正文"点击 X 链接查看此文件"不保证有那个容器 id | 2026-09-19 |
| 旧版 `.doc`（WPS 生成）模板抽取 | 想用 python-docx 读 → 不是 zip，直接失败；也别说"必须装 Word/LibreOffice" | 文件头 `d0cf11e0` 即 OLE2 旧格式；本机无 LibreOffice、pywin32+Word 虽在但杀鸡用牛刀——先用 UTF-16LE 连续段粗扫拿段落标题（够用且零依赖），要版式时再按同结构重建 .docx | 2026-09-19 |
| 截图给 VLM 读 | 用 browser_screenshot 返回的服务端路径喂 understand_image → 400/不存在 | 截图存在 localAgent 侧，IDE 不可读；提取内容走 DOM，别绕截图 | 2026-09-10 |
| 用"URL 里没有 login"判已登录 → 假绿灯 | `if "login" not in url and "sso.scnu" not in url: 已登录`；未登录访问 `/my/` 实际 302 到 `https://moodle.scnu.edu.cn/?redirect=0`，**URL 里既无 login 也无 sso**，于是脚本宣布"already logged in"，后面每页都取到"访客不能访问此课程"的空正文，误判成"该课没有作业" | **唯一判据 = URL 里真的含 `/my/`**（平台文档 §0 早写了"能进到 /my/ 才算登录"）；再兜一层：断言正文不含「访客不能访问此课程」 | 2026-09-20 |
| 重启调试浏览器 = 登录态必丢 | 以为 `chrome_debug/` 存了登录态，重启 Chrome 后直接抓页面 | Moodle 的 `PHPSESSID` 是**会话 cookie**，进程一退就没了（本轮 `page.close()` 关掉最后一个标签 → Chrome 进程退出 → 下次连上全是访客页）。跨脚本复用登录态时，**先按上一条判据复核 `/my/`，不通就走 §1 四步重登**；脚本里别关最后一个标签页 | 2026-09-20 |
| OAuth 页脚「确定登录」点完 URL 立刻读仍是 auth.html | `click()` 后 `sleep(2)` 读 `page.url`，还在 `openapi/auth.html` → 判 LOGIN FAILED 退出（**实际授权已成功**，下一轮新页访问 `/my/` 直接通过） | 跳转类操作不以"点完立刻看 URL"为准：**回落地页二次访问**再判定；另外 goto 偶发 `net::ERR_ABORTED`（被上一跳导航抢占），重试一次即可 | 2026-09-20 |

## URL 规则与重定向

- 登录链：`/login/index.php` → SSO `login.html`（`#btn-password-login`）→ `openapi/auth.html?sysname=砺儒云课堂`（gotoApp）→ 回跳 `/my/`
- 课程：`course/view.php?id=<N>`；活动：`mod/{resource|assign}/view.php?id=<N>`
- 文件直链：`/pluginfile.php/<file_id>/mod_{resource,assign}/...`（file_id ≠ cm id）

## 遗留问题

- ~~pluginfile 带 cookie 下载的机制化方案未定~~ → 2026-09-13 已解决（playwright CDP context request，见已知坑表）
- ~~重复文件 `scnu-edu-cn.md`~~ → 2026-09-19 已删除。逐条比对确认其「已知坑」唯一 1 行与本文件同坑（choicegroup 302），
  无独有内容可并（列1/列3 完全相同，列2 相似度 0.95 且 `302` / `/course/cms/` / `restricted` / `notify=choicegroupsaved` 等关键事实一致）；
  根因是 `browser_write_lesson` 只按「点改横线」规范名 (`scnu-edu-cn.md`) 查文件、看不见人工按主域命名的本文件，
  已在 `server/browser/site_lessons.py` 加写前查重（`_find_existing_lesson_file()`）修掉。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-10 | 初始创建，汇总自砺儒云首轮实跑 |
| 2026-09-10 | 补「我的课程」顶栏按钮坑（menuitem 非 link；`.dropdown-menu.show` 快照法） |
| 2026-09-13 | pluginfile 机制化下载验证成功（playwright CDP ctx.request），遗留问题关闭 |
| 2026-09-14 | 补 SSO 自动填充不稳定坑（未填充时点登录弹「请输入您的登录账号」；处理路径=聚焦/重载→用户手动填） |
| 2026-09-15 | 补大表静默截断坑（arch 13 实验漏扫 12 个根因）；moodle-scnu.md §2.3 推荐 JS innerText 取数法 |
| 2026-09-19 | 确认重复文件 `scnu-edu-cn.md` 无独有内容（唯一 1 行坑与本文件同坑）后删除；配套修 write_lesson 写前查重 |
| 2026-09-19 | T06 自主抓大作业要求：补「分组限定活动无锚点会被整批漏」（`li.activity id="module-<cmid>"` 取法，同步 moodle-scnu.md §2.4）、choicegroup 302 坑（自重复文件并入）、跨进程拿错标签页、`#region-main` 可能不存在、旧 .doc 粗扫法；记重复文件 `scnu-edu-cn.md` 待裁决。SSO 自动填充本轮复验成功 |
| 2026-09-20 | 全量重扫 + 数图建档：补 3 条登录态坑（**"URL 不含 login"是假绿灯，唯一判据是 URL 含 `/my/`**；**Moodle PHPSESSID 是会话 cookie，调试浏览器进程一退必丢登录**，而 `page.close()` 关掉最后一个标签会连带让 Chrome 退出；**OAuth 页脚「确定登录」点完立刻读 URL 仍停在 auth.html**，须回落地页二次访问判定，goto 偶发 `net::ERR_ABORTED` 重试即可）。SSO 自动填充第 3 次复验成功（脚本只读输入框长度） |
