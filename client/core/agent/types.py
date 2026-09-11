"""v6-lite 对话引擎核心数据类型（T01 最小集）

设计依据：
- v6-lite §3 W1：4 表 SQLite + MockLLM 跑通循环
- v6-01 §1.1：表 schema（lite 砍到 4 表：sessions/events/messages/tool_calls）
- v6-02 §3：deps/config 拆分（可测试性根基）

T01 范围：只含单轮文本对话所需类型（sessions/messages/events）。
tool_calls 相关类型（ToolCall/ToolResult/transition_reason next_turn 之外）在 T02 补。
"""

from __future__ import annotations

import json
import time
import uuid as _uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# ============================================================================
# 消息与事件
# ============================================================================


@dataclass
class Message:
    """对话消息（对应 messages 表一行）。

    content 支持纯文本字符串或结构化 list（OpenAI 兼容的多模态/工具结果 content blocks），
    落库时序列化为 content_json。

    source 区分消息来源（v6-01 §1.2）：
    - user: 用户输入
    - assistant: 模型回复
    - summary: L2 压缩摘要
    - system_reminder: 系统提醒
    - tool_result: 工具执行结果回灌
    - synthetic: 续轮合成消息（visible=False，transcript 不渲染，v6-02 §4）
    """

    role: str  # user / assistant / system / synthetic
    content: Any  # str | list[dict]（OpenAI content blocks）
    source: str = "user"
    visible: bool = True
    tool_call_id: str | None = None  # role=tool 时关联的 tool_call id
    tool_calls: list[dict] = field(default_factory=list)  # assistant 携带的 tool_calls
    thinking: str = ""  # v6-lite-streaming-gui T01: LLM thinking 内容（streaming 合并后落库）
    model: str = ""  # chat-panel-v2 T01: assistant 消息的模型名（D31，气泡下方小字显示）
    id: str = ""
    seq: int = 0
    session_id: str = ""
    created_at: float = 0.0

    def to_db(self) -> dict:
        """序列化为 messages 表行。"""
        return {
            "id": self.id or str(_uuid.uuid4()),
            "session_id": self.session_id,
            "seq": self.seq,
            "role": self.role,
            "content_json": json.dumps(self.content, ensure_ascii=False),
            "tool_call_id": self.tool_call_id,
            "source": self.source,
            "visible": 1 if self.visible else 0,
            "created_at": self.created_at or time.time(),
            "tool_calls_json": json.dumps(self.tool_calls, ensure_ascii=False) if self.tool_calls else None,
            "thinking_json": self.thinking if self.thinking else None,
            "model": self.model or None,
        }

    @classmethod
    def from_db(cls, row: dict) -> Message:
        # tool_calls_json 可能不存在（旧 schema），用 .get 兜底
        tool_calls_raw = row.get("tool_calls_json") if isinstance(row, dict) else None
        if tool_calls_raw is None and not isinstance(row, dict):
            # sqlite3.Row 支持 keys()
            try:
                tool_calls_raw = row["tool_calls_json"]
            except (IndexError, KeyError):
                tool_calls_raw = None
        tool_calls = json.loads(tool_calls_raw) if tool_calls_raw else []
        # thinking_json 可能不存在（旧 schema），用 .get 兜底
        thinking_raw = row.get("thinking_json") if isinstance(row, dict) else None
        if thinking_raw is None and not isinstance(row, dict):
            try:
                thinking_raw = row["thinking_json"]
            except (IndexError, KeyError):
                thinking_raw = None
        thinking = thinking_raw or ""
        # model 可能不存在（旧 schema），用 .get 兜底空字符串
        model_raw = row.get("model") if isinstance(row, dict) else None
        if model_raw is None and not isinstance(row, dict):
            try:
                model_raw = row["model"]
            except (IndexError, KeyError):
                model_raw = None
        model = model_raw or ""
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            seq=row["seq"],
            role=row["role"],
            content=json.loads(row["content_json"]),
            tool_call_id=row["tool_call_id"],
            source=row["source"],
            visible=bool(row["visible"]),
            created_at=row["created_at"],
            tool_calls=tool_calls,
            thinking=thinking,
            model=model,
        )


