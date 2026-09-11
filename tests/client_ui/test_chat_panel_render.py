"""UI 渲染回归测试：classify_tool 分类 + Anti-Cheat 禁止 hex 硬编码

注：原 Ticket 05 三件套测试（_MessageBubble 渲染 / _ToolCallCard 染色 /
_ToolResultCard 结构化）已在 chat-panel-v2 重设计中随死代码一并删除。
新块类（_UserBubble / _AssistantTextBlock / _ThinkingBlock / _ToolCallBlock /
_SystemBlock）的覆盖见 tests/test_chat_panel_v2_timeline.py 与
tests/test_chat_panel_v2_tool_call_block.py。

本文件保留：
- TestToolCategoryClassification：classify_tool(name) 按 ToolCategory 5 类映射（spec D3）
- TestNoHexHardcoding：chat.py 源码无 hex 颜色硬编码（spec Anti-Cheat）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Qt offscreen 模式（必须在 import PySide6 之前设置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHAT_PY = PROJECT_ROOT / "client" / "panels" / "chat.py"


# ============================================================================
# D3: ToolCategory 分类函数（5 类 + other）
# ============================================================================


class TestToolCategoryClassification:
    """classify_tool(name) 按 ToolCategory 5 类映射（spec D3）。"""

    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            # execute: exec_*/exec_cmd
            ("exec_cmd", "execute"),
            ("exec_python", "execute"),
            ("exec_status", "execute"),
            ("exec_inspect", "execute"),
            ("exec_kill", "execute"),
            ("exec_terminal_spawn", "execute"),
            # read: read/docviewer/ocr
            ("read_file", "read"),
            ("docviewer_read", "read"),
            ("ocr_file", "read"),
            ("ocr_path", "read"),
            ("ocr_status", "read"),
            # edit: apply_patch/edit/write
            ("exec_apply_patch", "edit"),
            ("apply_patch", "edit"),
            ("edit", "edit"),
            ("write", "edit"),
            ("delete_file", "edit"),
            # search: grep/glob/search
            ("grep", "search"),
            ("glob", "search"),
            ("search", "search"),
            ("search_codebase", "search"),
            # skill: agent_guide/memory_*/skill
            ("agent_guide", "skill"),
            ("memory_set", "skill"),
            ("memory_get", "skill"),
            ("memory_list", "skill"),
            ("skill_create", "skill"),
            # other: 未匹配
            ("unknown_tool", "other"),
            ("browser_navigate", "other"),
            ("screen_capture", "other"),
        ],
    )
    def test_classify_tool(self, tool_name, expected):
        from client.panels.chat import classify_tool

        assert classify_tool(tool_name) == expected, (
            f"classify_tool({tool_name!r}) 应返回 {expected!r}"
        )


# ============================================================================
# Anti-Cheat: 禁止 hex 硬编码
# ============================================================================


class TestNoHexHardcoding:
    """chat.py 源码无 hex 颜色硬编码（spec Anti-Cheat）。"""

    def test_no_hex_color_in_chat_py(self):
        """chat.py 源码不含 #[0-9a-fA-F]{6} 颜色硬编码。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        # 排除注释行（# 开头的 Python 注释，但保留 stylesheet 中的 hex）
        # 简单 grep：所有 #XXXXXX 模式
        import re

        matches = re.findall(r"#[0-9a-fA-F]{6}\b", source)
        assert matches == [], (
            f"chat.py 中发现 {len(matches)} 处 hex 颜色硬编码（spec Anti-Cheat 禁止，"
            "应用 lib/ui/tokens 常量）: " + ", ".join(matches)
        )
