"""D4 · 工具并发安全默认值改 fail-closed（spec D14）。

v6-05 设计：`is_concurrency_safe: bool = False  # 默认串行`。
当前实现 default=True（fail-open），新工具未声明时默认可并发——不安全。

D4 修复：
- ToolEntry.is_concurrency_safe 默认改 False（fail-closed）
- 已知安全的内置工具（file_read/grep/glob/ls/ask_user/web_search/web_fetch）
  在 builtin_tool_executor.py 显式设 True（已存在）

测试策略（red-green）：
- 红测试：新建 ToolEntry 不传 is_concurrency_safe，断言默认 True（修复前"通过"，
  但这是 bug 行为；修复后断言 False）
- 绿测试：修复后断言默认 False
- 显式 True/False 仍可覆盖默认值
- builtin tools 仍显式 True（read-only 安全）
- REST 工具（未显式声明）默认 False
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.tool_registry import ToolEntry  # noqa: E402


class TestToolEntryDefaultConcurrencySafe:
    """ToolEntry 默认 is_concurrency_safe 应为 False（fail-closed，D14）。"""

    def test_default_is_false(self):
        """新建 ToolEntry 不传 is_concurrency_safe → 默认 False（fail-closed）。"""
        entry = ToolEntry(
            operation_id="test_tool",
            method="GET",
            path="/test",
            summary="test",
        )
        # D4：默认 False（fail-closed），新工具未声明时串行执行
        assert entry.is_concurrency_safe is False, (
            "D4: ToolEntry.is_concurrency_safe 默认应为 False（fail-closed），"
            "新工具未声明时默认串行；当前默认 True 是 fail-open，不安全"
        )

class TestBuiltinToolsExplicitConcurrencySafe:
    """内置工具（read-only）应显式设 is_concurrency_safe=True。"""

    def test_builtin_tools_are_concurrency_safe(self):
        """BuiltinToolExecutor 注册的工具应显式 is_concurrency_safe=True。"""
        from client.core.agent.builtin_tool_executor import BuiltinToolExecutor

        executor = BuiltinToolExecutor()
        entries = executor.list_tool_entries()
        assert len(entries) > 0, "应至少注册一个内置工具"

        for entry in entries:
            # 内置工具是 read-only（file_read/grep/glob/ls/ask_user/web_search/web_fetch）
            # 应显式设 True（D4 后默认 False，必须显式覆盖）
            assert entry.is_concurrency_safe is True, (
                f"D4: 内置工具 {entry.operation_id} 应显式 is_concurrency_safe=True "
                f"（read-only 工具可并发），当前为 {entry.is_concurrency_safe}"
            )


class TestRestToolsDefaultFailClosed:
    """REST 工具（从 openapi 拉取）未显式声明时默认 False（fail-closed）。"""

