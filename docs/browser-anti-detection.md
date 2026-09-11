# 浏览器反检测（伪装）规范

**结论先说**：localAgent 用 `connect_over_cdp` 连接**用户手动启动的真实有头 Chrome**，
指纹天然合规。**不要注入任何 JS 伪装补丁**。项目曾使用 `playwright-stealth`，
2026-09-03 实测确认它在真实浏览器上是负资产，已从 `server/` 与全部 workspace 脚本中移除。

> 本文的实测数据来自 2026-09-03 的一次外部报告交叉验证（agentweb-kit 探索报告），
> 复现环境：Chrome 140.0.7339.208 / Windows 10 / 中文 locale / NVIDIA RTX 4070 Laptop GPU。

---

## 1. 架构事实：为什么 localAgent 天生可信

`tools/browser/start_debug_browser.py` 启动的是**用户自己的 Chrome**，带独立
`--user-data-dir`（`chrome_debug/`）与 `--remote-debugging-port=9222`；
所有访问都通过 `connect_over_cdp` 接入（`server/browser/playwright_executor.py:17`）。

站在风控服务器的分层视角：

| 风控层 | 本方案现状 | 评价 |
|---|---|---|
| TLS/JA3/JA4 | 真 Chrome 发出的握手，非 curl/requests 伪造 | 天然合规 |
| HTTP 头顺序 | Chromium 原生顺序 | 天然合规 |
| UA | 真实 UA，无 Headless 字样 | 天然合规 |
| `navigator.webdriver` | **`false`**（非 WebDriver 会话，实测确认） | 天然合规 |
| `plugins` / `mimeTypes` | 原生 5 个合规 plugin，`instanceof PluginArray` 为真 | 天然合规 |
| `window.chrome` / `csi` / `loadTimes` | 真实有头浏览器全部具备 | 天然合规 |
| Cookie / 登录态 | 用户真实 profile（可 `--copy-user-data` 复制） | 天然合规，且远强于空白 profile |
| IP 信誉 | 家用宽带 | 天然合规 |
| 行为节奏 | 人工驱动，本就是人速 | 天然合规 |

**也就是说：在真实浏览器上，"不伪装"就是最好的伪装。** 第三方检测站
`bot.sannysoft.com` 实测裸跑即 8/8 全绿（WebDriver / Chrome / Permissions /
Plugins Length / Plugins Type / Languages 等），加补丁不会让它更绿。

---

## 2. 反面教材：playwright-stealth 的实测危害

`playwright-stealth`（2.0.3）是为「**Playwright 自己 launch 的无头 Chromium**」
设计的**加法型**补丁库。把它用在真实有头浏览器上，会同时造成三类伤害。

### 2.1 篡改真实指纹，制造自相矛盾（3 项）

同一台机器、同一个浏览器的 A/B 实测（临时隔离 context，注入 vs 不注入）：

| 指纹项 | 裸跑（真实值） | 应用 stealth | 后果 |
|---|---|---|---|
| `navigator.languages` | `["zh-CN"]` | `["en-US","en"]` | 与 HTTP 头、`navigator.language` 双重矛盾 |
| WebGL UNMASKED_VENDOR | `Google Inc. (NVIDIA)` | `Intel Inc.` | 独显被伪装成集显 |
| WebGL UNMASKED_RENDERER | `ANGLE (NVIDIA, RTX 4070 Laptop GPU)` | `Intel Iris OpenGL Engine` | 与 16 核 / 8GB / 高分辨率形成"拼接指纹" |

**最硬的一条矛盾**（`bot.sannysoft.com` 单次访问的同一页面内取值）：

```
navigator.language  = zh-CN            ← 补丁没改这个
navigator.languages = ["en-US","en"]   ← 补丁改了这个
```

真实浏览器里这两者**恒等**，一次取值即穿帮。同时 HTTP 请求头仍是
`Accept-Language: zh-CN,zh;q=0.9`——**JS 层与 HTTP 头层自相矛盾**。
补丁只能改 JS 侧，改不了浏览器已按系统 locale 发出的头。

> 这比"不伪装"更糟：不伪装的浏览器是自洽的，伪装的浏览器在多个层之间打架。

### 2.2 污染用户真实 tab（持久副作用）

`add_init_script` 在 Playwright 中是**持久化**的。localAgent 在每次
`browser_action` / `browser_screenshot` / `browser_navigate` 等端点调用时
对用户正在使用的 tab 注入脚本，实测后果：

```
[1] 新建真实 tab，导航到页面 A    → languages = ["zh-CN","zh"]
[2] 在该 tab 上 apply stealth
[3] 导航到页面 B（未再注入）      → languages = ["en-US","en"]   ← 已污染
[4] 再导航到页面 C（未再注入）    → languages = ["en-US","en"]   ← 持续污染
[5] 新开 tab D（未注入）          → languages = ["zh-CN","zh"]   ← 不跨 tab
```

即：**该 tab 生命周期内的后续所有导航都带着假指纹**，包括用户随后自己手动上网。
用户日常浏览被静默污染，GPU 变成 Intel 集显、语言变成英文。

### 2.3 留下可检测的补丁痕迹

| 痕迹 | 说明 |
|---|---|
| `HTMLDivElement.prototype` 凭空多出 `offsetHeight` | hairline 补丁添加；真实浏览器该属性在 `HTMLElement.prototype` 上，`Object.getOwnPropertyNames` 一查即露 |
| 多处属性描述符被 `defineProperty` 替换 | `vendor` / `userAgent` / `userAgentData` 即使值未变也会留下非原生描述符 |
| `permissions.query` / `canPlayType` / `createElement` 被 Proxy 包裹 | 依赖 `Function.prototype.toString` 替换来隐藏，属于补丁库与被检测方的持续军备竞赛 |

