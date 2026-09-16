"""v6-lite SessionRunner：对话主循环（T02 含工具回灌 + T08 L2 压缩）

设计依据：
- v6-02 §1：主循环骨架（load → build → llm.call → append → tool_calls? → execute → 回灌 → next_turn）
- v6-02 §3：deps/config 拆分，纯逻辑所有 I/O 通过 deps
- v6-lite §3 W1：T01 单轮文本 + T02 一次工具回灌
- v6-lite §4.1：完整 tool call 解析后再调度（不流式执行工具）
- v6-lite §4.10：tool_calls 表先写 pending 再执行（副作用前落 durable record）
- v6-lite §4.5：error-as-output-variant（工具错误是结构化输出，不抛 traceback）
- v6-lite §3 W5（T08）：L2 上下文压缩 + reactive_compact_retry + ContextOverflow 处理
- v6-02 §2.1：has_attempted_reactive_compact retry 时不重置（防死循环硬规则）

T01 范围（已完成）：
- 主循环：load → build → llm.call → append assistant → 无 tool_calls → finalize
- 预算检查 + max_iterations 守卫

T02 范围（已完成）：
- tool_calls 分支：解析 → 落 pending → running → execute → append_tool_result → next_turn
- transition_reason = "next_turn"

T08 范围（新增）：
- ContextOverflow 异常处理（LLMGateway 检测 413 时抛，runner 捕获后压缩重试）
- has_attempted_reactive_compact 标志位：第一次 413 → 压缩重试；第二次 413 → 明确失败
- compactor.compact() 调用：摘要中间消息 + 保留 head + tail
- transition_reason = "reactive_compact_retry"（压缩后写 transition 事件）

不做（v6-lite §5 / T02 bounds）：
- coordinator.claim（无并发场景）
- recover_pending_inputs（T06 启动恢复）
- stop_hook / verification_nudge（延后清单）
- transition_reason 其他 4 种（T03/T07）
- 审批桥接（T06）
- 中断 / graceful shutdown（T06/T07）
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from client.core.agent.compactor import ContextOverflow
from client.core.agent.doom_loop import (
    DOOM_LOOP_STOP_REASON,
    DoomLoopConfig,
    DoomLoopDetector,
)
from client.core.agent.types import (
    TOOL_SAFETY_READ_ONLY,
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_RUNNING,
    TRANSITION_NEXT_TURN,
    TRANSITION_REACTIVE_COMPACT_RETRY,
    TRANSITION_USER_INTERRUPTED,
    LLMRequest,
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    RunOutcome,
    ToolCall,
    ToolExecutor,
    ToolResult,
)

if TYPE_CHECKING:
    # 仅用于类型注解（_repair_dangling_tool_calls 的 store 参数）。
    # 运行时避免循环导入：EventStore 在 client.core.agent.__init__ 中导入 runner，
    # runner 再导入 EventStore 会循环；用 TYPE_CHECKING 守卫解决。
    from client.core.agent.event_store import EventStore

logger = logging.getLogger("localagent.agent.runner")


# ============================================================================
# v6-lite-chat-fix T01: system prompt 自动注入（模块级工具函数 + 常量）
# - compute_today: 5:00 分界线纯函数
# - _read_stable_prefix_files: 读 AGENTS.md + project_rules.md 全文
# - _call_agent_guide_http: HTTP GET /agent_guide（首轮自动调用）
# - _format_guide_result: guide dict → 可读字符串
# 模块级函数便于测试用 unittest.mock.patch.object(runner_mod, ...) 替换/spy
# ============================================================================

# runner.py 位于 client/core/agent/runner.py → 项目根 = parents[3]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
AGENTS_MD_PATH = _PROJECT_ROOT / "AGENTS.md"
PROJECT_RULES_MD_PATH = _PROJECT_ROOT / ".agents" / "rules" / "project_rules.md"
DEFAULT_GUIDE_BASE_URL = "http://127.0.0.1:8766"
DEFAULT_GUIDE_TIMEOUT_S = 5.0


def compute_today(now: datetime) -> date:
    """5:00 分界线：当前≥05:00 用当天，<05:00 用前一天。

    01:18 → 前一天；14:00 → 当天；05:00 → 当天；04:59 → 前一天。

    spec D2 / 我替领导拍的板：runner 启动时算一次，不每轮重算（跨午夜 5:00 边界可接受）。
    """
    if now.hour >= 5:
        return now.date()
    return (now - timedelta(days=1)).date()


def _merge_tool_call_delta(accumulated: list[dict], tc: dict) -> None:
    """C3（spec D7）：防御性合并 streaming tool_call delta 分片。

    server 端 pool.stream() 已用 ToolCallAccumulator 按 index 合并分片，每个
    tool_call_delta 事件应是完整 tool_call。但 client 端不信任上游，自己再合并
    一次，防止：
      - server 端逻辑变更导致 OpenAI 分片直接透传
      - 未来接入其他 provider 直接透传分片

    判断依据：tc 是否含 ``index`` 字段。
      - 无 index → server 已合并的完整 tool_call，直接 append（当前默认路径，行为不变）
      - 有 index → OpenAI 分片格式，按 index 合并到同 index 的最后一个 tool_call：
        * id / type / function.name 覆盖（若分片携带）
        * function.arguments 字符串拼接

    幂等性：server 已合并时每个事件无 index → 直接 append，与修复前行为一致。
    """
    if not isinstance(tc, dict):
        return

    # 无 index 字段 → server 已合并的完整 tool_call，直接 append
    if "index" not in tc:
        accumulated.append(tc)
        return

    # 有 index 字段 → OpenAI 分片格式，按 index 合并
    idx = tc.get("index")
    fn = tc.get("function") or {}

    for existing in reversed(accumulated):
        if existing.get("index") == idx:
            # 合并 id / type / function.name（分片携带时覆盖，OpenAI 第一个分片带这些字段）
            if tc.get("id"):
                existing["id"] = tc["id"]
            if tc.get("type"):
                existing["type"] = tc["type"]
            if fn.get("name"):
                existing.setdefault("function", {})["name"] = fn["name"]
            # arguments 字符串拼接（OpenAI 分片主要拼接 arguments）
            if fn.get("arguments"):
                existing_fn = existing.setdefault("function", {})
                existing_fn["arguments"] = existing_fn.get("arguments", "") + fn["arguments"]
            return

    # 没找到同 index 的 → 新 tool_call（保留 index 字段供后续分片合并）
    accumulated.append(tc)


def _read_stable_prefix_files() -> str:
    """读取 AGENTS.md + project_rules.md 全文，返回拼接字符串。

    spec D2: stable prefix 注入 AGENTS.md 全文 + project_rules.md 全文
    （用户明确"全都要，不要省"；AGENTS.md 静态不损 prompt cache，token 重 ~30k 可接受）。

    失败时返回空字符串（不阻断 runner，stable prefix 仍含身份铁律）。
    """
    parts: list[str] = []
    for path, label in [
        (AGENTS_MD_PATH, "AGENTS.md"),
        (PROJECT_RULES_MD_PATH, "project_rules.md"),
    ]:
        try:
            content = path.read_text(encoding="utf-8")
            parts.append(f"=== {label} 全文 ===\n{content}")
        except Exception as e:
            logger.warning("读取 %s 失败（stable prefix 缺该文件）: %s", path, e)
    return "\n\n".join(parts)


def _call_agent_guide_http(
    task: str,
    base_url: str = DEFAULT_GUIDE_BASE_URL,
    timeout: float = DEFAULT_GUIDE_TIMEOUT_S,
) -> dict | None:
    """HTTP GET /guide?task=... 调用，返回 dict 或 None（失败）。

    spec D2: runner 首轮自动调 agent_guide(task=用户首句)，不走 MCP
    （runner 是 client 进程，不持有 MCP 连接）。

    路由：server/agent_guide.py 的 router prefix="/guide"，端点 @router.get("")，
    operation_id="agent_guide"。完整路径是 /guide（不是 /agent_guide，否则 404）。

    失败返回 None（_build_ephemeral_context 会写降级标记）。
    """
    try:
        resp = requests.get(
            f"{base_url}/guide",
            params={"task": task},
            timeout=timeout,
            headers={"X-Agent-Caller": "v6-lite-runner"},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
        logger.warning("agent_guide 返回非 dict: %r", type(data).__name__)
        return None
    except Exception as e:
        logger.warning("agent_guide 调用失败 (task=%r): %s", task, e)
        return None


def _format_guide_result(guide: dict) -> str:
    """把 agent_guide 返回的 dict 格式化为可读字符串。

    提取关键字段（spec D2 ephemeral context）：
    task_type / matched_skill / first_action / memory_index / mcp_tools_priority / key_pitfalls
    缺失字段跳过；全缺时返回 mode + match_hint 兜底。
    """
    if not isinstance(guide, dict):
        return f"(agent_guide 返回非 dict: {type(guide).__name__})"
    lines: list[str] = []
    # 优先字段（spec 列出的 5 个）
    for key in (
        "task_type",
        "matched_skill",
        "first_action",
        "memory_index",
        "mcp_tools_priority",
        "key_pitfalls",
    ):
        if key in guide and guide[key] is not None:
            val = guide[key]
            # 列表/字典用 JSON 紧凑展示，标量直接 str
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            lines.append(f"{key}: {val}")
    # 兜底：关键字段全缺时至少展示 mode + match_hint
    if not lines:
        for fallback_key in ("mode", "match_hint"):
            if fallback_key in guide and guide[fallback_key]:
                lines.append(f"{fallback_key}: {guide[fallback_key]}")
    return "\n".join(lines) if lines else "(agent_guide 无可消费字段)"


# ============================================================================
# v6.1 T27: StablePrefixBuilder 五段式（spec D11）
# 段 1 Identity / 段 2 Project context / 段 3 Memory protocols
# 段 4 Tool routing / 段 5 Output contract
# ============================================================================


def _build_stable_prefix() -> str:
    """构建五段式 stable prefix（spec D11）。

    结构：
    1. Identity — 身份铁律 + 核心约束
    2. Project context — AGENTS.md + project_rules.md 全文（参考材料）
    3. Memory protocols — 使用协议 + WHAT_NOT_TO_SAVE + staleness 警告说明
    4. Tool routing — 决策流程 + 优先级 + 安全级别 + 工具结果变体
    5. Output contract — 输出规范

    AGENTS.md 全文 ~10k token + project_rules.md 全文 ~20k token 放在段 2，
    通过段标题让模型知道"这是参考材料，按需查找"，不强制逐字阅读。
    """
    parts: list[str] = [
        _build_identity_section(),
        _build_project_context_section(),
        _build_memory_protocols_section(),
        _build_tool_routing_section(),
        _build_output_contract_section(),
    ]
    return "\n\n".join(parts)


def _build_identity_section() -> str:
    """段 1: Identity（身份铁律）。"""
    return """# Identity

