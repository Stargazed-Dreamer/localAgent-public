---
name: computer_use
description: >
  通过截图+视觉AI理解屏幕内容，模拟键鼠操作控制电脑。支持指定窗口操作、
  操作确认、紧急停止等安全机制。触发词：操作电脑、屏幕操作、点击、截图、
  帮我操作、翻页、自动操作、computer use、screen control。当用户需要
  AI操控电脑界面、自动化GUI操作、屏幕交互时触发，即使只说"帮我点一下"。
---

# Computer Use - 屏幕操控技能

## 概述

通过截图+视觉AI理解屏幕内容，模拟键鼠操作控制电脑。支持指定窗口操作、操作确认、紧急停止等安全机制。

## 安全铁律

1. **任何步骤都必须截图思考一遍再点击，不能用脚本直接点**
2. **如果完全不能执行就放弃，不要硬做/瞎点**
3. **涉及删除/支付/关机等危险操作必须用户确认**
4. **优先使用指定窗口模式，避免误操作其他程序**
5. **每次操作后必须截图验证结果**

## 窗口操作铁律（重要）

1. **始终使用指定窗口截图** — 给模型看图用 `format="inline"`，传给后续工具用 `format="path"`；文字定位直接用 `screen_ocr(mode="window")`
   - 全屏截图元素太多，解析慢且容易误操作
   - 给多模态 LLM 看图用 `capture_screen(format="inline")`（返回 ImageContent，不污染上下文）
   - 只判断文字是否存在用 `screen_snapshot`；需要文字 bbox 定位用 `screen_ocr(mode="window", engine="ocr")`
2. **始终使用指定窗口操作** — `execute_action(window_title="xxx", ...)` 或 `batch_actions(window_title="xxx", ...)`
   - 超出窗口范围的坐标会被自动拦截（防误操作）
   - 不传 window_title/hwnd 时会返回警告"坐标可能落到错误窗口"
3. **操作前设置窗口焦点** — 在点击/输入前，确保目标窗口处于前台
   - `execute_action` 和 `batch_actions` 默认 `activate_window=True`，会自动用 `_force_focus_window` 激活窗口
   - 也可手动调用 `focus_window(window_title="xxx")` 先激活
   - **Windows UIPI 会阻止普通进程的 SetForegroundWindow**，后端用 AttachThreadInput + WScript.Shell.AppActivate 组合绕过
   - 如果窗口被遮挡或最小化，操作可能发送到错误窗口
4. **操作后验证窗口焦点** — 点击后截图确认焦点仍在目标窗口，而非弹窗或其他程序抢夺了焦点
5. **窗口标题匹配** — 先用 `list_windows` 或 `screen_snapshot` 获取精确窗口标题，再用于截图和操作
   - 游戏窗口标题可能包含空格（如 "异环  "），需精确匹配
6. **同名窗口冲突** — 多个应用可能有相同标题的窗口（如 Trae 和 qBittorrent 都有"选项"窗口）
   - 用 `process_name` 参数过滤（如 `process_name="qbittorrent.exe"`）
   - 或用 `hwnd` 直接指定窗口句柄（最精确，`list_windows` 返回的 hwnd）
   - `_find_window(title, process_name)` 会先按进程名过滤再匹配标题
7. **最小化窗口** — `screen_snapshot` 默认 `include_minimized=true`（会包含最小化窗口）
   - 最小化窗口的 bbox 是 `(-32000, -32000, ...)`（屏幕外无效坐标），不能直接用于坐标计算
   - 操作最小化窗口前先用 `focus_window` 恢复

### window_token vs hwnd 衔接（重要！避免混淆）

项目中存在两种窗口标识符，**用途完全不同，不要混用**：

| 标识符 | 类型 | 用于哪些端点 | 来源 |
|--------|------|-------------|------|
| `hwnd` | int | `execute_action` / `focus_window` / `screen_ocr` / `capture_screen` / `screen_snapshot` / `screen_wait_for` / `screen_analyze` / `batch_actions` / `desktop_transaction` / `screen_semantic_action` / `scroll_capture` | `list_windows` 返回的 `hwnd` 字段，或 `screen_window_resolve` 响应 `matches[].canonical_hwnd` |
| `window_token` | dict `{canonical_hwnd, pid, process_create_time, process_name, title}` | 仅 4 个窗口生命周期端点：`screen_window_minimize` / `screen_window_restore` / `screen_window_raise` / `screen_window_close` | `screen_window_resolve` 响应 `matches[].window_token` |

**衔接规则**：
1. **action 系列端点用 `hwnd`**：`execute_action(hwnd=12345, ...)`、`screen_ocr(mode="window", hwnd=12345)`、`focus_window(hwnd=12345)` 等
2. **window 生命周期端点用 `hwnd` + 可选 `window_token`**：`screen_window_minimize(hwnd=12345, window_token={...})`、`screen_window_close(hwnd=12345, window_token={...})`
   - `window_token` 是可选的，传了会做三层校验（hwnd 存在 + pid 匹配 + process_create_time 不变），防止进程重启后操作到错误窗口
   - 不传 `window_token` 时跳过校验，仅按 `hwnd` 操作（向后兼容）
