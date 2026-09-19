# Computer Use 详细参考

> 本文件从 `.agents/skills/computer_use/SKILL.md` 拆分而来，包含 UIA 语义层、桌面事务、窗口生命周期、截图方案决策树、DPI 缩放、API 速查、注意事项等详细参考内容。核心操作流程见 [computer_use/SKILL.md](../.agents/skills/computer_use/SKILL.md)。

## 当前任务授权

### 核心接口

| 接口 | operation_id | 用途 |
|------|--------------|------|
| `POST /screen/control/request` | `screen_request_control` | 弹出用户确认窗，请求本次接管及可选的当前任务授权（agent 在请求时指定 `mode=normal` 或 `mode=watchdog`，用户只能允许/拒绝不能改模式） |
| `POST /screen/control/release` | `screen_release_control` | 主动收回当前任务授权，隐藏授权覆盖层并停止 worker |

请求示例：

```json
{
  "task_description": "整理桌面窗口并归类文件",
  "source": "agent",
  "mode": "normal"
}
```

确认窗中：normal 模式只有允许/拒绝按钮；watchdog 模式额外显示时长输入（1-999h，默认 10）和"允许关机"复选框（默认勾选）。授权不会写盘，也不会跨后端重启保留。

> **GUI 用户开关（2026-09-05 起）**：用户也可以不经 agent 发起，直接在 client 的 **Monitoring 面板**（"电脑操作许可"复选框）或**系统工具面板**（"电脑操作许可"卡）打开/关闭授权——打开同样要经过后端确认窗（安全闸门保留），请求固定 `source="gui_panel"`。开关勾选态以 `/health.screen.takeover_persistent` 为唯一真源轮询回写，agent 调 `screen_release_control`、idle 自动撤销或 watchdog 到期降级后开关自动回弹。

### 三档权限模式

会话管理层（`server/screen/session/`）维护全局 SessionManager 单例，状态机分三档：

| 模式 | 权限范围 | 时间策略 | 降级目标 |
|------|---------|---------|---------|
| `NO_PERMISSION` | 只读端点放行；所有副作用端点 403 | 无 | — |
| `NORMAL` | 副作用端点放行；危险/CONFIRM 仍逐次确认 | `idle_warning_seconds`(默认 600s) 无操作进告警（黄色）；再过 `idle_grace_seconds`(默认 1200s) 降级 | `NO_PERMISSION` |
| `WATCHDOG` | 同 NORMAL；可选允许关机（`shutdown_permitted`） | 不因空闲撤销；`max_duration_seconds`(默认 36000s=10h) 到期降级 | `NORMAL`（不直接释放） |

**降级链**：`WATCHDOG → NORMAL → NO_PERMISSION`。每次降级发布 `transition` 事件，OverlayClient 据此刷新 UI 颜色和倒计时格式。**反向升级**需用户重新授权（agent 调 `screen_request_control` 触发新弹窗）。

### 状态机与续期

- 所有副作用端点（`execute_action` / `focus_window` / `batch_actions` / `desktop_transaction` / `screen_semantic_action` / `scroll_capture` / `app_launch` / `window_minimize` / `window_restore` / `window_raise` / `window_close`）在 NORMAL/WATCHDOG 模式下免重复确认。
- 危险/不可逆操作、窗口关闭和 `require_confirm=true` 仍逐次确认；block 类危险操作仍拒绝。
- WATCHDOG 模式下 CONFIRM 级别操作仍弹窗（用户不在场=超时 deny=操作被阻止）。
- 只有成功且 `delivery_status != leaked` 的控制投递调用 `extend()` 重置 idle 计时。
- 只读、dry-run、blocked、cancelled、failed 与泄漏投递不续期。
- release、task closure、紧急停止、shutdown 和 restart 都清除授权。

`/health` 的兼容路径 `screen.takeover_persistent` 返回 `configured`、`active`、`mode`、`phase`、`task_description`、`source`、`granted_at`、`last_activity_at`、`expires_at`、`remaining_seconds`、`max_duration_seconds`、`shutdown_permitted`、`revoked_reason` 等结构化状态。旧 `persistent` 命名只为兼容，用户可见概念统一为"当前任务授权"。

30 秒确认响应超时与 NORMAL 模式 30 分钟 idle 降级是两套独立计时器。确认超时默认 `cancel`，不会自行授权或执行操作。

### 受控托管模式（API 兼容名：watchdog mode）

**场景**：用户离开前让 agent 持续监控/操作（如睡前盯着任务跑完）。用户不在场，无法响应弹窗审批。详细场景说明见 [.agents/skills/computer_use/SKILL.md §5 受控托管模式](../.agents/skills/computer_use/SKILL.md)。现有 API 的 `mode="watchdog"` 参数继续保留，以兼容已部署调用方。

**开启方式**：

- agent 调 `screen_request_control(mode="watchdog", task_description="...", source="agent", max_duration_hours=10)`
- GUI 面板"请求任务授权"按钮 → 勾选"看门狗模式"复选框

**watchdog 模式 vs normal 模式差异**：

| 维度 | normal 模式 | watchdog 模式 |
|------|------------|---------------|
| idle 降级 | 10min 告警 + 20min 降级到 NO_PERMISSION | **不生效**（agent 合理等待不被判定为空闲） |
| takeover_confirm | 顶栏未显示时弹窗 | **禁用**（用户不在场，弹窗会阻塞） |
| 硬上限 | 无 | **默认 10h 到期降级到 NORMAL**（config `takeover_watchdog_default_hours` 可调，1-999h 范围） |
| require_confirm | safe 短路，CONFIRM 弹窗 | **不变**（safe 短路，CONFIRM 仍弹窗=超时 deny） |
| DANGER_KEYWORDS_BLOCK | 拦截 | **不变**（永远拦截"关机/重启/格式化"等） |
| EmergencyStop | Ctrl+` 撤销+冷却 | **不变**（用户随时可中断） |
| shutdown_permitted | 始终 False | 用户在授权弹窗勾选（默认勾选）；不勾选时 `/auto_shutdown/trigger` 返回 403 |

**撤销条件**（三重保障）：

1. 用户主动撤销（GUI 按钮 / `screen_release_control` 端点），reason=`manual`
2. 紧急停止触发（Ctrl+`），reason=`emergency_stop`
3. 硬上限到期，reason=`max_duration_exceeded`（默认 10h，config 可调）——到期降级到 NORMAL 而非直接释放

**关键约束**（spec §6 Anti-Cheat）：

- **AFK 时必须 fail-closed**：用户不在场且遇到无法通过已授权、可观测、安全路径解决的问题时，立即停止并报告；不得绕过确认、放宽安全策略、切换未授权工具/通道、修改配置、无限重试或用替代动作"碰运气"。不确定是否安全时，一律按无法解决处理并放弃。
- watchdog 模式**不豁免 CONFIRM 级别操作**（删除/支付/卸载等仍弹窗，用户不在场=超时 deny=操作被阻止）
- watchdog 模式**不豁免 DANGER_KEYWORDS_BLOCK**（关机/重启/格式化等永远拦截）
- watchdog 模式**不影响 command_guard**（exec_python/exec_cmd 的命令审批逻辑不变，agent 不能通过 exec_python 直接调 shutdown）
- 关机走专门端点（`POST /auto_shutdown/trigger`，agent 自己跑 loop + 调端点，**不走 computer use**）；watchdog 模式未勾选"允许关机"时该端点返回 403
- agent 不能自行修改 config 调长 max_duration_seconds（config 修改需用户手动）
- agent 不能自行撤销 watchdog 授权后重新开启（撤销需用户主动或紧急停止）

**health 上报**（`takeover_persistent` 兼容字段下）：

- `mode` = "no_permission" | "normal" | "watchdog"
- `phase` = "inactive" | "normal"（浅蓝正常） | "warning"（黄色告警） | "watchdog"（全程浅蓝）
- `max_duration_seconds` = watchdog 模式硬上限（normal/no_permission 模式为 null）
- `remaining_seconds` = watchdog 模式下表示"距硬上限剩余秒数"；normal 模式下表示"距 idle 降级剩余"（含告警阶段）；no_permission 为 0
- `shutdown_permitted` = watchdog 模式下用户是否勾选允许关机（其他模式为 false）
- `revoked_reason` = `max_duration_exceeded` 时表示看门狗硬上限到期降级

