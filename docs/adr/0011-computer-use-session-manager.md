# computer use 会话管理层独立 + GUIClient 拆分（SessionManager + OverlayClient）

`GUIClient` 单类一身五职（IPC + Overlay + 弹窗 + 授权管理 + auto_hide_timer）1000+ 行，授权状态被 GUI"私藏"导致 health/routes/security 反向 import `gui_client`（8 处跨层耦合）。本决策新建 `server/screen/session/` 子包，`SessionManager` 单例替代 `ControlGrantManager` 从 `gui_client` 剥离；`GUIClient` 拆为 `OverlayClient`（IPC + OverlayWidget + ConfirmDialog）+ `SessionManager`（授权生命周期）；`Mode` enum 三档（NO_PERMISSION/NORMAL/WATCHDOG）替代 `is_persistent_mode()` × `is_watchdog_mode()` 组合判定；health/routes/security/overlay_client 均通过 `SessionManager` 访问授权状态。

## Status

accepted (2026-08-05)

## Context

重构前 computer use 的会话管理逻辑散落在 4 处，跨层耦合严重：

| # | 问题 | 位置 |
|---|------|------|
| 1 | `core/health.py._get_persistent_status` 反向 import `gui_client` | core 反向依赖 GUI |
| 2 | `screen/security.py._trigger` 调 `gui_client.release_task_control` + `hide_overlay` | 紧急停止跨层调会话管理 |
| 3 | `screen/routes.py` 的 `_ensure_takeover_approved` 等 helper 反向 import `gui_client` | 路由层做会话管理的活 |
| 4 | `core/lifecycle.py` shutdown 钩子中 `gui_client.shutdown()` 被调两次 | 重复 shutdown |

根因：`GUIClient`（`server/gui_process.py`，1053 行）单类一身五职，`ControlGrantManager` 被持有为 `GUIClient._control_grant`，授权状态被 GUI 客户端"私藏"，路由层只能反向访问。

权限模式通过 `is_persistent_mode()` × `is_watchdog_mode()` 组合判定（3 状态：无授权=False×False / normal=True×False / watchdog=True×True），语义不清，且 normal 的 5 分钟空闲撤销与 watchdog 的 10 小时硬上限混在同一套 `expire_if_idle` 逻辑里。

## Decision

1. **新建 `server/screen/session/` 子包**，含 `modes.py`（`Mode` StrEnum）、`manager.py`（`SessionManager` 单例）、`policy.py`（时间策略 idle/warning/grace/timeout）、`events.py`（状态变更事件总线）、`state.py`（会话状态）。
2. **`SessionManager` 单例替代 `ControlGrantManager`，从 `gui_client` 剥离**。`SessionManager` 持有授权生命周期（grant/release/touch/expire/worker 全套机制），`OverlayClient` 仅持有 IPC 句柄不持有授权状态。`server/screen/control_grant.py` 暂留为 re-export shim（过渡期保留旧 `ControlGrantManager` 签名兼容调用方，待所有调用方迁移完再移除）。
3. **`GUIClient` 拆为 `OverlayClient`**（IPC 通信 + OverlayWidget 渲染控制 + ConfirmDialog 弹窗）——文件从 `server/gui_process.py` 重命名为 `server/overlay_client.py`。`OverlayClient` 订阅 `SessionManager` 状态变化渲染倒计时，授权相关方法（`grant_task_control`/`release_task_control`/`is_persistent_mode`/`is_watchdog_mode`/`touch_input_time`/`get_task_authorization_status`）改为转发 `SessionManager`。
4. **`Mode` enum 三档替代组合判定**：

   ```python
   class Mode(StrEnum):
       NO_PERMISSION = "no_permission"  # 只读端点放行，副作用端点返回 403
       NORMAL = "normal"                # 10 分钟告警 + 30 分钟降级到无权限
       WATCHDOG = "watchdog"            # 默认 10h（1-999h 可调），到期降级到 NORMAL
   ```

   `SessionManager.can_operate(required)` 按 Mode 判定（NO_PERMISSION 任何模式满足；NORMAL 要求 mode==NORMAL 或 WATCHDOG；WATCHDOG 要求 mode==WATCHDOG），替代 `is_persistent_mode()` × `is_watchdog_mode()` 组合。
