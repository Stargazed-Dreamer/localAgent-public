# Auto-Scoring Routing（6 因子加权评分路由）

## Status

accepted (2026-07-22)

## Context

`LLMPool._acquire` 自 v1 起使用 round-robin 在所有可用 key 间公平轮询。这保证了流量均匀分布，但忽略了 key 之间的真实差异：一个 429 频繁、p95 延迟 8s、quota 已耗 80% 的 key，与一个成功率 100%、p95 200ms、quota 余量充足的 key，在 round-robin 下会获得相同调用机会。

v12/v13 引入了 saturation 回流、多维配额跟踪、p50/p95/p99 延迟分位、Provider 级熔断器等"信号源"，但只用于 `available_now` 过滤（淘汰明显不可用的 key）和等待时排序，未参与主选路决策。

## Decision

新增 `strategy="auto"` 评分路由模式（默认仍为 `round_robin`，用户显式开启 auto），用 6 因子加权评分替代 round-robin 主选路：

| 因子 | 权重 | 数据源 | 方向 |
|------|------|--------|------|
| health | 0.25 | `KeyStats` 近 N 次成功率 | 越高越好 |
| quota_remaining | 0.25 | saturation + 多维配额 | 越高越好 |
| latency_p95_inv | 0.20 | `KeyStats.latency_samples` p95 倒数 | 越高越好（延迟低） |
| cost_inv | 0.10 | tier 1-5 反比映射（tier 1=1.0, tier 5=0.2） | 越高越好（便宜） |
| tier_match | 0.10 | USE_CASE_REGISTRY 的 tier 范围是否命中 | 命中=1.0 / 不命中=0.0 |
| lkgp_bonus | 0.10 | session_id 粘性命中 | 命中=1.0 / 不命中=0.0 |

精简自 OmniRoute 12 因子版本（去掉了 `caveman_state` / `provider_health` / `retry_count_remaining` / `request_priority` / `cache_hit` / `semantic_similarity` 等本地不适用或暂未实现的因子）。

**分层保留**：auto 模式仍遵守 v12/v13 的分层限制——OPEN 熔断器 / expired / cooldown / model lockout / max_concurrency 满 → 直接过滤；评分只在"已通过基础过滤的可用 key"间做加权选择。HALF_OPEN 探测槽位也仍由 `CircuitBreaker.allow_request` 原子占用，评分不影响。

## Considered Options

1. **保留 round-robin，仅做权重倾斜**（在现有 `available_now` + 等待排序中加权重）——被拒绝：仅影响"等待时选谁"，主路径仍轮询，无法实现"健康 key 优先"的核心目标。
2. **复制 OmniRoute 完整 12 因子**——被拒绝：本地缺 `cache_hit`/`semantic_similarity` 等数据源；`retry_count_remaining` 与 `LLMPool.call()` 的重试逻辑重叠；`caveman_state` 与压缩模块耦合度过高，调试困难。
3. **完全替换 round-robin，强制 auto**——被拒绝：round-robin 在低数据量启动期（新池、冷启动）更稳健，且部分用户场景需要"流量均匀分摊避免单 key 被封"。默认保留 round_robin，auto 作为可选策略。
4. **6 因子精简版**（本决策）——采用：覆盖健康/配额/延迟/成本/tier/会话 6 个核心维度，数据源全部已在 v13 中可用，无需额外采集。

## Consequences

**正面**：
- 健康 key 优先承担流量，整体成功率与延迟改善（预期 A/B 对比：auto 模式成功率 ≥ round_robin，p95 延迟降低 30%+）。
- 6 因子全部来自 v13 已有数据源，零额外采集开销。
- 默认 `round_robin` 保留向后兼容，用户可平滑切换。

**负面**：
- 流量向高评分 key 集中，可能加速单 key 配额耗尽——但 `quota_remaining` 因子会自动降权已耗 80% 的 key，形成自反馈。
- 评分计算有轻微 CPU 开销（6 个字段读 + 加权求和），实测 < 0.1ms / key / 选择，可忽略。
- 切换策略后用户可能对"为什么某些 key 调用多某些少"产生疑问——通过 `get_status` 暴露 `last_score` 字段供监控。

**回退路径**：将 `strategy` 配置改回 `"round_robin"` 即可立即回退，无 schema 变更，无数据丢失。

## References

- OmniRoute 路线图：`docs/llm-pool-omniroute-roadmap.md` Phase 3.3（注：该路线图已于 2026-07-31 删除，v14 完成情况详见 `docs/llm-pool.md`）
- 数据源依赖：v12 saturation / model lockout / LKGP；v13 quota / latency_p95 / circuit breaker
- 评估标准：`.agents/skills/dev/domain-modeling/ADR-FORMAT.md`（3/3 通过：Hard to reverse / Surprising / Real trade-off）
