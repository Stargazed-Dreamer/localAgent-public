# watchdog 模式关机预授权走 auto_shutdown 模块（agent 不持有 shutdown 权限）

watchdog 模式下关机权限走现有 `auto_shutdown` 模块（独立 CLI 子进程），agent 不直接调 shutdown 命令。`SessionManager` 记录 `shutdown_permitted=True`（来自 watchdog 弹窗的"允许关机"复选框），`/auto_shutdown/trigger` 调 `SessionManager.can_shutdown()` 校验——agent 在整个链路中不持有任何 shutdown 权限，消除 agent 误调 shutdown 的风险。

## Context

watchdog 模式是为"用户睡觉时 agent 持续监控并可能在任务完成时关机"设计的长时授权场景（默认 10h，1-999h 可调）。用户在 watchdog 弹窗中勾选"允许关机"复选框（默认勾选）后，期望 agent 在任务完成时能触发关机。

挑战在于：agent 的工具链里有 `exec_cmd`（可执行任意 shell 命令，含 `shutdown /s /t 60`）。如果把 shutdown 权限直接下放给 agent，会出现：

1. **agent 误调风险**：LLM 幻觉、prompt injection、工具调用参数错误都可能让 agent 误调 `shutdown`，用户睡着时无法及时 `shutdown /a` 中止。
2. **权限粒度失控**：agent 持有 `exec_cmd` 即等于持有所有 shell 权限，无法单独回收 shutdown 权限。
3. **审计困难**：agent 直接调 shutdown 走通用 exec_cmd 路径，与正常命令混在一起，难以单独审计。

项目里已有 `server/auto_shutdown.py` 模块——独立 CLI 子进程，含 6 种触发条件 + 延迟复检 + 60s 倒计时 + `shutdown /a` 取消，是"关机"的专用链路。复用这条链路比给 agent 新发 shutdown 权限更安全。

> 注：watchdog-mode SDD 的 DD-1（扩展 ControlGrantManager 加 mode 字段）已被 `computer-use-session-refactor` 决策 1（SessionManager 替代 ControlGrantManager）取代，本 ADR 只覆盖 DD-2（关机预授权走 auto_shutdown），不重复 ControlGrantManager → SessionManager 的演进（详见 ADR-0012）。

## Decision

关机预授权链路如下：

```
watchdog 弹窗（用户勾选"允许关机"）
   ↓
SessionManager.grant(mode=WATCHDOG, shutdown_permitted=True)
   ↓ state.shutdown_permitted = True
agent 调 POST /auto_shutdown/trigger
   ↓
auto_shutdown.trigger_shutdown(req)
   ↓ session = SessionManager 单例
   ↓ session.can_shutdown() 校验：
   ↓   mode == WATCHDOG 且 state.shutdown_permitted == True → 放行
   ↓   否则 → 403
   ↓
_trigger_shutdown_command()（独立 CLI 子进程，含 60s 倒计时）
```

**关键约束**：

1. **agent 不持有任何 shutdown 权限**：agent 工具链里没有 `shutdown` / `poweroff` / `exec_cmd with shutdown` 的快捷方式。agent 想关机只能调 `POST /auto_shutdown/trigger`。
2. **`SessionManager.can_shutdown()` 是唯一权限校验点**：`auto_shutdown.trigger_shutdown()` 第一行调 `session.can_shutdown()`，SessionManager 不可用时 fail-closed 返回 403（不依赖任何 fallback）。
3. **`shutdown_permitted` 仅 watchdog 模式有效**：normal 模式即使误设 `shutdown_permitted=True`，`can_shutdown()` 仍返回 False（双重校验：mode + flag）。
4. **`auto_shutdown` 是独立 CLI 子进程**：后端重启不中断已启动的关机倒计时（用户可 `shutdown /a` 中止）。

