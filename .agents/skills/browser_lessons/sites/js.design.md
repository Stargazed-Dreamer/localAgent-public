---
aliases: [即时设计, jsdesign, js.design]
---

# 即时设计 (js.design)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | js.design |
| **首次接触日期** | 2026-09-16 |
| **相关 skill** | homework（hci 作业流水线）、`workspace/homework/tools/jsd_research/` |
| **登录态要求** | 需登录（自注册账户）；客户端登录态存 `%APPDATA%\js.design\` |
| **抓取频次** | 周期（每次上机实验 / 大作业） |
| **文件创建日期** | 2026-09-18 |
| **最后更新** | 2026-09-18 |

## 网站概况

云端 UI 设计工具，本学期 HCI 课程的原型工具（替代 Axure）。agent 在此做的事：导入/导出 `.jsd` 工程、
打开文件核对渲染结果。**桌面客户端只是加载本站 Web 应用的 Electron 壳**，所以浏览器经验两边通用。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 A（网页版） | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一调试浏览器 |
| 连接方式 B（**客户端壳**） | 启动加 `--remote-debugging-port=9223`，raw CDP websocket 直连 | 2026-09-18 实测可用，登录态自动复用 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 反风控 | 不注入 JS 伪装补丁 | 项目统一，见 `docs/browser-anti-detection.md` |
| CDN 静态资源 | 必须带 `Referer: https://js.design/` | 否则 403（img.js.design） |

## DOM 结构与提取规则

### 字段提取表（工作区页 `/workspace`）

