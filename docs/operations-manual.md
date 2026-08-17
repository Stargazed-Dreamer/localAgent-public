# 操作手册（Operations Manual）

从 AGENTS.md 拆出的操作性指南：后端运维、终端 API、触发型任务（日总结/挂机关机/浏览器经验）、记忆系统、工具脚本、GUI 性能。AGENTS.md 仅保留每次会话必读的核心 agent 行为规范。

## 后端重启流程

项目经常需要更新后端代码并重启服务。**必须按以下流程操作，否则会关不掉旧进程或打不开新进程。**

### 1. 优雅关闭旧后端

调用 `POST /shutdown` 接口，后端收到后会自动退出（0.5秒延迟后 `os._exit(0)`）：

```bash
# 方式一：curl（推荐）
curl -X POST http://127.0.0.1:8766/shutdown

# 方式二：PowerShell
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8766/shutdown

# 方式三：MCP 工具（如果 MCP 还能用）
mcp_localagent_exec_python 执行: import urllib.request; urllib.request.urlopen("http://127.0.0.1:8766/shutdown")
```

**注意**：
- 不要用 `taskkill /F` 强杀进程，可能导致端口未释放、临时文件未清理
- 如果 `/shutdown` 无响应（后端已卡死），才用 `taskkill /PID <pid> /F` 强杀
- 关闭后等 2-3 秒再启动新进程，确保端口释放

### 2. 启动新后端

运行根目录的 `start.bat`，它会自动完成：

1. **检测管理员权限** → 无权限则自动 UAC 提权（弹窗申请管理员权限）
2. **杀掉占用 8766 端口的旧进程** → `netstat` 查找 + `taskkill /F`
3. **启动后端** → `.venv\Scripts\python.exe -m server.main`

```bash
# 直接双击或命令行运行
start.bat
```

**管理员权限说明**：键鼠操控（`SetCursorPos`）需要管理员权限，否则 Windows UIPI 会阻止。`start.bat` 会自动检测并申请提权。

### 3. 验证启动成功

```bash
curl http://127.0.0.1:8766/health
```

返回 `"status": "ok"` 即启动成功。

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 端口 8766 被占用 | 旧进程未完全退出 | 等 2-3 秒，或手动 `netstat -ano \| findstr :8766` 找 PID 杀掉 |
| `start.bat` 闪退 | UAC 被拒绝 | 右键 → 以管理员身份运行 |
| MCP 工具不可用 | 后端未启动 | 先运行 `start.bat` |

## API 常见陷阱

> 从 AGENTS.md 拆出的详细 API 调用陷阱和避免方法。AGENTS.md 仅保留核心铁律指针。

### PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名（重要！）

在 PowerShell 终端运行 `curl -s http://127.0.0.1:8766/health` 会触发 `Invoke-WebRequest`，它把 `-s` 解析为 `-Session`，然后等待用户输入 `Uri:` 参数，导致终端卡住，必须 Ctrl+C 才能中断。

**避免方法**：
- 用 `curl.exe` 强制调用真正的 curl：`curl.exe -s http://127.0.0.1:8766/health`
- 或用 `Invoke-RestMethod`：`Invoke-RestMethod -Uri http://127.0.0.1:8766/health`
- **绝对不要**在 PowerShell 中直接用 `curl -s ...`，会卡住终端

### PowerShell 不支持 `&&` 串联命令语句（反复踩坑！）

Windows PowerShell 5.1 不支持 `&&`（管道链运算符），用 `cmd1 && cmd2` 会报 `The token '&&' is not a valid statement separator`。

**避免方法**：
- 用 `;` 顺序执行（不管前一个是否成功）：`cmd1 ; cmd2`
- 用 `$LASTEXITCODE` 显式判断：`cmd1 ; if ($LASTEXITCODE -eq 0) { cmd2 }`
- 或改用 PowerShell 7（`pwsh.exe`，支持 `&&` 和 `||`）
- **Shell 工具链式命令时不要用 `&&`**，用 `;` 或拆成多次调用

### Git 命令默认走 pager 会卡住终端（agent 自动执行必读！）