```python
# server/screen/session/manager.py
def can_shutdown(self) -> bool:
    """关机权限检查：mode == WATCHDOG 且 shutdown_permitted。"""
    with self._lock:
        if self._state is None:
            return False
        return (self._state.mode == Mode.WATCHDOG
                and self._state.shutdown_permitted)

# server/auto_shutdown.py
@router.post("/trigger", operation_id="auto_shutdown_trigger")
async def trigger_shutdown(req: TriggerRequest) -> dict:
    """权限：需 watchdog 模式且勾选允许关机（SessionManager.can_shutdown()）。"""
    try:
        session = _get_session_manager()
        if not session.can_shutdown():
            return JSONResponse(status_code=403, content={...})
    except Exception as e:
        # SessionManager 不可用时 fail-closed
        return JSONResponse(status_code=403, content={...})
```

## Considered Options

1. **签发一次性 approval_token（agent 仍需 exec_cmd 调 shutdown）**——被拒绝：token 只对 shutdown 有效但 agent 仍需调 `exec_cmd`，agent 持有 `exec_cmd` 即等于持有所有 shell 权限，token 限制等于无；且 token 重放攻击面新增。
2. **command_guard shutdown 白名单**——被拒绝：白名单一旦泄漏，任何 agent 调 shutdown 都放行，无法回收；command_guard 是"通用命令过滤"层，不应承担"模式感知的关机授权"职责。
3. **watchdog 模式 approval_level 降级**——被拒绝：范围过大，approval_level 降级会让所有命令都更宽松，不只是 shutdown；与 watchdog 模式"仍阻止 CONFIRM 级危险操作"的设计意图冲突（DD-4）。
4. **走 auto_shutdown 模块 + SessionManager.can_shutdown()（本决策）**——采用：agent 不持有 shutdown 权限，专用链路 + 专用权限校验，fail-closed 兜底；auto_shutdown 已有完整实现（6 种条件 + 延迟复检 + 60s 倒计时 + `shutdown /a` 取消）。

## Consequences

**正面**：
- agent 在整个链路中不持有任何 shutdown 权限，从根本上消除 agent 误调 shutdown 的风险。
- `can_shutdown()` 双重校验（mode + flag）防止单点失误（normal 模式即使 flag 误设也不放行）。
- SessionManager 不可用时 fail-closed，与 ADR-0012 纯内存模型一致——重启后无授权 = 无关机权限。
- 复用 auto_shutdown 现有 60s 倒计时 + `shutdown /a` 中止机制，用户有最后救命窗口。

**负面**：
- agent 必须通过 HTTP 端点 `/auto_shutdown/trigger` 触发关机，比直接调 `exec_cmd shutdown` 多一跳——可接受的代价，因安全收益远大于性能损失。
- `auto_shutdown` 模块有 2 个已知 bug（R1 后端重启孤儿进程 / R2 time_reached 立即关机），需另开会话修复后才能完全稳定——但不影响本决策的方向（bug 修复 vs 架构选择是两件事）。

**回退路径**：若 `auto_shutdown` 模块长期不稳定，可临时把 `can_shutdown()` 永远返回 False（关闭关机预授权），用户改为任务完成后手动关机——agent 仍不持有 shutdown 权限，安全模型不变，仅损失"自动关机"便利。

## References

- SDD 来源：`temp/sdd/watchdog-mode/design-decisions.md` DD-2（关机命令预授权方式）
- 关键代码：
  - [server/auto_shutdown.py](file:///<project_root>/server/auto_shutdown.py)（`trigger_shutdown` 调 `can_shutdown()` L160-L204；fail-closed 分支 L192-L197）
  - [server/screen/session/manager.py](file:///<project_root>/server/screen/session/manager.py)（`can_shutdown()` 双重校验 L234-L242；`shutdown_permitted` 字段 L72）
- 相关 ADR：ADR-0012（SessionManager 纯内存模型，本决策的 `can_shutdown()` 依赖此模型——重启后无授权 = 无关机权限）