| 字段 | 选择器 | 注意事项 |
|------|--------|---------|
| 文件卡片 | `[class*=cardPanel]` | 带 `data-id`（文件 id）、`data-recentteamid`；React props 只有 `onDoubleClick`/`onContextMenu`/`onMouseDown`，**没有 onClick** |
| 文件名 | `[class*=cardName]` | 单击名称框无任何反应，别拿它当点击目标 |
| 封面区（可点） | `[class*=cardImg]` | 双击坐标取卡片高度 ~35% 处（避开名称/底部信息条） |
| 导入入口 | `input[type=file]`（工作区页实测 2 个） | 但 `window.showOpenFilePicker` 也存在 → 是否真走原生选择器**未实测** |
| 编辑器 URL | `https://js.design/f/<shortKey>?p=<nodeKey>&mode=design` | 打开文件时**新开 target**，原 `/workspace` 页 URL 不变 |

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| 全屏"新功能引导"弹窗吃掉所有点击，界面看着没反应 | 反复发点击事件、怀疑坐标算错 | 查 `document.elementFromPoint(x,y)`；遮罩类名 `updateMask__*`/`updatePopupPanel__*`（尺寸=全视口），点 `[class*=updatePopup] [class*=closeIcon]` | 2026-09-18 |
| 找关闭按钮找错 | `document.querySelector('[class*=closeIcon]')` | 该选择器**先命中别处**的 closeIcon（实测 1330,104 是侧栏的），必须限定在弹窗子树内查（实测真关闭键 934,233） | 2026-09-18 |
| playwright 连客户端调试口挂死 | `connect_over_cdp("http://127.0.0.1:9223")` | 该壳有 4-5 个 `url` 为空的 page target，playwright 会 30s+ 无输出；改 `websocket-client` 直连具体 target 的 `webSocketDebuggerUrl`（`suppress_origin=True`） | 2026-09-18 |
| 判定"文件没打开" | 只轮询当前页 `location.href` | 轮询 `/json/list` 找 `/f/` 新 target | 2026-09-18 |
| 读截图里的文字 | 调 `ocr_file`（一次请求把 localAgent 后端 :8766 整个打死，端口拒连） | 单张判定直接用多模态 Read 读 PNG；必须批量 OCR 时先想清楚重启成本 | 2026-09-18 |
| 「确认导入」点不动 | `Input.dispatchMouseEvent` 鼠标事件（坐标正确也无反应） | 该按钮是 `<a class="baseBtn…submitBtn">`，要用 `el.click()` 沿祖先找 React `onClick` 才生效 | 2026-09-18 |
| 导入 input 认错 | 取第一个 `input[type=file]`（`#0 accept=".fig"` 是上传面板） | 挑 **`accept` 含 `.jsd`** 的那个（实测 `#1 accept=".xd, .jsd, .fig, .sketch"`） | 2026-09-18 |
| `Page.captureScreenshot` 挂死 | 反复调参数（`fromSurface`/`captureBeyondViewport`/`png` 降级） | **未被壳注册成可见标签的 webContents 上它永远挂死**，参数降级无效。改走：`ShowWindow(SW_RESTORE)`+`SetForegroundWindow` → 后端 `/screen/capture {mode:"fullscreen",format:"path"}` → 按 `list_windows` 的 bbox 自己裁 | 2026-09-18 |
| 截图拿到纯色假成功 | `mode=window` 直接信返回值 | 窗口最小化时 PrintWindow 返回**全图 1 种颜色**（1800×1127 仅 8.5KB）。截图后先数色彩数（`Image.getcolors()`），个位数即假图 | 2026-09-18 |
| 双击卡片开的编辑器"存在但看不见" | 以为 `Target.activateTarget` 返回 ok 就切到了标签 | 壳标签栏 `desktopTabBar.html` 是**独立 target**，只注册 1 个标签；`activateTarget` 只改 OS 窗口标题，标签栏 innerText 不变。**往 tabbar 派发点击也没用**——那个工程根本没被登记成标签 | 2026-09-18 |
| **`Target.closeTarget` 关"孤儿编辑器 target" → 客户端主进程崩溃** | 觉得"存在但壳没给标签"的 webContents 是垃圾，关掉再双击重开 | **禁止对即时设计壳的 webContents 调 `Target.closeTarget`**。壳的 `TabService` 仍登记着它，下一次双击走 `TabService._goto` 读到 `null` → Electron **主进程** 抛 `TypeError: Cannot read properties of null (reading 'getURL')` 并弹模态错框，整个客户端废掉（实测踩过）。要清只能**重启客户端** | 2026-09-18 |
| 想让某个指定工程变成"可见标签" | 双击它的卡片 | 该工程**已在别处打开**时双击不再注册新标签（tabbar 文字一直停在 `我的文件`）。→ **桌面壳 R1 拿不到"任意指定工程"的可见像素**；逐工程截图验收要走 R2 网页版（CDP 9222，标签模型由 Chromium 管，没这层壳内 IPC 记账） | 2026-09-18 |
| 卡片上的 `data-id` 当工程 key 用 | 拿 `data-id` 拼 `js.design/f/<data-id>` | `data-id`（实测 `6aad30cf6fc7`）**不是** URL 里的 shortKey（实测 `87siY3`）。只能按"新出现的 `/f/` target + 面包屑工程名"认工程 | 2026-09-18 |
| 以为 CDP 截图对可见标签稳定可用 | 只走 `Page.captureScreenshot` | **可见标签也会超时**（同一 target 一次超时、下一次成功）。必须留"置前 + 全屏裁剪"兜底，且**两路都过色彩数检查** | 2026-09-18 |
| 抄近路用 URL 打开工程 | `location.assign('/f/<key>')` | 若该工程已在别处打开过，会渲染成**空白新文档**（`页数：1 / 按快捷键「A」/「F」开始创建画板`），`Page.reload(ignoreCache)` 也救不回。开文件必须走 UI（卡片双击） | 2026-09-18 |
| 把面包屑里的 `NN%` 当导入进度 | 看到 `43%` 不动就判"卡在 43%" | 那是**画布缩放读数**（同一位置另一标签显示 `37%`）。导入是否成功看：`/json/list` 长出 `/f/` 新 target + 面包屑含工程名 + 图层树有条目 | 2026-09-18 |
| 在 DOM 里搜文本层内容 | `querySelectorAll('*')` 全文搜哨兵串 | 文本层文字**只画在 canvas 上，不在 DOM**（实测 0 命中）。DOM 只能验结构（图层名/页数/面包屑），验内容渲染要读像素 + OCR | 2026-09-18 |
| 导入后在工作区找新卡片 | 轮询当前页 innerText 找卡片 | **列表页导入后不自动刷新**，卡片不会出现。判据换成上一条（新 `/f/` target + 面包屑） | 2026-09-18 |
| 从截图版式猜"桌面壳 or 网页版" | 数顶上有没有"标签药丸行"，没有就判网页版 | **那行会被裁切**。实测我把用户在桌面客户端点的截图判成网页版，据此写下"换了载体复现"的错误归因（被用户纠正）。判载体用 UA / `desktopTabBar.html` 是否存在 / 窗口类名，或**直接问** | 2026-09-19 |
| 对壳内 `/f/` 编辑器发键鼠事件却"什么都没发生" | 以为 CDP `Input.dispatch*` 一定能驱动页面 | 这些 target 的 `document.visibilityState` **恒为 `hidden`**（壳没注册成可见标签），`Page.bringToFront` 与 `Page.setWebLifecycleState{active}` 都改不动 → **输入事件不落地**（按 A 画不出画板、双击图层行 `activeElement` 仍是 `BODY`）。同一个原因还解释了 `captureScreenshot` 挂死。**判据：动手前先读 `visibilityState`** | 2026-09-19 |
| 导入后编辑器里「文件已升级完成，如有疑问…」**弹窗吃输入** | 只清 `[class*=updatePopup]` 那类引导弹窗 | 还有一条 `SPAN.iKnow` 的「我知道了」，要用 `react_click('我知道了')` 关；没关掉时所有键鼠实验都是空转，**别把空转读成"应用不保存"** | 2026-09-19 |
| 按 `/f/<shortKey>` 找用户人工打开的工程页面 | `url.contains('/f/TOMOWE')` | **人工双击**开出来的 target，URL 用的是 **24 位 `_id`**（`/f/6aad83d8c81e…`）而不是 shortKey；程序化导入时应用自开的才用 shortKey。两种都要匹配，否则会把"页面还在"误读成"页面被关了" | 2026-09-19 |

