"""v6-lite 对话引擎核心库（纯 Python，不依赖 Qt）

模块组织（T01 起）：
- types.py        核心数据类型（Message/Event/Session/LLMRequest/LLMResponse/RunnerDeps/RunnerConfig/ToolCall/ToolResult/ToolExecutor）
- event_store.py  SQLite 持久化（sessions/messages/events/tool_calls 四表，T02+）
- mock_llm.py     MockLLM：脚本化响应（测试用，T01）
- mock_tool_executor.py  MockToolExecutor：脚本化工具结果（测试用，T02）
- runner.py       SessionRunner：主循环（build → llm.call → append → tool_calls? → execute → 回灌 → finalize）
- tool_registry.py  T05: ToolRegistry 从 /openapi.json 物化 catalog
- http_client_tool_executor.py  T05: 真实 HTTP 工具执行器（带 X-Agent-Caller 头，T06+ 审批桥接）
- llm_pool_gateway.py  T05: 真实 LLM gateway（接 /llm/pool/chat-tools）
- reconciler.py   T06d: 启动 reconciliation（streaming→interrupted + yieldMissingToolResultBlocks）
- compactor.py    T08: L2 上下文压缩（Compactor + ContextOverflow）
- l0_artifact_store.py  T08: L0 大工具结果落盘

T01 范围：单轮文本对话端到端。
T02 范围：工具调用回灌端到端（MockLLM 工具轮 + MockToolExecutor）。
T03+：连续工具轮 + 终止 + transition_reason 其他 6 种。
T05+: 真实 server 接入（ToolRegistry + HttpClientToolExecutor + LLMPoolGateway）。
T06+: 审批桥接 + 中断 + 启动恢复。
T08+: 流式（SSE）+ L0 落盘 + L2 压缩（reactive_compact_retry）。
"""

from client.core.agent.builtin_tool_executor import BuiltinToolExecutor
from client.core.agent.compactor import (
    CompactionResult,
    Compactor,
    ContextBudget,
    ContextOverflow,
    PruningConfig,
)
from client.core.agent.doom_loop import (
    DOOM_LOOP_AVOID_REPETITION_PROMPT,
    DOOM_LOOP_STOP_REASON,
    DoomLoopConfig,
    DoomLoopDetector,
)
from client.core.agent.event_store import EventStore
from client.core.agent.facade import APPROVAL_DECISION_PATH, SessionFacade
from client.core.agent.http_client_tool_executor import (
    AGENT_CALLER_HEADER_VALUE,
    HttpClientToolExecutor,
)
from client.core.agent.l0_artifact_store import L0ArtifactStore
from client.core.agent.llm_pool_gateway import CHAT_TOOLS_PATH, STREAM_PATH, LLMPoolGateway
from client.core.agent.mock_llm import MockLLM
from client.core.agent.mock_tool_executor import MockToolExecutor
from client.core.agent.reconciler import ReconcileResult, reconcile
from client.core.agent.runner import SessionRunner
from client.core.agent.template_store import (
    DEFAULT_TEMPLATES_PATH,
    SKILL_INDEX_PATH,
    Template,
    build_skill_path_map,
    format_skills_prompt,
    list_all_task_types,
    resolve_skill_path,
)
from client.core.agent.template_store import (
    create as template_create,
)
from client.core.agent.template_store import (
    delete as template_delete,
)
from client.core.agent.template_store import (
    get as template_get,
)
from client.core.agent.template_store import (
    load_all as template_load_all,
)
from client.core.agent.template_store import (
    save_all as template_save_all,
)
from client.core.agent.template_store import (
    update as template_update,
)
from client.core.agent.tool_registry import ToolEntry, ToolRegistry
from client.core.agent.types import (
    Event,
    LLMGateway,
    LLMRequest,
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    RunOutcome,
    Session,
    ToolCall,
    ToolExecutor,
    ToolResult,
    ToolResultVariant,
)

__all__ = [
    "AGENT_CALLER_HEADER_VALUE",
    "APPROVAL_DECISION_PATH",
    "BuiltinToolExecutor",
    "CHAT_TOOLS_PATH",
    "DEFAULT_TEMPLATES_PATH",
    "CompactionResult",
    "Compactor",
    "ContextBudget",
    "ContextOverflow",
    "DOOM_LOOP_AVOID_REPETITION_PROMPT",
    "DOOM_LOOP_STOP_REASON",
    "DoomLoopConfig",
    "DoomLoopDetector",
    "Event",
    "EventStore",
    "HttpClientToolExecutor",
    "L0ArtifactStore",
    "LLMGateway",
    "LLMPoolGateway",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MockLLM",
    "MockToolExecutor",
    "PruningConfig",
    "ReconcileResult",
    "RunOutcome",
    "RunnerConfig",
    "RunnerDeps",
    "SKILL_INDEX_PATH",
    "STREAM_PATH",
    "Session",
    "SessionFacade",
    "SessionRunner",
    "Template",
    "ToolCall",
    "ToolEntry",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolResultVariant",
    "build_skill_path_map",
    "format_skills_prompt",
    "list_all_task_types",
    "reconcile",
    "resolve_skill_path",
    "template_create",
    "template_delete",
    "template_get",
    "template_load_all",
    "template_save_all",
    "template_update",
]