**已知限制**：SessionManager 是内存态，后端重启即丢失授权（包括 watchdog），用户需重新开启。

## UIA 语义层

Windows UI Automation 语义层，让 agent 像 Browser Use 操作 DOM 一样操作桌面应用：拿 accessibility tree 快照 -> 用 element_id 调语义动作，**不再依赖坐标点击**。

### 核心接口

| 接口 | operation_id | 用途 |
|------|--------------|------|
| `POST /screen/accessibility/snapshot` | `screen_accessibility_snapshot` | 拿 UIA accessibility tree 快照（role/name/value/checked/bounds/element_id + **flags/actions 能力标志**） |
| `POST /screen/accessibility/action` | `screen_semantic_action` | 对 element_id 执行语义动作（invoke/select/toggle/set_value/expand/collapse/scroll） |

### 元素能力标志（ZCode 对齐，2026-09-05）

snapshot 的每个元素带 `flags`（空格连接短串）和 `actions`（语义动作列表），一次观察即知"能做什么"，不用按 role 猜 pattern：

| flag | 含义 | 对应 action |
|------|------|------------|
| `pressable` | 支持 InvokePattern | `invoke` |
| `editable` | ValuePattern 非只读 | `set_value` |
| `toggleable` | 支持 TogglePattern | `toggle` |
| `selectable` | 支持 SelectionItemPattern | `select` |
| `expandable` | 支持 ExpandCollapsePattern | `expand` / `collapse` |
| `focused` | 当前持有键盘焦点 | — |

### 坐标点击 UIA 融合（strategy 参数，默认 auto）

`execute_action` / `batch_actions` 的 `action="click"` 支持 `strategy` 参数（ZCode computer-use 同款机制）：

- **`auto`（默认）**：先 UIA hit-test（`ControlFromPoint` + 祖先链 ≤5 层找最近可点击元素）；命中 → 直接 UIA invoke（**后台执行、不抢焦点、零键鼠事件**），响应 `transport_status="sent_uia_invoke"`；未命中 → 自动回退原始键鼠（`transport_status="sent"`）
- **`event`**：强制原始键鼠（旧行为回归口）
- **`uia_only`**：必须命中 UIA 可点击元素，否则 `blocked` 不回退（防 agent 在自绘界面上误用真实键鼠）

关键行为：融合命中时**跳过窗口激活**（不抢焦点是核心收益）；danger/takeover/confirm 等安全检查不受影响；`expected`/`verify_prompt` 后验照走；融合路径任何异常自动回退原始键鼠（auto 下绝不因融合而失败）。仅 `click` 参与融合——double_click/right_click/scroll/drag 始终走原始键鼠。总闸：`config.toml [screen] coordinate_uia_fusion = false` 时全部走原始键鼠。

**选型建议**：标准 Win32/Qt/WinUI 应用直接 `strategy=auto`（默认）即可获得后台点击；DirectX 游戏/自绘 UI 会自然落入未命中分支回退原始键鼠，无需干预。

### 语义动作 vs 坐标动作

| 维度 | UIA 语义动作 | 坐标点击（execute_action） |
|------|-------------|---------------------------|
| 稳定性 | 高（RuntimeId BFS 重新定位，无坐标漂移） | 中（窗口移动后 STALE_COORDINATES） |
| 焦点依赖 | 无（UIA 直接 invoke） | 强（键盘动作必须焦点校验） |
| 适用场景 | 原生 Win32/WPF/WinUI/Forms 控件 | DirectX 游戏/Electron/无 UIA 支持的应用 |
| 失败码 | STALE_ELEMENT / UIA_NOT_AVAILABLE | FOCUS_LEAK_PREVENTED / STALE_COORDINATES |

### element_id 格式

`{canonical_hwnd}:{snapshot_id[:8]}:{element_index}` — 绑定窗口 + 快照版本。element_id 失效后返回 `STALE_ELEMENT`，必须重新 snapshot 拿新 element_id。

### 关键约束

- **UIA 语义优先**：能用 `screen_accessibility_snapshot` + `screen_semantic_action` 就别用坐标点击——invoke/select/toggle 比 SendInput 稳定且无焦点依赖
- **OCR 是 UIA 不可用时的 fallback**：UIA 友好应用（记事本/Win32 表单/系统设置）必须先 `screen_accessibility_snapshot` 拿 element_id；OCR 仅用于 `UIA_NOT_AVAILABLE` 或无语义控件的场景。定位优先级：UIA（首选）> browser DOM（网页）> OCR bbox（UIA 不可用时的 fallback）> vision_locate（纯图标兜底）
- **UIA_NOT_AVAILABLE**：`uiautomation` 库不可用（非 Windows 或依赖缺失）时回退 OCR/视觉定位，不要假定 UIA 一定可用（`/health` 的 `screen.uia` 字段反映可用性）
- **element_id 不等于 RuntimeId**：element_id 是快照内编号，语义动作通过 RuntimeId BFS 重新定位元素（不依赖 element_index 持久有效）
- **UIA 不支持 Electron/Chromium**：浏览器、VS Code、Discord 等 Electron 应用 UIA 树不完整，应走浏览器 DOM 或 OCR/视觉定位

### uia_value_equals 内建后验

`screen_semantic_action` 支持 `expected={"type":"uia_value_equals","text":"..."}`，在 `set_value` 动作成功后立即用 `ValuePattern.Value` 读回比对，无需 agent 再调 snapshot：

| 后验结果 | `success` | `status` | `postcondition_status` |
|---------|-----------|----------|------------------------|
| Value 匹配 expected.text | `True` | `executed` | `verified` |
| Value 不匹配 | `True` | `postcondition_failed` | `failed` |
| 元素不支持 ValuePattern | `True` | `executed_unverified` | `error` |
| 未声明 expected | `True` | `executed_unverified` | `executed_unverified` |

**关键约束**：仅 `action="set_value"` 时有效；其他 action 传该 expected 会被忽略；不触发 OCR 截图。

```python
# UIA set_value + 内建后验：无需 agent 再调 snapshot
POST /screen/uia/action
{
  "element_id": "12345:abcdef12:0",
  "snapshot_id": "snap_xxx",
  "action": "set_value",
  "value": "LocalAgent 中文",
  "expected": {"type": "uia_value_equals", "text": "LocalAgent 中文"}
}
# -> status="executed", postcondition_status="verified"
```

### UIA 语义层使用前提

UIA 语义层与键鼠操作共用同一套安全机制。`screen_semantic_action` 的 invoke/select/toggle 等动作**不经过 SendInput**，但仍然受 `takeover_confirm` 保护——顶栏 overlay 未显示时会弹窗确认。使用前应：

1. **管理员权限启动后端**（`start.bat` 自动 UAC 提权），否则 `focus_window` 无法激活窗口
2. **先请求当前任务授权**（`screen_request_control`），由用户决定仅允许本次或勾选任务授权
3. **OCR 首次调用慢**（模型冷启动 15s+），预热后正常

### UIA vs 键鼠对比

| 维度 | UIA 语义动作 | 键鼠坐标点击 |
|------|-------------|-------------|
| 定位方式 | element_id（UIA 树结构） | x/y 坐标（OCR bbox/VL） |
| 窗口移动 | 不受影响（RuntimeId BFS 重定位） | STALE_COORDINATES（需重新截图） |
| 焦点依赖 | invoke 直接 COM 调用，不依赖前台焦点 | 键盘动作必须焦点校验 |
| 适用应用 | Win32/WPF/WinUI/Forms 原生控件 | 全部应用（含 DirectX/Electron） |
| 安全机制 | 共用 takeover_confirm | 共用 takeover_confirm + 焦点校验 |

两者**都受安全机制保护**，不存在绕过安全机制的路径。选择依据是目标应用是否支持 UIA 树。