3. **从 resolve 拿到 hwnd**：`screen_window_resolve(title="...")` 返回 `matches[].canonical_hwnd`，可作为 `hwnd` 传给 action 系列端点；同时返回 `matches[].window_token`，传给生命周期端点做校验

**推荐工作流**（窗口操作前先 resolve）：
```
1. screen_window_resolve(title="选项", process_name="qbittorrent.exe")
   -> matches[0] = {canonical_hwnd=12345, window_token={canonical_hwnd, pid, ...}, ...}
2. action 类操作：execute_action(hwnd=matches[0].canonical_hwnd, ...)
3. 生命周期操作：screen_window_close(hwnd=matches[0].canonical_hwnd, window_token=matches[0].window_token)
```

**为什么 canonical_hwnd 而不是 list_windows 的 hwnd**：
- UWP/WinUI 应用（如"设置"、"计算器"）存在 ApplicationFrameHost 外框 + 核心子窗口的多层 HWND
- `canonical_hwnd` 是 `resolve_canonical_window()` 规范化后的根 owner HWND，无论 agent 传入外框还是子窗口 hwnd，都返回同一个 canonical_hwnd，避免同族窗口混淆
- `list_windows` 返回的 hwnd 是直接枚举的，可能是子窗口；用 canonical_hwnd 更稳

## 标准操作流程

### 当前任务授权（副作用操作前）

第一次执行点击、输入、聚焦、UIA 动作等副作用操作前，先调用：

```text
screen_request_control(
  task_description="具体、可识别的任务说明",
  source="agent",
  mode="normal"  # 或 "watchdog"（看门狗模式，用户不在场时使用）
)
```

会话管理层（`server/screen/session/`）维护三档权限模式：

| 模式 | 权限 | 时间策略 | 降级 |
|------|------|---------|------|
| `NO_PERMISSION` | 只读端点放行，副作用端点 403 | 无 | — |
| `NORMAL`（普通执行） | 副作用放行，危险/CONFIRM 仍逐次确认 | 10min 无操作进告警(黄色)+20min 降级 | → `NO_PERMISSION` |
| `WATCHDOG`（看门狗） | 同 NORMAL，可选允许关机 | 不因空闲撤销；默认 10h 到期降级 | → `NORMAL` |

**用户确认窗**：agent 在请求时指定 `mode`，用户只能允许/拒绝，不能改模式。
- normal 模式：只有允许/拒绝按钮
- watchdog 模式：额外显示时长输入(1-999h，默认 10)和"允许关机"复选框(默认勾选)

**续期规则**：
- 危险/不可逆操作、`require_confirm=true` 和既有安全阻断仍逐次生效（watchdog 模式下 CONFIRM 仍弹窗=超时 deny）。
- 30 秒未响应默认取消；NORMAL 模式最后一次成功投递控制操作后 10min 进告警，30min 自动降级到 NO_PERMISSION。
- 只读、dry-run、blocked、cancelled、failed 或 delivery leaked 不续期。
- 任务完成或中断时调用 `screen_release_control` 主动收回，不依赖自动降级收尾。

### 标准操作流程（OCR 优先）

```
1. 桌面快照      → screen_snapshot(with_ocr=true)  # 一次获取窗口列表+OCR文本
2. 请求任务授权  → screen_request_control(task_description="...", source="agent")
3. 激活目标窗口  → focus_window(hwnd=xxx)  # 优先用 hwnd，避免标题歧义
4. 定位目标元素  → localagent_advanced_tool(tool="screen_ocr", params={mode:"window", hwnd:xxx, engine:"ocr"})
                   # screen_ocr 不在直连 MCP 白名单，必须经 advanced_tool 网关调用
                   # 从目标文字 bbox 中心计算窗口内坐标，再加 window_rect.left/top 得到屏幕坐标
                   # 网页优先 browser_action(action=click, target={css/text/...})，纯图标才用 vision_locate
5. 必要时预览位置 → preview_action(points=[{x,y,label}], mode="window", hwnd=xxx)
   - 危险操作、OCR 多候选或相邻控件很密时才预览；普通文字控件无需强制 VL
6. 执行操作      → execute_action(action="click", x:..., y:..., hwnd=xxx)  # 用确认后的屏幕坐标
                  或 batch_actions(actions=[...], hwnd=xxx)  # 多步操作
7. 验证结果      → screen_ocr / screen_snapshot / screen_wait_for 先做 OCR 验证
                  → 只有状态无法由文字判断时才用 understand_image 描述
8. 等待条件      → screen_wait_for(expected={"type":"ocr_contains","text":"完成"}, timeout=10)
                   # expected 为 dict 格式（与其他端点统一），type 可省略默认 ocr_contains
9. 重复4-8       → 直到任务完成
10. 主动收回授权 → screen_release_control()
```