| **把"网页版未登录"写进文档** | 打开 `https://js.design/login`，看到没有登录表单就判"未登录" | **误判**。`/login` 会 **302 到营销首页 `/home`**（首页只有「前往工作台」span，不是登录表单）。**判登录态要开 `https://js.design/workspace`**：出现「我的文件 / 我创建的 / 共享给我的 / 回收站」+ 文件列表 + **没有任何「登录/注册」按钮** = 已登录 | 2026-09-19 |
| 判断 `networkidle` 能等到 | `goto(url, wait_until="networkidle")` | 本站有长连接/长轮询，**`networkidle` 永不触发**（实测 45 s 超时）。用 `domcontentloaded` + `wait_for_timeout(3000~6000)` | 2026-09-19 |

## 可机判的验收配方（09-18 实测跑通，`jsd_research/SELFCHECK.py`）

- **L0 云端（09-19 新增，最便宜且最确定，`jsd_research/CONTENT.py`）**：先判"云端到底存了多少"，
  再看渲染。`POST /backend/api/v2/projects/fetchContent`（body `projectId=<24位_id>`）→
  `data.lastSave.lastSeq` + `data.autoSaves[].content`（**RFC 6902 JSON Patch 批次**）。
  度量 = `lastSeq` 是否 >1、patch 里 `"path":"/element/…"` 的个数。
  基线参照：新建空工程 `lastSeq=1 / 2727B / 0 个 element`；正常编辑过的是 13~31 次保存、10 万+ 字节。
  ⚠ 别按"云端有一个 projectData.json"想（那个 OSS 对象只是空槽，读到的是 0 字节）。
  同源 fetch 要在 js.design 页面里发（编辑器 target 也能发，即使它是 hidden 的）。

