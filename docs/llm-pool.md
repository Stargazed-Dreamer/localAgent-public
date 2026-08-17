# LLM 并发池架构
> 本文档是 `AGENTS.md` 中 LLM 并发池的详细展开。

## LLM 并发池

`server/llm_pool/` 包提供多 key 并发管理，可被后端、独立脚本、批处理工具统一导入。

### 核心能力

- **多 key 并发**：每个 key 独立并发限制（默认 3），round-robin 选择，429 自动冷却
- **per-project token 统计**：每次调用按 project 标签记录 prompt/completion/total tokens，持久化到 JSON
- **key 定期健康检查**：每 7 天检查一次，连续失败 3 次自动清除
- **余额耗尽自动剔除**：402/401 状态码自动标记 key 为 expired
- **后端统一管理（拉链排队）**：所有脚本通过 HTTP 代理调用后端共享池，多来源任务自动交错调度

### 后端统一管理架构（拉链排队）

```
脚本A (20并发) ─┐
脚本B (30并发) ─┤── HTTP ──→ 后端 /llm/pool/call ──→ 共享 LLMPool (N keys × max_concurrency 总并发)
脚本C (N并发)  ─┘                                        ↓
                                                    round-robin 交错调度
                                                    某脚本完成后自动释放槽位
```

> 实际 key 数和并发上限取决于 `data/llm/keys.json` 中的可用记录数和每条记录的 `max_concurrency`，无硬编码。

- **所有脚本共享同一个池**：不再各自初始化独立池，避免 key 并发浪费
- **自动交错调度**：pool 的 round-robin + per-key 并发限制自然交错所有来源的请求
- **动态分配**：某脚本完成后，其请求释放 key 槽位，其他脚本的待处理请求自动获得更多并发
- **后端启动时自动初始化池**：无需手动调用 `/llm/pool/init`
- **线程池扩容**：startup 时将 anyio 线程池上限从 40 提高到 200，支持大量并发代理调用

### API 端点