**关键改进**：
- 第3步起优先用 `hwnd` 而非 `window_title`（避免同名窗口冲突）
- 第4步 `screen_ocr` 必须经 `localagent_advanced_tool` 网关调用（不在直连 MCP 白名单）；返回的 `window_rect` 直接用于坐标转换，无需再调 list_windows
- 第4步文字定位默认使用 OCR bbox；浏览器默认使用 DOM locator；`vision_locate` 降为纯图标兜底
- 第5步 preview_action 标记 L 形角标（点击前确认坐标，避免盲点误操作）
- 第6步仅在高风险、失败重试或状态难判断时传 `verify_prompt`，避免普通点击消耗 VL 配额
- 第7步优先 OCR 验证；`understand_image` 只做非坐标描述
- 第8步 `screen_wait_for.expected` 已改为 dict 格式（旧版 `condition_type`+`expected:str` 已废弃）

### VL 反馈闭环（execute_action 的 verify_prompt 参数）

`execute_action` 支持 opt-in 的 `verify_prompt` 参数。传入时服务端在操作后（无论成败）自动截图 + 调远程 VL，返回 <=200 字结构化画面描述。它只用于高风险操作、状态难由 OCR 判断、失败重试和长程关键检查点；普通文字点击优先 OCR 验证，避免每步调用远程 VL。`batch_actions` 不支持。

> 注：`screenshot_after` 参数已废弃——不再返回 base64 截图；如需操作后截图请用 `capture_screen(format="inline")`。

**verify_prompt 写作规范**：
1. **期望状态**：一句话描述操作成功后画面应呈现什么（如"复选框被勾选"、"弹窗已关闭"）
2. **异常排查**：列出 1-2 个本步骤最可能的阻塞异常（如"焦点被模态窗口抢占"、"坐标落到错误控件"）
3. **进度标记**：长程任务（>=3 步）标注当前进度（如"step 2/4: 选项卡应切到'下载'"）
4. **长度**：<=80 字，纯文本

**何时传 verify_prompt**：长程任务、上一步失败重试、复选框/标签页切换等需视觉验证的操作、涉及模态窗口的场景。
**何时不传**：trivial 操作、`batch_actions`（不支持）。

**响应字段**：
- `vl_description: str|null` — <=200 字描述（`【状态】...｜【观察】...｜【建议】...`），仅当传了 verify_prompt 且 VL 调用成功时非空
- `vl_skipped: bool|null` — True 表示跳过（config 关闭/截图失败/VL 不可用）
- `vl_skip_reason: str|null` — 跳过原因

**失败也反馈**（核心价值）：操作失败时仍截图 + 调 VL。例如坐标偏出目标控件时，VL 描述"点击落在'连接'标签上，'下载'标签未高亮"，agent 据此重试而非盲点。

**降级**：VL 不可用（限流冷却/未配置）时 `vl_skipped=true`，主操作正常返回，agent 可改用 `screen_analyze` 或 `understand_image` 手动验证。

### 点击-验证-重试循环（重要！解决点击精度问题）

即使 OCR/DOM/VL 给出坐标，点击仍可能因窗口移动、焦点抢占、控件重绘或相邻控件过密而落空。每次点击后必须验证，失败则重试，最多 3 次。

```
attempt = 0
while attempt < 3:
    attempt += 1
    1. 定位目标     -> 文字目标重新 screen_ocr；网页重新 DOM locator
                     纯图标且低成本路径失败时才重新 vision_locate
    2. 预览标记     -> preview_action(points=[{x, y, label=f"尝试{attempt}"}], mode="window", hwnd=xxx)
                     把图给用户看，或用 understand_image 问"红点是否在'XX'按钮上"
    3. 执行点击     -> execute_action(action="click", x, y, hwnd=xxx,
                                    verify_prompt="期望状态：XX标签应被选中（高亮），无错误弹窗")
    4. 检查结果：
       |- OCR/DOM 状态符合预期 -> 成功，退出循环
       |- 文字仍未变化或落在相邻控件 -> 重新截图和 OCR 后重试
       |- 焦点被抢占 -> focus_window(hwnd=xxx) 后重试
       |- 状态无法由文字判断 -> 用 understand_image/verify_prompt 描述一次
    5. 三次仍失败 -> 停止，向用户报告"3 次点击均未命中，请帮忙确认坐标"

# 不要：连续盲点 N 次不验证
# 不要：失败后用相同坐标重试（要重新截图并定位）
```