`git log` / `git diff` / `git show` 等命令在 Windows 上默认使用 `less` 作为 pager，输出超过一屏时进入分页模式，等待用户按键（空格/q/回车）才显示后续或退出。在 agent 自动执行场景下命令会一直挂起，直到被用户手动跳过或 Ctrl+C，外观像"命令在运行但没输出"。

**避免方法**：
- 用 `git --no-pager log ...` / `git --no-pager diff ...` 强制单次关闭 pager（推荐，最稳）
- 或显式重定向 pager：`git -c core.pager=cat log ...`（输出直送 cat，不暂停）
- 或限制输出长度：`git log -n <N>`、`git diff --stat`、`git diff <ref>~1 <ref> -- <path>`
- 全局禁用 pager：`git config --global core.pager cat`（影响所有仓库，需用户授权，不要擅自改全局配置）
- **绝对不要**在 agent 自动执行场景下直接用 `git log` / `git diff` 不加 `--no-pager` 或长度限制，会卡住终端

### 不要根据文档注释判断文件是否存在（反复踩坑！）

AGENTS.md / _index.md / skill 文件中的注释可能过时（如"XX 尚未创建"、"XX 在 wip 待办中"），直接采信会导致错误结论。

**避免方法**：
- 文件是否存在 → 用 `Read` / `Glob` / `Test-Path` 实际验证，不要信文档注释
- 文件内容 → 用 `Read` 实际读取，不要根据文档描述推断
- 接口是否存在 → 用 `curl.exe` / `Invoke-RestMethod` 实际调用验证
- **典型案例**：AGENTS.md 曾写"CHANGELOG.md 尚未创建"，但实际文件已存在（40KB），agent 没读就下结论导致 CHANGELOG 漏更新

### 禁止大量返回 Base64 污染上下文（强制规则）

所有代码、脚本、MCP 工具调用都不得返回大段 base64 数据（截图、文件、音频等）到 LLM 上下文。遇到 base64 立即改代码或调用方式：
- 截图给多模态 LLM：用 `capture_screen(format="inline")`（返回 ImageContent，由 MCP 协议处理，不进文本上下文）
- 截图特征分析：用 `screen_analyze`（返回纯文本的颜色/亮度/异常分析，无 base64）
- 截图+OCR：优先 `localagent_advanced_tool(tool="screen_ocr", params={...})`，直接返回文字和 bbox，不经过 base64
- 文件读取：用 `docviewer_read` 或 `exec_python` 提取文本，绝不返回 base64
- **违反此规则会导致上下文窗口被数 MB 的 base64 字符串撑爆，LLM 无法继续工作**

### 截图+OCR 的正确流程（重要！）

优先调用高级工具 `screen_ocr`，一步截图并返回 `text + details[].box`，不返回图片数据：

```
localagent_advanced_tool(
    tool="screen_ocr",
    params={"mode":"window", "hwnd":123, "engine":"ocr"}
)
```

需要已有图片文件时用 `ocr_file` / `ocr_path`。需要把截图交给其他工具时可用 `capture_screen(format="path")` 获取临时路径，再传给 `ocr_path`。**绝对不要**直接调用 `capture_screen(format="base64")` 把数 MB base64 返回上下文。

### 截图特征分析（远程 VL 不可用时的图像辅助）

当远程 VL（ModelScope）不可用（限流冷却中或网络异常）、OCR 只能识别文字时，用 `screen_analyze` 获取图像特征（主色/亮度/异常检测/网格分区），全部返回文本，无 base64：

```
screen_analyze(params={"mode":"fullscreen"})  # 全屏分析
screen_analyze(params={"mode":"window","window_title":"异环","grid_size":4})
```

返回：主色列表（hex+名称+占比）、亮度统计、颜色多样性、边缘密度、4x4 网格分区主色、异常告警（蓝屏/黑屏/白屏/单色统治/警告色等）、一句话总结。**用于快速判断画面是否异常**（如游戏卡加载、应用崩溃、蓝屏等），比 `capture_screen` 更有信息量，又不会像 base64 那样污染上下文。

