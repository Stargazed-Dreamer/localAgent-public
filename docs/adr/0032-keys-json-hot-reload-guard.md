# keys.json 热重载守护：轮询 mtime + 在途让位，而非文件监听或手动 reload

入站网关 0.43.0 上线后，编辑 `data/llm/keys.json`（例如给某把出站 key 的 `allowed_uses` 补 `"inbound_gateway"`）不会自动生效——LLM 池只在后端启动时 `load_provider_keys()` 读一次。忘了手动 `POST /llm/pool/init`，`use_case_eligible("inbound_gateway")` 会对所有 key 判 False，入站每次调用被白名单挡光、返回 `no available keys`，外观像"功能坏了"。现新增 `server/core/keys_watch.py`：后台 asyncio 任务以 20s 周期轮询 keys.json 的 mtime，变更即重建全局池，并在启动/重载后自检 `inbound_gateway` 授权覆盖，为 0 时打醒目告警直指根因。

## Status

accepted (2026-09-02)

## Context

`server/llm_pool/` 的池是进程内单例（`init_pool`/`get_pool`/`is_initialized`），启动时从 keys.json 一次性构建，**没有文件监听**。这在 2026-09-01 造成一次实际故障：后端 21:12 启动、keys.json 21:19 被编辑补 `inbound_gateway` 白名单，内存里仍是旧 keys，调用端持续 `no available keys`。当时靠手动 `POST /llm/pool/init` 恢复——但这是靠人记得做，下一次还会踩。

需要决定"如何让 keys.json 编辑可靠地、低维护地生效"。约束：`server/llm_pool/` 子树按 [ADR-0031](0031-inbound-gateway-own-sqlite-stats.md) 决策#5 被视为敏感只读区，热重载逻辑不应侵入池内部；重建池不能打断正在流式传输的请求。

## Decision

1. **新增独立守护模块 `server/core/keys_watch.py`**，不放进 `server/llm_pool/`，只用其**公开 API**（`load_provider_keys` / `init_pool` / `_load_default_policy` / `_resolve_default_stats_file`）重建单例。池子树对热重载零改动。
2. **轮询 mtime 而非文件事件**：`watch_keys_json_loop()` 默认每 `DEFAULT_INTERVAL=20.0s` 做一次 `stat().st_mtime` 比对，变了才重建。单文件、低频、成本可忽略。
3. **在途调用让位**：重建前查 `get_pool().get_status()["current_active"]`，有在途调用则本 tick 跳过、下个 tick 再试，最多连续让位 `MAX_INFLIGHT_DEFER_TICKS=15`（≈5 分钟）后强制重载，防止被一条永不结束的流饿死配置更新。
4. **fail-safe**：解析出 0 个 key / stat 失败 / tick 内任意异常 → 保持旧池、只推进基准，绝不因坏配置把好池拆掉。
5. **首 tick 只记基准不重载**：监听启动时 `_ensure_pool_initialized` 刚按当前文件建好池，首 tick 记基准即可，避免启动瞬间误判。
6. **use_case 覆盖自检 `warn_use_case_coverage()`**：启动时 + 每次重载后，遍历 `USE_CASE_REGISTRY` 统计每个 use_case 有多少 key 授权；`inbound_gateway` 授权数为 0 时打 ⚠️ 日志，把"这是配置错不是代码坏"直接指出。核心决策逻辑抽成可单测的 `check_keys_changed_and_reload()`（不依赖 sleep）。
7. **生命周期接线**：`server/core/lifecycle.py` startup 建好池后 `create_task(watch_keys_json_loop())` 并存 `app.state.keys_watch_task`；shutdown 取消该任务。

## Considered Options

1. **保持现状，靠人手动 reload（被拒）**——正是本次故障根因。"记得重载"是不可靠的隐性契约，每次改 keys 都要人肉执行一步运维动作。
2. **文件监听库（watchdog / 系统级 inotify/ReadDirectoryChangesW）（被拒）**——单个 20KB 配置文件、变更频率以"天"计，引入一个后台监听线程 + 新依赖 + 去抖动逻辑是过重；轮询一次 stat 的代价可忽略，且轮询天然幂等、崩溃自愈（下个 tick 补上）。
3. **每次入站请求前比对 mtime / 请求触发式 reload（被拒）**——把重载判断塞进请求热路径，给 `router.py` 乃至池调用链增加每请求开销与并发重建竞态；后台低频轮询把成本移出热路径。
4. **在 `pool.py` 内部自监听（被拒）**——违反 ADR-0031 只读边界，把"配置守护"职责混进"调度"职责，且流式打断处理要在池锁内做，风险面大。

## Consequences

**正面**：
- 改 keys.json 最多 20s 自动生效，消除"编辑不生效"这一反复踩坑的 footgun；`POST /llm/pool/init` 降级为可选的即时手动触发。
- 启动自检 + 重载自检让"没有任何 key 放行 inbound_gateway"这类配置错误第一时间在日志里喊出来，而非伪装成功能损坏。
- 热重载职责与池子树解耦，池的调度行为零回归。

**负面 / 注意**：
- 多一层后台 asyncio 任务；在途让位意味着极端情况下（持续高并发流）配置生效最多延迟约 5 分钟后强制重载——刻意取舍，避免"永不生效"和"打断在途请求"两个极端。
- 轮询有最长 20s 生效延迟；要求"改完立即生效"的场景仍需 `POST /llm/pool/init`。
- 重载会整体 `init_pool` 替换单例引用；依赖"跨重载持有同一池对象"的调用方需注意（现状无此类持有者）。

## References

- 实现：[server/core/keys_watch.py](file:///<project_root>/server/core/keys_watch.py)（守护 + 自检），[server/core/lifecycle.py](file:///<project_root>/server/core/lifecycle.py)（startup/shutdown 接线）
- 触发故障：入站网关 `use_case=inbound_gateway` 白名单 + 池仅启动时加载（`server/llm_pool/` 单例）
- 相关决策：[ADR-0031](0031-inbound-gateway-own-sqlite-stats.md)（入站网关统计自持；其决策#5"池子树只读"边界亦由本轮流式透明化以纯增量方式有限放宽，见 0031 修订）
- 测试：[tests/server_endpoints/test_keys_watch.py](file:///<project_root>/tests/server_endpoints/test_keys_watch.py)（`check_keys_changed_and_reload` 各分支）
- CHANGELOG `[Unreleased]` Added「keys.json 热重载守护」条
