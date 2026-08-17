# 审批领域文件整合：合并 approval_log → approval_review，合并 command_guard_router → command_guard

`server/approval_*` 与 `server/command_guard*` 历史上有 5 个文件交织管理审批状态、日志、REST 端点和 GUI 弹窗。本次重构把 `approval_log.py` 合入 `approval_review.py`、把 `command_guard_router.py` 合入 `command_guard.py`，减少 2 个文件；`http_guard.py` 显式保持独立。我们**刻意没有**引入统一的 `ApprovalStore` 类。

## Context

重构前审批领域文件分布：

| 文件 | 职责 | 行数 |
|------|------|------|
| `approval_log.py` | 审计日志写入（`log_approval` / `log_approval_detailed`） | 85 |
| `approval_review.py` | 三层审批逻辑（规则→LLM→人审）+ 路径映射 | ~450 |
| `command_guard.py` | shell 命令拦截状态机 + dcg 调用 | ~210 |
| `command_guard_router.py` | `/command-guard/*` REST 端点 + GUI 弹窗启动 | 156 |
| `http_guard.py` | HTTP 请求审批（独立 fingerprint 方案） | ~180 |

`approval_log.py` 仅被 `approval_review.py` 和 `mcp_gateway.py` 调用；`command_guard_router.py` 仅被 `main.py` 注册路由时引用。两者都是**浅模块**（接口复杂度 ≈ 实现复杂度），deletion test 验证：删除后复杂度集中在原有调用方，而非转移到新位置——满足"删除是收益"的标准。

## Decision

1. **`approval_log.py` → `approval_review.py`**：日志函数就地合并到调用最频繁的模块。`mcp_gateway.py` 的 2 处 `from server.approval_log import log_approval_detailed` 改为从 `approval_review` 导入。
2. **`command_guard_router.py` → `command_guard.py`**：REST 端点和 `run_gui_dialog` 共享函数合并到状态机所在文件。`main.py` 的 `from server.command_guard_router import router` 改为 `from server.command_guard import router`。
3. **`http_guard.py` 保持独立**：HTTP 审批用 `method+path+body` 指纹，shell 审批用 `command+shell+cwd` 指纹，方案不同；强行合并会引入"参数形状不一"的分支复杂度。
4. **不引入 `ApprovalStore` 类**：考虑过把 `_pending` / `_tokens` / `_dcg_cache` 等状态封装为 `ApprovalStore`，但当前 `command_guard.py` 和 `http_guard.py` 的状态访问模式已经清晰（模块级 dict + `_cleanup` + `_invalidate_*_cache`），引入类只会增加一层间接而无新能力。

## Consequences

- **正向**：文件数从 5 减为 3；新读者只需打开 1 个文件就能理解 shell 审批完整闭环（state + endpoint + GUI）；`main.py` 路由注册少了 1 行。
- **负向**：`command_guard.py` 行数从 210 增至 ~330，但所有新增代码都是同一职责（REST endpoint）的天然归属，不属于"复杂度集中"。
- **未来反转成本**：若日后需要把 `command_guard` 拆回 `state` + `router` 两文件，按 `# REST endpoints` 注释分隔即可，不需要重新设计边界。
- **审批状态查询**：`GET /command-guard/status` 仍由 `command_guard.py` 提供；`/health` 中 `command_guard` 子状态通过 `command_guard.get_command_guard_config()` 读取，未受影响。