**关键原则**：
1. **每次重试都重新截图并定位** —— OCR 文字目标重新识别；网页重新查询 DOM
2. **多候选时增加结构约束** —— 如"导航栏第三个'设置'文字"，而不是立刻升级到 VL 坐标
3. **3 次失败必须停止** —— 不要无限重试，向用户报告并求助
4. **验证是核心，VL 不是核心** —— 能用 OCR/DOM 验证就不调用远程 VL
5. **VL 只处理语义状态** —— 文字无法表达选中、遮挡或视觉异常时再调用

### 何时不用循环

- **trivial 操作**（如点关闭按钮、按 Escape）
- **`batch_actions`**（不支持 verify_prompt，需要单独验证）
- **目标坐标已用 `preview_action` 给用户确认过**

### 快速判断画面状态（无需 VL）

当远程 VL 不可用或只需快速判断画面是否异常时：
```
screen_analyze(mode="fullscreen")  # 返回主色/亮度/异常告警，无 base64
```

### 旧流程（仍可用，但推荐新工具）

```
1. 获取窗口列表 -> list_windows
2. 截取目标窗口 -> capture_screen(mode="window", window_title="xxx")
3. 解析UI元素   -> understand_image  # 远程 VL（OmniParser 已移除）
4. 执行操作     -> execute_action(action="click", x:..., y:...)
5. 验证结果     -> 重复 2-4
```

## 定位优先级与工具选择

### OCR 与 VL 的职责（核心！必读）

**默认：OCR 负责文字识别和文字坐标；VL 负责图像描述、状态理解和无文字元素兜底。**
不再为普通文字按钮默认调用 `vision_locate`。远程 VL 昂贵、慢、受配额和网络影响，
只有 OCR/DOM/UIA 无法完成时才用于坐标定位。

**OCR 定位的边界**：
1. **适合直接定位**：按钮文字、菜单项、标签页、列表项、链接、输入框标签、分页文字。
2. **需要结构偏移**：复选框在文字左侧、图标与文字同一行、整行可点击但文字只占一部分。
3. **不适合 OCR**：纯图标、无文字画布、游戏内纹理按钮、严重遮挡/手写/极低对比度文字。
4. **浏览器优先 DOM**：网页有可访问 DOM 时，优先 `browser_action(action=click, target={css/text/...})` 的 selector/text locator；OCR 只用于 canvas、远程桌面、图片化网页或 DOM 与画面不一致时。

### 定位优先级

| 场景 | 首选 | 次选 | 最后兜底 |
|------|------|------|----------|
| 浏览器文字/表单 | DOM selector / visible text | OCR | VL locate |
| **桌面原生控件** | **UIA 语义层**（screen_accessibility_snapshot + screen_semantic_action） | OCR bbox | VL locate |
| 桌面文字控件（UIA 不可用） | OCR bbox | OCR + 图像描述确认结构 | VL locate |
| 纯图标/无文字元素 | VL 图像描述 | 固定结构猜测 | VL locate |
| 复杂画面状态判断 | OCR 文本状态 | `understand_image` 描述 | 人工确认 |

> **UIA 语义层**：Win32/WPF/WinUI/Forms 应用优先用 UIA 语义动作（invoke/select/toggle），比坐标点击稳定（不受窗口移动影响）且不依赖焦点。但 UIA 语义动作仍受当前任务授权与危险确认保护，第一次副作用操作前先调用 `screen_request_control`。详见 `docs/computer-use-reference.md`。

### VL 坐标归一化（关键约束）

Qwen3-VL 系列（含 Qwen2.5-VL / Qwen3-VL）**内部始终使用 0-1000 归一化坐标**——这是模型训练时的固定表示，不会变。但不同工具对 VL 返回值的处理不同：

| 工具 | VL 返回值处理 | 调用者拿到的坐标 | 适合做什么 |
|------|-------------|----------------|-----------|
| **`vision_locate`** | 自动反归一化 + 加窗口偏移 | **屏幕物理像素坐标**，可直接传 `execute_action` | OCR/DOM/UIA 无法定位时兜底 |
| **`understand_image`** | 不反归一化，原样返回 | **0-1000 归一化坐标**，需要手动反算 | 不要用来问坐标；用于非坐标问答（"复选框是否勾选"） |

> **OmniParser 已移除**：原 `parse_screen` 端点（YOLOv8 + Florence2 本地图标检测）已于 2026-07-31 删除（MCP 调用 0 次）。纯图标/无文字元素场景改用 `understand_image` 描述 + `vision_locate` 兜底。详见 `temp/sdd/omniparser-removal/spec.md`。

**反归一化公式**（`vision_locate` 内部自动完成）：
```
pixel_x = round(nx * image_width / 1000)
pixel_y = round(ny * image_height / 1000)
```

**实测结论**（图片 992x786，5 个已知标记）：
- **直接把 `understand_image` 的坐标当像素用**：Y 轴偏差随 Y 值增大而增大（y=100->偏27, y=686->偏177），**完全不可用**
- **反归一化后**：偏差在 +/-22 像素内（圆心识别误差），可接受
- **结论**：`understand_image` 主要用于描述，不要把其回答里的坐标直接当像素用；确需 VL 坐标时才调用 `vision_locate`

