---
aliases: ['问卷星', 'wjx.cn', 'wenjuanxing', 'wjx']
---

# wjx.cn (wjx.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | wjx.cn |
| **首次接触日期** | 2026-09-19 |
| **相关 skill** | （待补） |
| **登录态要求** | （待补） |
| **抓取频次** | （待补） |
| **文件创建日期** | 2026-09-19 |
| **最后更新** | 2026-09-19 |

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
| `browser_snapshot` 在 wjx.cn 首页/编辑器页返回 >130KB 被截断 | 节点多且响应里的 `dom_hash` 字段无上限，`root_selector`/`max_nodes` 都压不住它 | 编辑器类页面禁用 browser_snapshot；改用 Playwright `connect_over_cdp('http://127.0.0.1:9222')` 直连同一实例读 DOM，截图只用 `browser_screenshot(return_base64=false)` | 2026-09-19 |
| 首页导航显示「登录/免费注册」≠ 未登录 | chrome_debug 调试浏览器已带问卷星账号登录态，但 wjx.cn 首页导航按未登录渲染 | 判断登录态要访问 `https://www.wjx.cn/login.aspx`，已登录会直接跳进「我的问卷」 | 2026-09-19 |
| 文本导入语法 | 首行=问卷标题；`N.题干 [填空题]` / `[单选题]`；单/多选选项逐行写 `A.xxx`；`[量表题]`/`[矩阵题]`/`[排序题]` 同样可用；导入后默认**全部必答** | 入口：创建问卷 → 右侧「文本导入」；改题请改文本重新导入，别在编辑器里逐题点 | 2026-09-19 |
| 点「完成」生成问卷会弹腾讯「点击开始智能验证」 | 人机验证，可能升级为滑块/选图 | **必须人工完成，agent 不得代过**；先告知用户再重试提交 | 2026-09-19 |
| 「必答」勾选框 JS 点击无效 | `input[id^=req_]` 被包在 `span.wjx__templet__beautifyInput` 里，`span.click()` 不触发 | 取该 span 内的 `label` 用 Playwright 真实 `handle.click()`；`req_<题号>_<随机>` 的中间数字就是题号，可据此校验点没点错题 | 2026-09-19 |
| 填空题类型校验的选项叫「邮件」不叫「邮箱」 | `select.verify-check-type`（onchange=`cur.setVerify`）选项为 不验证/整数/小数/年月/日期/时间/手机/固话/**邮件**/密码/网址/身份证号/学号/QQ… | 找选项要按 `innerText==='邮件'` 匹配；设完 dispatch `change` 事件才会生效 | 2026-09-19 |
| 属性验证/必答等设置行只对「当前激活题」渲染 | 未激活的题没有这些控件；页面上同时只有一份 | 改某题设置前，先 JS `click()` 它的题干文本激活它 | 2026-09-19 |
| 说明语写进了问卷标题 | `.surveyhead` 里 `querySelector('textarea')` 先命中 `#paper_attr_title`（标题），wangEditor 的说明区是 `.surveydescription .w-e-text`（contenteditable），`#paper_attr_desc` 只是隐藏镜像 | 填标题用 `#paper_attr_title`；填说明用 `.surveydescription .w-e-text` 并 dispatch `input`，之后回读 `#paper_attr_desc` 确认已同步 | 2026-09-19 |
| 保存按钮文本「完成编辑」有 5 个同名节点 | `browser_action` 按 text 定位报 MULTIPLE_MATCHES | 直接用 `css="#hrefFiQ"`；保存后跳 `designstart.aspx?activity=<id>`，页面显示「此问卷处于草稿状态」+「发布此问卷」 | 2026-09-19 |


## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-19 | 初始创建（由 browser_write_lesson 自动生成） |
| 2026-09-19 | 追加 已知坑（来自 https://www.wjx.cn/wjxdesignnew/designnew.aspx） |
