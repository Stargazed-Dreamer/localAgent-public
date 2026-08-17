"""v6-lite-streaming-gui T05: SessionFacade — 纯 Python Facade 封装 SessionRunner

设计依据（spec D12/D13）：
- v6-10 §1：SessionFacade 5 方法封装（start/steer/interrupt/approve/events）
- D13：ChatPanel worker 通过 facade 调用引擎，不再直接构造 SessionRunner
- 向后兼容：薄封装，不改变现有行为。单元测试可用 facade，旧测试用 runner 直调都行

5 方法职责：
- start(session_id, text, mode)：创建 store + runner + 写 user message + 调 runner.run()
- steer(session_id, text)：写 steer message（role=user, source=steer），runner 下一轮读到
- interrupt(session_id)：调 runner.interrupt_event.set()
- approve(approval_id, decision, feedback)：POST /command-guard/decision 提交审批决定
- events(session_id, after_seq)：从 EventStore 读 after_seq 之后的事件（AsyncIterator）

steer 消息语义（spec D13）：
- source="steer" 标记引导消息（与正常 user 消息区分，UI 用特殊样式渲染）
- runner 当前工具批次结束后下一轮 load_messages 时读到
- 不打断当前工具执行（与 interrupt 互补）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from client.core.agent.event_store import EventStore
from client.core.agent.runner import SessionRunner
from client.core.agent.types import Message, RunnerConfig, RunnerDeps, RunOutcome

logger = logging.getLogger("localagent.agent.facade")

# POST /command-guard/decision 端点（server/command_guard.py operation_id=command_guard_record_decision）
# 提交 approval_id + decision(approve|deny) + feedback，签发一次性放行令牌
APPROVAL_DECISION_PATH = "/command-guard/decision"


class SessionFacade:
    """纯 Python Facade，封装 SessionRunner（v6-10 §1，spec D12）。

    GUI worker 通过它调用引擎，核心逻辑不依赖 Qt。
    生命周期：一个 facade 实例对应一次 run()（runner 单次使用，run() 结束即终止）。

    用法：
        facade = SessionFacade(deps=deps, config=config)
        outcome = await facade.start(session_id, "hello")
        # 工具执行中：
        await facade.steer(session_id, "请额外查询 X")
        # 中断：
        await facade.interrupt(session_id)
        # 事件流（GUI 增量渲染）：
        async for event in facade.events(session_id, after_seq=last_seq):
            ...
    """

    def __init__(self, deps: RunnerDeps, config: RunnerConfig):
        self._deps = deps
        self._config = config
        self._runner: SessionRunner | None = None
        self._store: EventStore | None = None
        self._session_id: str | None = None
        # A2（spec D9）：保存 runner 所在 task，interrupt() 时 cancel()
        # 解决 LLM hang 时 interrupt_event 不被检查的问题（runner 卡在 LLM await 点）
        self._runner_task: asyncio.Task | None = None
        # E2（spec D22）：串行化 start() 调用，防止同 facade 实例并发 start
        # 导致 _runner 被 overwrite、第一个 run 变成孤儿 task。
        # facade 生命周期是单次使用（一个 facade 对应一次 run()），但防御性加 Lock
        # 保证误用时第二次 start 排队等待第一次完成（不拒绝，保证用户消息不丢）。
        self._start_lock: asyncio.Lock = asyncio.Lock()

    @property
    def store(self) -> EventStore | None:
        """暴露 store 供外部读取（ChatPanel 主线程轮询读 EventStore）。"""
        return self._store

    @property
    def runner(self) -> SessionRunner | None:
        """暴露 runner 供外部检查状态（如 interrupt_event）。"""
        return self._runner

    async def start(
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "dialogue",
        model_override: str = "",
        template_skills: list[str] | None = None,
        client_message_id: str = "",
    ) -> RunOutcome:
        """启动新 session：创建会话 + 写 user message + 调 runner.run()。

        Args:
            session_id: 会话 ID（外部生成，如 "sess-{uuid.hex[:12]}"）
            text: 用户消息文本
            mode: 会话模式（dialogue / plan / auto_approve），默认 dialogue
            model_override: 模型覆盖（v6-lite-chat-fix T06，spec D10）。
                非空时覆盖 config.model，让前端模型选择器切换后下条消息生效。
                空字符串时用 config 原值（向后兼容）。
            template_skills: chat-panel-v2 T03 模板 skills[] 注入（decisions D2）。
                非空时覆盖 config.template_skills（task_type list，如
                ["recurring.accounting"]），SessionRunner 启动时把 skills 转成
                system prompt 段（仅路径列表 + 引导语，不注入全文），agent 自己读
                SKILL.md。None 或空 list = 不注入。
            client_message_id: E3 幂等键。非空时先查 EventStore 是否已处理过，
                已存在则返回上次 outcome，不重复触发 LLM。空字符串 = 不启用幂等。

        Returns:
            RunOutcome：runner.run() 的返回值（或缓存的 outcome）
        """
        # E3（spec）：client_message_id 幂等去重
        if client_message_id:
            store = self._deps.event_store
            if store is not None:
                cached = await store.get_outcome_for_client_message_id(client_message_id)
                if cached is not None:
                    logger.info(
                        "SessionFacade.start: duplicate client_message_id=%s, "
                        "returning cached outcome (session=%s status=%s)",
                        client_message_id, cached["session_id"], cached["outcome_status"],
                    )
                    return RunOutcome(
                        session_id=cached["session_id"],
                        status=cached["outcome_status"],
                        stop_reason=cached["outcome_stop_reason"],
                        iterations=0,
                        total_tokens=0,
                    )

        # E2（spec D22）：串行化 start()，防止同 facade 并发调用导致 _runner overwrite
        async with self._start_lock:
            outcome = await self._start_impl(
                session_id, text, mode=mode,
                model_override=model_override,
                template_skills=template_skills,
            )

        # E3（spec）：记录 client_message_id → outcome 映射（run 完成后）
        if client_message_id and self._store is not None:
            try:
                await self._store.record_client_message_id(
                    client_message_id, session_id,
                    outcome.status, outcome.stop_reason or "",
                )
            except Exception as e:
                logger.warning(
                    "record_client_message_id failed (cmid=%s): %s",
                    client_message_id, e,
                )

        return outcome

    async def _start_impl(
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "dialogue",
        model_override: str = "",
        template_skills: list[str] | None = None,
    ) -> RunOutcome:
        """start() 的实际实现（E2 Lock 保护内执行）。"""
        self._session_id = session_id
        store = self._deps.event_store
        if store is None:
            raise RuntimeError("RunnerDeps.event_store is None; facade.start requires a store")
        self._store = store

        # 确保会话存在（如果 session_id 已存在则跳过创建，支持续轮/恢复场景）
        existing = await store.get_session(session_id)
        is_new_session = existing is None
        if is_new_session:
            await store.create_session(session_id=session_id, mode=mode)
        # 写用户消息
        await store.append_message(
            session_id,
            Message(role="user", content=text, source="user"),
        )

        # T06 spec D7：新建会话首句入库后自动取前 20 字写 sessions.title
        # （仅新建会话触发，续轮/恢复不覆盖已有 title）
        if is_new_session:
            title = (text or "").strip().replace("\n", " ")[:20]
            if title:
                try:
                    await store.update_session_title(session_id, title)
                except Exception as e:
                    logger.warning("update_session_title failed (session=%s): %s", session_id, e)

        # T06 spec D10：model_override 覆盖 config.model（前端模型选择器切换后下条消息生效）
        # chat-panel-v2 T03: template_skills 覆盖 config.template_skills
        config = self._config
        if model_override or template_skills:
            # RunnerConfig 是 frozen dataclass，用 dataclasses.replace 创建新实例
            from dataclasses import replace as _replace
            overrides: dict = {}
            if model_override:
                overrides["model"] = model_override
            if template_skills:
                # tuple 化（RunnerConfig.template_skills 是 tuple[str, ...]，frozen dataclass）
                overrides["template_skills"] = tuple(template_skills)
            config = _replace(config, **overrides)

        # 构造 runner（单次使用）
        self._runner = SessionRunner(deps=self._deps, config=config)
        # A2（spec D9）：保存当前 task 供 interrupt() cancel()
        # current_task() 返回 facade.start() 所在的 task，cancel 后 CancelledError
        # 会在 runner.run() 内部的 await 点抛出，由 runner 捕获并返回 interrupted
        self._runner_task = asyncio.current_task()
        logger.info(
            "SessionFacade.start: session=%s mode=%s model=%s skills=%s",
            session_id, mode, config.model or "(default)",
            list(config.template_skills) if config.template_skills else "(none)",
        )
        return await self._runner.run()

    async def steer(self, session_id: str, text: str) -> None:
        """steer：写 steer message（role=user, source=steer），runner 下一轮读到。

        不打断当前工具执行（与 interrupt 互补）。runner 当前工具批次结束后，
        下一轮 load_messages 时会读到这条消息，影响后续 LLM 请求。

        UI 上引导消息用特殊样式（缩进+左侧色条）与正常 user 消息区分（D13）。

        Args:
            session_id: 会话 ID
            text: 引导指令文本
        """
        if self._store is None:
            logger.warning("SessionFacade.steer: store not initialized (session=%s)", session_id)
            return
        await self._store.append_message(
            session_id,
            Message(role="user", content=text, source="steer", visible=True),
        )
        logger.info("SessionFacade.steer: session=%s text_len=%d", session_id, len(text))

    async def interrupt(self, session_id: str) -> None:
        """interrupt：set interrupt_event + cancel runner task（A2 spec D9）。

        双层保障：
        1. ev.set()：runner 主循环/工具循环顶部检查到 → 下一轮中断（A1）
        2. runner_task.cancel()：LLM hang 时 await 点被 CancelledError 中止（A2）

        A2 解决的问题：runner 卡在 LLM await 点时，ev.set() 不被检查（await 不返回），
        必须靠 task.cancel() 强制中止。runner._run_loop 捕获 CancelledError 并返回
        RunOutcome(status="interrupted")。

        如果 runner 还没启动（_runner is None）或 interrupt_event 为 None，则 no-op。
        """
        if self._runner is None:
            logger.warning("SessionFacade.interrupt: runner not started (session=%s)", session_id)
            return
        ev = self._deps.interrupt_event
        if ev is None:
            logger.warning("SessionFacade.interrupt: interrupt_event is None (session=%s)", session_id)
            return
        # 第一层：set event（A1 路径，工具循环/主循环顶部检查）
        ev.set()
        # 第二层：cancel task（A2 路径，LLM hang 时强制中止）
        # runner._run_loop 捕获 CancelledError → 写 transition(user_interrupted) + 返回 interrupted
        if self._runner_task is not None and not self._runner_task.done():
            self._runner_task.cancel()
        logger.info("SessionFacade.interrupt: session=%s (event set + task cancelled)", session_id)

    async def approve(
        self,
        approval_id: str,
        decision: str,
        feedback: str = "",
    ) -> dict[str, Any]:
        """审批：提交 approve/deny + feedback 到 server 端 /command-guard/decision。

        桥接现有审批流（spec D12）：server 端 command_guard_record_decision 端点
        接收 {approval_id, decision, feedback}，签发一次性放行令牌。

        典型场景：
        - ChatPanel 自定义审批弹窗（绕过 server 端 PySide6 子进程弹窗）
        - 用户在 ChatPanel 内点击 approve/deny → facade.approve → server 签发令牌
        - HttpClientToolExecutor 拿到令牌后重试原请求

        Args:
            approval_id: 审批 ID（来自 server 返回的 approval_required 响应）
            decision: "approve" 或 "deny"
            feedback: 用户反馈文本（可选）

        Returns:
            server 响应 dict：{"approved": bool, "decision": str, "feedback": str, ...}
        """
        import httpx  # 延迟导入，避免 facade 模块强依赖 httpx

        # 从 config 拿 base_url（LLMPoolGateway 已有，但 facade 不直接访问）
        # 改为从 deps 拿（LLMPoolGateway.base_url）
        gateway = self._deps.llm_gateway
        base_url = getattr(gateway, "base_url", "") or "http://127.0.0.1:8766"
        url = f"{base_url}{APPROVAL_DECISION_PATH}"

        payload = {
            "approval_id": approval_id,
            "decision": decision,
            "feedback": feedback,
        }
        headers = {"X-Agent-Caller": "v6-lite-agent"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()

        logger.info(
            "SessionFacade.approve: approval_id=%s decision=%s approved=%s",
            approval_id, decision, result.get("approved"),
        )
        return result

    async def events(self, session_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
        """事件流：从 EventStore 读 after_seq 之后的事件（用于 GUI 增量渲染）。

        Args:
            session_id: 会话 ID
            after_seq: 起始 seq（不包含），返回 seq > after_seq 的事件

        Yields:
            事件 dict（含 seq/type/payload/trace_id 等字段）
        """
        if self._store is None:
            logger.warning("SessionFacade.events: store not initialized (session=%s)", session_id)
            return

        events = await self._store.load_events(session_id)
        for ev in events:
            if ev.seq > after_seq:
                yield {
                    "seq": ev.seq,
                    "type": ev.type,
                    "payload": ev.payload,
                    "trace_id": ev.trace_id,
                    "created_at": ev.created_at,
                }