**切换 VL 模型后必做**：单独实测 `vision_locate` 的反归一化和窗口偏移逻辑。GPT-4o / Claude 等模型可能使用像素坐标或 0-1 归一化，需验证。

### understand_image vs vision_locate 选择

- **`understand_image`**：描述布局、状态、遮挡、弹窗、选中状态；这是默认 VL 用途。裁剪后携带原图 bbox 的坐标错配已修复，`bbox` 适合聚焦区域做**非坐标描述**（如"这个复选框是否已勾选"）。不要把其自然语言坐标回答当作可点击像素。
- **`vision_locate`**：无文字或 OCR/DOM 无法定位时的坐标兜底，不是普通文字定位默认项。返回屏幕物理像素坐标（窗口模式已加偏移），可直接传给 `execute_action`。精度约 +/-20 像素。不接受 `hwnd`，只能用 `window_title` + `process_name`。
- **`execute_action(verify_prompt=...)`**：仅在高风险或状态难用 OCR 验证时开启；普通文字导航可用 OCR 验证，节省配额。

### 推荐工作流

#### 桌面文字控件

```
1. list_windows 获取 hwnd + 窗口 bbox
2. screen_ocr(mode="window", hwnd=..., engine="ocr")
   -> details[].box 是窗口截图内的物理像素坐标
3. 按目标文字筛选；多候选时结合上下文、行列位置、置信度选择
4. OCR 中心：cx = round(sum(x)/4), cy = round(sum(y)/4)
5. 屏幕坐标：screen_x = window_left + cx, screen_y = window_top + cy
6. 文字就是点击目标 -> 直接 execute_action(click)
   文字旁有复选框/图标 -> 按相邻结构偏移，必要时用 understand_image 描述布局
7. 操作后优先用 OCR 文本/高亮状态验证；画面语义复杂时再用 VL 描述
```

也可以用 `capture_screen(mode="window", format="path")` + `ocr_path(path)`；坐标转换规则相同。
全屏/双屏定位时 bbox 是拼接截图坐标，必须再加虚拟屏原点；普通任务优先窗口模式，避免遗漏负坐标显示器偏移。

候选选择规则：先做 Unicode/大小写/空白归一化，再按"完全匹配 > 包含匹配 > 相似匹配"筛选；同名候选结合所在行列、邻近文字和置信度消歧。不要只取第一个结果。

点击后重新截图/OCR，不复用旧 bbox。连续三次失败即停止并报告，禁止盲点。

#### 浏览器控件

1. 优先 `browser_action(action=click, target={css/text/...})`，由 Playwright DOM locator 定位。
2. DOM 不可用（canvas、图片、远程桌面）时再截图 + OCR。
3. 只有纯图标、OCR 漏识别或候选语义无法消歧时，才使用 `vision_locate`。

## 安全机制

### 焦点安全

**背景**：防止 ChatGPT/Codex/Trae/Cursor 等受保护进程抢占前台时，键盘输入泄漏到错误窗口。

#### 键盘动作焦点强校验

`type` / `type_immediate` / `hotkey` 三类键盘动作执行前必走 `verify_focus_for_input`：

| 状态码 | 触发条件 | 处理 |
|--------|----------|------|
| `FOCUS_LEAK_PREVENTED` | 前台是受保护进程（ChatGPT/Codex/Trae/Cursor 等默认 agent 宿主）且目标不是它 | **零按键发送**，即使 `allow_unfocused_input=true` 也不放行 |
| `FOCUS_NOT_VERIFIED` | 未指定 `target_hwnd`/`window_title` 且 `allow_unfocused_input=false` | **零按键发送**，agent 必须显式传 hwnd 或承认风险 |
| `focus_verified` | 目标已指定且前台属于目标 family（canonical 同族） | 放行 |
| `unfocused_input_allowed` | 未匹配但 agent 显式传 `allow_unfocused_input=true` | 放行（agent 承担风险） |

**关键约束**：
1. `allow_unfocused_input=true` **不能绕过** `FOCUS_LEAK_PREVENTED`——前台是 ChatGPT 时一律拒绝
2. 默认 `allow_unfocused_input_default=false`（config 可改）；agent 必须显式 `allow_unfocused_input=true` 才能放行未验证焦点的盲输入
3. `click`/`scroll`/`drag` 等坐标动作不走焦点校验（focus_check_status=skipped）

#### canonical window token

UWP/WinUI 应用（如"设置"、"计算器"）存在 ApplicationFrameHost 外框 + 核心子窗口的多层 HWND，标题相同但 HWND 不同。`resolve_canonical_window(hwnd)` 通过 `GetAncestor(GA_ROOTOWNER)` 解析同族 HWND 集合，agent 传入任一同族 HWND 都能正确匹配。响应中的 `canonical_window` 字段返回规范化后的 token（`canonical_hwnd/title/pid/process_name/is_uwp_host`）。

