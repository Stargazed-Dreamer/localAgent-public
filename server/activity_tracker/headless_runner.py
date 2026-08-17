"""headless-agent-session: 后端自主启动 agent 会话的启动逻辑

spec: temp/sdd/headless-agent-session/spec.md
ticket: Ticket 02 (主 agent 启动) + Ticket 03 (judge agent)

设计要点（spec Solution 节）：
- 在后端 event loop 中 import client.core.agent，组装 RunnerDeps + RunnerConfig
- 创建 headless session（mode="headless"），调 SessionFacade.start
- 主 agent 跑完后从 EventStore 取最后 N 条 assistant 消息作为 judge 输入
- 通过 HTTP 自环（base_url=http://127.0.0.1:8766）调后端 REST/MCP 工具

资源限制默认值（spec 我替领导拍的板 + 用户反馈放宽）：
- max_iterations: 100（chat 会话 50 翻倍，正常任务能跑完）
- wall_clock_budget_secs: 1800（30 分钟，复杂任务需多轮工具调用）

B2（spec D2/D3）：删 max_budget_usd 字段，本地无法准确算 cost（缓存比例不明），
只记账 token 不控制预算。

interrupt registry（Ticket 07 用）：
- 模块级 _headless_sessions: dict[session_id, asyncio.Event]
- run_main_agent / run_judge_agent 启动时注册，结束时清理
- REST 端点调 _headless_sessions[sid].set() 触发中断
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("localagent.headless_runner")

# ============================================================================
# 常量与默认值（spec 我替领导拍的板）
# ============================================================================

DEFAULT_BASE_URL = "http://127.0.0.1:8766"

# 主 agent 资源限制默认值（spec 我替领导拍的板 + 用户反馈放宽）
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_WALL_CLOCK_BUDGET_SECS = 1800  # 30 分钟

# judge agent 资源限制默认值（更紧凑，判定任务简单）
DEFAULT_JUDGE_MAX_ITERATIONS = 10
DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS = 300  # 5 分钟

# judge 取主 agent 最后 N 条 assistant 消息作为判定材料
DEFAULT_JUDGE_MAIN_OUTPUT_MESSAGES = 20

# session_id 前缀约定（spec Implementation Decisions 节）
HEADLESS_MAIN_PREFIX = "headless-"
HEADLESS_JUDGE_PREFIX = "headless-judge-"


# ============================================================================
# interrupt registry（Ticket 07 用，Ticket 02 提前留接口）
# ============================================================================

# 模块级 registry：session_id → asyncio.Event
# 后端 event loop 中创建的 asyncio.Event，供 REST 端点跨请求访问
_headless_sessions: dict[str, asyncio.Event] = {}


def register_interrupt_event(session_id: str, event: asyncio.Event) -> None:
    """注册 session 的 interrupt_event（run_main_agent / run_judge_agent 启动时调）。"""
    _headless_sessions[session_id] = event
    logger.debug("Registered interrupt event for session %s", session_id)


def unregister_interrupt_event(session_id: str) -> None:
    """清理 session 的 interrupt_event（run_main_agent / run_judge_agent 结束时调）。"""
    _headless_sessions.pop(session_id, None)
    logger.debug("Unregistered interrupt event for session %s", session_id)


def get_interrupt_event(session_id: str) -> asyncio.Event | None:
    """查询 session 的 interrupt_event（REST 端点用）。"""
    return _headless_sessions.get(session_id)


def interrupt_session(session_id: str) -> bool:
    """触发 session 中断（REST 端点调）。

    Returns:
        True 如果 session 存在且已触发中断，False 如果 session 不存在
    """
    ev = _headless_sessions.get(session_id)
    if ev is None:
        return False
    ev.set()
    logger.info("Interrupted headless session %s", session_id)
    return True


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class HeadlessConfig:
    """headless session 配置（从 [loops.headless_session.*] config 段解析）。

    所有字段都有默认值，支持部分配置（缺失字段用默认值）。
    """
    trigger_text: str = ""
    skill: str = ""  # trigger_text 留空时用 skill 的 first_action
    one_shot: bool = False

    # 主 agent 资源限制（spec 默认值，宽松）
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost
    wall_clock_budget_secs: int = DEFAULT_WALL_CLOCK_BUDGET_SECS
    model: str = ""  # 留空用默认 tier

    # judge agent 配置
    judge_model: str = ""  # 留空用默认 tier
    judge_max_iterations: int = DEFAULT_JUDGE_MAX_ITERATIONS
    # B2（spec D2/D3）：删 judge_max_budget_usd，本地无法准确算 cost
    judge_wall_clock_budget_secs: int = DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS

    # 其他
    base_url: str = DEFAULT_BASE_URL
    db_path: str = ""  # 留空用 EventStore 默认路径


@dataclass
class MainAgentResult:
    """run_main_agent 的返回值。"""
    session_id: str
    outcome: Any  # RunOutcome（用 Any 避免 import cycle）
    last_assistant_messages: list[str] = field(default_factory=list)
    error: str = ""


# ============================================================================
# JUDGE_PROMPT_TEMPLATE（spec 草拟）
# ============================================================================

JUDGE_PROMPT_TEMPLATE = """你是任务成功判定 agent。下面给你一个 headless agent 任务及其最终输出，请判定任务是否成功完成。

