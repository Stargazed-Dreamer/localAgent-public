"""v6-lite T08: L2 上下文压缩（v6-lite §3 W5 + v6-06 §6）

设计依据：
- v6-lite §3 W5：L2 `estimate_tokens`（复用 compression.py）超 85% → cheap tier 生成摘要
  + 最近 3 轮 tail 重建上下文；`NO_TOOLS_PREAMBLE` 防压缩触发工具
- v6-06 §6（参考）：摘要 7 字段固定顺序
- v6-02 §2.1：`has_attempted_reactive_compact` 标志位 retry 时不重置（防死循环）

职责：
1. `should_compact(messages, max_tokens)` —— 估算 tokens 是否超 85% 阈值
2. `compact(messages, llm_gateway)` —— 调 cheap tier LLM 生成摘要，重建上下文：
   - 中间消息 → 摘要（source=summary，visible=true，模型看得到，TASK_GOAL 字段覆盖任务目标）
   - 最近 3 轮（last 6 条 assistant+user+tool）原样保留
   - 不保留 messages[0]（旧 user prompt 完全进摘要的 TASK_GOAL 字段）
3. `NO_TOOLS_PREAMBLE` 系统提示：禁止摘要生成时触发工具调用
4. 摘要失败 → 返回原 messages（fail-open，让 reactive_compact_retry 失败由 runner 处理）

不依赖 Qt（纯 Python，可在 CLI 脚本和 GUI 中复用）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from client.core.agent.types import LLMGateway, LLMRequest, Message

logger = logging.getLogger("localagent.agent.compactor")

# v6-lite §3 W5：85% 阈值
COMPACT_THRESHOLD_RATIO = 0.85

# 保留的最近 tail 消息数（约 3 轮 = 6 条 user/assistant/tool 交替）
# 实际取 max(TAIL_KEEP, 6)，且不超过 messages 总数
DEFAULT_TAIL_KEEP = 6

# 摘要 prompt 模板（v6-06 §6 七字段固定顺序 + NO_TOOLS_PREAMBLE + T31d stale-task 防护）
_SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Your job is to compress the conversation "
    "history into a structured summary that preserves key information.\n\n"
    "CRITICAL: Do NOT call any tools. Do NOT attempt to continue the task. "
    "Only produce a summary.\n\n"
    "Output the summary in this exact 7-field format:\n"
    "1. TASK_GOAL: The user's primary objective in this session\n"
    "2. KEY_FILES: File paths, IDs, or resources mentioned (preserve exact paths)\n"
    "3. DECISIONS: Decisions made and rationale\n"
    "4. PROGRESS: What has been accomplished so far\n"
    "5. BLOCKERS: Issues, errors, or unresolved questions\n"
    "6. NEXT_STEPS: Pending actions or planned next moves\n"
    "7. KEY_CONTEXT: Any other critical context the assistant must remember\n\n"
    "Be concise. Preserve all file paths, IDs, and technical identifiers verbatim.\n\n"
    # T31d stale-task 防护（回收 v6-06:253-262）：摘要中的 Next 字段是参考用，
    # 不当指令执行；当前任务以最新用户消息为准
    "注意：以下是历史摘要，仅供参考。当前任务以最新用户消息为准，"
    "不要把摘要里的 NEXT_STEPS 字段当指令执行。"
)


# ============================================================================
# T31a: PruningConfig 5 字段（回收 v6-06:88-97）
# ============================================================================


@dataclass(frozen=True)
class PruningConfig:
    """L2 压缩前的裁剪配置（grok 生产验证默认值）。

    回收 v6-06:88-97，5 字段覆盖两层裁剪：
    - keep_last_n_turns：保留最近 N 轮完整对话（Turn 层）
    - soft_trim_threshold：Token 层软裁剪触发点
    - soft_trim_head / soft_trim_tail：软裁剪保留头部/尾部 token 数
    - hard_clear_age_turns：Turn 层硬清除（超过此轮龄的全删，不进摘要）

    当前 Compactor.compact() 用 tail_keep 保留尾部，PruningConfig 作为
    更精细的配置预留，后续 ticket 启用 soft_trim/hard_clear 逻辑。
    """

    keep_last_n_turns: int = 3          # 保留最近 3 轮完整对话
    soft_trim_threshold: int = 4000     # Token 层软裁剪触发点
    soft_trim_head: int = 1500          # 软裁剪保留头部
    soft_trim_tail: int = 1500          # 软裁剪保留尾部
    hard_clear_age_turns: int = 10      # Turn 层硬清除（10 轮前的全删）


# ============================================================================
# T31b: ContextBudget 8 字段 + estimate_at_last_response 锚点（回收 v6-06:25-51）
# ============================================================================


@dataclass
class ContextBudget:
    """上下文 token 预算 8 字段细分（替代 bytes/4 简化估算）。

    回收 v6-06:25-51，8 字段覆盖上下文所有消耗来源：
    - system_prompt：系统提示词（stable prefix + ephemeral）
    - messages：对话历史消息
    - tool_schemas：工具 schema（OpenAI function calling 格式）
    - tool_results：工具调用结果回灌
    - image_estimated：图片消息估算（bytes/4）
    - reasoning_replay：reasoning 模型的思考重放
    - output_budget：输出预留（模型生成空间）
    - safety_buffer：安全余量（防边界溢出）

    total_estimated_tokens 求和属性，供 should_compact 使用。
    estimate_at_last_response 锚点：记录上次响应时的 token 估算，
    下次只算增量（不从零重算）。None 表示尚未建立锚点。
    """

    system_prompt: int = 0
    messages: int = 0
    tool_schemas: int = 0
    tool_results: int = 0
    image_estimated: int = 0       # 新增：图片估算
    reasoning_replay: int = 0      # 新增：reasoning 模型重放
    output_budget: int = 4096      # 输出预留
    safety_buffer: int = 1024      # 安全余量
    # estimate_at_last_response 锚点：记录上次响应时的 token 估算，
    # 下次只算增量（不从零重算）。None 表示尚未建立锚点。
    estimate_at_last_response: int | None = field(default=None)

    @property
    def total_estimated_tokens(self) -> int:
        """求和所有 token 消耗来源。"""
        return (
            self.system_prompt
            + self.messages
            + self.tool_schemas
            + self.tool_results
            + self.image_estimated
            + self.reasoning_replay
            + self.output_budget
            + self.safety_buffer
        )


# ============================================================================
# T31c: CompactionResult + compaction_occurred 标记（回收 v6-06:237-247）
# ============================================================================


@dataclass
class CompactionResult:
    """compact_with_result() 返回类型（回收 v6-06:237-247）。

    - messages：压缩后的消息列表（[summary_msg] + tail，或 fail-open 时原 messages）
    - compaction_occurred：是否真的发生了压缩（False = skip/fail-open/noop）
    - summary_message：生成的摘要消息（None = 未生成摘要）

    runner.py 用 compaction_occurred 判断是否替换 messages，避免 fail-open 时
    误判为"已压缩"导致无限重试。
    """

    messages: list[Message]
    compaction_occurred: bool = False
    summary_message: Message | None = None


class Compactor:
    """L2 上下文压缩器。

    用法：
        compactor = Compactor(max_context_tokens=32000, llm_gateway=gateway)
        if compactor.should_compact(messages):
            new_messages = await compactor.compact(session_id, messages)

    T31c+：需要压缩元数据时用 compact_with_result()，返回 CompactionResult
    （含 compaction_occurred + summary_message），runner.py 用此判断是否替换 messages。
    """

    def __init__(
        self,
        *,
        max_context_tokens: int = 32000,
        tail_keep: int = DEFAULT_TAIL_KEEP,
        cheap_tier_model: str = "",
        pruning_config: PruningConfig | None = None,
    ):
        self.max_context_tokens = max_context_tokens
        self.tail_keep = max(tail_keep, DEFAULT_TAIL_KEEP)
        self.cheap_tier_model = cheap_tier_model
        # T31a: PruningConfig 预留（后续 ticket 启用 soft_trim/hard_clear 逻辑）
        self.pruning_config = pruning_config

    # ------------------------------------------------------------------
    # 估算
    # ------------------------------------------------------------------

    def should_compact(self, messages: list[Message]) -> bool:
        """判断是否需要压缩：估算 tokens 超 85% 阈值时返回 True。"""
        try:
            from server.llm_pool.compression import estimate_tokens
        except ImportError:
            # server 不可用时用本地粗估（4 char/token）
            def estimate_tokens(text: str) -> int:
                return len(text) // 4 if text else 0

        total = 0
        for msg in messages:
            total += estimate_tokens(self._msg_to_text(msg))
        threshold = int(self.max_context_tokens * COMPACT_THRESHOLD_RATIO)
        logger.debug(
            "should_compact: total_tokens=%d, threshold=%d (%d%% of %d), compact=%s",
            total, threshold, int(COMPACT_THRESHOLD_RATIO * 100),
            self.max_context_tokens, total > threshold,
        )
        return total > threshold

    # ------------------------------------------------------------------
    # 压缩
    # ------------------------------------------------------------------

    async def compact(
        self,
        session_id: str,
        messages: list[Message],
        llm_gateway: LLMGateway,
    ) -> list[Message]:
        """压缩 messages：摘要 + 最近 tail 保留（向后兼容 thin wrapper）。

        内部调 compact_with_result()，返回 .messages。
        需要压缩元数据（compaction_occurred/summary_message）时请用 compact_with_result()。

        Returns:
            new_messages：[summary_msg] + [tail_messages]
            若 messages 太少（<= tail_keep）则原样返回（无需压缩）
        """
        result = await self.compact_with_result(session_id, messages, llm_gateway)
        return result.messages

    async def compact_with_result(
        self,
        session_id: str,
        messages: list[Message],
        llm_gateway: LLMGateway,
    ) -> CompactionResult:
        """压缩 messages 并返回 CompactionResult（含 compaction_occurred + summary_message）。

        策略（spec Decision 8：不保留 head）：
        - 中间消息 → 调 LLM 生成摘要（source=summary，visible=true，TASK_GOAL 字段覆盖任务目标）
        - 最近 tail_keep 条原样保留
        - 摘要失败 → 返回原 messages（fail-open，compaction_occurred=False）
        - 旧 messages[0]（user prompt）完全进摘要，不再以原貌出现

        Returns:
            CompactionResult：messages + compaction_occurred + summary_message
        """
        if len(messages) <= self.tail_keep:
            logger.info(
                "compact: skip (messages=%d <= tail_keep=%d)",
                len(messages), self.tail_keep,
            )
            return CompactionResult(messages=messages, compaction_occurred=False)

        middle = messages[:-self.tail_keep]
        tail = messages[-self.tail_keep:]

        # 构造摘要请求
        summary_request_text = self._build_summary_request_text(middle)
        summary_req = LLMRequest(
            messages=[Message(role="user", content=summary_request_text, source="user")],
            system=_SUMMARY_SYSTEM_PROMPT,
            model=self.cheap_tier_model,
            tools=[],  # 显式空 tools，防 provider 调用工具
            tool_choice="none",  # 强制无工具
            max_tokens=2000,
        )

        try:
            response = await llm_gateway.call(summary_req)
        except Exception as e:
            logger.warning("compact: LLM call failed (fail-open): %s", e)
            return CompactionResult(messages=messages, compaction_occurred=False)

        if not response.content or response.stop_reason == "error":
            logger.warning(
                "compact: LLM returned error/empty (fail-open): %s",
                response.content[:200] if response.content else "(empty)",
            )
            return CompactionResult(messages=messages, compaction_occurred=False)

        # 构造摘要 Message（source=summary，模型看得到）
        summary_msg = Message(
            role="system",
            content=(
                "[CONTEXT_SUMMARY]\n"
                f"{response.content}\n"
                "[/CONTEXT_SUMMARY]\n\n"
                "Above is a summary of the previous conversation. "
                "Continue from here with the recent messages below."
            ),
            source="summary",
            visible=True,
        )

        new_messages = [summary_msg] + tail
        logger.info(
            "compact: session=%s, original=%d msgs, new=%d msgs (summary+tail=1+%d)",
            session_id, len(messages), len(new_messages),
            len(tail),
        )
        return CompactionResult(
            messages=new_messages,
            compaction_occurred=True,
            summary_message=summary_msg,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _msg_to_text(self, msg: Message) -> str:
        """把 Message 转成纯文本用于 token 估算。"""
        if isinstance(msg.content, str):
            text = msg.content
        elif isinstance(msg.content, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in msg.content
            )
        else:
            text = str(msg.content) if msg.content is not None else ""
        # 加上 tool_calls 的 arguments 也算 token
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    text += " " + (func.get("arguments", "") or "")
        return text

    def _build_summary_request_text(self, messages: list[Message]) -> str:
        """构造给摘要 LLM 的输入文本。"""
        lines = [
            "Summarize the following conversation history. "
            "Preserve all file paths, IDs, decisions, and technical details.",
            "",
        ]
        for i, msg in enumerate(messages):
            role = msg.role.upper()
            text = self._msg_to_text(msg)
            # 截断超长单条（防止单条把整个摘要请求撑爆）
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated for summary) ..."
            lines.append(f"--- {role} (msg {i}) ---")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)


# ============================================================================
# ContextOverflow 异常（T08e: SessionRunner 捕获后触发 reactive_compact_retry）
# ============================================================================


class ContextOverflow(Exception):
    """LLM 返回 413 / context too large 时抛出。

    SessionRunner.run() 捕获后调 Compactor.compact() 压缩上下文并重试。
    若 has_attempted_reactive_compact 已 True，则不再重试，直接 fail。
    """

    def __init__(self, message: str = "context overflow", *, status_code: int = 413):
        super().__init__(message)
        self.status_code = status_code