#### 分层状态（transport / delivery / postcondition）

| 字段 | 取值 | 含义 |
|------|------|------|
| `transport_status` | `sent` / `not_sent` / `error` | 键鼠事件是否真正发送（FOCUS_LEAK_PREVENTED 时 not_sent） |
| `delivery_status` | `delivered` / `leaked` / `unknown` / `skipped` | 操作后焦点是否仍在目标族（leaked=漂移到非目标） |
| `postcondition_status` | `verified` / `failed` / `error` / `executed_unverified` / `not_checked` | 声明式 expected OCR 后验结果 |

| `status` 取值 | 含义 |
|---------------|------|
| `executed` | 动作成功 + 声明式后验通过（`postcondition_status=verified`） |
| `executed_unverified` | 动作成功但未声明 `expected`（`postcondition_status=executed_unverified`） |
| `postcondition_failed` | 动作成功（`transport_status=sent`）但声明式 OCR 后验失败 |
| `blocked` | 动作未执行（`transport_status=not_sent`）——焦点泄漏/STALE_COORDINATES/危险关键词/参数缺失等 |
| `dry_run` | dry_run 模式下通过所有前置校验但未投递输入事件（`transport_status=not_sent`） |
| `confirmed` / `cancelled` | `require_confirm=true` 时用户确认/取消 |
| `emergency_stopped` | 紧急停止激活中 |

**重要语义**：
- 未声明 `expected` 时 `status="executed_unverified"`（不再伪称 `executed`）
- `expected={"type":"ocr_contains","text":"完成"}` 时动作后自动 OCR，找到->`verified`/`executed`；未找到->`failed`/`postcondition_failed`
- `expected={"type":"ocr_not_contains","text":"错误"}` 用于验证错误弹窗未出现
- `transport_status=sent` + `delivery_status=delivered` + `postcondition_status=not_checked` 不再被包装成 error——动作本身成功，只是没做后验

### dry_run 模式（预演不投递）

`ActionRequest` / `BatchActionsRequest` / `DesktopTransactionRequest` 支持 `dry_run: bool = False`。dry_run=true 时：
- 通过所有前置校验（focus / snapshot 新鲜度 / 坐标范围 / 危险关键词 / 焦点校验）
- **不投递任何输入事件**（`transport_status=not_sent`）
- 返回 `status=dry_run` + 诊断字段：`would_focus` / `would_send` / `would_activate` / `target_match`

**用途**：验证危险操作安全性、受保护窗口焦点漂移测试、多步事务预演。

```python
# 危险操作前先 dry_run
execute_action(action="click", x=880, y=495, hwnd=xxx, dry_run=True)
# 返回 status=dry_run, would_focus=True, would_send=True -> 安全可执行
execute_action(action="click", x=880, y=495, hwnd=xxx, dry_run=False)  # 真实执行
```

### STALE_COORDINATES：坐标绑定 snapshot_id

`capture_screen` 返回 `snapshot_id` 和 `window_rect`。`execute_action` / `batch_actions` 的坐标动作可传 `snapshot_id`，服务端校验窗口几何是否变化：

- 窗口移动/缩放后旧坐标失效 -> `STALE_COORDINATES` + `status=blocked` + `transport_status=not_sent`
- snapshot 超过 5 分钟 TTL -> `STALE_COORDINATES`
- snapshot_id 不存在 -> `STALE_COORDINATES`

**正确工作流**：
```
1. capture_screen -> 拿到 snapshot_id 和 window_rect
2. OCR/VL 定位坐标
3. execute_action(snapshot_id=..., x=..., y=...)  # 校验窗口未移动
4. 若返回 STALE_COORDINATES -> 重新截图定位（不要重试用旧坐标）
```

### 接管确认与当前任务授权

`screen_request_control` 是主动入口，受用户可见 GUI 介导，不会自行授权。弹窗展示任务说明、安全边界、5 分钟空闲撤销规则，以及默认未勾选的任务授权复选框。

当前任务授权覆盖 `execute_action`、`focus_window`、`batch_actions`、`desktop_transaction`、`screen_semantic_action` 等普通副作用控制入口。授权期间：

- 普通操作跳过重复接管和默认逐动作确认。
- 危险关键词、窗口关闭、`require_confirm=true` 仍弹逐动作确认；block 类危险操作仍拒绝。
- 焦点保护、窗口边界、STALE_COORDINATES、紧急停止、后验检查不受影响。
- 成功且未泄漏的控制投递才把 300 秒截止时间向后延长。

