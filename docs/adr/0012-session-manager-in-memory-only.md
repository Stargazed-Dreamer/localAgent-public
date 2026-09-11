# 会话管理层纯内存不持久化（后端重启重置为无权限）

SessionManager 的授权状态（mode / `shutdown_permitted` / idle 计时等）纯内存，后端重启后一律重置为 NO_PERMISSION，agent 必须重新走 `screen_request_control` 请求授权。这是 computer use 场景下"重启 = 用户重新在场"的语义假设，避免持久化让"上次授权但用户已离开"的状态被自动恢复。

## Context

computer use 涉及键鼠控制和关机权限，授权状态本质是"用户在场并允许 agent 操作"的临时凭证。SessionManager 持有三档模式（NO_PERMISSION / NORMAL / WATCHDOG），其中 WATCHDOG 模式还携带 `shutdown_permitted`（允许关机）和最长 10h 硬上限——这是项目里权限粒度最粗、风险最高的状态。

持久化这个状态会引入"幽灵授权"风险：用户睡前授权 watchdog 后端意外重启，agent 醒来后仍持有 watchdog + `shutdown_permitted`，此时用户已不在场，任何 agent 误调都无人在场监督。

后端重启在 LocalAgent 是高频事件：开发期间频繁 reload、`uv run python -m server.main` 日常启停、崩溃自动恢复、command_guard 升级触发重启。如果每次重启都需要"用户在场重新授权"，本身就是日常体验的一部分；如果持久化则把"开发期便利"和"授权态延续"绑死，得不偿失。

## Decision

SessionManager 状态纯内存，无任何 load/save 持久化逻辑（`manager.py` 中不存在 `persist` / `load` / `save` / `json.dump` / `sqlite` 等关键字）：

- 后端启动 → `SessionManager` 初始化为 NO_PERMISSION（`shutdown_permitted=False`，无 mode）
- 后端重启 → 内存清空，回到 NO_PERMISSION
- agent 调副作用端点 → 403 + 提示"先调 `screen_request_control` 请求授权"
- 用户重新授权 → 走 GUI 弹窗 → SessionManager 写入新状态
- watchdog 时长（1-999h，默认 10）不保留，重新授权时由用户重新输入

`/auto_shutdown/trigger` 调 `SessionManager.can_shutdown()` 校验，SessionManager 不可用（含启动早期、未授权、单例未初始化）时 fail-closed 返回 403，不依赖任何持久化的"上次授权"。

## Considered Options

1. **持久化授权状态（重启后保留模式 + 剩余时长）**——被拒绝：computer use 授权本质是"用户在场证明"，重启场景下用户大概率已离开，自动恢复 = 幽灵授权；watchdog + `shutdown_permitted` 自动恢复尤其危险。
2. **持久化但重启后降级到 NORMAL**——被拒绝：NORMAL 仍允许键鼠操作，幽灵授权风险只是降级未消除；且"持久化 + 降级"双重语义让状态机更复杂，调试困难。
3. **持久化授权但 watchdog 时长不保留**——被拒绝：保留授权但不保留时长会让 watchdog 模式无限期延续（`max_duration` 检查失效），比纯持久化更危险。
4. **纯内存重启重置（本决策）**——采用：重启 = 强制用户重新在场，符合 computer use 的安全默认态；与 `auto_shutdown.trigger_count` 一致（也纯内存不持久化），整个 computer use 子系统遵循同一原则。

## Consequences

**正面**：
- 重启天然成为"安全 reset"，无需任何显式清场逻辑。
- SessionManager 无磁盘 I/O，启动快、状态机简单、测试无需 mock 文件系统。
- 与 auto_shutdown 模块"`trigger_count` 纯内存"原则一致，整个 computer use 子系统对重启行为统一可预测。

**负面**：
- 开发期频繁重启会打断 agent 的长任务（重启后需重新授权才能继续副作用操作）——这是有意代价，agent 可重新请求授权续跑。
- watchdog 长任务（如 8h 监控）被意外重启打断后无法自动续期，需用户重新勾选时长——可接受的代价，因 watchdog 的设计意图是"用户在场监督"。

**回退路径**：若未来需要"长任务跨重启续期"，可在 SessionManager 加一个独立的 `--persist-session` flag（默认关闭），仅 watchdog 模式可选持久化 mode / `shutdown_permitted`（仍不持久化 idle 计时）。但需配套"重启后弹窗二次确认"机制避免幽灵授权。

## References

- SDD 来源：`temp/sdd/computer-use-session-refactor/00-context-and-decisions.md` 决策 11
- 关键代码：
  - [server/screen/session/manager.py](file:///<project_root>/server/screen/session/manager.py)（`class SessionManager` L28；`shutdown_permitted` 默认 False L72；无 load/save 方法）
  - [server/auto_shutdown.py](file:///<project_root>/server/auto_shutdown.py)（`trigger_count` 也纯内存不持久化 L13；`trigger_shutdown` 调 `can_shutdown()` fail-closed L192-L197）
- 相关 ADR：ADR-0015（watchdog 关机预授权走 auto_shutdown 模块，依赖本决策的纯内存模型——重启后无授权 = 无关机权限）