@dataclass
class Event:
    """事件（append-only，对应 events 表一行）。

    预留字段（v6-01 §1.1，T01 只留列不写逻辑）：
    - prompt_index: 回滚锚点（rewind 用）
    - invalidated_seq: append-only 不可删，恢复/undo 时标记
    - parent_trace_id: 子 agent 保留 parent
    - depth: 子 agent +1
    """

    seq: int
    session_id: str
    type: str  # session_created / user_message_appended / assistant_message_appended / session_finalized / ...
    payload: dict
    created_at: float = 0.0
    trace_id: str | None = None
    # 预留字段（T04 补逻辑，T01 只留列）
    prompt_index: int | None = None
    invalidated_seq: int | None = None
    parent_trace_id: str | None = None
    depth: int = 0


@dataclass
class Session:
    """会话（对应 sessions 表一行）。"""

    id: str
    title: str = ""
    mode: str = "dialogue"  # dialogue / headless / headless_judge
    status: str = "idle"  # idle / streaming / awaiting_tools / compacting / completed / interrupted / failed
    created_at: float = 0.0
    updated_at: float = 0.0
    # 预留字段（T04+ 补逻辑，T01 只留列）
    prompt_index: int = 0
    last_compaction_prompt_index: int | None = None
    # chat-panel-v2 T01/T06 新增（D10：分组 + 置顶）
    group_name: str | None = None  # NULL = 未分组
    pinned: bool = False  # False=未置顶，True=已置顶


# ============================================================================
# LLM 请求/响应（OpenAI 兼容协议，T01 最小集）
# ============================================================================


@dataclass
class LLMRequest:
    """LLM 调用请求（非流式）。"""

    messages: list[Message]
    model: str = ""
    tools: list[dict] = field(default_factory=list)  # T02+ 工具定义
    tool_choice: str | dict = "auto"
    system: str = ""  # 系统提示（独立于 messages，build 时拼到最前）
    max_tokens: int | None = None


@dataclass
class LLMResponse:
    """LLM 调用响应（非流式）。"""

    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # T02+ OpenAI 格式 tool_calls
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens, total_tokens}
    stop_reason: str = "end_turn"  # end_turn / tool_use / max_tokens / ...
    model: str = ""
    # B2（spec D2/D3）：删 cost_usd 字段，本地无法准确算 cost（缓存比例不明）
    # v6-lite-streaming-gui T02: LLM thinking 内容（stream 模式下累积，非流式默认空）
    thinking: str = ""
    # E1（spec D21）：wall_clock_budget SSE 循环内检查标记
    # streaming 中检测到 wall_clock 超时时设 True（不立即终止流，runner 看到 flag 后
    # 不执行 tool_calls / 不回调 LLM）
    wall_clock_exceeded: bool = False


# ============================================================================
# 工具调用（T02+）
# ============================================================================


# 工具安全等级（v6-lite §3 W2，对应 /openapi.json 的 x-tool-safety）
TOOL_SAFETY_READ_ONLY = "read_only"
TOOL_SAFETY_SAFE = "safe"
TOOL_SAFETY_APPROVAL_REQUIRED = "approval_required"
TOOL_SAFETY_AGENT_BLOCKED = "agent_blocked"

# 工具调用状态（对应 tool_calls.status）
TOOL_STATUS_PENDING = "pending"      # 已写入 durable record，尚未开始执行
TOOL_STATUS_RUNNING = "running"      # 执行中
TOOL_STATUS_COMPLETED = "completed"  # 成功完成
TOOL_STATUS_FAILED = "failed"        # 执行失败（error-as-output-variant）
TOOL_STATUS_INTERRUPTED = "interrupted"  # 被中断（T06）


# transition_reason 7 种（v6-02 §2，T02 先实现 next_turn，其余 T03/T07/T08 补）
TRANSITION_NEXT_TURN = "next_turn"                       # 正常下一轮（T02）
TRANSITION_REACTIVE_COMPACT_RETRY = "reactive_compact_retry"      # 413 后压缩重试（T08）
TRANSITION_COLLAPSE_DRAIN_RETRY = "collapse_drain_retry"          # drain 崩溃后重试（延后）
TRANSITION_MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"  # max_output_tokens 恢复（延后）
TRANSITION_STOP_HOOK_BLOCKING = "stop_hook_blocking"              # stop hook 阻塞（延后）
TRANSITION_TOKEN_BUDGET_CONTINUATION = "token_budget_continuation"  # token 预算续轮（延后）
TRANSITION_VERIFICATION_NUDGE = "verification_nudge"              # 验证 nudge（延后）
TRANSITION_USER_INTERRUPTED = "user_interrupted"                  # 用户中断（T06/T07）