## 剪贴板 / zoom / 鼠标分段原语（ZCode 对齐，2026-09-05）

### 剪贴板读写

| 接口 | operation_id | 用途 |
|------|--------------|------|
| `GET /screen/clipboard` | `read_clipboard` | 读剪贴板文本（默认截断 2000 字符，`full=true` 取全文；`has_text=false` 表示无文本内容） |
| `POST /screen/clipboard` | `write_clipboard` | 写文本到剪贴板（body `{"text": "..."}`；响应只回前 100 字符摘要） |

典型用法：**长文本/特殊字符输入前置**——write_clipboard 后 `hotkey ctrl+v`，绕过逐字符 SendInput 的 IME/丢字问题；粘贴后 read_clipboard 验证内容。安全：读写都受会话权限保护（403）；write 过危险关键词 block 级拦截（confirm 级不拦——写入本身不执行任何内容）。

### zoom 局部放大（最近帧复用）

| 接口 | operation_id | 用途 |
|------|--------------|------|
| `POST /screen/zoom` | `screen_zoom` | 对最近一帧截图按 region 裁剪（可选 scale 放大 0.5-4x），**不重新截图** |
| `POST /screen/ocr`（带 `snapshot_id`/`region`） | `screen_ocr` | 对缓存帧做局部 OCR（小字更准），不重拍、不扰动覆盖层 |

- `snapshot_id` 来自 `capture_screen` 返回值（服务端缓存最近 3 帧，TTL 5min，LRU 淘汰）；过期/未知返回 404 提示重拍
- `region = [x0, y0, x1, y1]` 截图内像素坐标（与 capture 响应 width/height 同坐标系），越界自动 clamp，clamp 后 <4px 报 400
- `output=inline` 走 MCP ImageContent（多模态 LLM 直接看图，不进文本上下文）；`output=path` 返回临时 PNG 路径（可传 `ocr_file`）
- 适用场景：小字/图标/密集控件近距离辨认——capture → zoom 放大看细节，省一次全屏截图

### 鼠标分段原语

`execute_action` / `batch_actions` / `desktop_transaction` 新增三个动作：

| action | 参数 | 用途 |
|--------|------|------|
| `mouse_down` | x, y 必填；`button=left/right` | 按下不释放（与 mouse_up 配对：分段拖拽、长按、自绘滑块） |
| `mouse_up` | x, y 可选（提供则先移动再释放）；`button` | 释放按键（必须与 mouse_down 配对） |
| `mouse_move` | x, y 必填 | 移动指针不点击（hover 悬停、拖拽中间步进） |

三者均纳入 `_COORDINATE_ACTIONS`（窗口范围检查/STALE_COORDINATES/确认流程与 click 一致）。一体化拖拽仍用 `drag`；需要中途停顿或路径控制的拖拽才用 down/move/up 组合。

## 桌面事务与批量操作

### batch_actions 焦点漂移停止

`stop_on_focus_drift=true`（默认）时，任一步骤执行后 `delivery_status=leaked` 立即停止剩余动作。响应含 `aborted=true` 和 `aborted_reason` 说明漂移发生在第几步。`stop_on_focus_drift=false` 时即使焦点漂移也继续执行剩余动作（不推荐）。

### desktop_transaction vs batch_actions

| 维度 | `desktop_transaction` | `batch_actions` |
|------|----------------------|-----------------|
| `expected` | **必填**（事务级后验） | 可选（每步独立后验） |
| `rollback_policy` | 支持（none/auto） | 不支持 |
| `rollback_actions` | 支持（自定义回滚步骤） | 不支持 |
| `timeout` | 事务级总超时 | 无 |
| `dry_run` | 支持（全流程预演） | 支持 |
| 失败行为 | 失败时停止 + 可选自动回滚 | 失败时停止（stop_on_error） |
| 适用场景 | 多步原子操作（如"填表->提交->验证"需保证最终状态） | 多步顺序操作（无事务语义） |

### 事务状态语义

- `committed`：全部步骤成功 + 事务级 expected 后验通过
- `postcondition_failed`：全部步骤成功但事务级 expected 未满足
- `aborted`：某步失败（焦点漂移/STALE_COORDINATES/参数错误等）导致事务中断
- `rolled_back`：事务失败 + rollback_policy=auto + 全部回滚步骤成功
- `rollback_failed`：事务失败 + rollback_policy=auto + 部分回滚步骤失败
- `dry_run`：预演模式，未实际执行（顶层 `success=True` 表示预演通过前置校验）
- `emergency_stopped`：紧急停止激活

## 窗口生命周期 API

### 核心接口

| 接口 | operation_id | 用途 |
|------|--------------|------|
| `GET /screen/app/list` | `screen_app_list` | 应用列表（按 `{process_name}@{pid}` 聚合） |
| `POST /screen/app/launch` | `screen_app_launch` | 启动应用（DETACHED_PROCESS 独立子进程，shell=False 防注入） |
| `POST /screen/app/wait` | `screen_app_wait` | 等待窗口出现（轮询 + 超时，多匹配返回 `ambiguous`） |
| `POST /screen/window/resolve` | `screen_window_resolve` | 解析窗口为 `window_token`（多匹配返回所有候选 + WINDOW_AMBIGUOUS） |
| `POST /screen/window/minimize` | `screen_window_minimize` | 最小化窗口（返回 post_state） |
| `POST /screen/window/restore` | `screen_window_restore` | 恢复窗口（返回 post_state） |
| `POST /screen/window/raise` | `screen_window_raise` | 置顶窗口（返回 post_state） |
| `POST /screen/window/close` | `screen_window_close` | 关闭窗口（force=false 自动处理"是否保存"模态；force=true 杀进程会丢未保存数据） |

### window_token 三层校验

`window_token` 含 `{canonical_hwnd, pid, process_create_time, process_name, title}`，任一层失效返回 `WINDOW_TOKEN_INVALID`：

1. **hwnd 存在**（`IsWindow`）— 窗口是否已销毁
2. **pid 匹配**（`GetWindowThreadProcessId`）— hwnd 是否被复用
3. **process_create_time 不变**（`psutil.Process(pid).create_time()`）— 进程是否重启（pid 可被 OS 复用）

### window_token vs hwnd 衔接（避免混用）

项目中两种窗口标识符用途不同：

| 标识符 | 类型 | 用于端点 | 来源 |
|--------|------|---------|------|
| `hwnd` | int | `execute_action` / `focus_window` / `screen_ocr` / `capture_screen` / `screen_snapshot` / `screen_wait_for` / `screen_analyze` / `batch_actions` / `desktop_transaction` / `screen_semantic_action` / `scroll_capture` | `list_windows.hwnd` 或 `screen_window_resolve.matches[].canonical_hwnd` |
| `window_token` | dict | 仅 4 个生命周期端点：`screen_window_minimize` / `screen_window_restore` / `screen_window_raise` / `screen_window_close` | `screen_window_resolve.matches[].window_token` |

**衔接规则**：
- 生命周期端点签名为 `WindowOpRequest{hwnd: int, window_token: dict | None}`，`window_token` 是**可选**校验字段
- 传 `window_token` 时先做三层校验，失效返回 `status=WINDOW_TOKEN_INVALID` + `success=False`
- 不传 `window_token` 时跳过校验，仅按 `hwnd` 操作（向后兼容）
- `screen_window_resolve` 响应同时返回 `canonical_hwnd`（作 hwnd 用）和 `window_token`（作生命周期校验用），二者 `canonical_hwnd` 字段一致
- UWP/WinUI 应用建议优先用 `screen_window_resolve.matches[].canonical_hwnd` 而非 `list_windows.hwnd`，前者经 `resolve_canonical_window()` 规范化为根 owner HWND，避免同族窗口混淆

### post_state 验证

窗口操作（minimize/restore/raise/close）返回 `post_state`：`{exists, is_visible, is_minimized, is_foreground, title, bbox}`——必须查 post_state 验证操作生效，不要假定成功。

### 关键约束