### 2.4 一个被实证纠正的细节

按默认参数推断，`hardwareConcurrency` 应被无条件改成 4。实测却是 16（未变）。
根因在上游：`playwright_stealth/stealth.py` 的 `_evasion_scripts` 属性**漏了**
yield `navigator_hardware_concurrency`（`SCRIPTS` 字典与构造函数里都有，唯独
生成脚本列表时遗漏）。属于上游 bug，对本项目是歪打正着。

同理，多数伪造补丁带 `if` 保护，在真实有头浏览器上会自动跳过：
`navigator_webdriver`（`if (navigator.webdriver)`）、`navigator_plugins`
（有 plugins 则跳过）、`chrome_app`（有 `chrome.app` 则跳过）、
`navigator_platform`（`Win32` 相等则跳过）。
**无条件执行的只有 `languages` / `vendor` / `userAgent` / `userAgentData` /
`webgl_vendor` / `permissions` / `media_codecs` / `hairline` / `error_prototype`**——
危害正来自这些。

---

## 3. 规范（强制）

1. **禁止**在浏览器链路中注入任何 JS 伪装补丁，包括但不限于
   `playwright-stealth`、`undetected-chromedriver`、手写 `add_init_script` 篡改
   `navigator.*`。
2. **禁止** `p.chromium.launch()`。统一 `connect_over_cdp` 连调试浏览器实例
   （见 `docs/environment-constraints.md`「Playwright 浏览器操作」）。
3. 需要"反风控"时，用**行为层面**的手段，不要用指纹层面：
   随机延迟、`page.mouse` 真实点击而非 `locator.click()`、逐条处理不批量、
   先查站点经验库（`browser_match_site`）。
4. 改动浏览器链路后，跑一次指纹自检（见 §5）。

> **原则：伪装要做"减法"（删除/还原真实语义），慎做"加法"（伪造）。**
> 每条新增补丁都可能引入新的可检信号。这条原则来自 agentweb-kit 报告的
> 实战教训，也已被本项目的实测独立验证。

---

## 4. 已知缺口（评估过、暂不实现）

以下能力借鉴自 agentweb-kit 报告，评估结论是**均为启发式、可靠性不保证**，
当前不做。留档以便将来重新评估。

| 缺口 | 说明 | 为什么不保证可靠 |
|---|---|---|
| 错误页/风控识别 + 自动重试 | 检测"百度安全验证"/412/Cloudflare 特征后换页重试 | 依赖特征字符串，站点改版即失效；误判会让正常页面被当成风控页反复重试 |
| 正文提取（readability-lite） | 打分选容器、递归下沉、导航噪声剪除、关键词聚焦 | 对 SPA / 非标准 DOM / Canvas 渲染会退化成整页文本；各站效果差异大 |
| 站点级熔断冷却 | 连续被风控后冷却 N 分钟（文件级跨进程） | localAgent 是人工驱动的 GUI 操作，查询频率远低于阈值，几乎不触发 |
| 来源链导航（direct→home→serp） | 先访问首页再进目标页，带 Referer | 对 IP 级频控无效（实测京东/知乎的频控墙三种导航都过不去） |

现有替代：`server/browser/site_lessons.py` 提供**静态**站点经验库
（`.agents/skills/browser_lessons/sites/*.md`），靠人工沉淀，可靠但覆盖有限；
`browser_navigate` 已区分 `navigation_completed` 与 `wait_timeout` 两个语义。

---

## 5. 指纹自检工具

```bash
uv run python tools/browser/fingerprint_selfcheck.py
uv run python tools/browser/fingerprint_selfcheck.py --json temp/fp.json
```

退出码：`0` 通过 / `1` 有 FAIL 项 / `2` 无法执行（调试浏览器未启动）。

**它检测的是"自洽性"而非"像不像机器人"**——这是有意为之。
`bot.sannysoft.com` 这类检测站会对加了补丁的浏览器给出全绿结果，
却看不出 `navigator.language` 与 `navigator.languages` 的矛盾。
**通过率不等于安全。**

14 项检查覆盖：`language↔languages` 一致、HTTP 头↔JS 一致、
`webdriver` 为 false 且非自有属性、`plugins` 类型合规、UA 无 Headless 标记、
UA 版本↔`userAgentData` 版本一致、WebGL 为真实 GPU（且不是 stealth 的伪造默认值
`Intel Iris OpenGL Engine`）、平台一致、硬件信号合理、无注入痕迹。

回归防护已实测验证：临时给 context 注入 `Stealth().script_payload` 后，
自检准确报告 4 项 FAIL（languages 矛盾 ×2、WebGL 伪造值、hairline 注入痕迹）。

工具本身用临时隔离 context、不加 init script，不会污染用户 profile。

---

## 6. 变更记录

- **2026-09-03**：移除 `playwright-stealth` 依赖与全部调用
  （`server/browser/` 7 处、`server/templates.py` 2 处、workspace 抽卡/爬虫脚本 34 行）；
  新增本文与 `tools/browser/fingerprint_selfcheck.py`；
  同步修正 `docs/environment-constraints.md`、`.agents/skills/browser_lessons/sites/_template.md`、
  各 workspace SKILL.md 与 `展示文档/` 中过时的"必须使用 stealth"表述。
