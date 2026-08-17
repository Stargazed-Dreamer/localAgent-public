# ToolResult 改为 5 变体 enum（替代二态 is_error）

现有 `ToolResult` 是二态（`is_error` + `content`），把"用户拒绝/工具失败/超时"全混进 `is_error=True`，模型无法区分处理策略（拒绝应换方案，超时可重试，失败应查错）。本决策将 `ToolResult` 改为 5 变体 enum（SUCCESS/ERROR/USER_DENIED/APPROVAL_REQUIRED/TIMEOUT），附加 `error_type`/`approval_id`/`timeout_seconds`/`artifact_path`/`next_action_hint` 字段，保留 `is_error` property 向后兼容。审批超时单独标 `variant=TIMEOUT` + `error_type=approval_timeout`（vs 用户拒绝 `USER_DENIED`）。

## Status

accepted (2026-08-05)

## Context

v6-lite 引擎的 `ToolResult` 原是二态结构（`is_error: bool` + `content: str`），所有非成功结果都进 `is_error=True`。这导致：

- 模型无法区分"用户拒绝审批"和"工具执行失败"——前者应改道换方案，后者应查错重试，处理策略完全不同。
- 模型无法区分"审批超时（用户没响应）"和"工具执行超时"——前者可重试或再问用户，后者应检查工具参数。
- 审批在 executor 内部完成，runner 看不到 `approval_id`/决策路径，无法在系统消息中告知用户"工具调用经审批通过/被拒"。
- 图片/大文本结果无"下一步该怎么用"的提示，模型瞎调或重复调用。

现有代码（`http_client_tool_executor.py` L276-285）已有审批超时处理，但返回的是 `is_error=True` 的二态结果，模型看不出是审批超时还是其他错误。

## Decision

1. **`ToolResult` 从二态改为 5 变体 enum**：

   ```python
   class ToolResultVariant(Enum):
       SUCCESS = "success"               # 成功，content 是结果
       ERROR = "error"                   # 工具失败，content 含错误信息 + error_type
       USER_DENIED = "user_denied"       # 用户拒绝审批，content 是 user_feedback
       APPROVAL_REQUIRED = "approval_required"  # 需审批，含 approval_id
       TIMEOUT = "timeout"               # 超时，content 含超时秒数
   ```

2. **附加字段**：`error_type`（variant=ERROR 时填，如 `http_error`/`parse_error`/`not_found`/`build_failed`/`approval_timeout`/`execution_timeout`）、`approval_id`（variant=APPROVAL_REQUIRED 时填）、`timeout_seconds`（variant=TIMEOUT 时填）、`artifact_path`（L0 落盘路径，与 `L0ArtifactStore` 配合）、`next_action_hint`（多媒体/大结果时填，提示模型下一步动作）、`created_at`。

3. **审批超时与用户拒绝明确区分**：
   - 用户主动点"拒绝"：`variant=USER_DENIED`，content 是 user_feedback。
   - 审批请求超时（用户 180s 内未响应）：`variant=TIMEOUT` + `error_type=approval_timeout`。
   - 工具执行本身超时（非审批）：`variant=TIMEOUT` + `error_type=execution_timeout`。

4. **审批仍在 executor 内部完成（信息透明）**：executor 收到 403 + `approval_required` 时仍内部调 `/command-guard/request-approval` → 拿 token → 重试原请求，但通过 `ToolResult.variant` 把审批决策路径透明暴露给 runner（通过=SUCCESS + content 末尾追加 `[approved: approval_id=xxx]`；拒绝=USER_DENIED；请求失败=ERROR + error_type=approval_request_failed）。runner 不主动介入流程，但能在系统消息中告知用户。

5. **保留 `is_error` property 向后兼容**：`is_error = variant != SUCCESS`，避免现有代码大改。

6. **多媒体反提示**：`next_action_hint` 字段在工具返回图片/视频/大文本时由 executor 或 `L0ArtifactStore` 填充（如 `screen_ocr` 返回 bbox：`"bbox 是窗口内坐标，点击需加 list_windows 返回的 left/top"`；L0 落盘大文本：`"完整结果已落盘 path，preview 见 content，需要全文时用 file_read 读取"`）。

## Considered Options

1. **保留二态加 `error_type` 字段**（不引入 enum）——被拒：模型仍需解析 content 文本判断类别（是失败？拒绝？超时？），enum variant 让模型直接从结构化字段读取类别，无需文本解析。
2. **引入 8+ 变体（含 `MultiMediaContent`/`NotFound`）**——被拒：多媒体场景用 `artifact_path` + `next_action_hint` 字段已解决（无需单独 variant）；`NotFound` 归入 `ERROR` + `error_type=not_found` 即可。5 变体覆盖主要场景，避免过度枚举。

## Consequences

**正面**：
- 模型可从 `variant` 直接区分拒绝/超时/失败，给出不同后续动作（拒绝→换方案；超时→可重试或问用户；失败→查错）。
- 审批决策路径透明，runner 能在系统消息中告知用户"工具调用经审批通过/被拒"。
- `next_action_hint` 减少模型对多媒体/大文本结果的无效往返。

**负面**：
- dataclass 字段增多（7 个可选字段），构造 `ToolResult` 时需注意填对字段。
- enum 变体是闭集，未来新场景需扩 variant（如 `RATE_LIMITED` 单独标？目前归入 `ERROR` + `error_type`）。

**回退路径**：`is_error` property 保留，依赖二态的旧代码无需改动即可继续工作；enum variant 是增量，可随时新增变体不破坏现有变体。

## References

- 决策来源：[`temp/sdd/memory-prompt-tool-refactor/00-decisions.md`](file:///f:/<project_root>/temp/sdd/memory-prompt-tool-refactor/00-decisions.md) D6（5 变体 enum）、D10（approve 阶段协议 executor 内部完成信息透明）、D10.1（审批超时 variant=TIMEOUT + error_type=approval_timeout）、D18（next_action_hint 多媒体反提示）
- 关键代码路径：[client/core/agent/types.py](file:///f:/<project_root>/client/core/agent/types.py)（`ToolResult` + `ToolResultVariant` 定义）、[client/core/agent/http_client_tool_executor.py](file:///f:/<project_root>/client/core/agent/http_client_tool_executor.py)（variant 实际使用，USER_DENIED/APPROVAL_REQUIRED/TIMEOUT 分支）、[client/core/agent/builtin_tool_executor.py](file:///f:/<project_root>/client/core/agent/builtin_tool_executor.py)、[client/core/agent/runner.py](file:///f:/<project_root>/client/core/agent/runner.py)、[client/core/agent/event_store.py](file:///f:/<project_root>/client/core/agent/event_store.py)
- 相关 ADR：[ADR-0006](0006-v6-lite-scope-cut.md)（v6-lite 砍范围，error-as-output-variant 是保留的 v6 精华之一）