- **WINDOW_TOKEN_INVALID**：进程重启/hwnd 销毁/pid 复用都会失效，必须重新 `screen_window_resolve` 拿新 token
- **多匹配处理**：`screen_window_resolve` 支持 4 种 criteria（title/process_name/pid/automation_id），多匹配时返回所有候选 + `status=WINDOW_AMBIGUOUS`，**不要随机选**——用更具体 criteria 二次 resolve 缩小范围
- **close 严格语义（不谎报）**：close 的 `success` 严格按 `post_state.exists=False` 判定，窗口仍存在则 `success=False` + `status=failed`。响应字段：`status` 含 `executed` / `WINDOW_TOKEN_INVALID` / `modal_blocking` / `failed`，`modal_info`（仅 close 特有）：`{dialog_hwnd, dismissed, dismiss_method, button_name}` 透传模态对话框处理详情
- **close force=true 杀进程语义**：走 `TerminateProcess` 直接终止进程，明确"会丢失未保存数据"。Win32 `DestroyWindow` 是同进程 API 跨进程调用必然失败，已废弃
- **close force=false 自动处理模态**：默认走 `WM_CLOSE` -> 1.5s 轮询等窗口消失 -> 2s 模态检测轮询。发现"是否保存"模态时优先用 UIA 在主窗口子树 BFS 找"不保存(N)"/"Don't Save"按钮 Invoke（Win11 Notepad 模态是 owned window 而非 child window，`EnumChildWindows` 跨进程枚举不到，但 UIA 树可见），UIA 不可用时回退 `EnumWindows` + `PostMessage WM_COMMAND IDNO`（Win32 标准 IDNO=7）。模态处理失败时返回 `status=modal_blocking` + `modal_info`，agent 可决定是否升级 `force=true`
- **WM_CLOSE vs TerminateProcess**：默认 WM_CLOSE 让应用有机会弹保存对话框并由后端自动选"不保存"（优先）；`force=true` 杀进程会丢数据，仅在前者失败（`modal_blocking`）或明确不需保存时使用
- **screen_app_launch 不等待**：launch 是异步的（DETACHED_PROCESS），需等待窗口用 `screen_app_wait`

## 截图方案决策树（重要！避免漏看弹窗）

**性能提示**：尽量使用窗口截图而非全屏截图——全屏元素太多，OCR 解析慢（全屏 138+ 元素 vs 窗口 20-30 元素）。只在需要全局视角时才用全屏截图（如找不到窗口、需要看任务栏等）。

**核心痛点**：用 `understand_image(window_title=...)` 截图窗口时，**看不到应用程序打开的弹窗/对话框**——弹窗不属于主窗口。用户按 Delete 打开确认对话框后，window_title 截图仍是主窗口内容，agent 误以为什么都没发生。

### 决策树

```
需要截图 -> 判断目的：
|- 检查窗口内容（按钮位置、标签页状态、列表项）
|   -> capture_screen(mode="window", hwnd=xxx, format="inline")
|   -> 或 understand_image(window_title="xxx", question="...")
|
|- 检查弹窗/对话框/右键菜单（操作后验证、确认对话框、错误提示）
|   -> capture_screen(mode="fullscreen", format="inline")
|   -> 或 screen_snapshot(with_ocr=true)  # 一次拿窗口列表+OCR
|   -> 或 understand_image(window_title="", question="屏幕上有什么弹窗？")
|      （不传 window_title，understand_image 默认截全屏）
|
|- 操作后验证（点击/按键后确认结果）
|   -> 全屏截图（弹窗可能跳出主窗口）
|   -> 推荐：execute_action(verify_prompt="期望状态")  # 服务端自动截图+VL反馈
|
`- 找不到目标元素（不知道在哪个窗口）
    -> screen_snapshot(with_ocr=true)  # 一次拿所有窗口列表+OCR
    -> 或 list_windows 看顶层窗口