@dataclass
class ToolCall:
    """工具调用记录（对应 tool_calls 表一行）。

    OpenAI 格式 tool_call 在 LLMResponse.tool_calls 里是 dict：
        {"id": "call_abc", "type": "function",
         "function": {"name": "get_weather", "arguments": "{\"location\":\"SF\"}"}}

    ToolCall 是落库前的规范化结构，args 已 parse 为 dict。
    """

    id: str  # 幂等键（来自 LLM tool_call.id 或自生成）
    session_id: str = ""
    seq: int = 0  # 与 assistant 消息同 seq（一轮可含多个 tool_call）
    name: str = ""
    args: dict = field(default_factory=dict)  # 已 parse 的参数
    args_raw: str = ""  # 原始 JSON 字符串（LLM 返回的 arguments）
    safety: str = TOOL_SAFETY_READ_ONLY
    status: str = TOOL_STATUS_PENDING
    started_at: float = 0.0
    ended_at: float = 0.0
    trace_id: str | None = None

    def to_db(self) -> dict:
        """序列化为 tool_calls 表行。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "seq": self.seq,
            "name": self.name,
            "args_json": json.dumps(self.args, ensure_ascii=False),
            "safety": self.safety,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_db(cls, row: dict) -> ToolCall:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            seq=row["seq"],
            name=row["name"],
            args=json.loads(row["args_json"]) if row["args_json"] else {},
            safety=row["safety"],
            status=row["status"],
            started_at=row["started_at"] or 0.0,
            ended_at=row["ended_at"] or 0.0,
            trace_id=row["trace_id"],
        )

    @classmethod
    def from_openai_tool_call(cls, tc: dict, session_id: str = "", seq: int = 0) -> ToolCall:
        """从 OpenAI 格式 tool_call dict 构造（LLMResponse.tool_calls[i]）。

        OpenAI 格式：
            {"id": "call_abc", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"location\":\"SF\"}"}}
        """
        func = tc.get("function", {})
        name = func.get("name", "")
        args_raw = func.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            # 坏 JSON tool args（v6-lite §6 故障注入之一）：保留原字符串，args 留空
            args = {}
        return cls(
            id=tc.get("id", ""),
            session_id=session_id,
            seq=seq,
            name=name,
            args=args,
            args_raw=args_raw,
            status=TOOL_STATUS_PENDING,
        )


class ToolResultVariant(Enum):
    """工具结果变体（spec D6 + 回收 v6-04 反提示字段）。

    5 变体中等版，覆盖主要场景：
    - SUCCESS：成功，content 是结果
    - ERROR：工具失败，content 含错误信息 + error_type
    - USER_DENIED：用户拒绝审批，content 是 user_feedback
    - APPROVAL_REQUIRED：需审批，含 approval_id（runner 介入）
    - TIMEOUT：超时，content 含超时秒数
    """

    SUCCESS = "success"
    ERROR = "error"
    USER_DENIED = "user_denied"
    APPROVAL_REQUIRED = "approval_required"
    TIMEOUT = "timeout"


@dataclass
class ToolResult:
    """工具执行结果（回灌到 messages 表 role=tool 的消息）。

    error-as-output-variant（v6-lite §4.5）：工具错误是结构化输出，不抛 traceback。

    **variant 是新真源**（spec D6），is_error 字段保留向后兼容：
    - 旧代码 ``ToolResult(content=..., is_error=True)`` 仍可用，__post_init__ 自动设 variant=ERROR
    - 新代码应直接用 ``ToolResult(variant=ToolResultVariant.USER_DENIED, content=...)`` 精确表达
    - ``result.is_error`` 仍可读（向后兼容 property 同步）

    回收 v6-04 反提示字段：
    - next_action_hint：多媒体/大结果给模型的下一步提示
    - was_bare_echo：bash 裸 echo 检测（doom-loop 信号）
    - output_truncated：输出被截断标志
    """

    tool_call_id: str
    content: str  # 结果文本（error 时是错误描述）
    # 新真源：variant 必须是 ToolResultVariant（默认 SUCCESS）
    variant: ToolResultVariant = ToolResultVariant.SUCCESS
    # 向后兼容：旧代码传 is_error=True，__post_init__ 同步到 variant
    is_error: bool = False
    # variant=ERROR 时的错误类型细分（http_error/parse_error/not_found/build_failed/...）
    error_type: str | None = None
    # variant=APPROVAL_REQUIRED 时填（runner 介入审批流程用）
    approval_id: str | None = None
    # variant=TIMEOUT 时填
    timeout_seconds: float | None = None
    # T05+: 大结果落盘路径（L0，v6-lite W5）
    artifact_path: str | None = None
    preview: str = ""  # 大结果时的预览
    # 回收 v6-04 反提示字段（默认值偏向"无信号"，fail-open）
    next_action_hint: str | None = None  # 多媒体/大结果给模型的下一步提示
    was_bare_echo: bool = False  # bash 裸 echo 检测（doom-loop 信号）
    output_truncated: bool = False  # 输出被截断标志
    created_at: float = 0.0

    def __post_init__(self):
        """双向同步 is_error ↔ variant（向后兼容旧代码）。

        - 旧代码 is_error=True 但 variant=SUCCESS（默认）→ variant 改为 ERROR
        - 新代码 variant != SUCCESS 但 is_error=False（默认）→ is_error 改为 True
        - 二者一致时无操作
        """
        if self.is_error and self.variant == ToolResultVariant.SUCCESS:
            self.variant = ToolResultVariant.ERROR
        elif not self.is_error and self.variant != ToolResultVariant.SUCCESS:
            self.is_error = True


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行器协议（MockToolExecutor / 真实 HttpClientToolExecutor 都实现此协议）。

    execute() 必须返回 ToolResult，不允许抛异常（error-as-output-variant，v6-lite §4.5）。
    内部异常应捕获并转为 is_error=True 的 ToolResult。
    """

    async def execute(self, tool_call: ToolCall) -> ToolResult: ...