### `ocr_file` / `ocr_path` 的适用场景

识别已有图片路径；实时屏幕定位用 `screen_ocr`。`screen_snapshot(with_ocr=true)` 只返回文字摘要，不返回 bbox。

### 坐标参数必须为整数

`/screen/action` 的 `x`/`y` 类型为 `int`，传浮点数会返回 422 验证错误。脚本中应使用 `int(x)` 转换。

### 422 验证错误

Pydantic 验证失败时返回 422，响应体包含 `detail` 数组，每项有 `loc`（出错字段路径）和 `msg`（错误描述），可用于快速定位问题。

## 终端会话 API（长时间运行命令）

对于模型下载、pip 安装、批量处理等长时间运行命令，使用终端会话 API 而非 `/exec/python`（后者有 30s 默认超时）：

> **注意**：当 agent 平台的终端功能较差或无法满足需求时，才考虑使用本项目的终端 API。本项目的终端实现可能较为原始，主要用于后端长时间运行命令的监控和管理。

| MCP 工具 | 用途 |
|---------|------|
| `exec_terminal_spawn` | 启动后台终端（返回 tid，不阻塞） |
| `exec_terminals_list` | 列出所有终端（状态/PID/输出字符数） |
| `exec_terminal_detail` | 查看终端详情（stdout/stderr，支持 tail 截取） |
| `exec_terminal_output` | 按字节游标增量读取完整 stdout/stderr（单次最多 256 KiB） |
| `exec_terminal_input` | 向运行中终端发送输入（交互式命令/Ctrl+C） |
| `exec_terminal_kill` | 终止运行中终端 |
| `exec_terminal_delete` | 删除已结束终端记录 |

**典型流程**：
1. `exec_terminal_spawn(cmd="python download.py", label="download_model")` → 获取 tid
2. 轮询 `exec_terminal_detail(tid, tail=3000)` 查看近期进度（`tail=0` 默认仅返回 8000 字符安全预览）
3. 如需交互：`exec_terminal_input(tid, text="y")` 回应提示
4. 需要完整日志时，用 `exec_terminal_output(tid, channel="stdout", offset=0, limit=65536)` 按 `next_offset` 分页读取
5. 完成后 `exec_terminal_delete(tid)` 清理会话和磁盘日志

终端输出以 `temp/terminals/` 下的日志文件为真源，内存只保留每通道最近 64 KiB；stdout/stderr 会并发排空，避免高输出命令因管道写满死锁。运行中终端默认上限为 10 个，防止 Agent 无界创建后台进程。

**终端监控**：通过 GUI 客户端（`start_client.bat`）的 Monitoring 面板查看终端管理标签页，实时查看所有终端输出

## 日总结功能（用户问"总结今日"时触发）

**"今天"的定义（关键，agent 易误解）**：用户说"总结今日" / "今天的日报" 时，"今天" = **从上一个 05:00 到当前时刻**，不是"等到下一个 05:00"。用户要日报时通常是要睡觉或结束工作，**不会期待更多记录**——立即基于已有 hourly 文件汇总生成，不要因"今天还没到 05:00 结束"而推迟或询问。文件名由当前时间决定：当前 ≥ 05:00 用当天日期，当前 < 05:00 用前一天日期（如 07-14 04:00 请求 → `20260713.md`）。详见 `.agents/skills/daily_summary.md` 的"日期划分规则"。

**数据源优先级（必读）**：用户说"总结今日工作" / "今日做了什么" / "总结一下" 时，按以下顺序收集信息：