【任务描述】
{task}

【主 agent 的对话历史（最后 {msg_count} 条消息）】
{main_output}

【判定规则】
- success：主 agent 完成了任务描述要求的工作，输出明确表示完成或给出了所需结果
- failed：主 agent 明确报告失败、无法完成、或输出与任务要求不符
- uncertain：主 agent 输出含"不确定""可能""大概""未确认"等模糊表述，或主 agent 中途中断/未给出明确结论

【输出格式】
严格输出以下 JSON，不要其他内容：
{{"verdict": "success" | "failed" | "uncertain", "reason": "简短说明（一句话）"}}
"""


# ============================================================================
# judge agent 数据类
# ============================================================================


@dataclass
class JudgeResult:
    """run_judge_agent 的返回值。"""
    verdict: str  # "success" / "failed" / "uncertain"
    reason: str = ""
    session_id: str = ""
    raw_output: str = ""  # judge 最后一条 assistant 消息原文（调试用）
    error: str = ""  # judge 自身失败时的错误信息


# verdict 合法值集合
VALID_VERDICTS = {"success", "failed", "uncertain"}


# ============================================================================
# 主 agent 启动逻辑
# ============================================================================


def _generate_main_session_id() -> str:
    """生成主会话 session_id：headless-{uuid4.hex[:12]}。"""
    return f"{HEADLESS_MAIN_PREFIX}{_uuid.uuid4().hex[:12]}"


def _generate_judge_session_id() -> str:
    """生成 judge 会话 session_id：headless-judge-{uuid4.hex[:12]}。"""
    return f"{HEADLESS_JUDGE_PREFIX}{_uuid.uuid4().hex[:12]}"


async def run_main_agent(
    trigger_text: str,
    config: HeadlessConfig,
    *,
    session_id: str | None = None,
    interrupt_event: asyncio.Event | None = None,
) -> MainAgentResult:
    """启动主 agent 会话（mode="headless"）。

    在后端 event loop 中 import client.core.agent 组装 deps + config，
    调 SessionFacade.start 跑完主 agent，取最后 N 条 assistant 消息作为 judge 输入。

    Args:
        trigger_text: 触发任务文本（如 "查 dmca-backup 最近一次 run 的日志"）
        config: HeadlessConfig（资源限制 + base_url + db_path）
        session_id: 可选 session_id（留空自动生成 headless-{uuid}）
        interrupt_event: 可选中断事件（留空自动创建；Ticket 07 REST 端点用）

    Returns:
        MainAgentResult：含 session_id + outcome + last_assistant_messages
        失败时 outcome 可能是 None，error 字段含错误信息
    """
    # 延迟 import 避免 server 启动时强依赖 client.core.agent
    from client.core.agent import (
        EventStore,
        HttpClientToolExecutor,
        LLMPoolGateway,
        RunnerConfig,
        RunnerDeps,
        SessionFacade,
        ToolRegistry,
    )

    sid = session_id or _generate_main_session_id()
    own_interrupt_event = interrupt_event is None
    if own_interrupt_event:
        interrupt_event = asyncio.Event()

    # 注册到 interrupt registry（Ticket 07 REST 端点用）
    register_interrupt_event(sid, interrupt_event)

    store: EventStore | None = None
    try:
        # 组装 EventStore（共享 data/client/agent.db，WAL 模式）
        store = EventStore(db_path=config.db_path) if config.db_path else EventStore()
        store.init()

        # 组装 deps（HTTP 自环调后端）
        gateway = LLMPoolGateway(base_url=config.base_url)
        registry = ToolRegistry(base_url=config.base_url)
        try:
            registry.refresh()
        except Exception as e:
            logger.warning("ToolRegistry refresh failed (will run without tools): %s", e)
        executor = HttpClientToolExecutor(registry=registry, base_url=config.base_url)

        deps = RunnerDeps(
            llm_gateway=gateway,
            event_store=store,
            tool_executor=executor,
            tool_registry=registry,
            interrupt_event=interrupt_event,
        )

        # 组装 config（RunnerConfig 是 frozen dataclass）
        runner_config = RunnerConfig(
            session_id=sid,
            model=config.model,
            max_iterations=config.max_iterations,
            # B2（spec D2/D3）：删 max_budget_usd 传参
            wall_clock_budget_secs=config.wall_clock_budget_secs,
        )

        # 调 SessionFacade.start（mode="headless"）
        facade = SessionFacade(deps=deps, config=runner_config)
        logger.info(
            "headless main agent start: session=%s trigger_len=%d max_iter=%d wall=%ds",
            sid, len(trigger_text), config.max_iterations, config.wall_clock_budget_secs,
        )
        outcome = await facade.start(
            sid, trigger_text, mode="headless",
        )

        # 取最后 N 条 assistant 消息作为 judge 输入
        last_msgs = await _extract_last_assistant_messages(
            store, sid, DEFAULT_JUDGE_MAIN_OUTPUT_MESSAGES
        )

        return MainAgentResult(
            session_id=sid,
            outcome=outcome,
            last_assistant_messages=last_msgs,
        )

    except Exception as e:
        logger.exception("headless main agent failed: session=%s", sid)
        return MainAgentResult(
            session_id=sid,
            outcome=None,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        # 清理 interrupt registry
        unregister_interrupt_event(sid)


async def _extract_last_assistant_messages(
    store, session_id: str, count: int
) -> list[str]:
    """从 EventStore 取最后 N 条 assistant 消息的 content 文本。

    Args:
        store: EventStore 实例
        session_id: 会话 ID
        count: 取最后 N 条（不足则全取）

    Returns:
        list[str]：每条 assistant 消息的 content 文本（按时间顺序，最老的在前）
    """
    try:
        messages = await store.load_messages(session_id)
    except Exception as e:
        logger.warning("load_messages failed (session=%s): %s", session_id, e)
        return []

    # 过滤 assistant 消息
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    # 取最后 N 条
    last_n = assistant_msgs[-count:] if count > 0 else assistant_msgs

    # 提取 content 文本（content 是 list[dict] OpenAI 格式，提取 text 部分）
    result: list[str] = []
    for m in last_n:
        content = m.content
        if isinstance(content, str):
            result.append(content)
        elif isinstance(content, list):
            # OpenAI 格式：[{"type": "text", "text": "..."}, ...]
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            result.append("\n".join(text_parts))
        else:
            result.append(str(content))
    return result


# ============================================================================
# 工具函数（供 Ticket 04 HeadlessSessionAction 用）
# ============================================================================


def parse_headless_config(config_dict: dict) -> HeadlessConfig:
    """从 dict 解析 HeadlessConfig（缺失字段用默认值）。

    Args:
        config_dict: loop task config dict（如 {
            "trigger_text": "查日志",
            "max_iterations": 50,
            "one_shot": True,
        }）

    Returns:
        HeadlessConfig
    """
    return HeadlessConfig(
        trigger_text=config_dict.get("trigger_text", ""),
        skill=config_dict.get("skill", ""),
        one_shot=config_dict.get("one_shot", False),
        max_iterations=config_dict.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        # B2（spec D2/D3）：删 max_budget_usd 解析（字段已删，配置文件残留被 .get 忽略）
        wall_clock_budget_secs=config_dict.get(
            "wall_clock_budget_secs", DEFAULT_WALL_CLOCK_BUDGET_SECS
        ),
        model=config_dict.get("model", ""),
        judge_model=config_dict.get("judge_model", ""),
        judge_max_iterations=config_dict.get(
            "judge_max_iterations", DEFAULT_JUDGE_MAX_ITERATIONS
        ),
        # B2（spec D2/D3）：删 judge_max_budget_usd 解析（字段已删）
        judge_wall_clock_budget_secs=config_dict.get(
            "judge_wall_clock_budget_secs", DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS
        ),
        base_url=config_dict.get("base_url", DEFAULT_BASE_URL),
        db_path=config_dict.get("db_path", ""),
    )


# ============================================================================
# judge agent 启动逻辑
# ============================================================================


def _parse_judge_verdict(raw_output: str) -> tuple[str, str]:
    """解析 judge agent 输出的 JSON verdict。

    Args:
        raw_output: judge 最后一条 assistant 消息的 content 文本

    Returns:
        (verdict, reason) tuple。解析失败时 verdict="uncertain", reason 含解析错误说明。
    """
    import json
    import re

    if not raw_output or not raw_output.strip():
        return ("uncertain", "judge output is empty")

    text = raw_output.strip()

    # 尝试直接解析整段为 JSON
    try:
        data = json.loads(text)
        verdict = str(data.get("verdict", "")).strip().lower()
        reason = str(data.get("reason", "")).strip()
        if verdict in VALID_VERDICTS:
            return (verdict, reason or f"verdict={verdict}")
        return ("uncertain", f"invalid verdict value: {verdict!r}")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 兜底：尝试从文本中提取 JSON 块（如 ```json ... ``` 或 {...}）
    json_match = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            verdict = str(data.get("verdict", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()
            if verdict in VALID_VERDICTS:
                return (verdict, reason or f"verdict={verdict}")
            return ("uncertain", f"invalid verdict value: {verdict!r}")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    return ("uncertain", f"judge output not valid JSON: {text[:100]!r}")


async def run_judge_agent(
    main_output: list[str],
    trigger_text: str,
    config: HeadlessConfig,
    *,
    session_id: str | None = None,
    interrupt_event: asyncio.Event | None = None,
) -> JudgeResult:
    """启动 judge agent 会话（mode="headless_judge"）判定主 agent 任务成败。

    流程（spec Solution 节阶段 2）：
    1. 创建 judge session（mode="headless_judge"）
    2. judge prompt = JUDGE_PROMPT_TEMPLATE.format(task=trigger_text, main_output=...)
    3. 调 facade.start(judge_session_id, judge_prompt, model_override=judge_model)
    4. 解析 judge 最后一条 assistant 消息 JSON
    5. 降级规则：
       - JSON 解析失败 → verdict="uncertain"
       - judge session 自身失败（outcome.status != completed）→ verdict="uncertain"
       - judge 超过 max_iterations → verdict="uncertain"

    Args:
        main_output: 主 agent 最后 N 条 assistant 消息文本列表
        trigger_text: 主 agent 的触发任务文本（判定时对照任务描述）
        config: HeadlessConfig（judge_model / judge_max_iterations 等字段用）
        session_id: 可选 judge session_id（留空自动生成 headless-judge-{uuid}）
        interrupt_event: 可选中断事件（Ticket 07 REST 端点用）

    Returns:
        JudgeResult：含 verdict + reason + session_id + raw_output
    """
    from client.core.agent import (
        EventStore,
        HttpClientToolExecutor,
        LLMPoolGateway,
        RunnerConfig,
        RunnerDeps,
        SessionFacade,
        ToolRegistry,
    )

    sid = session_id or _generate_judge_session_id()
    own_interrupt_event = interrupt_event is None
    if own_interrupt_event:
        interrupt_event = asyncio.Event()

    register_interrupt_event(sid, interrupt_event)

    store: EventStore | None = None
    try:
        store = EventStore(db_path=config.db_path) if config.db_path else EventStore()
        store.init()

        gateway = LLMPoolGateway(base_url=config.base_url)
        registry = ToolRegistry(base_url=config.base_url)
        try:
            registry.refresh()
        except Exception as e:
            logger.warning("judge ToolRegistry refresh failed: %s", e)
        executor = HttpClientToolExecutor(registry=registry, base_url=config.base_url)

        deps = RunnerDeps(
            llm_gateway=gateway,
            event_store=store,
            tool_executor=executor,
            tool_registry=registry,
            interrupt_event=interrupt_event,
        )

        runner_config = RunnerConfig(
            session_id=sid,
            model=config.judge_model,
            max_iterations=config.judge_max_iterations,
            # B2（spec D2/D3）：删 judge_max_budget_usd 传参
            wall_clock_budget_secs=config.judge_wall_clock_budget_secs,
        )

        # 组装 judge prompt
        main_output_text = "\n---\n".join(main_output) if main_output else "(无主 agent 输出)"
        judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
            task=trigger_text,
            msg_count=len(main_output),
            main_output=main_output_text,
        )

        facade = SessionFacade(deps=deps, config=runner_config)
        logger.info(
            "headless judge agent start: session=%s judge_model=%s max_iter=%d",
            sid, config.judge_model or "(default)", config.judge_max_iterations,
        )
        outcome = await facade.start(
            sid, judge_prompt, mode="headless_judge",
        )

        # 取 judge 最后一条 assistant 消息
        judge_msgs = await _extract_last_assistant_messages(store, sid, 1)
        raw_output = judge_msgs[-1] if judge_msgs else ""

        # 降级规则 1：judge session 自身失败
        if outcome is None:
            return JudgeResult(
                verdict="uncertain",
                reason="judge session failed (outcome is None)",
                session_id=sid,
                raw_output=raw_output,
                error="outcome is None",
            )

        # 降级规则 2：judge 超过 max_iterations（stop_reason 含 max_iterations）
        stop_reason = getattr(outcome, "stop_reason", "") or ""
        if "max_iterations" in stop_reason:
            return JudgeResult(
                verdict="uncertain",
                reason="judge exceeded max_iterations",
                session_id=sid,
                raw_output=raw_output,
            )

        # 降级规则 3：judge session status 不是 completed
        outcome_status = getattr(outcome, "status", "") or ""
        if outcome_status != "completed":
            return JudgeResult(
                verdict="uncertain",
                reason=f"judge session not completed (status={outcome_status})",
                session_id=sid,
                raw_output=raw_output,
            )

        # 降级规则 4：raw_output 为空
        if not raw_output.strip():
            return JudgeResult(
                verdict="uncertain",
                reason="judge output is empty",
                session_id=sid,
                raw_output=raw_output,
            )

        # 解析 JSON verdict
        verdict, reason = _parse_judge_verdict(raw_output)
        return JudgeResult(
            verdict=verdict,
            reason=reason,
            session_id=sid,
            raw_output=raw_output,
        )

    except Exception as e:
        logger.exception("headless judge agent failed: session=%s", sid)
        return JudgeResult(
            verdict="uncertain",
            reason=f"judge session failed: {type(e).__name__}: {e}",
            session_id=sid,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        unregister_interrupt_event(sid)