未主动请求授权时，副作用端点仍通过 `takeover_confirm` 做被动兜底。GUI 不可用、用户拒绝或 30 秒超时都返回 cancelled，禁止重复请求向用户施压。`takeover_confirm_enabled=false` 会关闭被动兜底（不推荐），但不会自动创建当前任务授权。

> **`confirm_start` 与 `screen_request_control` 的关系**：`confirm_start`（`POST /screen/confirm/start`）是早期版本的接管确认端点，仅弹简单确认窗（task_description + hotkey_hint），无任务授权复选框、无 5 分钟空闲撤销、无 source 字段。**已被 `screen_request_control` 完全取代**——agent 应统一用 `screen_request_control`，不要再调 `confirm_start`。`confirm_start` 端点保留仅为向后兼容，未来可能删除。

### 审批机制补充说明

#### 1. hide_overlay 副作用历史陷阱（已修复，保留说明避免回归）

**历史 bug**：`gui_client.hide_overlay()` 曾在内部调用 `release_task_control("overlay_hidden")`，导致 agent 在持久授权期间调任何截图端点（`capture_screen`/`screen_ocr`/`screen_snapshot`/`screen_wait_for`/`screen_analyze`，这些端点截图前会临时隐藏覆盖层）后，任务授权意外丢失。

**当前修复**（2026-08）：`hide_overlay()` 不再调用 `release_task_control`，仅做视觉隐藏。任务授权的撤销只发生在以下显式场景：
- `screen_release_control` 端点（agent 主动收回）
- 紧急停止触发（`EmergencyStopManager._trigger` 显式调 `release_task_control("emergency_stop")`）
- 5 分钟空闲超时（`ControlGrantManager.expire_if_idle` 内部撤销）

**回归检测**：截图后调 `gui_client.is_persistent_mode()` 应返回 True；若返回 False 说明 hide_overlay 副作用回归。

#### 2. 紧急停止覆盖面

紧急停止（`Ctrl+`` `）触发后 10 秒冷却期内，以下端点检查 `emergency.can_operate()` 并返回 `status=emergency_stopped`：
- `execute_action` / `batch_actions` / `desktop_transaction` / `screen_semantic_action`
- `screen_wait_for` / `screen_analyze` / `scroll_capture`

**不检查紧急停止的端点**（只读/查询类，应保持可用）：
- `list_windows` / `screen_snapshot` / `screen_app_list` / `screen_window_resolve`
- `capture_screen` / `screen_ocr` / `screen_accessibility_snapshot`
- `focus_window`（仅激活窗口，不发送键鼠事件）
- 窗口生命周期 `screen_window_minimize`/`restore`/`raise`/`close`

#### 3. require_confirm 短路顺序（execute_action 内部）

`execute_action` 的确认流程按以下顺序短路（先到先决定）：

```
1. emergency.can_operate()=False  →  status=emergency_stopped（最高优先级）
2. _validate_action_params 失败    →  status=blocked
3. 窗口范围检查失败                →  status=blocked
4. STALE_COORDINATES               →  status=blocked
5. danger_level=block              →  status=blocked（危险关键词拦截）
6. _ensure_takeover_approved 失败  →  status=cancelled（接管确认未通过）
7. auto_skip（坐标缓存命中 + danger=safe + 非 explicit_confirm）→ 跳过确认
8. danger_level=confirm            →  强制 require_confirm=True（覆盖任务授权）
9. _task_authorization_is_active() →  require_confirm=False（任务授权短路）
10. config require_confirm_by_default=True → 弹确认窗
```

**关键点**：
- `danger_level=block` 永远拒绝，无法被任务授权绕过
- `danger_level=confirm`（如 `delete`/`format` 等危险关键词）永远弹确认窗，任务授权也不绕过
- `auto_skip` 只在 `danger_level=safe` 时生效，且 agent 显式 `require_confirm=True` 可强制确认
- 任务授权只短路"普通"操作（`danger_level=safe` 且不在 auto_skip 缓存）

#### 4. 任务授权续期条件

任务授权的 5 分钟空闲倒计时**仅在控制操作成功投递且焦点未泄漏时**才向后延长（`_touch_task_authorization_after_delivery`）：

```python
if not success or dry_run or delivery_status == "leaked":
    return False  # 不续期
gui_client.touch_input_time()  # 续期
```

**不续期的场景**：
- 操作失败（`success=False`）
- dry_run 模式（`dry_run=True`，未实际投递）
- 焦点泄漏（`delivery_status="leaked"`，操作后焦点漂移到非目标窗口）
- 只读/查询端点调用（不触发续期逻辑）
- `blocked` / `cancelled` / `emergency_stopped` 状态

**续期的场景**：
- `execute_action` / `batch_actions` / `desktop_transaction` / `screen_semantic_action` 中动作成功投递（`transport_status=sent`）且焦点仍在目标族（`delivery_status=delivered` 或 `unknown`）

#### 5. 受控托管模式（API 兼容名：mode="watchdog"）