1. **首选：[data/activity/hourly/](file:///f:/<project_root>/data/activity/hourly) 目录下的 `YYYYMMDD_HH.md` 文件** — 这是本功能的本源数据，由 `hourly_summarize` loop 任务每整点自动生成。**先读这些文件，不要先查 git log**。
2. **补全缺失时段**：用 `git log --since="YYYY-MM-DD 05:00" --until="now"` 补全 hourly summary 缺失或失败的时段（注意 since 用 05:00 不是 00:00，与日期划分规则一致）。
3. **memory 仅作参考**：`c:\<user_home>\.trae-cn\memory\projects\-f-project-temp-localAgent\YYYYMMDD\topics.md` 提供会话级上下文，但**不是主要数据源**。

**易踩的坑**：
- hourly summary 文件可能整小时缺失（LLM 失败、后端重启）— 此时必须用 git log 补全
- 若 hourly summary 标注 "VL 屏幕描述数据整小时缺失" → `screen_vl` loop 任务可能被暂停，查 `loop_list_tasks`（走 `localagent_advanced_tool` 网关调 `loop_status`）确认
- 若 hourly summary 显示"LLM 返回空内容"占位文件 → `hourly_summarize` 任务最近失败，可手动 `loop_run_task` 重试
- **不要在日报中加入 agent 的 task_closure 工作报告** — 日报是用户的活动记录，不是 agent 的收尾报告，两者是不同的东西

**判断偏差反馈**：agent 容易下意识把 git log + memory 当成主要数据源，而忽略本功能的内部数据源 hourly summary。这是错误的——必须先查 hourly summary，再外求。

## 挂机监控 + 条件关机（用户说"我要睡觉了，XX 条件就关机"时触发）

**触发词**：「我要睡觉了」「挂机」「跑完就关机」「XX 完成就关机」「条件达成后关机」「定时关机」「23:30 关机」等表达睡前/离开后让 agent 监控并关机的意图。

> **2026-08-03 重构**：原 `workspace/auto_shutdown/monitor.py` 独立 CLI 子进程已删除。关机流程收进后端 REST 端点（`server/auto_shutdown.py`）。agent 现在在 Trae 会话里自己跑检测 loop，不依赖独立子进程。原因：80% 真实场景复杂（识图/查日志/多条件组合），简单条件检测覆盖不了，必须 agent 在场判断；agent 在场 = 后端必须活着 + Trae 必须开着，"独立子进程持续运行"价值大打折扣。

### 工作流（agent 自己跑 loop + 调端点）

1. **确认触发条件 + task_id**：复述用户给的条件，确认理解无误；定一个 task_id（用于截屏目录隔离）
2. **agent 自己写检测 loop**：用 `time.sleep` 或 `asyncio.sleep` 控制检测间隔（60s 常用），检测函数 agent 自行编写（识图 / 查日志 / 查 API / 查文件 / 查时间）
3. **条件满足 → 关窗口程序 → 调 `POST /auto_shutdown/trigger`**：
   - **关窗口程序（强制）**：模拟器/全屏应用/有未保存数据的程序会阻碍关机。agent 调 `list_windows` 查窗口，用 `screen_window_close` 或 `execute_action`（Alt+F4）逐个关闭。不要用 `taskkill` 强杀（可能丢失用户数据）
   - **调端点**：端点内部自动截屏 + 推 inbox + 调 `shutdown /s /t 120`（120s 倒计时，多 60s 宽限用户刹停）
4. **用户反悔 → 调 `POST /auto_shutdown/cancel`**：端点调 `shutdown /a` 中止倒计时，幂等
5. **关机完成 / 用户中止**

### 端点参数

#### POST /auto_shutdown/trigger

```json
{
  "task_id": "game_afk_20260803",  // 必填，截屏目录隔离用
  "reason": "游戏完成",             // 可选，默认 ""
  "screenshot": true,               // 可选，默认 true（关机瞬间截屏存档）
  "dry_run": false                  // 可选，默认 false（true 跳过 shutdown 调用，测试用）
}
```

返回：`{success, task_id, trigger_count, shutdown_scheduled, dry_run, screenshot_path, cancel_command}`

#### POST /auto_shutdown/cancel

```json
{"dry_run": false}  // 可选，默认 false
```

返回：`{success, dry_run, cancel_command}`

### MCP 工具调用

```
# 触发关机
localagent_advanced_tool(
    tool="auto_shutdown_trigger",
    params={"task_id": "game_afk", "reason": "游戏完成", "screenshot": true, "dry_run": false}
)

# 取消关机
localagent_advanced_tool(
    tool="auto_shutdown_cancel",
    params={"dry_run": false}
)
```

### 关键约束（强制）

1. **调 trigger 前先关闭有窗口的程序**：模拟器（MuMu/雷电/Nox）、游戏客户端、IDE、有未保存文档的程序在系统直接关机时会阻碍关机流程（弹窗阻止 / 资源占用 / 文件锁）。agent 调 trigger 前应先关这些窗口，优先 graceful close，不要 taskkill 强杀
2. **120s 倒计时**：`shutdown /s /t 120` 触发后 Windows 弹窗 120s 倒计时，用户可在 120s 内喊"取消关机"或自行 `shutdown /a`
3. **关机命令绕过 command_guard**：用户睡前无法审批弹窗，端点内部直接调 `subprocess.Popen(["shutdown", "/s", "/t", "120"])`，绕过 command_guard（设计取舍，已明确同意关机）
4. **端点幂等**：重复调 trigger 递增 trigger_count，重复调 cancel 不报错
5. **副作用容错**：截屏失败（mss 异常）→ screenshot_path=None 仍触发关机；inbox 推送失败（后端不可用）→ 记日志仍触发关机。用户睡前 inbox 不重要，关机本身重要

### 状态记录与通知

- **状态目录**：`temp/auto_shutdown/<task_id>/final_screenshot.png`（关机瞬间截屏，失败时无此文件）
- **inbox 推送**：关机已触发（含 task_id/reason/截屏路径/取消提示）+ 关机已取消
- **trigger_count**：内存中不持久化，后端重启归零，inbox 有历史记录可查
- **/health**：响应含 `auto_shutdown` 字段（trigger_count / last_trigger_at / last_trigger_task_id）

### 示例检测脚本（agent 自行编写，仅供参考）

完整示例见 [.agents/skills/auto_shutdown.md](file:///f:/<project_root>/.agents/skills/auto_shutdown.md)，含 3 种场景：
- 游戏 AFK 识图检测（capture_screen + screen_ocr）
- 定时关机（处理跨日边界：23:35 启动 target="23:30" → 明天 23:30）
- 文件出现检测（标记文件协调跨 agent 任务）

### 受控托管模式联动（自然行为，不单独 spec）

- agent loop 跑 screen 操作前，应确认看门狗授权 active（调 `/screen/grant/status`）
- agent 每次 screen 操作成功会自动续期看门狗授权
- 用户撤销看门狗授权 → agent 下次 screen 操作被拒 → agent 应识别此信号并停止 / 询问用户
- 用户 AFK 时若遇到无法通过已授权、安全路径解决的问题 → agent 必须 fail-closed，立即停止，不得绕过确认、切换未授权通道或无限重试
- agent 触发关机时，看门狗授权自然失效（系统关机），无需显式撤销

> 相关文件：[server/auto_shutdown.py](file:///f:/<project_root>/server/auto_shutdown.py) + [tests/test_auto_shutdown.py](file:///f:/<project_root>/tests/test_auto_shutdown.py) + [.agents/skills/auto_shutdown.md](file:///f:/<project_root>/.agents/skills/auto_shutdown.md)

## 浏览器操作经验记录（强制）

**任何浏览器操作任务**（含 `browser_*` MCP 工具调用、Playwright、`connect_over_cdp`、`page.goto` 的任务，含 `web_archive`/`community_review`/`bilibili_gacha`/`arknights_gacha`/`endfield_gacha`/`official_gacha`/`html-dev-debug` 等浏览器相关 skill）必须遵守：

### 任务开始前（必做）

1. **识别目标网站主域**：从 URL 提取 eTLD+1（如 `https://www.xiaoheihe.cn/community/123` → `xiaoheihe.cn`）
2. **查网站索引**：`Read .agents/skills/browser_lessons/references/site_index.md` 看该主域是否已记录
3. **若有**：`Read .agents/skills/browser_lessons/sites/<domain>.md`，把"已知坑"和"DOM 结构"段读入上下文，**避免重复踩坑**
4. **若无**：任务中遇到非显然行为时按 `.agents/skills/browser_lessons/sites/_template.md` 创建新文件

### 任务收尾时（强制规则，已并入 task_closure step 5）

收尾 6 步流程的 step 5（文档自查）必须额外检查：本次浏览器操作是否遇到非显然行为？若是，写入对应 `sites/<domain>.md`，并同步更新 `references/site_index.md`。

**"非显然行为"判定标准**（满足任一即记录）：
- DOM 选择器与"看起来对"的不一致（如小黑盒作者应该是 `.info-box__username` 但实际取到评论者，正确是 `.link-user__username`）
- URL 有特殊参数携带真实数据（如小黑盒 `redirect_data` 含标题 JSON）
- 同一网站有多种页面布局（如小黑盒图文帖 vs 文章帖）
- 反爬/风控机制（如微信图片防盗链需 Referer）
- 登录态特殊处理（如必须用调试浏览器而非无头）
- 懒加载/异步渲染导致选择器失效（如微信图片 `data-src` 而非 `src`）
- 已知遗留问题（如微信评论 Vue 异步未渲染，需调 API）

**不需要记录的**：通用 Playwright/CDP 用法、一次性 bug（已修复）、skill 工作流内部决策（按点赞倒序等）。

### 与 key_pitfalls 的分工

| 类型 | 存放位置 |
|------|---------|
| 网站通用踩坑（DOM 结构、反爬、URL 规则、登录态、防盗链） | `sites/<domain>.md` |
| skill 特定踩坑（换网站就不适用的） | `agent_guide.py` 的 `key_pitfalls` |
| 跨网站通用浏览器踩坑（Playwright/CDP 通用问题） | AGENTS.md "API 常见陷阱" + `browser_lessons/SKILL.md` 末尾 |

> 详见 [`.agents/skills/browser_lessons/SKILL.md`](file:///f:/<project_root>/.agents/skills/browser_lessons/SKILL.md)。

### 知识老化规则（防陈旧经验腐化任务）

站点经验文件含"最后更新"日期，`browser_match_site` / `browser_session_create` 响应含 `staleness_level`：
- **fresh**（<90 天）：可直接参考
- **warn**（90-180 天）：网站可能已改版，验证后再用；验证通过后用 `browser_write_lesson` 更新"最近验证日期"列
- **critical**（>180 天）：高度可能过时，必须验证关键坑是否仍适用后再参考

陈旧的"坑"记录反而会阻碍任务（如网站已修复某 bug 但经验仍标注为坑）。`browser_write_lesson` 写入会自动重置 staleness 为 fresh。

## 桌面操作经验记录（强制）

**任何桌面软件操作任务**（含 `execute_action`/`batch_actions`/`screen_accessibility_snapshot`/`screen_semantic_action`/`focus_window` 等 screen_* 工具调用）必须遵守：

### 任务开始前（必做）

1. **识别目标软件 process_name**：从 `list_windows` 或 `screen_app_list` 获取目标窗口的 `process_name`（如 `qbittorrent.exe`、`Code.exe`、`WINWORD.EXE`）
2. **查软件经验**：调 `screen_match_app(process_name=...)` 查询 `.agents/skills/computer_use/apps/<process_name>.md`
3. **若有经验**：把"UIA 友好度"、"已知坑"、"常用快捷键"、"菜单路径"读入上下文，**避免重复踩坑**
4. **若无经验**：任务中遇到非显然行为时用 `screen_write_lesson` 创建新文件
5. **list_windows/screen_app_list 响应含 `app_lessons_hint`** 时说明已有经验文件，应主动查询

### 任务收尾时（强制规则，已并入 task_closure step 5）

收尾 6 步流程的 step 5（文档自查）必须额外检查：本次桌面操作是否踩坑或发现最佳实践？若是，用 `screen_write_lesson` 写入对应 `apps/<process_name>.md`。

**"非显然行为"判定标准**（满足任一即记录）：
- UIA 不可用或部分不可用（如 Qt 表格控件 UIA 读不到，需 OCR bbox 定位）
- 快捷键与预期不一致（如某些软件 Ctrl+S 不是保存而是其他功能）
- 菜单路径不直观（如代理设置藏在非 obvious 的子菜单）
- 对话框处理有特殊坑（如某些对话框只能用快捷键关闭不能用点击）
- 窗口标题含版本号/动态内容导致标题匹配不稳定
- 配置文件被界面覆盖（如直接改 ini 被界面保存覆盖）
- DPI 缩放导致坐标偏移

**不需要记录的**：通用键鼠操作、一次性 bug（已修复）、通用 Windows 行为。

### 知识老化规则（防陈旧经验腐化任务）

软件经验文件含"最后更新"日期，`screen_match_app` / `list_windows` / `screen_app_list` / `screen_accessibility_snapshot` 响应含 `staleness_level`：
- **fresh**（<90 天）：可直接参考
- **warn**（90-180 天）：软件可能已更新/界面改版，验证后再用；验证通过后用 `screen_write_lesson` 更新"最近验证日期"列
- **critical**（>180 天）：高度可能过时，必须验证关键坑是否仍适用后再参考

陈旧的"坑"记录反而会阻碍任务（如软件已修复某 bug 但经验仍标注为坑）。`screen_write_lesson` 写入会自动重置 staleness 为 fresh。

### 与 key_pitfalls 的分工

| 类型 | 存放位置 |
|------|---------|
| 软件通用踩坑（UIA 友好度、快捷键、菜单路径、对话框处理） | `apps/<process_name>.md` |
| skill 特定踩坑（换软件就不适用的） | `agent_guide.py` 的 `key_pitfalls` |
| 通用桌面操作踩坑（DPI/焦点安全/坐标绑定） | `docs/computer-use-reference.md` + AGENTS.md |

> 详见 [`.agents/skills/computer_use/SKILL.md`](file:///f:/<project_root>/.agents/skills/computer_use/SKILL.md)。

## PySide6 GUI 性能踩坑（client/ 开发必读）

**核心原则：`on_show()` 是高频调用，不能做重活。** 用户每次切换面板都会触发 `on_show()`，如果其中做同步 DB 查询 + 大量 widget 重建，会导致面板切换卡顿。

| 踩坑 | 后果 | 正确做法 |
|------|------|---------|
| `on_show()` 每次都重新读文件 + 销毁重建所有 widget | 工具面板每次切换卡 200-500ms（29 个 ToolDetailWidget 重建） | 用 mtime 检测：文件没变就不重建（[tools.py](file:///f:/<project_root>/client/panels/tools.py) 的 `_read_manifest_mtime`） |
| `on_show()` 每次都重新查 DB + 填充表格 | 记账面板每次切换卡 300-800ms | 加首次加载标志：`_data_loaded` 为 True 后跳过，用户点刷新按钮才重新加载 |
| 循环中每行创建多个 `QComboBox` 作为 `setCellWidget` | 512 行 × 3 个 QComboBox = 1536 个控件，主线程卡 500ms+ | 懒加载（双击才创建）或分页（只加载前 N 条） |
| N+1 查询：循环中每行单独查 DB | 512 次 SQLite 查询（虽然每次快，但累积开销大） | 一次性预取：`{cat: descriptions for cat in all_categories}` |
| `QCalendarWidget` 在 `__init__` 中创建 | QCalendarWidget 是 Qt 最重控件之一，2 个同时创建卡 100ms+ | 延迟到 tab 首次切换时才创建（懒加载） |
| 主线程同步 DB 查询 | 查询期间 UI 完全冻结 | DB 查询放 `QThread`，通过 `Signal` 回主线程填充 widget |

**异步加载模式（QThread + Signal）**：
```python
class _DataLoader(QThread):
    data_ready = Signal(dict)
    def run(self):  # 后台线程
        data = store.query(...)  # DB 查询
        self.data_ready.emit(data)

def _load(self):  # 主线程
    self._loader = _DataLoader(store)
    self._loader.data_ready.connect(self._on_data)
    self._loader.start()  # 不阻塞

def _on_data(self, data):  # 主线程，填充 widget
    ...
```

**SQLite 跨线程**：`AccountingStore` 用 `check_same_thread=False` 创建连接，可在 QThread 中安全读取（写操作有 `_write_lock` 保护）。

**防重复加载**：异步加载时用 `_loading` 标志拦截重复请求（用户快速切换过滤时），避免多个 loader 竞争。

> 详见 `.agents/skills/client_dev.md` 的性能规范章节。

## 记忆系统

后端提供三层记忆系统 v3（`/memory/*`）：Recent 滑动窗口 + SQLite 时间索引 + 向量语义检索 + BM25 + HMS 风格"存取解耦 + 全息重构"。旧 KV 记忆已兼容，首次启动时自动迁移到 SQLite。Schema v2→v3 自动迁移（幂等）。

**v3 新增（参考 Shadow-Weave/HMS）**：
- **EvidenceLedger 中间层**：`memory_search` 改为 messages（BM25/向量）+ facts（SQL LIKE）+ summaries（SQL LIKE）三源召回，经证据组织器做 Jaccard 交叉佐证 + 时间窗口冲突检测。原原始事实不变，但推理结论可被审视和修正（HMS 核心）
- **facts 结构化字段**：`memory_set` 顶层支持 `fact_type`/`occurred_at`/`consumption_contexts`/`trigger_keywords`，写入 `facts` 表独立列。`consumption_contexts` 支持 SQL LIKE 索引优化匹配 task_type
- **SearchTracer 检索可观测性**：自动记录 query/子检索延迟/融合结果，支持 `sample_rate` 采样率。新增 `GET /memory/search/traces` 等端点（走 `localagent_advanced_tool` 网关）

**使用原则**：
- **任务完成时写记忆**：每个 Skill 工作流结束后，将任务进度、关键结果写入记忆（含 `consumption_contexts` + `trigger_keywords` 确保未来可消费）
- **会话开始时读记忆**：处理用户请求前，先 `GET /memory/{key}` 了解上次做到哪了
- **语义搜索**：用 `POST /memory/search` 按语义查找相关记忆（v3 自动三源召回 + 证据组织）
- **记忆 key 规范**：每个 Skill 对应一个 key（如 `bilibili_gacha`、`accounting`），跨 Skill 偏好用 `preferences`

**记忆生成**（用户触发）：用户说"生成记忆"/"保存经验"/"记忆总结"时 → 调 `agent_guide(task_type='recurring.memory_generation')` 获取 5 步工作流（recall→classify→dedup→write→optional_maintain）。结构化记忆含 `type`/`consumption_contexts`/`trigger_keywords` 字段，确保未来任务能消费。

**记忆维护器**（后端自动）：每 6 小时运行一次（下次记忆 API 调用时触发），老化 >7 天未更新记忆为 stale + 用 LLM 验证准确性。同时自动清理过期 `search_traces` 和 `evidence_ledger` 记录。手动触发：`POST /memory/maintain`（force=true）。

> 三层架构、语义检索、记忆压缩、自动记录、完整 API 表、记忆生成工作流详解、维护器配置详见 `docs/memory-system.md`。设计文档见 `.agents/wip/memory_system_hms_improvement.md`。

## 工具脚本与 LLM 池

项目包含大量工具脚本（抽卡采集、社群爬取、媒体分类、图片整理、批量注释等），通过 `exec_python` 或直接命令行运行。完整脚本列表和用法详见 `docs/tools-guide.md`，工具目录总览见 `tools/README.md`，机器可读清单见 `tools_manifest.json`。

`client/panels/tools.py`（ToolsPanel）+ `client/widgets/tool_runner.py` 是 GUI 工具面板，默认只读取 `tools_manifest.json` 来展示工具。新增工具时通常先改清单；只有 GUI 的布局、分类、队列或运行逻辑需要变化时，才改 `client/widgets/tool_runner.py` 本身。（旧 `tools_launcher.py` 已删除）

脚本中调用 LLM 时，统一通过后端 LLM 并发池代理（`call_via_backend`），支持多 key 并发、拉链排队、per-project token 统计。

> LLM 并发池架构、API、使用方式详见 `docs/llm-pool.md`。