```

### 互补使用原则

| 场景 | 主用方案 | 互补方案 |
|------|---------|---------|
| 知道目标窗口，检查内部状态 | `window_title` 截图 | 操作后用全屏截图验证弹窗 |
| 操作后验证（如按 Delete 删除） | 全屏截图（看弹窗） | 用 `verify_prompt` 让服务端自动验证 |
| 不知道弹窗来自哪个窗口 | 全屏截图 + `list_windows` 看 z-order | `screen_snapshot` 一次拿全信息 |
| 弹窗消失后回到主窗口 | `window_title` 截图 | OCR 快速验证状态变化 |

### 弹窗检测的额外提示

1. **弹窗可能是子窗口** —— `list_windows` 不一定列出（子窗口不算顶层窗口）。用全屏截图能看到
2. **右键菜单也算弹窗** —— 右键后菜单可能不在主窗口的截图范围内
3. **操作前后对比** —— 不确定弹窗是否出现时，操作前后各截一张全屏图对比
4. **z-order 排序** —— `list_windows` 返回的窗口列表按 z-order 排序，最顶层窗口在前面（可识别弹窗）

## DPI 缩放与坐标系统（重要）

后端启动时调用 `SetProcessDpiAwareness(2)`（PER_MONITOR_AWARE），使进程使用**物理像素坐标**而非逻辑像素坐标。

- **pyautogui/mss/PrintWindow 都使用物理像素坐标** — 在 150% 缩放的显示器上，1920x1080 逻辑分辨率对应 2880x1620 物理分辨率
- **截图尺寸 = 物理像素** — `capture_screen` 返回的 width/height 是物理像素（如 4160x2560 表示 2x 4K 屏幕全屏）
- **OCR bbox 坐标 = 物理像素** — OCR 返回的 box 坐标基于截图的物理像素
- **execute_action 的 x/y = 物理像素** — 直接传给 pyautogui，必须是物理像素
- **比例缩放不需要** — 两者都是物理像素，比例 1:1；但窗口截图是局部坐标，点击前仍须加窗口 `left/top`，全屏双屏截图须加虚拟屏原点
- **VL/远程视觉模型返回的坐标** — 远程 VL 返回 0-1000 归一化坐标（非像素坐标），详见上方"VL 坐标归一化"章节。该约定按模型系列成立（Qwen3-VL 已实测），换默认模型后须用已知位置目标复核一次

### OCR bbox 坐标修复（关键基线）

**当前结论：截图 OCR bbox 已可用于像素级文字定位。** 大偏移的根因不是 PP-OCRv6 检测网络，而是 PaddleOCR v3 默认 UVDoc 去畸变改变了 UI 截图几何，返回的 `rec_polys` 又没有逆映射回原图。`server/ocr.py` 已显式设置 `use_doc_unwarping=False`。

端到端网格回归结果：800x600、1920x1080、2560x1440 的 bbox 误差均为 1-3px。旧的 `kx约1.19`、`ky约1.13` 非恒等仿射参数已废弃，禁止恢复或照抄。

- `/ocr/*`、`/screen/ocr(engine="ocr")` 返回的 bbox 与输入截图 1:1 对应
- `screen_snapshot(with_ocr=true)` 只返回 OCR 文本和数量，不返回 bbox；需要定位时用 `screen_ocr` 或 `ocr_path`
- OCR bbox 是**文字区域**，不是整个按钮区域；文字本身可点击时取四点均值作为中心
- 文字旁有独立图标/复选框时，根据 UI 结构向相邻控件中心偏移，或用图像描述确认布局
- 详见 `.agents/wip/ocr_bbox_offset.md` 和 `tools/debug/verify_bbox_affine.py`

### OCR 可观测性

首次 OCR 慢（约 10s）是冷启动加载模型，不是 bug。`/ocr/status` 端点返回可观测字段：

| 字段 | 说明 |
|------|------|
| `model_ready` | 模型可用 |
| `cold_start` | 从未加载过 |
| `loading` | 正在加载 |
| `failed` | 加载失败 |
| `load_elapsed_ms` | 首次加载耗时 |
| `last_inference_ms` | 最近一次推理耗时 |
| `inference_count` | 累计推理次数 |
| `last_error` | 最近一次错误信息 |

- 高响应场景开 `[ocr].preload_on_startup=true` 预热模型
- `failed=true` 时查 `last_error` 修复配置，不要重试 OCR 调用
- `/health` 的 `ocr` 字段同步反映这些状态

## API 速查

### 截图（capture_screen）

| 参数 | 类型 | 说明 |
|------|------|------|
| `mode` | str | `fullscreen` / `window` |
| `window_title` | str | mode=window 时填（或用 hwnd） |
| `process_name` | str | 可选，避免同名窗口冲突 |
| `hwnd` | int | 可选，直接指定窗口句柄（最精确） |
| `format` | str | `path`（默认，写 temp 文件返回路径）/ `inline`（返回 ImageContent 给多模态 LLM）/ `base64`（返回 PNG base64，会进文本上下文） |
| `max_edge` | int | 最长边限制（控制 token） |
| `jpeg_quality` | int | JPEG 质量 |

**返回值**：`{path, width, height, window_title, elapsed_ms, snapshot_id, window_rect}`

- 优先级：hwnd > window_title+process_name > window_title
- 窗口截图使用三级 fallback：PrintWindow -> 提窗+mss -> mss全屏+裁剪
- `snapshot_id` 可传给 `execute_action` / `batch_actions` 做 STALE_COORDINATES 检测
- `window_rect` 是截图目标的窗口几何 `[left, top, right, bottom]`（fullscreen 时为虚拟屏）
- `format=inline` 时 `mcp_image_block=true`，MCP 补丁转为 ImageContent；VL 返回的坐标需按 `original_width/width` 比例还原为物理像素

### 截图 + OCR（screen_ocr）

| 参数 | 类型 | 说明 |
|------|------|------|
| `mode` | str \| None | `window` / `fullscreen`；**None 时用 config `screen.default_mode`**（默认 fullscreen） |
| `hwnd` | int | mode=window 时填（**优先于** window_title） |
| `window_title` | str | mode=window 时填（或用 hwnd） |
| `process_name` | str | 可选，进程名过滤（避免同名窗口冲突） |
| `engine` | str | `ocr`（经典 PaddleOCR，默认）/ `vl`（远程 VL） |
| `max_height` | int | 长图分块最大高度（默认 3000，仅 `engine=ocr` 时生效） |
| `force_fullscreen_crop` | bool | DirectX 全屏游戏用 true（默认 false） |

**返回值**：`{success, text, details:[{text, confidence, box}], image_size, window_rect?, window_title?, warning?, elapsed_ms}`

- `details[].box` 是窗口截图内物理像素坐标（4 点 `[x1,y1,x2,y2,x3,y3,x4,y4]`）。取四点中心后，加 `window_rect.left/top`，得到可传给 `execute_action` 的屏幕坐标
- `window_rect`：`[left, top, right, bottom]`，仅 `mode=window` 时返回；agent 据此计算 `screen_x = window_rect[0] + ocr_cx`，**无需再调 list_windows**
- `warning`：非致命警告。例如 `engine=vl` 时返回 `"engine=vl 不返回 bbox，定位请用 engine=ocr；engine=vl 仅适合纯文本提取/版面理解"`——agent 收到 warning 应改用 `engine=ocr` 做坐标定位
- `engine=ocr`：返回 `details` 含 bbox，适合文字定位
- `engine=vl`：返回 `text` 为 VL markdown，**`details` 永远为空**（remote_vl.ocr() 不返回 blocks），适合纯文本提取/版面理解，不适合坐标定位

> **MCP 调用方式**：`screen_ocr` 不在直连 MCP 白名单中，必须经 `localagent_advanced_tool(tool="screen_ocr", params={...})` 网关调用。

`details[].box` 是窗口截图内物理像素坐标。取文字四点中心后，加窗口 `bbox.left/top`，得到可传给 `execute_action` 的屏幕坐标。MCP 通过 `localagent_advanced_tool(tool="screen_ocr", params={...})` 调用。

### 窗口枚举（list_windows）

**返回值**：`{windows: [{hwnd, title, class_name, bbox, width, height, is_minimized, process_name}], count}`

### 键鼠操作（execute_action）

| 参数 | 类型 | 说明 |
|------|------|------|
| `action` | str | `click` / `double_click` / `right_click` / `type` / `type_immediate` / `hotkey` / `scroll` / `drag` |
| `x`, `y` | int | 屏幕物理坐标（click/double_click/right_click/scroll/drag） |
| `text` | str | 输入文本（type/type_immediate） |
| `keys` | list | 快捷键列表（hotkey） |
| `direction` | str | 滚动方向（scroll） |
| `amount` | int | 滚动量（scroll） |
| `dx`, `dy` | int | 拖拽偏移（drag） |
| `window_title` / `hwnd` | str/int | 限定操作窗口 |
| `process_name` | str | 可选，避免同名窗口冲突 |
| `require_confirm` | bool | 是否需要用户确认 |
| `verify_prompt` | str | 可选，操作后调 VL 反馈 <=200 字画面描述 |
| `allow_unfocused_input` | bool | 可选，键盘动作未验证焦点时是否放行 |
| `snapshot_id` | str | 可选，校验窗口几何（STALE_COORDINATES） |
| `expected` | obj | 可选，声明式后验：`{"type":"ocr_contains"/"ocr_not_contains", "text":"..."}` |
| `dry_run` | bool | 可选，预演模式 |
| `task_description` | str | 可选，任务描述（显示在接管确认弹窗中） |

**返回值关键字段**：`{success, status, message, focus_check_status, transport_status, delivery_status, postcondition_status, foreground_before, foreground_after, target_match_before, target_match_after, canonical_window, vl_description, vl_skipped, vl_skip_reason}`

**关键说明**：
- `status`: `executed` / `executed_unverified` / `postcondition_failed` / `confirmed` / `cancelled` / `blocked` / `dry_run` / `emergency_stopped`
- 防呆：未知 action / 缺少必要参数（如 click 无 x,y）返回 `status=blocked`
- 窗口范围检查：坐标超出指定窗口范围会被拦截
- 焦点安全：键盘动作前强制焦点校验，前台是受保护进程时 `FOCUS_LEAK_PREVENTED`
- `screenshot_after` 参数已废弃——不再返回 base64 截图

#### 按键操作示例（重要！没有 action="key"，单键也用 hotkey）

**没有 `action="key"` 或 `action="keypress"`** —— 按单键也用 `action="hotkey"`，`keys` 列表只放一个元素。

```python
# 按 Delete 键
execute_action(action="hotkey", keys=["delete"], hwnd=xxx)

# 按 Enter 键
execute_action(action="hotkey", keys=["enter"], hwnd=xxx)

# 按 Escape 键
execute_action(action="hotkey", keys=["escape"], hwnd=xxx)

# 按 Tab 键
execute_action(action="hotkey", keys=["tab"], hwnd=xxx)

# 按 Backspace 键
execute_action(action="hotkey", keys=["backspace"], hwnd=xxx)

# 按 F5 刷新
execute_action(action="hotkey", keys=["f5"], hwnd=xxx)

# 组合键 Ctrl+C
execute_action(action="hotkey", keys=["ctrl", "c"], hwnd=xxx)

# 组合键 Ctrl+Shift+Esc（打开任务管理器）
execute_action(action="hotkey", keys=["ctrl", "shift", "escape"])
```

**键名规范**（pyautogui 命名）：
- 单字母：`"a"`, `"b"`, ... `"z"`（小写）
- 数字：`"0"`, `"1"`, ... `"9"`
- 功能键：`"f1"`, `"f2"`, ... `"f12"`
- 修饰键：`"ctrl"`, `"shift"`, `"alt"`, `"win"`（Windows 键）
- 特殊键：`"enter"`, `"escape"`, `"tab"`, `"backspace"`, `"delete"`, `"space"`, `"up"`, `"down"`, `"left"`, `"right"`, `"home"`, `"end"`, `"pageup"`, `"pagedown"`
- 完整列表：参考 [pyautogui 文档 KEYBOARD_KEYS](https://pyautogui.readthedocs.io/en/latest/keyboard.html#keyboard-keys)

**输入文本（不是按键）**：
- `action="type"` —— 逐字符输入，带 20ms 间隔（更稳，绕过输入法，支持中文）
- `action="type_immediate"` —— 无间隔快速输入
- 不要用 `type` 来按键——`type` 只用于输入文本字符

### 窗口焦点激活（focus_window）

| 参数 | 类型 | 说明 |
|------|------|------|
| `window_title` | str | 窗口标题（或用 hwnd） |
| `hwnd` | int | 直接指定窗口句柄 |
| `process_name` | str | 可选，避免同名窗口冲突 |

**返回值**：`{success, target_hwnd, target_title, foreground_hwnd, foreground_title, message}`

组合 AttachThreadInput + BringWindowToTop + WScript.Shell.AppActivate 三套机制。防呆：未传参数返回"必须提供 window_title 或 hwnd"；窗口未找到时列出相似标题。

### 点击位置预览（preview_action）

| 参数 | 类型 | 说明 |
|------|------|------|
| `points` | list | `[{x, y, label}]`，屏幕物理像素坐标 |
| `mode` | str | `fullscreen` / `window` |
| `window_title` / `hwnd` | str/int | mode=window 时填 |
| `process_name` | str | 可选 |
| `max_edge` | int | 最长边限制（默认 1280） |
| `marker_size` | int | L形臂长（像素，默认 18） |
| `show_crosshair` | bool | 画贯穿十字线（默认 false） |
| `center_dot` | bool | 中心画小红点（默认 true） |
| `format` | str | `path`（默认，写 temp 文件）/ `inline`（返回 base64） |

**返回值**：`{path, points_info, note}`，`points_info` 含 `screen_x/screen_y`（传给 execute_action）和 `x/y`（图上位置）

标记方式：四个 L 形角标（开口朝向中心）+ 中心小红点，不遮挡点击中心。坐标系为屏幕物理像素（与 execute_action 完全一致）；mode=window 时 points 传屏幕坐标，内部自动减去窗口左上角偏移画点。用途：agent 看不到图，但用户可以，把带红点的图给用户看，用户确认坐标正确后再点击。

### 批量键鼠操作（batch_actions）

| 参数 | 类型 | 说明 |
|------|------|------|
| `actions` | list | 步骤列表，每个步骤含 action 及其参数；坐标动作可带 `snapshot_id` |
| `window_title` / `hwnd` | str/int | 限定操作窗口 |
| `activate_window` | bool | 批量开始前激活窗口（默认 true） |
| `stop_on_error` | bool | 遇错即停（默认 true） |
| `interval` | float | 每步额外间隔 |
| `allow_unfocused_input` | bool | 键盘动作未验证焦点时是否放行（默认 false） |
| `stop_on_focus_drift` | bool | 焦点漂移时立即停止剩余动作（默认 true） |
| `task_description` | str | 可选，任务描述（接管确认弹窗） |

**返回值**：`{success, total, executed, failed, blocked, aborted, aborted_reason, results, elapsed_ms, message}`

支持 `action="wait"` 在步骤间精确等待。每个键盘动作走焦点校验（FOCUS_LEAK_PREVENTED 时该步 blocked）。焦点漂移时 `delivery_status=leaked` 立即停止，返回 `aborted=true` + `aborted_reason`。

### 桌面事务（desktop_transaction）

| 参数 | 类型 | 说明 |
|------|------|------|
| `target` | obj | 目标窗口引用：`{hwnd}` 或 `{window_title, process_name}` 或 `{window_token}` |
| `actions` | list | 步骤列表（同 batch_actions） |
| `expected` | obj | **必填**，事务级后验：`{"type":"ocr_contains"/"ocr_not_contains", "text":"..."}` |
| `rollback_actions` | list | 回滚步骤（rollback_policy=auto 时使用） |
| `rollback_policy` | str | `none`（默认）/ `auto` |
| `timeout` | float | 事务总超时 |
| `interval` | float | 步骤间额外间隔 |
| `allow_unfocused_input` | bool | 默认 false |
| `dry_run` | bool | 预演模式 |
| `task_description` | str | 可选，任务描述（接管确认弹窗） |

**返回值**：`{success, status, dry_run, total_steps, executed_steps, failed_step_index, failed_reason, rollback_executed, rollback_steps_succeeded, rollback_results, transaction_postcondition, elapsed_ms, results, canonical_window, message}`

dry_run=true 时顶层 `success=True`（预演通过前置校验视为成功）。编排器应同时检查 `status=="dry_run"` + `dry_run==True` 判定本次未真实执行。

### 桌面快照（screen_snapshot）

| 参数 | 类型 | 说明 |
|------|------|------|
| `with_ocr` | bool | 是否附带 OCR 文本（默认 true） |
| `ocr_mode` | str | `fullscreen` / `active_window`（默认 fullscreen） |
| `max_windows` | int | 最大窗口数（默认 30） |
| `include_minimized` | bool | 是否包含最小化窗口（默认 **true**，agent 需知道所有窗口状态） |

**返回值**：`{success, foreground_hwnd, foreground_title, windows_count, windows: [...], ocr_text?, ocr_details_count?, ocr_image_size?, ocr_error?, elapsed_ms}`

- `windows_count`：过滤前的窗口总数（含最小化窗口）；`windows` 数组长度受 `max_windows` 限制
- `ocr_text` / `ocr_details_count` / `ocr_image_size`：仅 `with_ocr=true` 时存在；OCR 失败时改为返回 `ocr_error`
- `ocr_details_count` 不返回 `details` 本身（避免 base64 撑爆上下文），仅返回数量供 agent 判断 OCR 是否成功
- 最小化窗口的 `bbox` 是 `(-32000, -32000, ...)`（屏幕外无效坐标），不能直接用于坐标计算

一次调用获取"现在屏幕上有什么"（窗口列表 + OCR 文本），返回纯文本，无 base64。首次调用会触发 PaddleOCR 加载（5-10s）。

### 条件等待（screen_wait_for）

| 参数 | 类型 | 说明 |
|------|------|------|
| `expected` | **dict** | `{"type":"ocr_contains"/"ocr_not_contains"/"vl_contains", "text":"期望文本"}`；`type` 可省略默认 `ocr_contains`（与其他端点统一） |
| `timeout` | float | 超时秒数（默认 30） |
| `interval` | float | 轮询间隔（默认 1.0） |
| `mode` | str | `fullscreen` / `window`（默认 fullscreen） |
| `hwnd` | int | mode=window 时填（**优先于** window_title） |
| `window_title` | str | mode=window 时填（或用 hwnd） |
| `process_name` | str | 可选，进程名过滤（避免同名窗口冲突） |
| `region` | str | 可选，区域裁剪 `"left,top,right,bottom"`（4 个整数） |
| `case_sensitive` | bool | 可选，是否区分大小写（默认 false） |
| `max_size` | int | 可选，给 VL 用的图片最长边（默认 1280） |

**返回值**：`{success, condition_met, elapsed_ms, check_count, last_ocr_text, matched_at, message}`

循环截图 + OCR/VL 检查，直到条件满足或超时。返回纯文本，无 base64。

> ⚠️ **breaking change**：旧版 `expected: str` + `condition_type: str` 字段已废弃。改为统一 dict 格式后，旧调用会因 Pydantic 类型校验返回 422，需更新为 `expected={"type":"ocr_contains","text":"..."}`。

### 图像特征分析（screen_analyze）

| 参数 | 类型 | 说明 |
|------|------|------|
| `mode` | str | `fullscreen` / `window`（默认 fullscreen） |
| `hwnd` | int | mode=window 时填（**优先于** window_title） |
| `window_title` | str | mode=window 时填（或用 hwnd） |
| `process_name` | str | 可选，进程名过滤（避免同名窗口冲突） |
| `region` | str | 可选，区域裁剪 `"left,top,right,bottom"` |
| `grid_size` | int | 网格分区大小（默认 4） |
| `sample_step` | int | 采样步长（默认 4，越大越快但越粗糙） |

**返回值**：`{success, top_colors, brightness, diversity, edge_density, grid, alerts, summary}`

返回主色列表（hex+名称+占比）、亮度统计、颜色多样性、边缘密度、网格分区主色。异常告警：黑屏/白屏/蓝屏/单色统治/卡加载/警告色。用于远程 VL 不可用时的画面快速判断。

### 覆盖层（overlay）

```
POST /screen/overlay {action:"show", message:"Agent操作中", position:"top"}
POST /screen/overlay {action:"hide"}
```

**自动隐藏**：覆盖层显示后，若 `overlay_auto_hide_seconds`（默认 30s，在 `[screen]` 段配置，0=禁用）内无新的 `execute_action` 调用，会自动收起。每次 `execute_action` 都会重置闲置计时器（`poke_overlay`），因此连续操作期间不会被误判为"已完成"。任务结束时 agent 仍应显式调 `action=hide`，但忘记调也不会卡住——30s 后自动收起。

### 滚动截图（scroll_capture）

| 参数 | 类型 | 说明 |
|------|------|------|
| `hwnd` | int | 目标窗口句柄（与 `window_title` 二选一，hwnd 优先） |
| `window_title` | str | 目标窗口标题（或用 hwnd） |
| `process_name` | str | 可选，进程名过滤 |
| `scroll_method` | str | `keyboard`（默认）/ `mouse` |
| `scroll_key` | str | keyboard 模式按键：`pagedown`（默认）/ `arrow_down`/ `space` |
| `scroll_amount` | int | mouse 模式滚轮量（默认 5） |
| `scroll_count` | int | 最大滚动次数（默认 5） |
| `scroll_interval` | float | 每次滚动后等待秒数（默认 0.6） |
| `scroll_x` / `scroll_y` | int | mouse 模式滚动位置（默认窗口右侧 2/3 中心，避开左侧导航栏） |
| `go_top_first` | bool | 开始前先回顶部（默认 true） |
| `stitch` | bool | 是否拼接为长图（默认 true） |
| `return_inline` | bool | 返回 inline image（MCP ImageContent，默认 true） |
| `detect_end` | bool | 检测到底部自动停止（默认 true） |

**返回值**：`{success, segments_count, stitched_image_inline?, stitched_width?, stitched_height?, segment_heights, reached_bottom, message, elapsed_ms}`

适用于内容超出可见区域的长界面（如长设置对话框、长网页）。`keyboard` 模式依赖焦点已在可滚动区域（调用方需提前点击内容区聚焦）；`mouse` 模式不依赖焦点，滚轮作用于鼠标下方控件。

> 职责边界：本端点只负责滚动+截图+拼接。前期准备（激活窗口、点击内容区聚焦、切到目标标签页等）由调用方用 `focus_window`/`execute_action` 等工具提前完成。

### 视觉解析 / VL 元素定位

- **`understand_image`**：远程 VL 图像描述，用 **`question`** 参数（不是 prompt）提问。用于布局、状态、遮挡、弹窗、选中状态描述。不要把其坐标回答当像素用（详见"VL 坐标归一化"章节）。
- **`vision_locate`**（`POST /vision/locate`）：无文字元素坐标兜底。参数：`target`（目标描述）、`window_title`+`process_name`（窗口模式，与 `image` 二选一）、`image`（base64 直传）。自动处理 VL 归一化坐标 + 加窗口偏移，返回屏幕物理像素坐标 `{x, y, nx, ny, image_size, description, elapsed_ms}`。不接受 `hwnd`。精度约 +/-20 像素。详见"VL 坐标归一化"章节。

## 注意事项

- 游戏窗口截图可能黑屏（DirectX渲染），需要特殊处理
- 文本输入（`type`/`type_immediate`）均用 SendInput + KEYEVENTF_UNICODE 逐字符注入，绕过输入法，支持中文/Unicode；`type` 带 20ms 间隔（更稳），`type_immediate` 无间隔（更快），不再覆盖剪贴板
- **Unicode 代理对**：`_send_unicode_text()` 按 UTF-16 拆分高低代理对——BMP 字符（U+0000-U+FFFF）单次 SendInput（`wScan` 16 位可承载）；补充平面字符（U+10000+，如 emoji U+1F600、CJK Extension B U+20000）拆分高低代理对（高代理 = 0xD800 + ((cp - 0x10000) >> 10)，低代理 = 0xDC00 + ((cp - 0x10000) & 0x3FF)）分两次 SendInput；控制字符（CR/LF/Tab/Backspace）走 VK 路径；lone surrogate 跳过并 warn
- 双击（`double_click`）用 SendInput 发送原生 4 事件（down/up/down/up），系统识别为真实双击，Qt 等框架可正确触发 mouseDoubleClickEvent
- 焦点检测增强：操作前检查前台窗口是否为目标窗口，被模态窗口抢占时重试一次，仍失败则在 message 中追加"[警告] 焦点被模态窗口抢占"提示
- 小图标识别准确率低，对不确定的元素务必要求确认
- 操作前确保目标窗口未被遮挡或最小化
- **PaddlePaddle兼容性**：经典 OCR 使用 PaddlePaddle-GPU 3.2.2，并在首次加载时禁用 PIR。远程 VL 是无状态 API；经典 OCR 建议保持常驻，避免重复加载
- **PaddleOCR 漏识别**：先确认窗口截图正确，再尝试更清晰截图、局部裁剪/放大和近似文字匹配；仍失败且目标是纯图标时再用 `understand_image` 描述 + `vision_locate` 兜底
- **键盘导航不可靠**：Home/Down 键可能因焦点不在正确控件而失效；文字标签优先 OCR bbox + 鼠标点击，不再默认用 VL 坐标
- **mss 截图遮挡问题**：mss 全屏截图时如果目标窗口被遮挡，会截到前面的窗口内容。PrintWindow（后台截图）不受遮挡影响，但需要管理员权限
- **截图传递**：`inline` 只用于当前多模态模型直接查看，不能再作为参数转传。OCR 定位直接用 `screen_ocr`；VL 描述直接给 `understand_image` 传 `window_title/process_name` 让服务端自动截图，避免 base64 中转
- **验证脚本的关键词判断**：导航栏文字可能干扰（如"BitTorrent"既是导航项又是标签页名）。优先 OCR 右侧内容区域；只有视觉高亮等文字无法表达的状态才用 VL 描述
- **VL 提问要精确**（避免概念误识别）：VL 截图分析时，模糊提问会让 VL 把相似概念混淆（实测把"监听地址"误识别为"tracker"）。提问要具体：
  - 不要："有哪些 tracker？" -> VL 可能列出所有"监听地址"等无关信息
  - 推荐："这个种子的 tracker URL 是什么？" -> VL 知道找 .torrent 文件里的 announce URL
  - 不要："这个按钮在哪里？" -> 普通文字定位不应调用 VL
  - 推荐："OCR 已定位到导航栏第三个'设置'，请描述它左侧是否有独立图标、当前是否高亮" -> VL 只回答布局/状态
- **understand_image 参数命名差异**：`understand_image` 用 **`question`**（不是 prompt）；`agent_chat` / `agent_score` 用 **`prompt`**。报错 "Input validation error: 'question' is a required property" = 误用了 `prompt`
- **window_title 必须精确匹配** —— `understand_image` / `vision_locate` 的 `window_title` 不支持模糊匹配（部分包含）。窗口标题含动态内容（如正在播放的歌曲名）时，先用 `list_windows` 拿到精确标题再调用，或用 `process_name` 过滤

## 管理员权限

**键鼠操控需要以管理员身份启动后端**，否则Windows UIPI会阻止鼠标操作（`SetCursorPos`静默失败）。

启动方式：
```bash
# 右键终端 -> "以管理员身份运行"，然后：
uv run python -m server.main
```

`/screen/status` 的 `admin_privileges` 字段会指示当前是否具有管理员权限。非管理员时截图和窗口枚举正常，但键鼠操作会返回失败。

## 历史经验留档（byr.pt 任务实测，2026-07-14）

> 以下经验浓缩自一次 PT 站点保号+下载种子长任务的事后复盘。原 WIP `ux_issues_summary.md` 已删除，技术性经验（坐标/按键/截图/点击精度）已融入上文相应章节，此处仅保留未在其他章节体现的 agent 行为规范类教训。

### 关键决策点必须询问用户

- 涉及**卸载软件、重装、切换方案**等影响用户系统的操作前，必须用 AskUserQuestion 询问，提供多个可选方案及利弊分析
- 不要单方面决定影响用户系统的操作（如误判客户端封禁后建议卸载重装）
- 网络类任务开始时，主动询问用户：代理软件、TUN 模式、网络环境

### 长任务进展汇报与求助机制

- 长任务每 **5-10 分钟**汇报一次进展，不要长时间静默执行
- 同一问题尝试 **3 次以上**仍未解决时，主动询问用户是否介入
- 诊断超过 **10 分钟**未解决，主动汇报当前进展和下一步计划，或明确表示"我需要帮助"
- 用户说"算了我来帮你吧"是强烈信号——表明 agent 应该更早求助

### 系统性诊断流程

- 诊断前先列出所有假设和验证方法，不要逐个试错
- 网络诊断标准流程：DNS 解析 -> TCP 连接 -> TLS 握手 -> HTTP 请求 -> 应用层
- 每一步用 `curl.exe -v` 查看握手细节
- 测试脚本命名包含测试目的（如 `test_tls_version.py` 而非 `test_byr3.py`）
- 每次测试后记录结论，避免重复测试同一件事

### PT 站点特殊网络环境（byr.pt 实测，仅作参考）

PT 站点可能同时满足多个特殊条件，多重叠加时根因难以快速定位：
- IPv6-only（无 IPv4 地址，系统无公网 IPv6 路由时需通过 HTTP 代理访问）
- TLS 版本限制（如只支持 TLS 1.2，但 Clash TUN 模式可能处理兼容性）
- 客户端封禁（检查 peer_id 前缀和 User-Agent，curl 默认 UA 会被封禁）
- NexusPHP 首次下载提示页（需勾选 hidenotice 并点击 #continuedownload）
- tracker 返回任何响应（包括错误）都说明连接是通的，优先排查错误消息而非网络层

测试 tracker 时必须传正确 User-Agent：`curl.exe -A "qBittorrent/5.2.2" "https://byr.pt/announce.php?..."`

### qBittorrent 配置注意事项

> qBittorrent 的完整结构化经验（UIA 友好度/快捷键/菜单路径/已知坑）已迁移到
> [`.agents/skills/computer_use/apps/qbittorrent.md`](file:///<project_root>/.agents/skills/computer_use/apps/qbittorrent.md)。
> 调 `screen_match_app(process_name="qbittorrent.exe")` 可一键查询。以下仅保留历史摘要。

- 配置优先通过界面操作，不要直接改 ini（界面保存会覆盖 ini 修改）
- v4.1.8 存在 `[Preferences]` 和 `[Network]` 双配置段冲突
- 代理选项命名不直观："通过代理查找主机名"不勾选=让代理服务器解析 DNS（IPv6-only 站点的正确做法）
- Web UI 密码需要 PBKDF2 哈希格式，明文密码不被接受

### bencode / info_hash 编码陷阱

- 修改 .torrent 文件 tracker URL 需要同步修改字符串长度前缀（`76:https://...` 中的 `76:` 是长度）
- 手动解析 bencode 容易出错（整数类型 `i1409e` 等），应使用专业 bencode 库
- URL 编码 binary 数据（如 info_hash）必须用 `urllib.parse.quote(data, safe='')`，否则字母数字字节不会被 %xx 编码

## 附录：评估修复历史

> 以下为评估文档（`docs/evaluations/2026-07-20-computer-use-vs-localagent.md`）驱动的多轮改造记录，仅供历史参考。正文已整合所有修复的技术结论，此处保留版本标注作为变更追溯。

### 焦点安全 P0 改造

- **P0-1 焦点强校验**：`type`/`type_immediate`/`hotkey` 执行前必走 `verify_focus_for_input`，前台是受保护进程时 `FOCUS_LEAK_PREVENTED` 零按键发送
- **P0-2 canonical window token**：`resolve_canonical_window(hwnd)` 解析 UWP/WinUI 多层 HWND 同族集合，响应返回 `canonical_window` 规范化 token
- **P0-3 分层状态语义**：`transport_status`/`delivery_status`/`postcondition_status` 三层拆分，未声明 `expected` 时不再伪称 `executed`；`transport_status=sent` + `delivery_status=delivered` + `postcondition_status=not_checked` 不再被包装成 error
- **P0-4 preview_action 默认 format=path**：避免 base64 污染 LLM 上下文，JPEG 写 temp 文件返回路径

### UIA 语义层改造

- **P0-5 UIA 引入**：`screen_accessibility_snapshot` + `screen_semantic_action` 语义动作优先于坐标点击
- **CoInitialize 线程级初始化**：`_ensure_com_initialized()` 在每个 UIA 入口前初始化 COM（pywin32 CoInitialize + ctypes 回退 + 线程 ID 跟踪幂等），修复 "WinError -2147221008：尚未调用 CoInitialize"
- **ControlType 硬编码稳定映射**：`_CONTROL_TYPE_HARDCODED` 字典（41 项，来源 Microsoft UIAutomation Core ControlType Idl）跨版本稳定，避免 dir() 遍历依赖库版本。未知 ID 返回 `Unknown_{id}` 前缀
- **uia_value_equals 内建后验**：`set_value` 后立即用 `ValuePattern.Value` 读回比对，无需 agent 再调 snapshot

### OCR 可观测性 P1-A

- 四态状态机（cold_start/loading/model_ready/failed）
- `/ocr/status` 端点返回 `load_elapsed_ms`/`last_inference_ms`/`inference_count`/`last_error`
- `[ocr].preload_on_startup` 配置项（默认 false），后端启动时后台预热 PaddleOCR

### 窗口生命周期 P1-B

- 应用列表/启动/等待 + 窗口解析（多匹配候选）+ 窗口操作（最小化/恢复/置顶/关闭）
- `window_token` 三层校验（hwnd 存在 + pid 匹配 + process_create_time 不变）
- close 严格语义：`success` 严格按 `post_state.exists=False` 判定，不谎报；force=false 自动处理"是否保存"模态（UIA 优先找"不保存"按钮 Invoke，回退 PostMessage WM_COMMAND IDNO）

### desktop_transaction P2-1

- 原子多步 + 必填 `expected` 事务级后验 + `rollback_policy`（none/auto）+ `timeout` + `dry_run`
- 12 步流水线：基础校验 -> 解析 target -> window_token 验证 -> 加载配置 -> 激活目标窗口 -> 显示 overlay -> 逐步执行（含焦点漂移检测）-> dry-run 提前返回 -> 事务级后验 OCR -> 决定事务状态 -> 可选回滚 -> 最终 canonical_window 刷新

### rerun2/rerun3 修复清单

- **rerun2 P0-1**：CoInitialize 线程级初始化（pywin32 CoInitialize + ctypes 回退 + 线程 ID 跟踪幂等）
- **rerun2 P0-2**：Unicode 代理对拆分（UTF-16 高低代理对，修复 emoji/CJK Extension B 输入乱码）
- **rerun2 P0-4**：preview_action 默认 format=path（JPEG 写 temp 文件）
- **rerun2 P1-1**：dry_run 顶层 success=True（预演通过前置校验视为成功）
- **rerun2 P1-3**：screen_wait_for / screen_analyze 的 hwnd 字段统一优先于 window_title
- **rerun2 P1-4**：ControlType 硬编码稳定映射（41 项）
- **rerun2 P2-1**：desktop_transaction 原子多步 + 回滚
- **rerun3 P1-1**：desktop_transaction dry_run 响应新增 `dry_run: bool` 字段（旧版 success=False 容易被通用编排器误判为失败）
- **rerun3 P1-2**：UIA 内建后验 uia_value_equals（set_value 后 ValuePattern 读回比对）
- **rerun3 P3-1**：OCR fallback 语义明确化（UIA 首选，OCR 是 UIA 不可用时的 fallback，不是首选定位方式）