| 路径 | 方法 | operation_id | 说明 |
|------|------|-------------|------|
| `/llm/pool/status` | GET | `llm_pool_status` | 池状态（含 per-key 详情、per-model 详情、is_free/is_available 标识、近期调用数、**v11：keys[].protocol + models[].display_name**）；参数：`summary=true` 精简输出、`include_recent_calls=true` 附带调用历史、`recent_calls_limit`/`only_failed_calls` 过滤 |
| `/llm/pool/models` | GET | `llm_pool_models` | **v10/v11**：跨 key 聚合 per-model 统计（tier/is_free/keys/success_rate/total_attempts/available_now/last_used_str/**display_name**，按总调用次数倒序） |
| `/llm/pool/recent-calls` | GET | `llm_pool_recent_calls` | **v10**：近期调用历史（环形缓冲上限 200 条）；参数：`limit`/`model`/`key_name`/`only_failed` |
| `/llm/pool/init` | POST | `llm_pool_init` | 初始化池（从 `data/llm/keys.json` v11 加载含 `models: list` + `protocol` 字段 + 加载 policy） |
| `/llm/pool/call` | POST | `llm_pool_call` | **代理调用 LLM（完整版，返回 content+usage），支持 `model_tier`** |
| `/llm/pool/call-simple` | POST | `llm_pool_call_simple` | **代理调用 LLM（简化版，返回文本），支持 `model_tier`** |
| `/llm/pool/stats` | GET | `llm_pool_stats` | per-project token 统计（合并所有 stats 文件） |
| `/llm/pool/health-check` | POST | `llm_pool_health_check` | key 健康检查（`cleanup=false` 默认，测试所有 key 可用性） |
| `/llm/pool/cleanup` | POST | `llm_pool_cleanup` | 物理删除 `works=false` 的 key（自动备份到 `<path>.cleanup-backup-<ts>`） |
| `/llm/pool/health-status` | GET | `llm_pool_health_status` | 健康检查状态（不执行检查，返回各 key 的 fail_count/works） |

> **v10 监控能力**：`/llm/pool/models` 和 `/llm/pool/recent-calls` 已加入 `GATEWAY_EXCLUDE`（监控面板专用，不进 MCP），agent 用 `/health.llm_pool` 获取聚合状态。`/health.agent.models` 区分 configured vs available，key 冷却时不再让所有 model 从展示中消失。`LLMKey.is_free` 在 `__post_init__` 中基于 model 名 + privacy_warning 推断（关键词：`free`/`免费`/`trial`/`试用`），用于"免费服务可用性统计"。`_recent_calls: deque[CallRecord]` 环形缓冲（200 条）由 `self.lock` 保护线程安全，记录每次调用的 ts/key_name/model/success/tokens/error/duration_ms。
>
> **v11 协议分发 + 展示名**：`LLMKey` 新增 `protocol: str = "openai"` 字段决定后端如何构造请求——`_call_openai()`（原 OpenAI 兼容协议）或 `_call_anthropic()`（`POST /v1/messages` + `x-api-key` + `anthropic-version: 2023-06-01`，system 提取到顶层 `system` 字段、messages content 转换为 `[{"type":"text","text":...}]` 数组、`usage.{input_tokens,output_tokens}` 转换回 OpenAI 风格 `{prompt_tokens,completion_tokens,total_tokens}`）。每个 model 新增 `display_name` 字段（可选），UI 展示用，缺失 fallback 到 `name`（连接名）；`/llm/pool/status` 和 `/llm/pool/models` 响应中每个 key 含 `protocol`、每个 model 含 `display_name`。

#### `model_tier` 参数（v8）

`/llm/pool/call` 和 `/llm/pool/call-simple` 接受可选的 `model_tier` 字段：

- 取值：`1` / `2` / `3` / `4` / `5`（数字 tier，由 keys.json 中每个 model 的 `tier` 字段定义）
- 向后兼容：旧文本值 `"cheap"` / `"default"` / `"powerful"` 仍接受（映射为 2/3/5，见 `_LEGACY_TIER_MAP`）
- 行为：显式传单个 tier 时精确匹配；内部也接受 `[min,max]` 范围并优先选择范围内更低 tier。model 名仍是软偏好，不可用时 fallback 到范围内其他 model
- 不传时：pool 使用 `USE_CASE_REGISTRY` 中 use case 的 `default_tier` 范围

#### 调用路径差异（重要）

- **经池调用**：`/llm/pool/call*`、后端内部 `call_llm*`、外部脚本 `call_via_backend*` → 进 LLMPool，享受多 key 并发/重试/冷却/token 统计
- **不经池直连**：`agent_chat` / `agent_score` → 用 `resolve_key(use_case, model_tier)` 路由层直接选 key 发请求，不进池。原因：这些端点对单次调用延迟敏感，且需要按 `use_case`/`allowed_uses`/`privacy_warning` 做细粒度筛选，与 pool 的 round-robin 调度模型不匹配。`resolve_key` 是 v5 引入的统一路由层（见下方"v5 Schema 与路由层"小节），替代了 v4 时代的 3 条并行路径（`get_key_for_use` / LLMPool / VL）。sensitive/allowed_uses/scope 过滤在路由层统一完成
- **v15 统一改造**：`memory_compress` / `memory_validate` 已从"不经池直连"迁移到走 `pool.call()` / `pool.call_simple()`，统一享受 v15 model 级降级 / cooldown / 429 不计 consecutive_fails 等保护机制。详见下方"v15 Model 级降级"章节

### 使用方式

#### 方式一：通过后端代理（推荐，支持拉链排队）

```python
# 脚本端 - 通过 HTTP 代理调用后端共享池
from server.llm_pool import call_via_backend, call_via_backend_full, check_backend_pool

# 启动时检查后端池就绪（池未初始化会自动触发初始化）
if not check_backend_pool():
    raise SystemExit("后端不可用，请先启动: start.bat")

# 简单调用（返回文本或 None）
result = call_via_backend("分析代码", project="我的项目", max_tokens=8192)

# 完整调用（返回 dict 含 usage/key_name）
r = call_via_backend_full(
    [{"role": "user", "content": "hello"}],
    project="我的项目", max_tokens=4096
)
if r["ok"]:
    print(r["content"], r["usage"])
```

#### 方式二：直接导入池（仅后端内部使用）

```python
# 后端内部直接调用（不经 HTTP）
from server.llm_pool import get_pool, call_llm_simple

result = call_llm_simple("请回复OK", project="我的项目")
```

### Key 健康检查

- **自动提醒**：`load_keys_from_tested_file()` 加载时如果超过 7 天未检查，会打印提醒
- **手动触发**：`POST /llm/pool/health-check`（默认 `cleanup=false`，仅测试不删除）或 `check_keys_health("data/llm/keys.json")`
- **清理**：连续失败 3 次的 key 标记为 `status.works=false`；调用 `POST /llm/pool/cleanup` 或 `cleanup_expired_keys("data/llm/keys.json", backup=True)` 物理删除（删除前自动备份到 `<path>.cleanup-backup-<ts>`）
- **key 明文存储**：`data/llm/keys.json` 中 `key` 字段为明文（本地项目 + 公开分享的 key；文件已 gitignore）

### v15 Model 级降级

v15 引入 **model 级降级状态机**，解决"model 一直不可用但仍被反复选中"的问题。在 v12 的 per-model cooldown（429 单点冻结）和 v13 的 provider 级 CircuitBreaker 之上，新增一层 **基于连续失败次数的 model 自动 disabled + 探针恢复** 机制，让持续失败的 model 退出 round-robin 选池，避免持续拖累延迟与成功率。

#### 三层独立熔断（v12 + v13 + v15）

| 层级 | 触发条件 | 持续时间 | 恢复方式 | 字段 |
|------|----------|----------|----------|------|
| **per-model cooldown**（v12） | 单次 429 | 短（fallback 60s + Retry-After） | TTL 到期自动恢复 | `LLMKey.model_cooldowns[model] = cooldown_until` |
| **model disabled**（v15 新增） | 连续失败 N 次（默认 5） | 长（默认 300s 探针间隔） | 定时探针成功后解除 | `LLMKey.model_disabled[model] = {disabled_at, last_probe_at, probe_in_flight}` |
| **provider CircuitBreaker**（v13） | 同 base_url 连续失败 N 次 | OPEN（默认 60s）→ HALF_OPEN → CLOSED | 探针连续成功 M 次 | `LLMPool._breakers[base_url]` |

三者独立：单 model 429 只冻结该 model；连续失败升级为 disabled；provider 级故障触发 CircuitBreaker 冻结该 base_url 下所有 key。

#### 错误类型分级退避

`_apply_model_health_failure()` 按错误类型决定 base cooldown，**指数退避**（`base * 2^attempt`）封顶 60s：

| 错误类型 | base 秒数 | 推断条件 | 配置项 |
|----------|-----------|----------|--------|
| `timeout` | 5.0 | error 含 "timeout" | `cooldown_base_timeout` |
| `http_5xx` | 10.0 | error 含 "HTTP 5" / "HTTP 500" / "502" / "503" | `cooldown_base_http_5xx` |
| `http_other` | 10.0 | error 含 "HTTP"（非 5xx） | `cooldown_base_http_other` |
| `empty_content` | 15.0 | error 含 "empty content"（200 但响应空） | `cooldown_base_empty_content` |
| `exception` | 20.0 | 其他异常（TypeError / JSON 解析失败等） | `cooldown_base_exception` |

退避公式：`cooldown = min(base * 2^attempt, cooldown_max)`，其中 `attempt = consecutive_fails`（本次失败前的累计值）。首次失败 `base*1`，第二次 `base*2`，第三次 `base*4`...封顶 60s。

#### 429 与 401/402 不计 consecutive_fails

| 错误 | 计 consecutive_fails？ | 处理 |
|------|----------------------|------|
| 429（rate_limited） | ❌ 不计 | 仅写 `model_cooldowns`（短 cooldown），不动 disabled 状态 |
| 401/402（expired） | ❌ 不计 | 标记 `key.stats.expired = True`，整个 key 退出选池 |
| timeout / HTTP 5xx / HTTP 其他 / 空响应 / 异常 | ✅ 计 | 写 `model_cooldowns` + `consecutive_fails += 1`，达阈值则 disabled |

设计理由：429 是上游主动限流，退避后即可恢复，不应升级为 disabled；401/402 是 key 失效不可恢复，单独标记 expired 即可；只有持续异常（连接问题 / 上游故障 / 响应损坏）才应升级为 model disabled。

#### 探针恢复机制

disabled model 通过定时探针恢复：

1. `LLMKey.try_acquire_probe(model, probe_interval_seconds, probe_max_concurrency)` 检查：
   - 距 `last_probe_at` ≥ `probe_interval_seconds`（默认 300s）
   - 当前 in-flight 探针数 < `probe_max_concurrency`（默认 1，避免瞬间多请求打上游）
2. 满足条件时 `probe_in_flight = True`，调用方让该 model 被选中一次（探针调用）
3. 探针结束调 `LLMKey.resolve_probe(model, success)`：
   - **成功** → 移除 disabled 标记 + `consecutive_fails = 0`，model 重新进入选池
   - **失败** → 保留 disabled，`probe_in_flight = False`，等待下个探针窗口

#### A+C 双保险（避免选中已 disabled 的 model）

| 保险 | 位置 | 作用 |
|------|------|------|
| **A 保险** | `LLMPool._acquire` | round-robin 时跳过 tier 范围内无可用 model（含 disabled + cooldown）的 key，避免选到"tier 匹配 model 全不可用"的 key |
| **C 保险** | `LLMPool.call` 的 `req_model` 选择 | `is_model_available(model)` 二次过滤，排除 disabled / cooldown 中的 model；**v16：tier 约束激活时范围内无可用 model 不降级到非 tier 范围 model**（修复 tier 泄漏：旧版 `_first_available` fallback 不看 tier，导致 tier 3 model 被 429 时降级到 tier 1） |

两层独立兜底，确保即使 round-robin 选中了 key，也不会调用已 disabled 的具体 model。v16 起 A 保险也检查 `model_cooldowns`（429 冷却），C 保险的 fallback 链在 tier 约束激活时不再降级到非 tier 范围 model。

#### 配置项（`config.toml [llm.model_health]`）

```toml
[llm.model_health]
enabled = true                          # 总开关，false 时禁用 model_disabled 机制（仍保留 429 的 model_cooldown）
consecutive_fail_threshold = 5          # 连续失败 N 次触发 model_disabled
probe_interval_seconds = 300.0          # disabled 后探针间隔（秒，固定间隔）
probe_max_concurrency = 1               # 探针并发上限（通常 1，避免瞬间多请求打上游）
cooldown_base_timeout = 5.0             # timeout 错误的首次退避秒数
cooldown_base_http_5xx = 10.0           # HTTP 500/502/503 的首次退避秒数
cooldown_base_http_other = 10.0         # 其他非 200 HTTP 的首次退避秒数
cooldown_base_empty_content = 15.0      # 200 但 content 空的首次退避秒数
cooldown_base_exception = 20.0          # 其他异常的首次退避秒数
cooldown_max = 60.0                     # 退避封顶秒数（指数递增 base*2^attempt 后封顶）
```

Fail-Open：配置加载失败时用默认值（不阻断主流程）。

#### 监控字段（`/llm/pool/status` + `/health.llm_pool`）

`/llm/pool/status` 的 `keys[].models[]` 每条 model 含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `disabled` | bool | 是否被 disabled（v15） |
| `in_model_cooldown` | bool | 是否在 model 级 cooldown 中（v12） |
| `probe_in_flight` | bool | 是否是探针调用 |
| `consecutive_fails` | int | 当前连续失败次数（不计 429/401/402） |
| `disabled_info` | dict \| null | `{disabled_at, last_probe_at, probe_in_flight}`，仅 disabled 时非空 |

`/llm/pool/models` 跨 key 聚合含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `disabled_count` | int | 该 model 在多少个 key 上被 disabled |
| `probe_in_flight_count` | int | 该 model 当前有多少探针在飞 |
| `consecutive_fails_max` | int | 跨 key 的最大 consecutive_fails |

`/health.llm_pool` 含 `model_health_disabled_count`（全局被 disabled 的 model 数），用于监控面板顶部摘要。

#### memory 调用统一走 pool（v15）

`memory_compress` / `memory_validate` 从 v14 时代的"不经池直连"迁移到走 `pool.call()` / `pool.call_simple()`：

- `server/memory/compress.py::_llm_compress()` → `pool.call_simple(use_case="memory_compress", retries=1)`
- `server/memory/maintainer.py::_call_llm_validate_with_429_flag()` → `pool.call(use_case="memory_validate", retries=1)`

统一享受 v15 model 级降级 / cooldown / 429 不计 consecutive_fails 等保护。memory 模块保留 `consecutive_429` 批量熔断逻辑（连续 N 条记忆都 429 时跳过剩余，避免无谓重试），但单条调用的重试 / 退避 / disabled 全部由 pool 内部处理。

### Token 统计

```bash
# 查看统计
uv run python tools/llm/llm_token_stats.py

# 持续刷新
uv run python tools/llm/llm_token_stats.py --watch

# 从 checkpoint 导入历史数据
uv run python tools/llm/llm_token_stats.py --import
```

后端统一管理后，所有调用都写入 `data/llm/stats/llm_pool_stats.json`。历史 per-script 文件（`llm_pool_stats_comment.json`、`llm_pool_stats_lrc.json`、`llm_pool_stats_posts.json`）仍会被 `/llm/pool/stats` 合并统计。

### 统一 Key 库（`data/llm/keys.json`，schema v11）

所有 LLM/VL/AIGC key 统一存储在 `data/llm/keys.json`（schema v11），不再分散在 `mimo_keys_tested.json` / `extra_llm_keys.json` / `OnlyKey.txt` 等多文件中。

- **v5 核心变化**：单条 record 含 `models: list[str]`，一个物理 key 支持多模型，避免 v4 时代同一 key 被拆为多条 record。同时引入 `default_model` 字段标记默认模型，删除 `vendor`/`group`/`model` 字段。
- **v8 核心变化**：`models` 从 `list[str]` 升级为 `list[dict]`，每个 model 对象含 `name`/`scope`/`limits`/`tier`/`enabled` 字段。**tier (1-5)** 取代旧的 `config.toml [llm.models.*]` 段；`default_tier` 使用 `[min,max]` 范围，优先范围内更低 tier。模型可独立禁用，Key 的 `enabled` 仍是总开关。
- **v9 核心变化**：VL provider 参数（`max_concurrency`/`timeout`/`rate_limit_cooldown`）从 `config.toml [vision.vl_providers.*]` 段迁移到 keys.json 中含 `vl` scope model 的 key 记录的 `vision` 段。`config.toml [vision]` 段只保留全局开关和编码参数。`vl_default_provider` 字段移除（改由 `use_case` 驱动选 key）。`RemoteVLClient` 不再独立加载 key，统一通过 `key_store.resolve_keys(use_case)` 路由。
- **v10 核心变化**（运行时增强，schema 字段无变化）：`LLMKey` 运行时新增 `model_stats: dict[str, KeyStats]`（per-model 独立统计）+ `is_free: bool`（基于 model 名 + privacy_warning 推断）+ `_recent_calls: deque[CallRecord]` 环形缓冲（200 条）。`pool.get_status()` / `get_models_summary()` / `get_recent_calls()` 三个方法返回监控数据。新增 `/llm/pool/models` 和 `/llm/pool/recent-calls` 端点（已加入 `GATEWAY_EXCLUDE`）。**keys.json 文件无新字段，仅 version 字段保持 v9 不变（v10 是运行时增强，不需要 schema 迁移）**。
- **v11 核心变化**：key 级新增 `protocol: str` 字段（`"openai"` 默认 / `"anthropic"`），后端按协议路由到 `_call_openai()` 或 `_call_anthropic()`（后者用 `POST /v1/messages` + `x-api-key` + `anthropic-version: 2023-06-01`，system 提取到顶层 `system` 字段、messages content 转换为 `[{"type":"text","text":...}]` 数组、`usage.{input_tokens,output_tokens}` 转换回 OpenAI 风格 `{prompt_tokens,completion_tokens,total_tokens}`）；model 级新增 `display_name: str` 字段（可选，UI 展示用，缺失 fallback 到 `name`）。version 从 10 升到 11（迁移幂等：已有 key 补 `protocol: "openai"`，已有 model 不写入 `display_name` 字段直到用户主动编辑）。

#### 支持的 provider

| Provider | base_url | 隐私风险 | 默认并发 | 说明 |
|----------|----------|---------|---------|------|
| opencode | `https://opencode.ai/zen/v1` | **是** | 2 | OpenCode Zen 免费端点，数据可能被用于训练 |
| modelscope | `https://api-inference.modelscope.cn/v1` | 否 | 1 | 魔搭社区，支持 LLM + VL 模型 |

> **可用模型动态变化**：opencode 和 modelscope 的可用模型列表会变化，请前往对应文档查看最新列表：
> - OpenCode Zen：https://opencode.ai/docs/zh-cn/zen
> - ModelScope：https://modelscope.cn/models?filter=inference_type&page=1&tabKey=task

#### VL 模型说明

`Qwen/Qwen3-VL-*` 系列是图像理解模型。LLM 池（`/llm/pool/*`）仅支持文本对话（`/chat/completions`），不处理图像输入。**图像理解已通过独立的远程 VL 模块接入**：
- 模块：`server/vl/remote_vl.py`（`RemoteVLClient` 单例）
- 配置：v9 起，VL provider 配置（`base_url`/`api_key`/`model`/`max_concurrency`/`timeout`/`rate_limit_cooldown`）统一存放在 `data/llm/keys.json` 中含 `vl` scope model 的 key 记录的 `vision` 段；`config.toml [vision]` 段只保留全局开关和编码参数
- 默认模型：`Qwen/Qwen3-VL-235B-A22B-Instruct`（235B MoE，22B 激活，质量最佳）
- 接口：`/ocr/vl/*`（文档解析）+ `/vision/understand`（图像问答）
- Computer Use 使用策略：远程 VL 默认只做布局/状态描述；网页文字用 DOM、桌面文字用本地 OCR bbox，`vision_locate` 仅作无文字元素兜底，避免延迟和配额消耗
- 多 provider 自动 failover，429/5xx 触发 60s cooldown + 退避重试 (2, 4, 8, 16, 32, 60) 秒
- Key 选择：通过 `key_store.resolve_keys(use_case)` 路由（`vl_ocr`/`vl_vision`/`vl_activity_tracker`），与 LLM/AIGC 共用统一路由层
- 设计与历史留档：`.agents/wip/local_vl_decommissioned.md`

#### v5 Schema 与路由层

v5 重设计的核心是 **USE_CASE_REGISTRY + resolve_keys() 路由层**，替代 v4 时代分散在各模块的硬编码 `SENSITIVE_USES` 和 3 条并行调用路径（`get_key_for_use` / LLMPool / VL）。

##### USE_CASE_REGISTRY（17 个 use_case）

> **v15 变更**：废弃 `memory_maintainer`，拆分为 `memory_validate`（验证单条记忆 facts）。`memory_compress` / `memory_validate` 从"不经池直连"迁移到走 `pool.call()` / `pool.call_simple()`，统一享受 v15 model 级降级保护。同时新增 4 个 `stock_advisor_*` use_case（轻量监控+建议，不做交易）。

| use_case | scope | sensitive | default_tier | 典型调用方 |
|----------|-------|-----------|--------------|-----------|
| `agent_chat` | llm | 否 | (3, 5) | `/agent/chat` 路由 |
| `community_summarize` | llm | 否 | (3, 5) | `workspace/community_review/community_llm.py` 社群摘要（脚本调 `/llm/pool/call`） |
| `memory_compress` | llm | **是** | (2, 4) | `server/memory/compress.py` 记忆压缩（**v15 走 `pool.call_simple`**） |
| `memory_validate` | llm | **是** | (2, 4) | `server/memory/maintainer.py` 记忆验证（**v15 新增，走 `pool.call`，替代 `memory_maintainer`**） |
| `hourly_summarize` | llm | 否 | (3, 5) | `server/loop_actions.py` Loop 小时总结 |
| `download_watcher` | llm | **是** | (4, 5) | `tools/file_classifier/predictor.py` 下载文件分类 |
| `command_guard` | llm | 否 | (3, 5) | 命令审批 LLM 预审 |
| `stock_advisor_opening` | llm | 否 | (3, 5) | Stock Advisor 开盘分析（09:35） |
| `stock_advisor_closing` | llm | 否 | (3, 5) | Stock Advisor 收盘总结（15:05） |
| `stock_advisor_evening` | llm | 否 | (4, 5) | Stock Advisor 晚间深度复盘（21:00，含多空辩论） |
| `stock_advisor_query` | llm | 否 | (3, 5) | Stock Advisor 盘中午询（adhoc 即时回答） |
| `vl_ocr` | vl | **是** | (5, 5) | `server/remote_vl.py::ocr()` 文档 VL 识别 |
| `vl_vision` | vl | **是** | (5, 5) | `server/remote_vl.py::understand()/locate()` 图像理解/定位 |
| `vl_activity_tracker` | vl | **是** | (5, 5) | `server/loop_actions.py:163` Loop 活动追踪截图描述 |
| `aigc_image_gen` | aigc_image | 否 | (3, 5) | 文生图/图生图 |
| `aigc_image_edit` | aigc_image | 否 | (3, 5) | 图像编辑 |
| `aigc_video_gen` | aigc_video | 否 | (3, 5) | 视频生成 |

`sensitive=true` 的 use_case 在选 key 时自动跳过带 `privacy_warning` 的 key（如免费 opencode key）。

##### `resolve_key(use_case, model_tier)` 路由层工作流

1. 从 `USE_CASE_REGISTRY` 查 use_case 定义（含 scope/sensitive 标志）
2. 候选 key 过滤：
   - `enabled == True`
   - 只考虑 `models[].enabled != false` 的模型
   - `status.works != False`
   - `scope` 包含 use_case 对应的 scope（llm 或 vl）
   - `allowed_uses` 为空或包含 use_case
   - 若 use_case 为 sensitive：跳过 `privacy_warning` 非空的 key
3. tier 可为单值或 `[min,max]` 范围；范围匹配时优先更低 tier，model 名作为范围内的软偏好
4. 返回 `ResolvedKey` dataclass（含 `key_id`/`api_key`/`base_url`/`model`/`models`/`label`/`privacy_warning`/`scope`/`max_concurrency`/`source`）

> 调用方可直接使用 `from server.llm_pool.key_store import resolve_key` 取代旧的 `get_key_for_use`。

#### 🔒 隐私机制（三维度）

LLM 池的隐私控制由三个不同维度的机制协同工作：

| 维度 | 机制 | 配置位置 | 作用 |
|------|------|----------|------|
| **key 级** | `privacy_warning` | `data/llm/keys.json` record | 标注该 key 是否会泄露数据（如免费模型用于训练）。调用前务必检查 |
| **use_case 级** | `USE_CASE_REGISTRY` 的 `sensitive` 字段 | `server/llm_pool/key_store.py` | 哪些操作算敏感（不该用带 privacy_warning 的 key）。`resolve_key()` / `LLMPool._acquire()` / `RemoteVLClient._pick_provider()` 自动跳过 |
| **provider 级** | `data_safe` | `config.toml [llm.providers.xxx]` | 该 provider 是否允许发送文件内容（用于下载分类 Phase 2 内容增强） |

- key 记录中 `privacy_warning` 字段非空时，`/llm/pool/status` 返回值和 `print_backend_pool_status()` 终端输出中会显示该警告
- **调用前务必检查**：如果任务涉及个人/机密/敏感信息，应避免使用带 `privacy_warning` 的 key

**决策流程**（调用 LLM 时如何判断能否发送敏感数据）：
1. 检查 `use_case` 在 `USE_CASE_REGISTRY` 中是否 `sensitive=true` → 若是，`resolve_key()` 自动跳过带 `privacy_warning` 的 key
2. 检查任务是否涉及文件内容 → 若是，确认 provider 的 `data_safe=true`
3. 手动调用时：`/llm/pool/status` 返回值会显示每个 key 的 `privacy_warning`

> **配置变更**：v5 起，敏感 use_case 配置已迁移到 `server/llm_pool/key_store.py` 的 `USE_CASE_REGISTRY`（程序真源），不再支持通过 `config.toml [llm.privacy] sensitive_uses` 配置。新增敏感 use_case 需修改代码（保证 sensitive 标志与 use_case 语义一致）。

#### key 记录格式（v11 schema）

`data/llm/keys.json` 文件结构：

```json
{
  "version": 11,
  "updated_at": "2026-07-20",
  "keys": [
    {
      "id": "opencode_opencode_dsv4_flash_e2b1c8dd",
      "label": "opencode_dsv4_flash",
      "key": "sk-xxx",
      "base_url": "https://opencode.ai/zen/v1",
      "models": [
        {"name": "deepseek-v4-flash-free", "display_name": "DeepSeek V4 Flash", "scope": ["llm"], "limits": {}, "tier": 3, "enabled": true},
        {"name": "deepseek-v4-chat-free", "scope": ["llm"], "limits": {}, "tier": 3, "enabled": false}
      ],
      "default_model": "deepseek-v4-flash-free",
      "scope": ["llm"],
      "max_concurrency": 2,
      "privacy_warning": "免费模型，数据可能被用于训练，请勿传输个人/机密信息",
      "enabled": true,
      "allowed_uses": [],
      "protocol": "openai",
      "status": {
        "works": true,
        "fail_count": 0,
        "last_health_check": "2026-07-14 00:52:44",
        "last_check_status": "ok",
        "last_check_detail": ""
      }
    },
    {
      "id": "anthropic_anthropic_claude_a1b2c3d4",
      "label": "anthropic_claude",
      "key": "sk-ant-xxx",
      "base_url": "https://api.anthropic.com/v1",
      "models": [
        {"name": "claude-3-5-haiku-20241022", "display_name": "Claude 3.5 Haiku", "scope": ["llm"], "limits": {}, "tier": 4, "enabled": true},
        {"name": "claude-3-5-sonnet-20241022", "display_name": "Claude 3.5 Sonnet", "scope": ["llm"], "limits": {}, "tier": 5, "enabled": true}
      ],
      "default_model": "claude-3-5-haiku-20241022",
      "scope": ["llm"],
      "max_concurrency": 1,
      "privacy_warning": "",
      "enabled": true,
      "allowed_uses": ["agent_chat"],
      "protocol": "anthropic",
      "status": {
        "works": true,
        "fail_count": 0,
        "last_health_check": "",
        "last_check_status": "",
        "last_check_detail": ""
      }
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `id` | 记录唯一 ID（自动生成：`{provider}_{label_slug}_{uuid8}`） |
| `label` | key 显示名 |
| `key` | API key（明文存储，文件已 gitignore） |
| `base_url` | 端点基础 URL（**不含** `/chat/completions` 或 `/messages`，代码会按 `protocol` 自动拼接） |
| `models` | **v8**：模型对象列表，每项含 `name`/`scope`/`limits`/`tier`/`enabled`/`display_name`（v11）。单 key 支持多模型，每个 model 可独立配置 |
| `models[].name` | 模型连接名（传给 API 的 `model` 字段，API 调用时使用） |
| `models[].display_name` | **v11 新增**：模型展示名（UI 显示用）。缺失时 fallback 到 `name`。建议填写易读名称如 `Claude 3.5 Haiku`，与连接名 `claude-3-5-haiku-20241022` 区分 |
| `models[].scope` | 该模型的用途列表（`llm`/`vl`/`aigc_image`/`aigc_video`），覆盖 key 级 `scope` |
| `models[].limits` | 该模型的限流配置（`rpm`/`tpm`/`rpd`），覆盖 key 级 `limits` |
| `models[].tier` | **v8 核心**：模型能力层级 1-5（1=最轻量, 3=默认, 5=最强大）。`USE_CASE_REGISTRY.<use_case>.default_tier` 决定选哪个 tier |
| `models[].enabled` | 模型独立开关；`false` 时不参与 scope、Tier 路由和 LLM 池加载，不影响同一 Key 下其他模型 |
| `default_model` | 默认模型名（`models[].name` 中的某个值）。`LLMPool.call()` model 字段优先级：调用方传入 > `key.default_model` > `key.models[0].name` |
| `scope` | key 级用途列表，含 `llm` 和/或 `vl`（VL-only key 不进 LLM 池） |
| `max_concurrency` | 该 key 最大并发数 |
| `privacy_warning` | 隐私警告文本，空字符串表示无风险 |
| `enabled` | Key 总开关；`false` 时该 Key 下所有模型都跳过，但不改写各模型自己的 `enabled` 状态 |
| `allowed_uses` | 空数组表示不限制；非空时只允许列出的 use_case 使用（如 `["agent_chat", "community_summarize"]`） |
| `protocol` | **v11 新增**：连接协议（`"openai"` 默认 / `"anthropic"`）。决定后端如何构造请求：`openai` 走 `POST /chat/completions` + `Authorization: Bearer`；`anthropic` 走 `POST /v1/messages` + `x-api-key` + `anthropic-version: 2023-06-01`，system 提取到顶层、content 转数组、usage 字段映射。**Anthropic 协议的 key 健康检查**用 `claude-3-5-haiku-20241022` 最小请求测试，200/400/429 视为可连通 |
| `status` | 健康检查状态字典（见下） |
| `status.works` | 是否可用（健康检查会更新；`false` 时加载/cleanup 会跳过/删除） |
| `status.fail_count` | 连续失败次数（成功时重置为 0） |
| `status.last_health_check` | 上次检查时间戳（`YYYY-MM-DD HH:MM:SS`） |
| `status.last_check_status` | 上次检查结果描述（如 `ok` / `fail=2/3` / `REMOVED (fail=3)`） |
| `status.last_check_detail` | 上次失败详情（截断 100 字符） |
| `vision` | **v9 新增**：VL provider 级参数，仅当 key 含 `vl` scope model 时有效。含 `max_concurrency`/`timeout`/`rate_limit_cooldown`。缺失时使用默认值 `(1, 60, 60.0)`。迁移源：`config.toml [vision.vl_providers.*]`（按 `base_url` 匹配） |

> **v5 删除的字段**：`vendor`（用 base_url 区分 provider）、`group`（暂未使用）、`model`（替换为 `models: list` + `default_model`）。
>
> **v8 变更**：`models` 从 `list[str]` 升级为 `list[dict]`；新增 `tier` (1-5) 字段定义 model 能力层级；`config.toml [llm.models.*]` 段废弃。旧文本 tier（`cheap`/`default`/`powerful`）通过 `_LEGACY_TIER_MAP` 向后兼容（映射为 2/3/5）。
>
> **v9 变更**：新增 `vision` 字段（VL provider 级参数）；`config.toml [vision.vl_providers.*]` 段和 `vl_default_provider` 字段移除；`RemoteVLClient` 改用 `key_store.resolve_keys(use_case)` 路由选 key，不再独立加载 key。
>
> **v10 变更**（运行时增强，schema 无变化）：`LLMKey` 运行时新增 `model_stats`/`is_free`/`_recent_calls` 字段，keys.json 文件无新字段，version 仍为 v9。
>
> **v11 变更**：key 级新增 `protocol` 字段（默认 `"openai"`，支持 `"anthropic"`），后端按协议分发到 `_call_openai()` 或 `_call_anthropic()`；model 级新增 `display_name` 字段（可选，UI 展示用，缺失 fallback 到 `name`）。version 从 10 升到 11。**Anthropic 协议的 base_url 应填到 `/v1`**（如 `https://api.anthropic.com/v1`），代码会自动拼接 `/messages`；OpenAI 协议仍填到 `/v1`，代码拼 `/chat/completions`。
>
> **v12 变更**（OmniRoute 设计借鉴）：key 级新增 `pool_key` 字段（共享上游池去重标识，默认空字符串视为独立池）。同一 `pool_key` 的多个 key 视为共享上游配额（如多个 OpenRouter key 访问同一 Anthropic 上游池），`get_status` 的 `total_max_concurrency_deduped` 按 `pool_key` 分组取 `max(max_concurrency)`，`dedup_ratio` 反映真实并发上限与简单相加的比值。**运行时新增** 3 项机制（schema 无新增运行时字段，仅 `pool_key` 入文件）：
> - **Model Lockout**：`LLMKey.model_cooldowns` 字典（model_name → cooldown_until），单 model 429 时只冷却该 model 而不冻结整个 key，同 key 其他 model 仍可用。`_release(rate_limited=True, model="m1")` 会同时写 key 级 cooldown（fallback 信号）和 model 级 cooldown（精细隔离）。
> - **Saturation Reflow**：`LLMKey.saturation` (0-1) 记录上游响应头的饱和信号。`_call_openai/_call_anthropic` 解析 `x-ratelimit-remaining-*`/`anthropic-ratelimit-unified-*-utilization` 写回；429 时自动设为 1.0。`_acquire` 在 round-robin 中优先选 `effective_saturation < 0.7` 的低饱和 key，避免硬 429。saturation 有 30s TTL，过期视为 0。
> - **LKGP 会话粘性**：`call(session_id="xxx")` 传入会话 ID 时，优先复用上次成功的 (key, model)。命中条件：粘性记录未过期（默认 30 分钟）+ key 仍可用 + 该 model 未在 cooldown。失败回退到 round-robin 不阻塞。`rate_limited`/`expired` 自动清粘性，HTTP 500/timeout 不清（保留可能可恢复的会话）。`/llm/pool/session` GET 查看粘性详情，DELETE `{session_id}` 主动清粘性。
>
> version 从 11 升到 12。`pool_key` 仅在非空时写入 keys.json（保持文件简洁），用户通过 `/keys` API 显式设置。
>
> **v13 变更**（OmniRoute 设计借鉴，纯运行时增强，schema 无变化）：4 项机制（详见下方说明）：
> - **Provider 级 Circuit Breaker**：`LLMPool._breakers` 按 `base_url` 维护 `CircuitBreaker` 实例（CLOSED/HALF_OPEN/OPEN 三态）。连续 N 次失败 → OPEN（拒绝该 provider 所有请求），T 秒后转 HALF_OPEN 放探针，探针连续成功 M 次 → CLOSED。`_acquire` 过滤 OPEN 的 key，HALF_OPEN 时 `allow_request` 原子占用探针位。`expired`（401/402）不计失败（key 失效 ≠ provider 故障）。配置：`[llm.circuit_breaker] enabled / fail_threshold=5 / open_seconds=60 / half_open_probe_count=1 / success_threshold=2`。
> - **多维配额跟踪**：`KeyStats.quota_counters` 按 dimension（如 `requests/hour`、`tokens/day`）维护 `SlidingWindowCounter`（deque + sum_cache，O(1) 查询）。每次 `_release` 写入 key 级和 model 级配额。配置：`[llm.quota_tracking] enabled=true / dimensions=["requests/hour","tokens/hour","tokens/day"]`。dimension 格式 `<metric>/<unit>`，unit ∈ {minute, 5min, hour, 6h, 12h, day, week, month}。
> - **p50/p95/p99 延迟分位**：`KeyStats.latency_samples`（deque maxlen=100）记录每次调用延迟，`_release` 传 `duration_ms` 写入。`p50()/p95()/p99()` 用 nearest-rank 计算，无样本返回 None。`get_status` 的 per-key stats 和 per-model stats 均暴露 `latency_p50/p95/p99/sample_count`；`get_models_summary` 含跨 key 聚合分位。
> - **Fail-Open 显式化**：审查所有 try/except，区分必须失败 vs 可容错。见下方 **Fail-Open 策略表**。
>
> **Fail-Open 策略表**（v13）：
>
> | 位置 | 异常类型 | 策略 | 理由 |
> |------|----------|------|------|
> | `_load_stats` | JSON 解析失败 | fail-open（清空 project_stats） | 历史统计丢失不阻断调用 |
> | `_save_stats` | 文件写入失败 | fail-open（pass） | 落盘失败不阻断内存统计 |
> | `_save_stats` | 现有文件解析失败 | fail-open（视为空） | 合并失败不阻断新写入 |
> | `__init__` quota_tracking 配置加载 | 任意异常 | fail-open（禁用配额跟踪） | 监控功能失败不阻断调用 |
> | `__init__` circuit_breaker 配置加载 | 任意异常 | fail-open（禁用熔断器） | 保护机制失败不阻断调用 |
> | `_call_*` Retry-After 解析 | 任意异常 | fail-open（默认 0.0） | header 缺失用默认 cooldown |
> | `_parse_*_saturation` | header 解析失败 | fail-open（返回 0.0） | 未知饱和度视为充裕 |
> | `call()` 业务异常 | 任意异常 | **must-fail**（记失败 + 重试） | 影响业务正确性 |
> | `_call_*` HTTP 4xx/5xx | 非 200 响应 | **must-fail**（记失败 + 重试） | 上游错误需重试 |
> | `_call_*` 429 rate_limited | 429 | **must-fail**（cooldown + 重试） | 限流需退避 |
> | `_call_*` 401/402 expired | 401/402 | **must-fail**（标记 expired） | key 失效不可恢复 |
>
> 原则：监控/统计/落盘类异常 fail-open（仅 warn 不阻断）；业务/调用类异常 must-fail（影响正确性）。
>
> **v14 变更**（OmniRoute 设计借鉴 Phase 3，ADR-0001）：2 项机制（详见 `docs/adr/0001-auto-scoring-routing.md`）：
> - **RTK + Caveman 消息压缩**：`compress_messages(messages, mode, min_length)` 在 `call()` protocol dispatch 前对 messages 应用压缩。4 模式：`off` / `lite`（仅 code block RTK：strip 行号 + dedup stack trace + 截断）/ `standard`（lite + Latin whitespace/标点/短语 + CJK whitespace）/ `aggressive`（standard + CJK 填充短语过滤 + 截断更激进）。**Bloat Protection 两层**：单消息级 + 全局级，保证压缩永不反向增加长度。`CompressionStats` 暴露 `applied/mode/original_chars/compressed_chars/rules_applied + ratio/saved_chars`。配置：`[llm.compression] enabled=false / mode="standard" / min_length=2000`。Fail-Open：异常 → 返回原 messages 不阻塞。
> - **Auto 评分路由**：`strategy="auto"` 时 `_pick_by_score` 用 6 因子加权评分替代 round-robin 主选路（ADR-0001 精简自 OmniRoute 12-factor）：`health 0.25`（ok/(ok+fail)）+ `quota_remaining 0.25`（1-saturation）+ `latency_p95_inv 0.20`（1-p95/10000）+ `cost_inv 0.10`（按 tier {1:1.0, 2:0.8, 3:0.6, 4:0.4, 5:0.2}）+ `tier_match 0.10`（在 use_case tier_range 内）+ `lkgp_bonus 0.10`（session_id 粘性）。无数据/异常 → 中性 0.5 兜底。配置：`[llm.routing] strategy="round_robin" / mode_pack="balanced"`。Fail-Open：评分模块加载失败 → 取 `pick_pool[0]`。
> - **`get_status` 新增 v14 字段**：`compression_mode` / `compression_min_length` / `compression_total` / `compression_saved_chars` / `routing_strategy` / `routing_mode_pack` / `routing_last_scores`。
> - **`/health` 新增 v14 字段**：`compression_mode` / `compression_min_length` / `compression_total` / `compression_saved_chars` / `routing_strategy` / `routing_mode_pack` / `routing_last_scores_count`。version 从 "v13" 升到 "v14"。
>
> **关于 base_url**：代码会按 `protocol` 自动在 `base_url` 后拼接 `/chat/completions`（openai）或 `/messages`（anthropic），因此 `base_url` 只需填到版本号级别即可，**不要**包含 `/chat/completions` 或 `/messages` 后缀。常见 provider 的 base_url：
> - OpenAI 协议（通用）：`https://api.openai.com/v1` / `https://api.deepseek.com` / `https://open.bigmodel.cn/api/paas/v4`（智谱 GLM，兼容 OpenAI 协议）/ `https://dashscope.aliyuncs.com/compatible-mode/v1`（通义千问）
> - Anthropic 协议：`https://api.anthropic.com/v1`
>
> **v15 自动规范化**：`create_key` / `update_key` 时会自动去掉误填的 `/chat/completions` / `/messages` / `/completions` 后缀（`_normalize_base_url()`），避免 URL 重复拼接。但建议填写时就按上述格式填到版本号级别。
>
> **自动迁移**：`migrate_v4_to_v5()` → `migrate_v7_to_v8()` → `migrate_v8_to_v9()` → `migrate_v10_to_v11()` → `migrate_v11_to_v12()` 启动时自动检测旧格式并迁移。v7→v8 将 `models: list[str]` 转为 `list[dict]` 并赋默认 `tier=3`；v8→v9 从 `config.toml [vision.vl_providers.*]` 按 `base_url` 匹配写入 key 的 `vision` 段（需运行 `tools/migrate_keys_v8_to_v9.py`，因 config.toml 已无 VL 段，新部署无需此步）；v10→v11 为所有 key 补 `protocol: "openai"` 默认值（model 不主动写入 `display_name`，直到用户编辑时才写入）；v11→v12 仅升级 version 号，`pool_key` 由用户按需通过 `/keys` API 设置。迁移幂等，多次执行无副作用。

#### Key 管理 API

key 的增删改查通过 `/keys/*` 端点（`server/apikey.py`），不是 `/llm/pool/*`：

| 路径 | 方法 | 说明 |
|------|------|------|
| `/keys` | GET | 列出所有 key（默认 mask，`?unmasked=true` 显示明文） |
| `/keys/{id}` | GET | 获取单个 key 详情 |
| `/keys` | POST | 新增 key |
| `/keys/{id}` | PUT | 更新 key |
| `/keys/{id}` | DELETE | 删除 key |
| `/keys/usage` | GET | key 用量映射（每个 use_case 用哪些 key） |
| `/keys/health-check` | POST | 触发 key 健康检查（同 `/llm/pool/health-check`） |
| `/use-cases` | GET | 列出 USE_CASE_REGISTRY 全部 17 个 use_case（v5 新增，v15 扩展） |

### v5 迁移指引

#### keys.json v4 → v5 自动迁移

`migrate_v4_to_v5()` 启动时自动执行（幂等）。无需手动操作。变更：
- `model: str` → `models: [model]` + `default_model: model`
- 删除 `vendor`/`group` 字段
- `version: 4` → `version: 5`
- 自动备份到 `data/llm/keys.json.v4backup`（首次迁移时创建）

#### 调用方迁移模式

v5 引入 `resolve_key()` 路由层统一入口，替代 `get_key_for_use()`：

```python
# v4（已废弃，仍可通过 key_manager.py 兼容层使用）
from server.llm_pool.key_manager import get_key_for_use
unified = get_key_for_use(use_case, model_tier)  # 返回 dict
api_key = unified["api_key"]
model = unified["model"]

# v5（推荐）
from server.llm_pool.key_store import resolve_key
resolved = resolve_key(use_case, model_tier)  # 返回 ResolvedKey dataclass
api_key = resolved.api_key
model = resolved.model
```

#### LLMPool 调用方迁移

`LLMPool._acquire/call/call_simple` 支持 `use_case` 参数：

```python
# v4（不区分 use_case）
result = pool.call(messages, max_tokens=1024)

# v5（敏感用途自动跳过 privacy_warning key）
result = pool.call(messages, max_tokens=1024, use_case="download_watcher")
```

`/llm/pool/call` 和 `/llm/pool/call-simple` 请求体新增 `use_case` 字段（可选）。

#### VL 调用方迁移

`RemoteVLClient.ocr/understand/locate` 支持 `use_case` 参数：

```python
# v4（不区分 use_case）
result = remote_vl.understand(image, prompt)

# v5（显式传 use_case）
result = remote_vl.understand(image, prompt, use_case="vl_vision")
result = remote_vl.understand(image, prompt, use_case="vl_activity_tracker")  # Loop 场景专用
```

`ocr()` 默认 `use_case="vl_ocr"`；`understand()`/`locate()` 默认 `use_case="vl_vision"`。**所有调用方应显式传 use_case** 以防默认值漂移。