**场景**：用户离开前让 agent 持续监控/操作（如睡前盯着任务跑完）。用户不在场，无法响应弹窗审批。对外概念统一称为“受控托管模式”；`watchdog` 仅作为现有 API 的兼容参数名保留。

**开启方式**：
- agent 调 `screen_request_control(mode="watchdog", task_description="...", source="agent")`
- GUI 面板"请求任务授权"按钮 → 勾选"看门狗模式"复选框

**受控托管模式 vs normal 模式差异**：

| 维度 | normal 模式 | watchdog 模式 |
|------|------------|---------------|
| idle_timeout | 5 分钟空闲撤销 | **不生效**（agent 合理等待不被判定为空闲） |
| takeover_confirm | 顶栏未显示时弹窗 | **禁用**（用户不在场，弹窗会阻塞） |
| 硬上限 | 无 | **10h 到期撤销**（config `takeover_watchdog_max_duration_seconds` 可调，默认 36000s） |
| require_confirm | safe 短路，CONFIRM 弹窗 | **不变**（safe 短路，CONFIRM 仍弹窗=超时 deny） |
| DANGER_KEYWORDS_BLOCK | 拦截 | **不变**（永远拦截"关机/重启/格式化"等） |
| EmergencyStop | Ctrl+` 撤销+冷却 | **不变**（用户随时可中断） |

**撤销条件**（三重保障）：
1. 用户主动撤销（GUI 按钮 / `screen_release_control` 端点）
2. 紧急停止触发（Ctrl+`）
3. 硬上限到期（`max_duration_exceeded`，默认 10h）

**关键约束**：
- **AFK 时必须 fail-closed**：用户不在场且遇到无法通过已授权、可观测、安全路径解决的问题时，立即停止并报告；不得绕过确认、放宽安全策略、切换未授权工具/通道、修改配置、无限重试或用替代动作“碰运气”。不确定是否安全时，一律按无法解决处理并放弃。
- watchdog 模式**不豁免 CONFIRM 级别操作**（删除/支付/卸载等仍弹窗，用户不在场=超时 deny=操作被阻止）
- watchdog 模式**不豁免 DANGER_KEYWORDS_BLOCK**（关机/重启/格式化等永远拦截）
- watchdog 模式**不影响 command_guard**（exec_python/exec_cmd 的命令审批逻辑不变，agent 不能通过 exec_python 直接调 shutdown）
- 关机走专门端点（`POST /auto_shutdown/trigger`，agent 自己跑 loop + 调端点，**不走 computer use**）

**health 上报**：
- `takeover_persistent.mode` = "normal" | "watchdog"
- `takeover_persistent.max_duration_seconds` = watchdog 模式硬上限（normal 模式为 null）
- `takeover_persistent.remaining_seconds` = watchdog 模式下表示"距硬上限剩余秒数"（非 idle 剩余）

### 紧急停止

- 快捷键：`Ctrl + ``（反引号，Esc下方）
- 触发后10秒内禁止Agent操作
- 自动隐藏操作提示

### 配置项（config.toml `[screen]` 段）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `focus_protection_enabled` | `true` | 焦点安全总开关（关闭后等同旧版行为，不推荐） |
| `protected_processes` | `["ChatGPT.exe", "Codex.exe", "trae.exe", "Trae.exe", "cursor.exe", "Cursor.exe"]` | 受保护进程列表（留空用默认） |
| `allow_unfocused_input_default` | `false` | 键盘动作未验证焦点时是否放行的全局默认 |
| `takeover_confirm_enabled` | `true` | 接管确认总开关。顶栏未显示时 agent 调键鼠端点前弹窗询问用户 |
| `takeover_confirm_timeout_seconds` | `30` | 接管确认弹窗超时秒数 |
| `takeover_timeout_action` | `"cancel"` | 接管弹窗超时行为；默认 fail-closed，兼容旧配置 `proceed` |
| `takeover_persistent_enabled` | `true` | 当前任务授权功能总开关（旧配置名保留兼容） |
| `takeover_idle_timeout_seconds` | `300` | 最后一次成功控制投递后的空闲撤销秒数 |
| `takeover_watchdog_max_duration_seconds` | `36000` | 看门狗模式硬上限秒数（10h），到期自动撤销授权 |

`/health` 的 `screen.takeover_persistent` 兼容字段返回结构化当前任务授权状态，包括 `configured`、`active`、`mode`（"normal"/"watchdog"）、`task_description`、`source`、`last_activity_at`、`expires_at`、`remaining_seconds`（watchdog 模式下为距硬上限剩余）、`max_duration_seconds` 和 `revoked_reason`。


## 详细参考

> UIA 语义层、桌面事务、窗口生命周期、截图方案决策树、DPI 缩放、API 速查、注意事项、管理员权限、历史经验等详细内容已移至 [docs/computer-use-reference.md](../../docs/computer-use-reference.md)。