# ============================================================================
# deps / config 拆分（v6-02 §3）
# ============================================================================


@runtime_checkable
class LLMGateway(Protocol):
    """LLM 网关协议（MockLLM / 真实 LLMPoolClient 都实现此协议）。"""

    async def call(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> AsyncIterator[dict]:
        """SSE 流式调用，逐事件 yield（runner.py _stream_llm 依赖）。

        注意：stream 是异步生成器（async generator function，内部 yield），
        调用直接返回 AsyncIterator，无需 await——runner 用 `async for` 迭代。
        所以协议声明为非 async def（若标 async def 会要求 Coroutine 返回，
        与 async generator 的实现类型不匹配）。
        事件格式（dict）：
        - {"type": "text_delta", "delta": "..."}
        - {"type": "thinking_delta", "delta": "..."}
        - {"type": "tool_call_delta", "tool_call": {...}}
        - {"type": "usage", ...}
        - {"type": "done", ...}
        """
        ...


@dataclass
class RunnerDeps:
    """可注入的 I/O 依赖（v6-02 §3，便于测试 spy）。

    T01 最小集：event_store + llm_gateway + clock + uuid。
    T02 补 tool_executor；T05+ 补 tool_registry；T06+ 补 interrupt_event + compactor；T07+ 补 hook_executor。
    T26+ 补 builtin_executor（内置工具执行器，不经 HTTP）。
    """

    llm_gateway: LLMGateway
    event_store: Any = None  # EventStore | None（T01 必填，typing 用 Any 避免 import cycle）
    clock: Callable[[], float] = time.time
    uuid: Callable[[], str] = lambda: str(_uuid.uuid4())
    # T02+ 工具执行器（T02 起必填，T01 None）
    tool_executor: Any = None  # ToolExecutor | None
    # T05+ 工具 catalog（注入后 runner 把 tools 透传给 LLM，并按 safety 标注 ToolCall）
    tool_registry: Any = None  # ToolRegistry | None
    # T26: 内置工具执行器（file_read/file_write 等本地执行，不经 HTTP）
    # None 时所有工具都走 tool_executor（HTTP），向后兼容旧测试
    builtin_executor: Any = None  # BuiltinToolExecutor | None
    # T06c: 中断事件（asyncio.Event）。外部调 set() 后，run() 下一轮检查到即写
    # transition(user_interrupted) 事件并终止。不依赖异常传播（v6-lite §3 W3）。
    # None 表示不支持中断（向后兼容 T01-T05 测试）。
    interrupt_event: Any = None  # asyncio.Event | None
    compactor: Any = None  # Compactor | None
    hook_executor: Any = None  # HookExecutor | None
    # T02-exec-unify: terminal 管理（spec D6 + D11 第二层保障）
    # terminal_killer: async callback (tid: str) -> bool，用于 session 结束时 kill 残留 terminal
    # terminal_inspector: async callback (tid: str, timeout: int) -> dict，用于 3 分钟唤醒时 inspect
    # None 表示不支持 terminal 清理（向后兼容旧测试）
    terminal_killer: Any = None  # Callable[[str], Awaitable[bool]] | None
    terminal_inspector: Any = None  # Callable[[str, int], Awaitable[dict]] | None
    # v6-lite-streaming-gui T04: DoomLoopDetector 配置（spec D10/D11）
    # None 表示禁用 DoomLoop 检测（向后兼容）
    doom_loop_config: Any = None  # DoomLoopConfig | None


@dataclass(frozen=True)
class RunnerConfig:
    """进入 run() 时 snapshot 的不可变配置（v6-02 §3）。

    T01 最小集：session_id + max_iterations。
    T07+ 补 gates / wall_clock_budget_secs。
    B2（spec D2/D3）：删 max_budget_usd（本地无法准确算 cost，只记账 token 不控制预算）。
    """

    session_id: str
    model: str = ""
    max_iterations: int = 50
    wall_clock_budget_secs: int = 0  # 0 = 不限制（T07+ 启用）
    gates: dict = field(default_factory=dict)  # T07+ 填
    # T02-exec-unify: 3 分钟唤醒配置（spec D5）
    # long_tool_first_check_secs: 首次唤醒间隔，默认 180s（3 分钟）
    # 0 = 禁用唤醒机制（向后兼容旧测试）
    long_tool_first_check_secs: int = 180
    # v6-lite-streaming-gui T02: 流式配置（spec D05/D15）
    # use_stream: True=用 gateway.stream() 流式调用，False=回退 call() 非流式（向后兼容）
    use_stream: bool = True
    # 看门狗（D15）：0 = 禁用
    stream_idle_timeout_secs: int = 90      # SSE 流空闲超时（秒）
    stall_detection_window_secs: int = 30   # stall 检测窗口（秒）
    # v6-lite-streaming-gui T03: 打字机配置（spec D07/D08/D09）
    # typewriter_mode: close（一次性）/ fast（chunk 到达即渲染，默认）/ normal（16ms 60fps）
    # 注：此字段供 ChatPanel 读取默认值，SessionRunner 本身不使用
    typewriter_mode: str = "fast"
    typewriter_normal_interval_ms: int = 16  # normal 档 QTimer 间隔（ms）
    # chat-panel-v2 T03: 模板 skills[] 注入（decisions D2）
    # 模板的 task_type list（如 ["recurring.accounting"]），SessionRunner 启动时
    # 把 skills 转成 system prompt 段（仅路径列表 + 引导语，不注入全文），
    # agent 看到后用 file_read 工具读 SKILL.md。空 list = 不注入。
    template_skills: tuple[str, ...] = field(default_factory=tuple)


# ============================================================================
# 运行结果
# ============================================================================


@dataclass
class RunOutcome:
    """SessionRunner.run() 的返回值。"""

    session_id: str
    status: str  # completed / interrupted / failed
    stop_reason: str = ""  # end_turn / max_iterations / wall_clock_budget_exceeded / interrupted / ...
    iterations: int = 0
    error: str = ""
    # chat-panel-v2 T10（D47）：累计 token 数（prompt + completion），用于顶栏 token 显示
    # B2（spec D2/D3）：删 total_cost_usd，改用 total_tokens 记账（不算 cost 不控制预算）
    total_tokens: int = 0