- **L3 结构**（最稳，先跑）：`document.body.innerText` 里的面包屑 + `页数：N` + 图层树 frame 名。
- **L1 像素**：截图后数 `distinct_colors` + 画布区"墨迹占比"（亮度 ∈[20,235)），
  专治 PrintWindow 的**纯色假成功**（实测假图色彩数=1，真图 22,815）。
- **L2 文字**：文本层文字不在 DOM，但 **PaddleOCR 认得出来** —— `/ocr/path/json` 对 37% 缩放的
  整窗截图返回 151 行、命中注入串「jsd验收OK」、后端没崩。前提是**先查 `/health` 的
  `ocr.model_ready=true`**（上次打死后端的是冷启动路径，不是稳态推理）。
  ⚠ **必须先把视图对准，否则 OCR 必然落空**（09-19 补）：编辑器打开工程默认"显示全部"（实测 15%），
  17px 的字只剩 ~3px 高。做法 = 图层树选中该图层所在行 + `Ctrl+2`「显示选中内容」
  （已固化为 `SELFCHECK.py --focus <图层名>`）。实测 64% 下读出 `jsd验收OK` 置信度 0.998。
  **`--sentinel` 与 `--focus` 要成对给** —— 否则分不清"字没渲染"和"图太小"，会判成假红。
- **红→绿已验**：同一张图，假哨兵 → `pass:false` + `fail_reason`，真哨兵 → `pass:true`，退出码 1/0。

## 遗留问题

- **自主触发导入：已绕开（09-19 20:2x 打通，不再是遗留问题）**。原判红结论（`DOM.setFileInputFiles`
  + `change` + `el.click()` 确认导入能把流程走完、也在云端建出工程，但**内容没上到云**）**仍然成立**，
  但已不再是唯一路径：**内容从来不需要经过"应用的文件导入"** —— 它落在 OSS 的
  `assets/projectData/<_id>.json`，用 `POST /api/v2/projects/upload_url {id}` 拿现签政策直传即可
  （成功 = **HTTP 204**）。工具 `jsd_research/PUSH.py --from-jsd <a.jsd>`，一条命令、不碰键鼠、
  重开可见。⚠ **必须推在还没有任何 autoSave 的工程上**：新建工程那条 2727B 骨架 patch 第一 op 是
  `add /element {}`，会把基座元素覆盖成空。
  另一条已定位的根因：**导入流程自开的那个编辑器实例，保存管线是死的**（在里面合成输入，
  本地图层行 8→11 能落地，云端 `lastSeq` 120s 纹丝不动）；**重新打开**的实例保存正常。
  ⚠ 判据仍必须含"**关掉再重开后仍有内容**"，但"导入那一刻编辑器里的图层树"**是本地渲染、不能当证据**。
- ⚠ **改文案必须同步改框（09-19 新铁律，踩过）**：文本元素 `resizingVal.horizDirect="scale"` 时，
  渲染器会把**超出框宽的文字横向压扁**塞进去，画布上只认得出前两三个字（实测 36px 框塞 7 个字
  → 只认得出「jsd验」，OCR 读不全）。**应用自己的导入器会按新文案自适应框宽**，
  但"原样搬运元素表"的写入路径（`PUSH.py`）**不会** —— 调用方必须一起把
  `autoSizeType=1`、`w=textW`（自然宽度）、`x` 重新居中改对。实测改对后 OCR 字框 68×19px，
  与人工导入的 69×19px 等价。