你是 LocalAgent 的 v6-lite chat agent。

项目定位：个人 AI Agent 项目，集合电脑操控 / Agent 工具 / 日常可自动化工作流。

核心约束：
- 不返回大段 base64 到上下文（用 file_read / file_grep / 落盘 path 替代）
- 危险操作必走审批（approval_required 工具等待用户确认，不绕过）
- 中文回复
- 工具调用前用一句话说明意图
- 引用代码/文件用 [文件名](file:///绝对路径) 格式"""


def _build_project_context_section() -> str:
    """段 2: Project context（项目上下文，AGENTS.md + project_rules.md 全文）。"""
    files_content = _read_stable_prefix_files()
    return f"""# Project Context

以下是项目指引文档，作为参考材料按需查找相关段，不必逐字阅读。

{files_content}"""


def _build_memory_protocols_section() -> str:
    """段 3: Memory protocols（记忆使用协议 + WHAT_NOT_TO_SAVE + staleness）。"""
    return """# Memory Protocols

## 使用协议
1. 首轮调 agent_guide(task='用户任务描述') 获取 memory_index（摘要清单，已由 runner 自动注入 ephemeral）
2. 按需调 memory_get(key) / memory_search(query) 读详情
3. 引用记忆中的文件/函数前，用 file_read / file_grep 验证仍存在（TRUSTING_RECALL）

## WHAT_NOT_TO_SAVE（禁止写入记忆）
- 代码模式 / 架构 / 文件路径 / git history（代码本身可推导）
- AGENTS.md / project_rules.md 已有内容
- ephemeral task details（临时任务细节）

可以写入的：debug 解决方案 / 坑点 / 最佳实践（experience 类）、用户偏好 / 身份（user 类）、用户反馈（feedback 类）。
即使看似相关也不要保存上述禁清单内容（君子协议，后端不强制拒绝，但 prompt 强约束）。

## staleness 警告
读到 staleness_warning 时，此记忆可能过时，优先验证后再引用。
- user 类阈值 90 天 / feedback 类 30 天 / project 类 7 天 / reference 类 30 天 / experience 类 14 天
- trust_recall_hint 字段提示：引用文件/函数前请用 file_read/file_grep 验证仍存在"""


def _build_tool_routing_section() -> str:
    """段 4: Tool routing（工具路由协议）。"""
    return """# Tool Routing

## 决策流程
1. 先判断是否需要工具 → 不需要直接回答
2. 需要时按优先级选：
   a. 内置基本工具（file_read / file_grep / ask_user 等，本地执行不经 HTTP）
   b. 项目核心 MCP 工具（memory_get / agent_guide / exec_python 等，已去 mcp 前缀直接暴露）
   c. 本项目 MCP 桶（localagent_advanced_tool 网关，按子桶分类：browser/screen/exec/ocr/vision/memory/miscellaneous）
   d. 其他 MCP 桶（如 lark-cli，调 list_tools 查子工具）
   e. server REST 端点桶（rest_endpoints 虚拟工具，含 ~130 个端点。需要时先 web_fetch /openapi.json 查清单，再调 rest_endpoints(operation_id=..., args=...)）

## 安全级别
- read_only：只读，无副作用
- safe：有副作用但安全
- approval_required：需用户审批（executor 自动处理，结果通过 ToolResult.variant 暴露）
- agent_blocked：agent 不可调

## 工具结果变体（ToolResult.variant）
- SUCCESS：成功
- ERROR：失败（看 error_type 判断原因：http_error / not_found / build_failed / path_forbidden 等）
- USER_DENIED：用户拒绝审批（看 content 是 feedback，换方案）
- APPROVAL_REQUIRED：需审批（executor 内部处理，正常不会暴露给模型）
- TIMEOUT：超时（error_type=approval_timeout 用户没响应 / execution_timeout 工具执行超时）"""


def _build_output_contract_section() -> str:
    """段 5: Output contract（输出契约）。"""
    return """# Output Contract

- 工具调用前用一句话说明意图（"让我读取 X 文件"）
- 错误时说明原因和下一步（不重复同样的失败调用）
- 危险操作前等待审批结果，不绕过
- 中文回复
- 引用文件用 [文件名](file:///绝对路径) 格式
- 大段代码用 markdown 代码块（带语言标签）"""


class SessionRunner:
    """对话主循环（纯逻辑，所有 I/O 通过 deps）。

    使用方式：
        store = EventStore(db_path)
        store.init()
        await store.create_session(session_id="sess-1")
        await store.append_message("sess-1", Message(role="user", content="你好"))

        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(default_text="你好！"),
                event_store=store,
                tool_executor=MockToolExecutor(),  # T02+ 必填
            ),
            config=RunnerConfig(session_id="sess-1"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
    """

    def __init__(self, deps: RunnerDeps, config: RunnerConfig):
        self.deps = deps
        self.config = config
        # transition_reason 供测试断言（T03 验收）
        self.last_transition_reason: str | None = None
        # v6-02 §2.1 防死循环标志位（T07 预留字段，T08 L2 压缩触发 reactive_compact）
        # 硬规则：retry 时不重置（防止 compact + nudge 互触发死循环，
        # cc_src 真实踩坑 "burning thousands of API calls"）
        # T07 范围内只预留字段，实际触发逻辑在 T08（reactive_compact）和延后项（nudge）
        self.has_attempted_reactive_compact: bool = False
        self.has_attempted_nudge: bool = False
        # T02-exec-unify: session 级 terminal 追踪（spec D11 第二层保障）
        # exec_python 返回 terminal_id 后加入此集合，session 结束时统一 kill
        self._session_terminals: set[str] = set()
        # v6-lite-streaming-gui T04: DoomLoopDetector（spec D10/D11）
        # deps.doom_loop_config 注入时创建 detector，None 时禁用
        cfg = deps.doom_loop_config
        if cfg is not None and isinstance(cfg, DoomLoopConfig) and cfg.enabled:
            self._doom_loop_detector: DoomLoopDetector | None = DoomLoopDetector(cfg)
        else:
            self._doom_loop_detector = None
        # DoomLoop retry 计数器（每个 iteration 重置，连续命中累计）
        self._doom_loop_retry_count: int = 0
        # v6-lite-chat-fix T01: system prompt 注入状态
        # _stable_prefix_cache: session 内首次构建后缓存，后续轮次复用（不重复读 AGENTS.md）
        # _build_request_count: 用于判断首轮（==0 表示还未构建过任何 request）
        self._stable_prefix_cache: str | None = None
        self._build_request_count: int = 0

    async def run(self) -> RunOutcome:
        """运行对话主循环（wrapper：try/finally 确保 terminal 清理）。

        T02-exec-unify: spec D11 第二层保障——session 结束时 kill 所有残留 terminal。
        A2（spec D9）：捕获 CancelledError（facade.interrupt() 调 task.cancel() 触发），
        写 transition(user_interrupted) + 返回 RunOutcome(interrupted)。
        LLM hang 时 _run_loop 卡在 await 点不检查 interrupt_event，必须靠 task.cancel()
        强制中止。
        """
        try:
            outcome = await self._run_loop()
            return outcome
        except asyncio.CancelledError:
            # A2：facade.interrupt() 调 task.cancel() → CancelledError 中止 await
            # 检查是否是 interrupt 触发的 cancel（ev 已 set），不是则 re-raise
            if not self._is_interrupted():
                raise
            # facade.interrupt() 触发，写 transition + 返回 interrupted
            session_id = self.config.session_id
            store = self.deps.event_store
            if store is not None:
                try:
                    await asyncio.shield(self._handle_cancel_interrupt(session_id, store))
                except asyncio.CancelledError:
                    pass  # shield 内再次 cancel，忽略
            return RunOutcome(
                session_id=session_id, status="interrupted",
                stop_reason="user_interrupted",
                iterations=0,
                total_tokens=0,
            )
        finally:
            await self._cleanup_terminals()

    async def _handle_cancel_interrupt(self, session_id: str, store) -> None:
        """A2：CancelledError 捕获后写 transition(user_interrupted) + 更新 session 状态。

        用 asyncio.shield 保护，防止再次 cancel 中断写库。
        """
        await store.append_event(
            session_id, "transition",
            {
                "reason": TRANSITION_USER_INTERRUPTED,
                "iterations": 0,
                "tool_calls_count": 0,
            },
        )
        await store.update_session_status(session_id, "interrupted")
        self.last_transition_reason = TRANSITION_USER_INTERRUPTED
        logger.info(
            "Session %s cancelled via task.cancel() (LLM hang interrupt)",
            session_id,
        )

    async def _run_loop(self) -> RunOutcome:
        """运行对话主循环，直到终止条件命中。

        终止条件：
        - LLM 响应无 tool_calls → finalize（正常完成）
        - iterations 达 max_iterations → failed（max_iterations）
        - B2（spec D2/D3）：删 budget_exceeded 路径（不算 cost 不控制预算，只记账 token）
        """
        session_id = self.config.session_id
        store = self.deps.event_store
        if store is None:
            raise RuntimeError("RunnerDeps.event_store is required (T01+)")
        tool_executor = self.deps.tool_executor
        tool_registry = self.deps.tool_registry
        # T02+ 若 LLM 可能返回 tool_calls，tool_executor 必填
        # T01 单轮文本对话场景允许 tool_executor=None（向后兼容）

        # 标记 streaming（v6-02 §1 主循环入口）
        await store.update_session_status(session_id, "streaming")

        iterations = 0
        total_tokens = 0  # chat-panel-v2 T10（D47）：累计 token 用于顶栏显示
        # A4（spec D5/D21）：wall_clock_budget 起始时间
        # 0 = 不限制（默认），> 0 时按 wall_clock_budget_secs 限制总运行时长
        # D21：超时不立即终止，而是在步骤边界（不调 LLM / 不执行 tool_calls）优雅停止
        wall_budget = self.config.wall_clock_budget_secs
        run_start_ts = self.deps.clock() if wall_budget > 0 else 0.0

        while iterations < self.config.max_iterations:
            iterations += 1

            # T06c: 中断检查（每轮顶部）——外部调 interrupt_event.set() 后，
            # 下一轮检查到即写 transition(user_interrupted) 事件并终止。
            # 不依赖异常传播（v6-lite §3 W3）。
            if self._is_interrupted():
                await store.append_event(
                    session_id, "transition",
                    {
                        "reason": TRANSITION_USER_INTERRUPTED,
                        "iterations": iterations - 1,
                        "tool_calls_count": 0,
                    },
                )
                await store.update_session_status(session_id, "interrupted")
                self.last_transition_reason = TRANSITION_USER_INTERRUPTED
                logger.info(
                    "Session %s interrupted by user at iteration %d, tokens=%d",
                    session_id, iterations - 1, total_tokens,
                )
                # E6：persist runner state before return（中断路径落库）
                await self._persist_runner_state(
                    session_id, store, iterations - 1, total_tokens,
                )
                return RunOutcome(
                    session_id=session_id, status="interrupted",
                    stop_reason="user_interrupted",
                    iterations=iterations - 1,
                    total_tokens=total_tokens,
                )

            # A4（spec D5/D21）：wall_clock_budget 检查（循环顶部，不调 LLM）
            # 若启用且 elapsed > budget → 不开始下一步（不调 LLM），优雅返回 interrupted
            # D21：不在 LLM 调用中途终止，只在步骤边界检查
            if wall_budget > 0:
                elapsed = self.deps.clock() - run_start_ts
                if elapsed > wall_budget:
                    await store.append_event(
                        session_id, "wall_clock_budget_exceeded",
                        {
                            "elapsed_secs": elapsed,
                            "budget_secs": wall_budget,
                            "iterations": iterations - 1,
                        },
                    )
                    await store.update_session_status(session_id, "interrupted")
                    logger.info(
                        "Session %s wall_clock_budget exceeded at iteration %d: "
                        "elapsed=%.1fs > budget=%ds (not starting next LLM turn)",
                        session_id, iterations - 1, elapsed, wall_budget,
                    )
                    # E6：persist runner state before return（wall_clock 超时路径落库）
                    await self._persist_runner_state(
                        session_id, store, iterations - 1, total_tokens,
                        wall_clock_exceeded=True,
                    )
                    return RunOutcome(
                        session_id=session_id, status="interrupted",
                        stop_reason="wall_clock_budget_exceeded",
                        iterations=iterations - 1,
                        total_tokens=total_tokens,
                    )

            # 1. 加载会话消息（load_messages 默认跳过 visible=0 的 synthetic 消息）
            messages = await store.load_messages(session_id)

            # C4（spec D7 数据完整性）：_build_request 前扫描 dangling tool_calls
            # 上一轮 LLM 返回 tool_calls，部分执行后中断/崩溃 → 缺 tool_result →
            # OpenAI 协议要求每个 tool_call 必须配一条 role=tool 消息，否则 400。
            # C4 在循环内每轮 _build_request 前补 is_error=true 的 tool_result。
            await self._repair_dangling_tool_calls(messages, session_id, store)
            # 若补了 tool_result，需重新加载 messages（包含新增的 tool 消息）
            messages = await store.load_messages(session_id)

            # 2. 构建 LLM 请求
            # to_thread（2026-09-13 code review 8-1）：_build_request →
            # _build_system_prompt → 首轮同步 requests.get 调 agent_guide（5s
            # timeout），不包线程会阻塞 worker 事件循环
            request = await asyncio.to_thread(self._build_request, messages)

            # 3. 调 LLM
            # v6-lite-streaming-gui T02: use_stream=True 时用 gateway.stream() 流式调用
            # T08: ContextOverflow 捕获 → 触发 reactive_compact_retry
            # 防死循环硬规则（v6-02 §2.1）：has_attempted_reactive_compact retry 时不重置
            # 第一次 413 → 压缩重试；第二次 413 → 明确失败（不循环）
            try:
                if self.config.use_stream:
                    response, streaming_seqs = await self._stream_llm(
                        request, session_id, store,
                        wall_budget=wall_budget,
                        run_start_ts=run_start_ts,
                    )
                else:
                    response = await self.deps.llm_gateway.call(request)
                    streaming_seqs = []
            except ContextOverflow as e:
                # 已尝试过压缩仍 413 → 明确失败（不循环）
                if self.has_attempted_reactive_compact:
                    await store.append_event(
                        session_id, "context_overflow_after_compact",
                        {
                            "error": str(e),
                            "iterations": iterations,
                            "status_code": getattr(e, "status_code", 413),
                        },
                    )
                    await store.update_session_status(session_id, "failed")
                    logger.warning(
                        "Session %s context overflow after compact (fail): %s",
                        session_id, e,
                    )
                    # E6：persist runner state before return（failed 路径落库）
                    await self._persist_runner_state(
                        session_id, store, iterations, total_tokens,
                    )
                    return RunOutcome(
                        session_id=session_id, status="failed",
                        stop_reason="context_overflow_after_compact",
                        iterations=iterations,
                        error=f"context overflow after reactive compact: {e}",
                        total_tokens=total_tokens,
                    )
                # 首次 413 → 检查 compactor 是否注入
                compactor = self.deps.compactor
                if compactor is None:
                    # 无 compactor → 无法恢复，直接失败
                    await store.append_event(
                        session_id, "context_overflow_no_compactor",
                        {"error": str(e), "iterations": iterations},
                    )
                    await store.update_session_status(session_id, "failed")
                    logger.warning(
                        "Session %s context overflow but no compactor injected (fail): %s",
                        session_id, e,
                    )
                    # E6：persist runner state before return（failed 路径落库）
                    await self._persist_runner_state(
                        session_id, store, iterations, total_tokens,
                    )
                    return RunOutcome(
                        session_id=session_id, status="failed",
                        stop_reason="context_overflow_no_compactor",
                        iterations=iterations,
                        error=f"context overflow but no compactor: {e}",
                        total_tokens=total_tokens,
                    )
                # 首次 413 → 标记已尝试 → 压缩 → 替换 messages → 写 transition → 重试
                self.has_attempted_reactive_compact = True
                try:
                    # T31c: 用 compact_with_result() 获得精确的 compaction_occurred 信号，
                    # 替代旧的 "compacted is messages or len(compacted) == len(messages)" 启发式判断
                    compact_result = await compactor.compact_with_result(
                        session_id, messages, self.deps.llm_gateway,
                    )
                    compacted = compact_result.messages
                except Exception as compact_err:
                    # compactor 内部异常（不应发生，compactor 已 fail-open）→ 失败
                    await store.append_event(
                        session_id, "compact_failed",
                        {"error": f"{type(compact_err).__name__}: {compact_err}", "iterations": iterations},
                    )
                    await store.update_session_status(session_id, "failed")
                    logger.error(
                        "Session %s compactor.compact() raised: %s", session_id, compact_err,
                    )
                    # E6：persist runner state before return（failed 路径落库，含 has_attempted=true）
                    await self._persist_runner_state(
                        session_id, store, iterations, total_tokens,
                    )
                    return RunOutcome(
                        session_id=session_id, status="failed",
                        stop_reason="compact_failed",
                        iterations=iterations,
                        error=f"compactor.compact() raised: {compact_err}",
                        total_tokens=total_tokens,
                    )
                # T31c: 用 compaction_occurred 精确判断是否真的压缩了
                # False = skip（消息太少）/ fail-open（LLM 失败）→ 不再重试，直接失败
                if not compact_result.compaction_occurred:
                    await store.append_event(
                        session_id, "compact_noop",
                        {
                            "error": "compactor did not compact (too few or fail-open)",
                            "iterations": iterations,
                        },
                    )
                    await store.update_session_status(session_id, "failed")
                    logger.warning(
                        "Session %s compactor noop (compaction_occurred=False), fail: %s",
                        session_id, e,
                    )
                    # E6：persist runner state before return（failed 路径落库）
                    await self._persist_runner_state(
                        session_id, store, iterations, total_tokens,
                    )
                    return RunOutcome(
                        session_id=session_id, status="failed",
                        stop_reason="compact_noop",
                        iterations=iterations,
                        error=f"compactor noop after context overflow: {e}",
                        total_tokens=total_tokens,
                    )
                # 替换 messages（持久化压缩结果）
                await store.replace_messages(session_id, compacted)
                # 写 transition(reactive_compact_retry) 事件
                self.last_transition_reason = TRANSITION_REACTIVE_COMPACT_RETRY
                await store.append_event(session_id, "transition", {
                    "reason": TRANSITION_REACTIVE_COMPACT_RETRY,
                    "iterations": iterations,
                    "original_messages_count": len(messages),
                    "compacted_messages_count": len(compacted),
                    "status_code": getattr(e, "status_code", 413),
                })
                # E7（spec D24）：写 compactions 表（压缩历史持久化）
                # 记录 source_seq + summary + recent_json + prompt_hash，供审计
                try:
                    import hashlib
                    summary_text = (
                        compact_result.summary_message.content
                        if compact_result.summary_message else ""
                    )
                    # recent_json = tail messages（compacted[1:]，去掉 summary_msg）
                    tail_msgs = compacted[1:] if len(compacted) > 1 else []
                    recent_payload = [
                        {"role": m.role, "content": str(m.content) if m.content else ""}
                        for m in tail_msgs
                    ]
                    recent_json = json.dumps(recent_payload, ensure_ascii=False)
                    # prompt_hash = 原始 messages 的 hash（去重/审计）
                    orig_payload = [
                        {"role": m.role, "content": str(m.content) if m.content else ""}
                        for m in messages
                    ]
                    prompt_hash = hashlib.sha256(
                        json.dumps(orig_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:16]
                    await store.append_compaction(
                        session_id=session_id,
                        source_seq=iterations,
                        summary=summary_text,
                        recent_json=recent_json,
                        prompt_hash=prompt_hash,
                    )
                except Exception as compaction_err:
                    logger.warning(
                        "E7: append_compaction failed (non-fatal): %s", compaction_err,
                    )
                logger.info(
                    "Session %s context overflow → compacted %d→%d msgs, retrying iteration %d",
                    session_id, len(messages), len(compacted), iterations,
                )
                # 继续循环（重新 load_messages 取得 compacted 版本，重建 request，再调 LLM）
                continue

            # 4. B2（spec D2/D3）：累计 token（不算 cost 不控制预算，只记账）
            # chat-panel-v2 T10（D47）：累计 token（prompt + completion，从 LLM usage）
            _usage = response.usage or {}
            _prompt_tokens = int(_usage.get("prompt_tokens") or 0)
            _completion_tokens = int(_usage.get("completion_tokens") or 0)
            _total = int(_usage.get("total_tokens") or (_prompt_tokens + _completion_tokens))
            total_tokens += _total

            # E6（spec D23）：每轮 LLM 响应处理后持久化 runner 状态快照
            # 捕获 iterations / has_attempted_reactive_compact / total_tokens / wall_clock_exceeded
            await self._persist_runner_state(
                session_id, store, iterations, total_tokens,
                wall_clock_exceeded=bool(getattr(response, "wall_clock_exceeded", False)),
            )

            # D5（spec D20）：usage 持久化到 usage 表
            # 失败/断流也落库（中断路径仍执行到此处的上面，下面会落 usage）
            try:
                await store.append_usage(
                    session_id=session_id,
                    seq=iterations,
                    prompt_tokens=_prompt_tokens,
                    completion_tokens=_completion_tokens,
                    total_tokens=_total,
                    model=response.model or "",
                )
            except Exception as e:
                logger.warning("D5: append_usage failed (non-fatal): %s", e)

            # 4.5 v6-lite-streaming-gui T02: stream 模式中断检查
            # _stream_llm 在 chunk 间检查中断，中断时 stop_reason="user_interrupted"
            # 主循环在此走中断路径（不 append assistant 消息，不 finalize）
            if response.stop_reason == "user_interrupted":
                # C1（spec D7）：stream 中断时把已收到的 partial text 作为 partial message 落库
                # source="partial" + visible=True：reconciler/UI 可识别，与完整 assistant 消息区分
                if response.content:
                    partial_msg = Message(
                        role="assistant",
                        content=response.content,
                        source="partial",
                        visible=True,
                        thinking=response.thinking,
                    )
                    await store.append_message(session_id, partial_msg)
                # 标记 streaming 事件 invalidated（中断后不保留增量事件，partial message 已是合并态）
                if streaming_seqs:
                    await store.invalidate_events(session_id, streaming_seqs)
                await store.append_event(
                    session_id, "transition",
                    {
                        "reason": TRANSITION_USER_INTERRUPTED,
                        "iterations": iterations,
                        "tool_calls_count": 0,
                    },
                )
                await store.update_session_status(session_id, "interrupted")
                self.last_transition_reason = TRANSITION_USER_INTERRUPTED
                logger.info(
                    "Session %s stream interrupted by user at iteration %d, tokens=%d",
                    session_id, iterations, total_tokens,
                )
                return RunOutcome(
                    session_id=session_id, status="interrupted",
                    stop_reason="user_interrupted",
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # 4.6 B1（spec D10 重构）：DoomLoop 检测后停止 + 写 sys 系统消息
            # _stream_llm 检测到 thinking_delta 尾重复时 stop_reason="doom_loop"
            # D10 决策：不再 retry / 不注入 nudge，改为：
            #   1. 写 sys 系统消息（source="system_warning", visible=True, LLM 不可见）
            #   2. 返回 RunOutcome(status="interrupted", stop_reason="doom_loop_detected")
            # 用户消息作为隐式 nudge：用户看到 sys 警告后，决定换种问法或新开对话
            if response.stop_reason == DOOM_LOOP_STOP_REASON:
                # C1（spec D7）：DoomLoop abort 时把已收到的 partial text 作为 partial message 落库
                if response.content:
                    partial_msg = Message(
                        role="assistant",
                        content=response.content,
                        source="partial",
                        visible=True,
                    )
                    await store.append_message(session_id, partial_msg)
                # 标记 streaming 事件 invalidated（DoomLoop abort 后不保留增量事件）
                if streaming_seqs:
                    await store.invalidate_events(session_id, streaming_seqs)
                # 写 sys 系统消息（用户可见，LLM 不可见，由 _build_request 过滤）
                sys_msg = Message(
                    role="user",
                    content=(
                        "⚠️ 检测到模型推理循环，已停止当前会话。\n\n"
                        "建议：新开对话，或换种问法 / 补充新信息后重试。"
                    ),
                    source="system_warning",
                    visible=True,
                )
                await store.append_message(session_id, sys_msg)
                # 写 transition(doom_loop_detected) 事件
                await store.append_event(session_id, "transition", {
                    "reason": "doom_loop_detected",
                    "iterations": iterations,
                })
                await store.update_session_status(session_id, "interrupted")
                logger.warning(
                    "Session %s DoomLoop detected at iteration %d, stopping (no retry)",
                    session_id, iterations,
                )
                return RunOutcome(
                    session_id=session_id, status="interrupted",
                    stop_reason="doom_loop_detected",
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # DoomLoop 未命中，重置 retry 计数器（保留兼容，B1 后不再使用但字段保留）
            if self._doom_loop_retry_count > 0:
                self._doom_loop_retry_count = 0

            # 5. 追加 assistant 消息（v6-02 §1：store.append_assistant_and_tool_calls）
            # v6-lite-streaming-gui T02: stream 模式下带 thinking 字段
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                source="assistant",
                tool_calls=response.tool_calls,
                thinking=response.thinking,
                model=response.model or "",
            )
            appended = await store.append_message(session_id, assistant_msg)
            # T02: stream 模式下，完整 Message 已落库，标记 streaming 事件 invalidated
            # （防止下次启动 reconciliation 再次合并这些增量 → 重复 Message）
            if streaming_seqs:
                await store.invalidate_events(session_id, streaming_seqs)

            # E5（spec D7 数据完整性）：model_error + tool_calls → 不执行 tool_calls，
            # 补 is_error=true tool_result（yieldMissingToolResultBlocks 三处全覆盖之一）。
            # provider_error 时 tool_calls 可能有 corrupt arguments（流式中断），
            # 执行它们有风险；OpenAI 协议要求 tool_calls 必须配 tool_result，所以补 error result。
            if response.stop_reason == "error" and response.tool_calls:
                # 写 tool_calls 表为 pending（与正常路径一致，便于审计/恢复）
                parsed_calls_err = [
                    ToolCall.from_openai_tool_call(
                        tc, session_id=session_id, seq=appended.seq,
                    )
                    for tc in response.tool_calls
                ]
                for tc in parsed_calls_err:
                    await store.append_tool_call(session_id, tc)
                # 补 is_error=true tool_result（防 dangling，防 provider 400）
                messages_after_err = await store.load_messages(session_id)
                await self._repair_dangling_tool_calls(messages_after_err, session_id, store)
                await store.append_event(session_id, "transition", {
                    "reason": "model_error",
                    "iterations": iterations,
                    "tool_calls_count": len(response.tool_calls),
                })
                await store.update_session_status(session_id, "failed")
                logger.warning(
                    "Session %s model_error at iteration %d with %d tool_calls, "
                    "filled is_error tool_results and returning failed",
                    session_id, iterations, len(response.tool_calls),
                )
                return RunOutcome(
                    session_id=session_id, status="failed",
                    stop_reason="provider_error",
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # 6. 终止判断：无 tool_calls → finalize（T01 唯一终止路径）
            if not response.tool_calls:
                await store.finalize_session(session_id)
                logger.info(
                    "Session %s completed: iterations=%d, tokens=%d, stop_reason=%s",
                    session_id, iterations, total_tokens, response.stop_reason,
                )
                return RunOutcome(
                    session_id=session_id, status="completed",
                    stop_reason=response.stop_reason,
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # E1（spec D21）：wall_clock_budget SSE 循环内检查
            # response.wall_clock_exceeded=True 时不进 tool_calls 分支
            # （assistant 消息已落库，tool_calls 保留在消息中但不执行）
            if response.wall_clock_exceeded:
                elapsed = self.deps.clock() - run_start_ts
                await store.append_event(
                    session_id, "wall_clock_budget_exceeded",
                    {
                        "elapsed_secs": elapsed,
                        "budget_secs": wall_budget,
                        "iterations": iterations,
                        "skipped_tool_calls": len(response.tool_calls),
                    },
                )
                # E5（spec D7）：abort 路径补 is_error=true tool_result（防 dangling）
                # assistant 消息已带 tool_calls 落库，但都不会执行 → 补 error result
                messages_after_wc = await store.load_messages(session_id)
                await self._repair_dangling_tool_calls(messages_after_wc, session_id, store)
                await store.update_session_status(session_id, "interrupted")
                logger.info(
                    "Session %s wall_clock_budget exceeded (E1 flag): "
                    "elapsed=%.1fs > budget=%ds, skipping %d tool_calls",
                    session_id, elapsed, wall_budget, len(response.tool_calls),
                )
                return RunOutcome(
                    session_id=session_id, status="interrupted",
                    stop_reason="wall_clock_budget_exceeded",
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # 7. T02+ tool_calls 分支
            # 完整 tool call 解析后再调度（v6-lite §4.1，不流式执行工具）
            if tool_executor is None:
                # T01 向后兼容：tool_executor 未注入但有 tool_calls → 抛错
                raise RuntimeError(
                    "RunnerDeps.tool_executor is None but LLM returned tool_calls; "
                    "inject MockToolExecutor for tests or HttpClientToolExecutor for production (T02+)"
                )

            # 解析所有 tool_calls（OpenAI 格式 → ToolCall）
            parsed_calls = [
                ToolCall.from_openai_tool_call(
                    tc, session_id=session_id, seq=appended.seq,
                )
                for tc in response.tool_calls
            ]
            # safety 默认 read_only（T05+ 由 ToolRegistry 填）
            # T05+: 若 tool_registry 注入，按 operation_id 查 ToolEntry 取 safety
            for tc in parsed_calls:
                if tool_registry is not None:
                    entry = tool_registry.lookup(tc.name)
                    if entry is not None:
                        tc.safety = entry.safety
                        continue
                # fallback：默认 read_only（T02 行为，向后兼容）
                if not tc.safety:
                    tc.safety = TOOL_SAFETY_READ_ONLY

            # tool_calls 表先写 pending 再执行（v6-lite §4.10，副作用前落 durable record）
            for tc in parsed_calls:
                await store.append_tool_call(session_id, tc)

            # A4（spec D5/D21）：wall_clock_budget 检查（工具执行前，不执行 tool_calls）
            # D21：LLM 调用刚完成，若超 budget 则不执行刚返回的 tool_calls，优雅返回
            # 已落 pending 的 tool_calls 不执行（C4/E4 兜底机制下轮补 is_error tool_result）
            if wall_budget > 0:
                elapsed = self.deps.clock() - run_start_ts
                if elapsed > wall_budget:
                    await store.append_event(
                        session_id, "wall_clock_budget_exceeded",
                        {
                            "elapsed_secs": elapsed,
                            "budget_secs": wall_budget,
                            "iterations": iterations,
                            "pending_tool_calls": len(parsed_calls),
                        },
                    )
                    # E5（spec D7）：abort 路径补 is_error=true tool_result（防 dangling）
                    # tool_calls 已落 pending 但不会执行 → 立即补 error result，保证 DB 一致
                    messages_after_wc2 = await store.load_messages(session_id)
                    await self._repair_dangling_tool_calls(messages_after_wc2, session_id, store)
                    await store.update_session_status(session_id, "interrupted")
                    logger.info(
                        "Session %s wall_clock_budget exceeded before tool_calls at iteration %d: "
                        "elapsed=%.1fs > budget=%ds (%d tool_calls not executed)",
                        session_id, iterations, elapsed, wall_budget, len(parsed_calls),
                    )
                    return RunOutcome(
                        session_id=session_id, status="interrupted",
                        stop_reason="wall_clock_budget_exceeded",
                        iterations=iterations,
                        total_tokens=total_tokens,
                    )

            # 逐个执行：pending → running → execute → completed/failed
            # T05+ 可并发读操作（v6-02 §1 注释："读操作可并发，其他按资源锁串行"）
            # T02 简化：串行执行
            # A1（spec D9）：每个 tool_call 执行前检查 interrupt_event，命中则 break
            # 工具循环 + 写 transition(user_interrupted) + 返回 interrupted。
            # 已执行的 tool_result 保留，未执行的不补（OpenAI 协议允许 tool_call 无
            # result，由 C4/E4 的 HasDanglingToolCalls 检查在下轮 _build_request 前补
            # is_error=true 的 tool_result 兜底）。
            interrupted_in_loop = False
            for tc in parsed_calls:
                if self._is_interrupted():
                    interrupted_in_loop = True
                    break
                started_at = self.deps.clock()
                await store.update_tool_call_status(
                    tc.id, TOOL_STATUS_RUNNING, started_at=started_at,
                )
                # 执行（error-as-output-variant：executor 不抛异常，失败返回 is_error=True）
                # T02-exec-unify: exec_python 包装 3 分钟唤醒（spec D6）
                # T26: 内置工具走 builtin_executor（不经 HTTP），其他走 tool_executor
                if tc.name == "exec_python":
                    result = await self._execute_with_wakeup(tc, tool_executor, session_id, store)
                elif (
                    self.deps.builtin_executor is not None
                    and self.deps.builtin_executor.has_tool(tc.name)
                ):
                    result = await self.deps.builtin_executor.execute(tc)
                else:
                    result = await tool_executor.execute(tc)
                ended_at = self.deps.clock()
                final_status = TOOL_STATUS_FAILED if result.is_error else TOOL_STATUS_COMPLETED
                await store.update_tool_call_status(
                    tc.id, final_status, ended_at=ended_at,
                )
                # 工具结果作为 role=tool 消息回灌（OpenAI 协议：每个 tool_call 必须配一条 tool_result）
                await store.append_tool_result_message(session_id, result)
                # T02-exec-unify: 追踪 terminal_id（spec D11 第二层保障）
                if tc.name == "exec_python" and not result.is_error:
                    tid = self._extract_terminal_id(result)
                    if tid:
                        self._session_terminals.add(tid)

            # A1：工具循环中 interrupt 命中 → 写 transition(user_interrupted) + 返回
            # E5（spec D7）：未执行的 tool_calls 补 is_error=true tool_result（防 dangling）
            if interrupted_in_loop:
                # 重新加载 messages 后扫描 dangling（assistant.tool_calls 中
                # 缺 tool_result 的 id），补 is_error=true ToolResult。
                # 防止下次启动 _build_request 时 provider 返回 400
                # "tool_calls without matching tool messages"。
                messages_after_interrupt = await store.load_messages(session_id)
                await self._repair_dangling_tool_calls(
                    messages_after_interrupt, session_id, store,
                )
                await store.append_event(
                    session_id, "transition",
                    {
                        "reason": TRANSITION_USER_INTERRUPTED,
                        "iterations": iterations,
                        "tool_calls_count": len(parsed_calls),
                    },
                )
                await store.update_session_status(session_id, "interrupted")
                self.last_transition_reason = TRANSITION_USER_INTERRUPTED
                logger.info(
                    "Session %s interrupted during tool loop at iteration %d "
                    "(%d tool_calls parsed, interrupted before completion)",
                    session_id, iterations, len(parsed_calls),
                )
                return RunOutcome(
                    session_id=session_id, status="interrupted",
                    stop_reason="user_interrupted",
                    iterations=iterations,
                    total_tokens=total_tokens,
                )

            # 8. 写 transition 事件（transition_reason = next_turn，v6-02 §2）
            self.last_transition_reason = TRANSITION_NEXT_TURN
            await store.append_event(session_id, "transition", {
                "reason": TRANSITION_NEXT_TURN,
                "iterations": iterations,
                "tool_calls_count": len(parsed_calls),
            })

            # 9. 继续循环（build 下一轮，模型看到 tool_result 后决定是否再调工具或终止）
            logger.debug(
                "Session %s iteration %d: %d tool_calls executed, transitioning to next_turn",
                session_id, iterations, len(parsed_calls),
            )

        # max_iterations 守卫
        await store.append_event(
            session_id, "max_iterations_reached",
            {"max_iterations": self.config.max_iterations, "total_tokens": total_tokens},
        )
        await store.update_session_status(session_id, "failed")
        logger.warning(
            "Session %s max_iterations reached: %d iterations, tokens=%d",
            session_id, iterations, total_tokens,
        )
        return RunOutcome(
            session_id=session_id, status="failed",
            stop_reason="max_iterations",
            iterations=iterations,
            error=f"reached max_iterations={self.config.max_iterations}",
            total_tokens=total_tokens,
        )

    # ------------------------------------------------------------------
    # v6-lite-streaming-gui T02: 流式 LLM 调用（spec D05/D14/D15）
    # ------------------------------------------------------------------

    async def _stream_llm(
        self, request: LLMRequest, session_id: str, store,
        wall_budget: float = 0.0,
        run_start_ts: float = 0.0,
    ) -> tuple[LLMResponse, list[int]]:
        """流式调用 LLM，逐事件处理 + 写 streaming 事件到 EventStore。

        流程（spec D05）：
        1. async for event in gateway.stream(request)
        2. text_delta → 累积 text + 写 streaming_text_delta 事件
        3. thinking_delta → 累积 thinking + 写 streaming_thinking_delta 事件（DoomLoop 在 T04 接入）
        4. tool_call_delta → 累积 tool_calls + 写 streaming_tool_call 事件
        5. usage → 累积 usage
        6. context_overflow → 抛 ContextOverflow（由主循环 except 捕获走 reactive_compact_retry）
        7. provider_error → break（error-as-output-variant）
        8. done → break
        9. 每 chunk 后检查中断（_is_interrupted）
        10. E1（spec D21）：每事件检查 wall_clock_budget，超时设 wall_clock_exceeded=True（不中断流）

        Args:
            wall_budget: wall_clock_budget_secs（0 = 不限制）
            run_start_ts: run() 起始时间戳（wall_budget>0 时有效）

        Returns:
            (LLMResponse, streaming_seqs)
            - LLMResponse: 累积的 content/thinking/tool_calls/usage/stop_reason/wall_clock_exceeded
            - streaming_seqs: 本次 stream 写入的 streaming 事件 seq 列表
              （主循环写完整 Message 后调 invalidate_events 标记这些 seq）
        """
        # streaming 事件 trace_id（同一次 stream 调用的所有事件共享，reconciler 按此分组）
        trace_id = self.deps.uuid()

        accumulated_text = ""
        accumulated_thinking = ""
        accumulated_tool_calls: list[dict] = []
        usage: dict = {}
        # B2（spec D2/D3）：删 cost_usd 累积（不算 cost，只记账 token）
        stop_reason = "end_turn"
        model = ""
        streaming_seqs: list[int] = []
        # E1（spec D21）：wall_clock 超时标记（不中断流，runner 看到 flag 后处理）
        wall_clock_exceeded = False

        async for event in self.deps.llm_gateway.stream(request):
            # 每 chunk 检查中断（spec D14：用户中断 → break 流）
            if self._is_interrupted():
                logger.info(
                    "Session %s stream interrupted by user mid-stream, aborting",
                    session_id,
                )
                stop_reason = "user_interrupted"
                break

            # E1（spec D21）：wall_clock_budget SSE 循环内检查
            # 超时设 wall_clock_exceeded=True，但不立即终止 streaming（D21：等当前流完成）
            if wall_budget > 0 and not wall_clock_exceeded:
                elapsed = self.deps.clock() - run_start_ts
                if elapsed > wall_budget:
                    wall_clock_exceeded = True
                    logger.info(
                        "Session %s wall_clock_budget exceeded mid-stream "
                        "(elapsed=%.1fs > budget=%ds), flag set (stream continues)",
                        session_id, elapsed, wall_budget,
                    )

            ev_type = event.get("type")
            # 服务端 pool 包装层给每个事件都附实际选中的 model（setdefault），
            # 这里捕获一次，落库到 assistant message（UI 气泡下方小字显示）
            if not model:
                model = str(event.get("model") or "")
            if ev_type == "text_delta":
                delta = event.get("delta", "")
                accumulated_text += delta
                seq = await store.append_event(
                    session_id, "streaming_text_delta",
                    {"delta": delta}, trace_id=trace_id,
                )
                streaming_seqs.append(seq)
            elif ev_type == "thinking_delta":
                delta = event.get("delta", "")
                accumulated_thinking += delta
                # v6-lite-streaming-gui T04: DoomLoop 检测（spec D10）
                # 命中时 break 流，stop_reason=doom_loop，主循环走 retry 路径
                if self._doom_loop_detector is not None:
                    if self._doom_loop_detector.on_thinking_delta(delta):
                        logger.warning(
                            "Session %s DoomLoop detected mid-stream, aborting",
                            session_id,
                        )
                        stop_reason = DOOM_LOOP_STOP_REASON
                        break
                seq = await store.append_event(
                    session_id, "streaming_thinking_delta",
                    {"delta": delta}, trace_id=trace_id,
                )
                streaming_seqs.append(seq)
            elif ev_type == "tool_call_delta":
                tc = event.get("tool_call", {})
                # C3（spec D7）：防御性合并分片（server 端已合并时幂等，分片透传时按 index 合并）
                _merge_tool_call_delta(accumulated_tool_calls, tc)
                seq = await store.append_event(
                    session_id, "streaming_tool_call",
                    {"tool_call": tc}, trace_id=trace_id,
                )
                streaming_seqs.append(seq)
            elif ev_type == "usage":
                usage = {
                    "prompt_tokens": event.get("prompt_tokens", 0),
                    "completion_tokens": event.get("completion_tokens", 0),
                    "total_tokens": event.get("total_tokens", 0),
                }
                # B2（spec D2/D3）：usage 事件不再带 cost_usd（字段已删，只记账 token）
                # usage 事件不写 streaming 事件（非增量，是最终统计）
            elif ev_type == "context_overflow":
                # 413 → 抛 ContextOverflow（主循环 except 捕获走 reactive_compact_retry）
                err = event.get("error", "context overflow")
                status_code = event.get("status_code", 413)
                # C1（spec D7）：context_overflow 前已收到的 partial text 作为 partial message 落库
                # （ContextOverflow 抛出后主循环不再有 accumulated_text，必须在 raise 前落库）
                if accumulated_text:
                    partial_msg = Message(
                        role="assistant",
                        content=accumulated_text,
                        source="partial",
                        visible=True,
                    )
                    await store.append_message(session_id, partial_msg)
                # 先标记 streaming 事件 invalidated（避免 reconciliation 重复合并）
                if streaming_seqs:
                    await store.invalidate_events(session_id, streaming_seqs)
                raise ContextOverflow(err, status_code=status_code)
            elif ev_type == "provider_error":
                # error-as-output-variant：break，构造 error LLMResponse
                err = event.get("error", "provider error")
                logger.warning(
                    "Session %s stream provider_error: %s", session_id, err,
                )
                accumulated_text = accumulated_text or f"[stream error] {err}"
                stop_reason = "error"
                break
            elif ev_type == "done":
                finish_reason = event.get("finish_reason", "stop")
                stop_reason = self._normalize_finish_reason(finish_reason)
                break

        # 构造 LLMResponse（累积的 text/thinking/tool_calls/usage）
        from client.core.agent.types import LLMResponse
        response = LLMResponse(
            content=accumulated_text,
            tool_calls=accumulated_tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            model=model,
            thinking=accumulated_thinking,
            wall_clock_exceeded=wall_clock_exceeded,
        )
        return response, streaming_seqs

    @staticmethod
    def _normalize_finish_reason(finish_reason: str) -> str:
        """把 provider 的 finish_reason 归一化到 v6-lite 的 stop_reason。"""
        if not finish_reason:
            return "end_turn"
        fr = finish_reason.lower()
        if fr in ("stop", "end_turn"):
            return "end_turn"
        if fr in ("tool_calls", "tool_use"):
            return "tool_use"
        if fr in ("length", "max_tokens"):
            return "max_tokens"
        if fr in ("content_filter",):
            return "content_filter"
        if fr in ("error",):
            return "error"
        if fr in ("user_interrupted",):
            return "user_interrupted"
        return fr

    # ------------------------------------------------------------------
    # T02-exec-unify: terminal 生命周期管理（spec D6 + D11）
    # ------------------------------------------------------------------

    def _extract_terminal_id(self, result: ToolResult) -> str | None:
        """从 exec_python 的 ToolResult.content（JSON 字符串）提取 terminal_id。"""
        try:
            data = json.loads(result.content)
            tid = data.get("terminal_id", "")
            return tid or None
        except Exception:
            return None

    async def _cleanup_terminals(self) -> None:
        """session 结束时 kill 所有残留 terminal（spec D11 第二层保障）。

        - terminal_killer 未注入 → 跳过（向后兼容旧测试）
        - 已 done/killed 的 terminal → kill 返回 success=False 但无害
        - kill 失败不抛异常（best-effort，不影响 session 退出）
        """
        killer = self.deps.terminal_killer
        if killer is None or not self._session_terminals:
            return
        for tid in list(self._session_terminals):
            try:
                await killer(tid)
                logger.info(f"session cleanup: killed terminal {tid}")
            except Exception as e:
                logger.warning(f"session cleanup: kill terminal {tid} failed: {e}")
        self._session_terminals.clear()

    async def _execute_with_wakeup(
        self,
        tc: ToolCall,
        tool_executor: ToolExecutor,
        session_id: str,
        store,
    ) -> ToolResult:
        """exec_python 包装：3 分钟唤醒机制（spec D6）。

        流程：
        1. 调 exec_python → 立即返回 terminal_id + status=running
        2. 用 exec_inspect(tid, timeout=180) 等待子进程结束或 3 分钟到
        3. 若子进程结束 → 返回 tool_result（含完整 stdout/stderr）
        4. 若 3 分钟到子进程仍 running → 唤醒 LLM：
           - 自动 inspect 拿当前输出
           - 构造 LLM 请求：原任务 + 工具名 + 已耗时 + 当前输出 + 可用工具
           - LLM 决策：wait(N) / exec_kill / exec_inspect / exec_send_input
           - 后续不再自动唤醒，LLM 自己决定
        """
        # 1. 调 exec_python
        result = await tool_executor.execute(tc)
        if result.is_error:
            return result

        # 提取 terminal_id
        try:
            data = json.loads(result.content)
            tid = data.get("terminal_id", "")
        except Exception:
            return result  # 解析失败，原样返回
        if not tid:
            return result

        # v16: exec_python 内联等待 done 路径短路（spec Solution）
        # 后端 exec_python 在 inline_wait_secs 内子进程已结束 → status=done，
        # result 已含完整 stdout/stderr/exit_code，跳过 inspect 包装。
        # status=running（内联等待超时或禁用）时走原 3 分钟唤醒逻辑不变。
        if data.get("status") == "done":
            return result

        # terminal_inspector 未注入 → 直接返回（向后兼容旧测试）
        inspector = self.deps.terminal_inspector
        if inspector is None:
            return result

        # 2. 等 3 分钟（或子进程结束）
        wakeup_secs = self.config.long_tool_first_check_secs
        if wakeup_secs <= 0:
            return result  # 禁用唤醒机制

        try:
            snap = await inspector(tid, wakeup_secs)
        except Exception as e:
            logger.warning(f"exec_python wakeup inspect failed for {tid}: {e}")
            return result  # inspect 失败，原样返回

        # 3. 子进程已结束 → 把 inspect 的完整输出回填到 result
        if snap.get("status") in ("done", "killed", "error"):
            return self._build_result_from_inspect(tc, snap, tid)

        # 4. 3 分钟到，子进程仍 running → 唤醒 LLM
        logger.info(
            f"Session {session_id}: exec_python {tid} still running after {wakeup_secs}s, "
            f"waking up LLM for decision"
        )
        return await self._wakeup_llm_for_decision(tc, tid, snap, wakeup_secs, tool_executor, session_id, store)

    def _build_result_from_inspect(
        self, tc: ToolCall, snap: dict, tid: str
    ) -> ToolResult:
        """子进程已结束，用 inspect 快照构造 ToolResult。"""
        from client.core.agent.types import ToolResult
        stdout = snap.get("stdout_so_far", "")
        stderr = snap.get("stderr_so_far", "")
        exit_code = snap.get("exit_code", 0)
        status = snap.get("status", "done")
        is_error = status == "error" or exit_code != 0
        content = json.dumps({
            "success": not is_error,
            "terminal_id": tid,
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "elapsed": snap.get("elapsed", 0),
        }, ensure_ascii=False)
        return ToolResult(
            tool_call_id=tc.id,
            content=content,
            is_error=is_error,
            created_at=self.deps.clock(),
        )

    async def _wakeup_llm_for_decision(
        self,
        tc: ToolCall,
        tid: str,
        snap: dict,
        elapsed_secs: int,
        tool_executor: ToolExecutor,
        session_id: str,
        store,
    ) -> ToolResult:
        """3 分钟唤醒 LLM：自动 inspect + 调 LLM 决策。

        LLM 可用工具：exec_inspect / exec_kill / exec_send_input / wait
        LLM 决策后，按其 tool_call 执行，直到 LLM 决定 exec_kill 或子进程结束。
        """
        from client.core.agent.types import LLMRequest, Message, ToolCall

        # 构造 LLM 请求：当前状态 + 可用工具
        stdout_tail = snap.get("stdout_so_far", "")[-2000:]  # 尾部 2000 字符
        stderr_tail = snap.get("stderr_so_far", "")[-1000:]
        prompt = (
            f"你调用的 exec_python 已运行 {elapsed_secs} 秒，子进程仍在 running。\n\n"
            f"terminal_id: {tid}\n"
            f"已耗时: {elapsed_secs}s\n"
            f"stdout 尾部:\n{stdout_tail}\n\n"
            f"stderr 尾部:\n{stderr_tail}\n\n"
            f"请决策下一步：\n"
            f"- exec_inspect(tid={tid}, timeout=N): 等待 N 秒或新输出\n"
            f"- exec_kill(tid={tid}): 终止子进程\n"
            f"- exec_send_input(tid={tid}, text=...): 向 stdin 发送数据\n"
            f"- wait(seconds=N): 纯 sleep N 秒后再查\n"
            f"注意：后续不再自动唤醒，你需要自己决定何时 inspect/wait。"
        )

        llm_gateway = self.deps.llm_gateway

        # 构造 messages（含当前状态提示）
        wakeup_msg = Message(
            role="user",
            content=prompt,
            source="system_wakeup",
        )
        await store.append_message(session_id, wakeup_msg)

        # 构造工具定义（简化：只给名字 + 描述，参数 schema 用 minimal 定义）
        available_tools = [
            {"type": "function", "function": {
                "name": "exec_inspect",
                "description": "查询 terminal 状态，等待新输出或 N 秒",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tid": {"type": "string", "description": "terminal ID"},
                        "timeout": {"type": "integer", "description": "等待秒数，0=立即返回"},
                    },
                    "required": ["tid"],
                },
            }},
            {"type": "function", "function": {
                "name": "exec_kill",
                "description": "终止 terminal 子进程",
                "parameters": {
                    "type": "object",
                    "properties": {"tid": {"type": "string", "description": "terminal ID"}},
                    "required": ["tid"],
                },
            }},
            {"type": "function", "function": {
                "name": "exec_send_input",
                "description": "向 terminal stdin 发送文本",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tid": {"type": "string", "description": "terminal ID"},
                        "text": {"type": "string", "description": "要发送的文本"},
                    },
                    "required": ["tid", "text"],
                },
            }},
            {"type": "function", "function": {
                "name": "wait",
                "description": "纯 sleep N 秒",
                "parameters": {
                    "type": "object",
                    "properties": {"seconds": {"type": "integer", "description": "睡眠秒数"}},
                    "required": ["seconds"],
                },
            }},
        ]

        # 调 LLM（用 call(LLMRequest) 协议）
        try:
            llm_resp = await llm_gateway.call(LLMRequest(
                messages=[wakeup_msg],
                tools=available_tools,
            ))
        except Exception as e:
            logger.warning(f"wakeup LLM call failed: {e}")
            # LLM 调用失败 → 返回当前 inspect 快照
            return self._build_result_from_inspect(tc, snap, tid)

        # LLM 没调工具 → 直接返回当前状态
        if not llm_resp.tool_calls:
            return self._build_result_from_inspect(tc, snap, tid)

        # 执行 LLM 的 tool_calls（循环，直到 LLM 不再调工具或子进程结束）
        # A3（spec D9）：每个决策工具执行前检查 interrupt_event，命中则 break + 返回当前 snap
        # 与 A1 同理：用户 interrupt 后，决策循环也必须立即阻断（不执行下一个工具）
        interrupted_in_wakeup = False
        for wakeup_tc in llm_resp.tool_calls:
            if self._is_interrupted():
                interrupted_in_wakeup = True
                logger.info(
                    f"Session {session_id}: interrupt detected in wakeup decision loop "
                    f"before executing {wakeup_tc.get('function', {}).get('name', '?')}, breaking"
                )
                break
            decision_tc = ToolCall.from_openai_tool_call(
                wakeup_tc, session_id=session_id, seq=0,
            )
            await store.append_tool_call(session_id, decision_tc)
            await store.update_tool_call_status(
                decision_tc.id, "running", started_at=self.deps.clock(),
            )
            decision_result = await tool_executor.execute(decision_tc)
            await store.update_tool_call_status(
                decision_tc.id,
                "failed" if decision_result.is_error else "completed",
                ended_at=self.deps.clock(),
            )
            await store.append_tool_result_message(session_id, decision_result)

            # 若 LLM 决定 exec_kill → 返回 kill 前的输出
            if decision_tc.name == "exec_kill":
                return self._build_result_from_inspect(tc, snap, tid)

            # 若 LLM 决定 exec_inspect → 检查子进程是否结束
            if decision_tc.name == "exec_inspect":
                try:
                    decision_data = json.loads(decision_result.content)
                    if decision_data.get("status") in ("done", "killed", "error"):
                        return self._build_result_from_inspect(tc, decision_data, tid)
                    # 仍 running → 更新 snap，继续等 LLM 下一个决策
                    snap = decision_data
                except Exception:
                    pass

            # wait / exec_send_input → 继续 LLM 决策循环

        # LLM 用完所有 tool_calls / interrupt 命中 / 子进程仍 running → 返回最后状态
        if interrupted_in_wakeup:
            logger.info(
                f"Session {session_id}: wakeup decision loop interrupted, "
                f"returning current snap (tid={tid}, status={snap.get('status')})"
            )
        return self._build_result_from_inspect(tc, snap, tid)

    # ------------------------------------------------------------------
    # 中断检查（T06c）
    # ------------------------------------------------------------------

    def _is_interrupted(self) -> bool:
        """检查 interrupt_event 是否被 set。

        interrupt_event 为 None（未注入）时返回 False（向后兼容 T01-T05）。
        """
        ev = self.deps.interrupt_event
        if ev is None:
            return False
        try:
            return bool(ev.is_set())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 请求构建（v6-02 §1：context.build(state)）
    # ------------------------------------------------------------------

    async def _persist_runner_state(
        self,
        session_id: str,
        store: EventStore,
        iterations: int,
        total_tokens: int,
        wall_clock_exceeded: bool = False,
    ) -> None:
        """E6（spec D23）：把 runner 关键状态快照写入 runner_state 表。

        在主循环每轮关键状态变更后 + 每个 return 路径前调用。
        用途：审计 + 重启恢复（避免 has_attempted_reactive_compact 丢失导致二次压缩）。

        非致命：失败只记 warning，不阻断主流程（state 持久化是 best-effort）。

        Args:
            session_id: 关联的 session ID
            store: EventStore 实例
            iterations: 当前迭代轮数
            total_tokens: 累计 token
            wall_clock_exceeded: 最后一次 LLM 响应是否触发 wall_clock 超时
        """
        try:
            await store.upsert_runner_state(
                session_id,
                iterations=iterations,
                has_attempted_reactive_compact=self.has_attempted_reactive_compact,
                total_tokens=total_tokens,
                wall_clock_exceeded=wall_clock_exceeded,
            )
        except Exception as e:
            logger.warning(
                "E6: upsert_runner_state failed (non-fatal): %s", e,
            )

    async def _repair_dangling_tool_calls(
        self,
        messages: list[Message],
        session_id: str,
        store: EventStore,
    ) -> None:
        """C4（spec D7 数据完整性）：扫描 dangling tool_calls 并补 is_error=true tool_result。

        场景：上一轮 LLM 返回 tool_calls=[tc1, tc2]，执行 tc1 后中断/崩溃，
        tc2 缺 tool_result。下一轮 _build_request 前若不补，OpenAI 协议
        会因 "tool_calls without matching tool messages" 返回 400。

        策略（v6-02 §2.3 BuildConversationRequest 的 repair dangling tool calls）：
        1. 遍历 messages，收集所有 assistant.tool_calls 的 id
        2. 收集所有 role=tool 消息的 tool_call_id（已有 result）
        3. 对差集（dangling）补 is_error=true 的 ToolResult

        注：只处理 messages 内可检测的 dangling（assistant 带 tool_calls 但
        无配对 tool 消息）。中断后未执行的 tool_call 不在 messages 中留下
        running 记录（A1 break 后不写 tool_result），C4 兜底补 error result。
        """
        # 收集已有 tool_result 的 tool_call_id
        existing_result_ids: set[str] = {
            m.tool_call_id for m in messages
            if m.role == "tool" and m.tool_call_id
        }

        # 扫描 dangling：assistant 带 tool_calls，其中 id 不在 existing_result_ids
        dangling_ids: list[str] = []
        for m in messages:
            if m.role != "assistant" or not m.tool_calls:
                continue
            for tc in m.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id and tc_id not in existing_result_ids:
                    dangling_ids.append(tc_id)

        if not dangling_ids:
            return

        # 补 is_error=true 的 tool_result（防 provider 400）
        for tc_id in dangling_ids:
            result = ToolResult(
                tool_call_id=tc_id,
                content="Dangling tool call detected: no tool_result found "
                        "(possibly due to interrupt or crash). Treating as error "
                        "to satisfy OpenAI protocol tool_calls/tool pairing.",
                is_error=True,
                error_type="dangling_tool_call",
            )
            await store.append_tool_result_message(session_id, result)
            logger.warning(
                "C4 repaired dangling tool_call %s in session %s "
                "(appended is_error=true tool_result)",
                tc_id, session_id,
            )

    def _build_request(self, messages: list[Message]) -> LLMRequest:
        """从会话消息构建 LLMRequest。

        v6-lite-chat-fix T01: 自动注入 system prompt（stable prefix + 首轮 ephemeral）。
        - 取 visible 消息（load_messages 已过滤）
        - 按顺序传给 LLM
        - system 提示独立字段：T01 起自动注入（spec D2），不再 system=""

        T05+：若 deps.tool_registry 注入，把 catalog 转 OpenAI tools 格式传给 LLM。

        T06+ 会做 4 件事（v6-02 §2.3 BuildConversationRequest）：
        - clone conversation
        - prune old tool results
        - repair dangling tool calls
        - inject memory reminder

        C5（spec D7）：规范化消息顺序，确保 assistant(tool_calls) 后的 tool_result
        连续。steer 消息在工具批次执行中到达时，可能落在两个 tool_result 之间，
        破坏 OpenAI 协议配对要求。_normalize_message_order 将非 tool 消息移到
        该批次所有 tool_result 之后。
        """
        # 过滤掉空消息（防止 MockLLM 无内容时报错）
        valid_messages = [m for m in messages if m.content is not None]
        # B1（spec D10）：过滤 system_warning 消息（用户可见但 LLM 不可见）
        # DoomLoop 检测后写的 sys 系统消息只给 UI 渲染，不发给 LLM
        valid_messages = [m for m in valid_messages if getattr(m, "source", None) != "system_warning"]
        # C5（spec D7）：规范化消息顺序，确保 tool_result 连续
        valid_messages = self._normalize_message_order(valid_messages)
        # T05+: 若 tool_registry 注入，把 catalog 转 OpenAI tools 格式
        tools: list[dict] = []
        tool_choice: str | dict = "auto"
        if self.deps.tool_registry is not None:
            try:
                tools = self.deps.tool_registry.to_openai_tools()
            except Exception as e:
                logger.warning("tool_registry.to_openai_tools() failed: %s", e)
                tools = []
        # v6-lite-chat-fix T01: system prompt 自动注入（spec D2）
        system = self._build_system_prompt(messages)
        return LLMRequest(
            messages=valid_messages,
            model=self.config.model,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=None,  # 由 LLM 网关决定
        )

    def _normalize_message_order(self, messages: list[Message]) -> list[Message]:
        """C5（spec D7）：规范化消息顺序，确保 assistant(tool_calls) 后的 tool_result 连续。

        问题场景：工具批次执行中 steer 消息到达，落库顺序变成：
            assistant(tool_calls=[tc1, tc2]) → tool(tc1) → user(steer) → tool(tc2)
        OpenAI 协议要求 assistant(tool_calls) 后紧跟 role=tool 消息，中间不能
        插入其他 role 消息，否则 provider 返回 400。

        策略：遍历消息，遇到 assistant(tool_calls) 时，收集后续 tool 消息和夹在
        中间的非 tool 非 assistant 消息。tool 消息保留紧跟 assistant，非 tool
        消息推迟到该批次所有 tool 消息之后。只在遇到下一个 assistant 消息时
        停止收集（新轮次开始）。

        user 消息不触发停止：steer 消息是 role=user，若在 tool 之间到达需要被
        推迟；正常 user 新轮次消息后若紧跟 assistant 也会被推迟，但最终顺序
        仍正确（user → assistant 变成 tool → user → assistant，语义不变）。

        注：只调整发送给 LLM 的消息顺序，不修改 DB 中的实际存储顺序。
        """
        if not messages:
            return messages

        result: list[Message] = []
        i = 0
        n = len(messages)
        while i < n:
            msg = messages[i]
            result.append(msg)
            i += 1

            # 如果是 assistant 带 tool_calls，收集后续的 tool 消息和夹在中间的非 tool 消息
            if msg.role == "assistant" and msg.tool_calls:
                tool_msgs: list[Message] = []
                deferred_msgs: list[Message] = []

                while i < n:
                    next_msg = messages[i]
                    if next_msg.role == "tool":
                        tool_msgs.append(next_msg)
                        i += 1
                    elif next_msg.role == "assistant":
                        # 新轮次（下一个 assistant）开始，停止收集
                        break
                    else:
                        # user(steer) / system 等夹在中间 → 推迟到 tool 消息之后
                        deferred_msgs.append(next_msg)
                        i += 1

                # 先放 tool 消息（紧跟 assistant，保证配对连续）
                result.extend(tool_msgs)
                # 再放推迟的非 tool 消息（steer 等）
                result.extend(deferred_msgs)

        return result

    # ------------------------------------------------------------------
    # v6-lite-chat-fix T01: system prompt 构造（spec D2）
    # ------------------------------------------------------------------

    def _build_system_prompt(self, messages: list[Message]) -> str:
        """构建 system prompt：stable prefix + 首轮 ephemeral context。

        分层（spec D2）：
        - stable prefix（session 内缓存复用，AGENTS.md 静态不损 prompt cache）：
          身份铁律 + AGENTS.md 全文 + project_rules.md 全文 + 关键铁律强调
        - ephemeral context（仅首轮注入，动态）：
          agent_guide(task=首句) 自动调用结果 + 当前时间 + 5:00 算的"今天日期"
          + 双屏/管理员状态 + "自动调用注入，无需再调"声明

        后续轮次仅复用 stable prefix（不重复调 guide，不重算 ephemeral）。
        """
        stable = self._get_stable_prefix()
        # 判断首轮：_build_request_count == 0 表示还未构建过任何 request
        is_first_turn = (self._build_request_count == 0)
        # 无论是否首轮，本次构建都计数（必须先计数再分支，防止 guide 调用异常时反复重试）
        self._build_request_count += 1

        if not is_first_turn:
            # 后续轮次：仅 stable prefix（spec D2: ephemeral 只首轮带，后续靠 messages 历史）
            return stable

        # 首轮：取首句 visible user 消息作为 guide 的 task 参数
        first_user_text = ""
        for m in messages:
            if m.role == "user" and m.visible:
                first_user_text = str(m.content) if m.content is not None else ""
                break
        ephemeral = self._build_ephemeral_context(first_user_text)
        return stable + "\n\n" + ephemeral

    def _get_stable_prefix(self) -> str:
        """获取 stable prefix（session 内缓存复用，不重复读 AGENTS.md 文件）。

        spec D11 五段式结构：ID/Context/Mem/Tool/Output。
        缓存策略：session 内首次构建后缓存字符串，后续轮次复用。
        AGENTS.md 静态不损 prompt cache（v6-07 §1.6）。

        chat-panel-v2 T03: 若 config.template_skills 非空，在 stable prefix 尾部
        追加 "# Active Skills" 段（decisions D2：仅路径列表 + 引导语，不注入全文），
        agent 看到后用 file_read 工具读 SKILL.md。skill 段是会话级稳定配置，
        与 stable prefix 一起缓存复用，不破坏 prompt cache。
        """
        if self._stable_prefix_cache is None:
            base = _build_stable_prefix()
            # T03: 模板 skills 注入（仅当非空时追加）
            if self.config.template_skills:
                # 延迟导入避免循环依赖（template_store 不依赖 runner）
                from client.core.agent.template_store import format_skills_prompt
                skill_section = format_skills_prompt(list(self.config.template_skills))
                if skill_section:
                    base = base + "\n\n" + skill_section
            self._stable_prefix_cache = base
        return self._stable_prefix_cache

    def _build_ephemeral_context(self, first_user_text: str) -> str:
        """构建首轮 ephemeral context（spec D2）。

        内容：
        - 当前时间 + 按 5:00 分界线算出的"今天日期"
        - 双屏/管理员状态（简化：未检测，提示 agent 需要时调 /health）
        - agent_guide(task=首句) 自动调用结果（task_type/first_action/memory_index/...）
        - "以上由 agent_guide 自动调用注入，你无需再调"声明

        guide 调用失败时降级：仍写 stable prefix 已注入（在 _build_system_prompt 拼接），
        ephemeral 里写 "agent_guide 调用失败，请手动调" 标记，不抛异常不阻断对话。
        """
        now = datetime.now()
        today = compute_today(now)
        parts: list[str] = [
            "=== ephemeral context（首轮注入，动态）===",
            f"当前时间：{now.isoformat(timespec='seconds')}",
            f"今天日期（按 5:00 分界线算出，<05:00 用前一天）：{today.isoformat()}",
            f"项目根目录：{_PROJECT_ROOT}（file_read/file_grep 等工具需用绝对路径）",
            "双屏/管理员状态：未在 system 中检测（需要时调 /health 或 list_windows 确认）",
        ]

        # 调 agent_guide（首轮一次，失败降级不抛）
        # 注：本方法由 _build_system_prompt（同步）→ _run_loop（async）调用链进入，
        # 阻塞防护在 async 边界包 to_thread（见 _build_system_prompt 调用点，8-1）
        try:
            guide_result = _call_agent_guide_http(first_user_text)
        except Exception as e:
            # _call_agent_guide_http 内部已 try/except 返回 None；
            # 这里再兜底防御 mock 直接抛异常（测试 side_effect 场景）
            logger.warning("agent_guide 调用异常（兜底捕获）: %s", e)
            guide_result = None

        if guide_result is not None:
            parts.append(f"--- agent_guide 自动调用结果（task={first_user_text!r}）---")
            parts.append(_format_guide_result(guide_result))
            parts.append(
                "以上由 agent_guide 自动调用注入，你无需再调 agent_guide（首轮已调）。"
            )
        else:
            parts.append("--- agent_guide 调用失败，请手动调 agent_guide(task=...) ---")

        return "\n".join(parts)
