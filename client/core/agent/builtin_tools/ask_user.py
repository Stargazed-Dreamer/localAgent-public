"""T17: ask_user 工具（向用户提问）

通过回调触发 GUI 弹窗。BuiltinToolExecutor 在构造时注入 ask_callback，
未注入时返回 USER_DENIED（fail-closed，不让 agent 卡住）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from client.core.agent.builtin_tools.base import BuiltinTool
from client.core.agent.types import ToolCall, ToolResult, ToolResultVariant


class AskUserTool(BuiltinTool):
    """向用户提问并等待选择（结构化选项）。"""

    operation_id = "ask_user"

    def __init__(self, ask_callback: Callable[[list[dict]], list[dict]] | None = None):
        """ask_callback: 接收 questions 列表，返回用户选择列表。

        GUI 注入同步阻塞回调（弹窗等待用户点选）。
        未注入时返回 USER_DENIED（fail-closed）。
        """
        self._ask_callback = ask_callback

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        questions = args.get("questions", [])

        if not questions:
            return self._error(tool_call, "questions is required", "invalid_argument")
        if not isinstance(questions, list):
            return self._error(
                tool_call, "questions must be a list", "invalid_argument"
            )

        if self._ask_callback is None:
            # fail-closed：无 GUI 回调时返回 USER_DENIED
            return ToolResult(
                tool_call_id=tool_call.id,
                content="ask_user: no GUI callback available (cannot prompt user)",
                variant=ToolResultVariant.USER_DENIED,
            )

        try:
            # 同步调用回调（GUI 阻塞弹窗）
            answers = self._ask_callback(questions)
            return self._success(
                tool_call,
                json.dumps(answers, ensure_ascii=False, indent=2),
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"ask_user failed: {type(e).__name__}: {e}",
                "ask_user_error",
            )