- **应用会把外部写入的基座文档当一等公民**（09-19 实测）：打开后会自己回写一批 patch
  （逐元素补 `fillPath {0}`/`linePath []`、注册 `view.textsPath` 字体路径），`lastSeq` 0→3。
  但它**不修文本框宽度**（patch 里没有 `/w`）。
- **`IMPORT.Cdp.ev` 要带 `awaitPromise`**（09-19 修）：本项目探针几乎都是异步 IIFE
  `(async()=>{…fetch…})()`，不 await 拿回来的是 Promise 对象（序列化成 `{}` 或空串），
  曾让 `web_shortid` 拿空串崩、`upload_url` 探测拿回 `{}`。已改成默认 `await_promise=True`。
- **别从截图版式猜"这是桌面壳还是网页版"**（我踩过，被用户纠正）：我曾据"顶上少了标签药丸行"
  判用户那张截图是网页版，实际就是桌面客户端点的——那一行**会被裁切**。可靠的判载体方式：
  ① 直接问用户；② 看 `navigator.userAgent`（壳里带 Electron/客户端版本，且 `/json/list` 有 `desktopTabBar.html`）；
  ③ `list_windows` 的窗口类名/进程名。**版式缺失 ≠ 载体不同。**
- **重启客户端后壳的 tabbar target 会从 `/json/list` 里消失** → 依赖"标签栏标题"的可见性探针当场失效
  （返回空串）。别把探针空值读成"没有标签"；重启后要重新发现 tabbar target，或换不依赖壳的判据。
- **导入件的卡片封面普遍为空**：`img`/`canvas`/`background-image` 对 `accept_stage2_改字验收`、
  `iOS 常用组件-390` 这些**已知有内容**的工程同样取不到图（封面只出现在模板新建的工程上）
  → "无封面"是导入件常态，**不能当"内容没上传"的证据**。
- **逐工程像素验收：已解决（09-19）**。桌面壳上确实做不到（见上表"想让某个指定工程变成可见标签"），
  但**不需要在壳上做**：网页版（CDP 9222）标签模型由 Chromium 管、URL 就是打开路径，
  `SELFCHECK.py --web <工程名>` 会自动解析 shortId 并冷启动新标签渲染。
  更关键的是——**"任意工程可见像素"这格的真正解药是"自己能造工程"**：
  既然 `PUSH.py` 能按需造出**内容已知**的云端工程，就不必再依赖"导入那一刻恰好可见"。
  R4（桌面 UIA + 原生对话框）本票不需要。
- 客户端版本更新会让类名 hash 变化（`cardPanel__28Rxy` 这类带 hash 的选择器易失效）→
  选择器一律用 `[class*=前缀]` 模糊匹配，别写死 hash。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-18 | 初始创建，汇总自 `workspace/homework/tools/jsd_research/NOTES.md` 第三轮 R1 实测 |
| 2026-09-18 | 推翻"导入卡 43%"错误结论；补 6 条实测坑（截图挂死真因、PrintWindow 纯色假成功、tabbar 独立 target、URL 直开变空白、`NN%` 是缩放、canvas 文字不在 DOM） |
| 2026-09-18 | 建 `SELFCHECK.py`（三档判定，红→绿已验）；补 4 条新坑：**`Target.closeTarget` 会让壳主进程崩溃**、已打开的工程双击不再注册标签、卡片 `data-id` ≠ URL shortKey、可见标签的 CDP 截图也会超时 |
| 2026-09-19 | T02/T03 收口：**写路径改走"直传 OSS `assets/projectData/<_id>.json`"**（`PUSH.py`，成功=204），应用导入入口判红但不再阻塞；"逐工程像素验收"与"自主导入"两条遗留问题**解除**；补 4 条新坑：**OCR 必须先把视图对准（`--focus` = 选中图层行 + `Ctrl+2`）**、**改文案必须同步改框**（`horizDirect="scale"` 会压扁超长文字）、应用会回写派生数据但不修框宽、`Cdp.ev` 要带 `awaitPromise` |