5. **health/routes/security/overlay_client 均通过 `SessionManager` 访问授权状态**：`health._get_persistent_status` 改调 `SessionManager.status()`（`overlay_visible` 字段仍从 `overlay_client` 拿）；`routes._ensure_takeover_approved` 改调 `SessionManager`；`security._trigger` 的 `release_task_control` 改调 `SessionManager.release("emergency_stop")`，`hide_overlay` 保留调 `overlay_client`。`routes.py` 60 处 `gui_client` 引用分类处理：授权相关改调 `SessionManager`，Overlay 渲染/弹窗相关保留调 `overlay_client`。
6. **watchdog 关机权限绑定 `SessionManager`**：watchdog 弹窗增加"允许关机"复选框（默认勾选），`SessionManager` 记录 `shutdown_permitted=True`；`/auto_shutdown/trigger` 检查 `SessionManager`：watchdog 模式 + `shutdown_permitted` 才放行，否则 403。

## Considered Options

1. **保留 `ControlGrantManager` 在 `gui_client`**（路由层继续反向 import）——被拒：8 处跨层耦合无法消除；`GUIClient` 1000+ 行职责过载；core 反向依赖 GUI 违反分层。
2. **新增独立 watchdog 授权机制（与 task authorization 并列）**——被拒：watchdog 与 normal 授权是同一生命周期的不同档位，并列会重复 grant/release/touch/expire 机制；`Mode` enum 三档统一管理更清晰。
3. **仅调 config 不改代码**（用配置项区分 normal/watchdog 行为）——被拒：`is_persistent_mode()` × `is_watchdog_mode()` 组合判定语义不清，config 无法表达"watchdog 到期降级到 normal"的状态转换；需要 `Mode` enum + `SessionManager` 状态机。
4. **立刻改 `control_grant.py` 为 re-export**（T7 时机策略方案 B）——被拒：旧 `ControlGrantManager.grant` 签名（task_description/source/idle_timeout_seconds/mode）与新 `SessionManager.grant` 签名（mode: Mode/task_description/source/max_duration_hours/shutdown_permitted）不兼容，立刻改会破坏所有调用方（gui_process/测试套件）。采用方案 A：保留旧 `ControlGrantManager` 直到所有调用方迁移完，最后改 re-export。

## Consequences

**正面**：
- 消除 8 处跨层耦合，core 不再反向依赖 GUI；`GUIClient` 拆分后 `OverlayClient` 职责单一（IPC + 渲染 + 弹窗）。
- `Mode` enum 三档语义清晰，状态转换（无→normal→watchdog / watchdog→normal→无）由 `SessionManager` 状态机管理，替代组合判定。
- watchdog 关机权限绑定 `SessionManager`，`/auto_shutdown/trigger` 有明确权限模型。
- 倒计时由 `SessionManager` 事件驱动 `OverlayClient` 渲染，不再 `_on_task_authorization_expired` 直接操作 overlay。

**负面**：
- `control_grant.py` 暂留 re-export shim 过渡，新旧两套 API 并存一段时间，新读者需理解为何有两个类。
- `overlay_client.py` 重命名牵连 60+ 处 `from server.gui_process import gui_client` 改动。
- 1 秒倒计时 tick 实现需在 `OverlayClient` 主进程侧起独立计时器读 `SessionManager.status()` 推 IPC（`SessionManager` worker 是 5 秒周期，不会每秒 publish）。

**回退路径**：`SessionManager` → `ControlGrantManager` 重命名即可回退（签名兼容期保留 re-export）；`Mode` enum → `is_persistent_mode()`/`is_watchdog_mode()` 回退需恢复组合判定逻辑。`server/screen/control_grant.py` re-export shim 在所有调用方迁移完前一直可用。

## References

- 决策来源：[`temp/sdd/computer-use-session-refactor/00-context-and-decisions.md`](file:///f:/<project_root>/temp/sdd/computer-use-session-refactor/00-context-and-decisions.md) 决策 1（会话管理层位置 + GUIClient 拆分）、决策 3（watchdog 关机权限）、决策 17（agent 指定 mode）、§3 跨层耦合 8 处清单、§5 上层架构评估
- 关键代码路径：[server/screen/session/manager.py](file:///f:/<project_root>/server/screen/session/manager.py)（`SessionManager` 单例）、[server/screen/session/modes.py](file:///f:/<project_root>/server/screen/session/modes.py)（`Mode` StrEnum 三档）、[server/screen/session/policy.py](file:///f:/<project_root>/server/screen/session/policy.py)（时间策略）、[server/overlay_client.py](file:///f:/<project_root>/server/overlay_client.py)（原 `gui_process.py` 拆分后仅 IPC + Overlay + 弹窗）、[server/screen/control_grant.py](file:///f:/<project_root>/server/screen/control_grant.py)（re-export shim 过渡）
- 相关 ADR：[ADR-0005](0005-approval-domain-consolidation.md)（审批领域文件整合，http_guard 保持独立与本决策的权限分层互补）
