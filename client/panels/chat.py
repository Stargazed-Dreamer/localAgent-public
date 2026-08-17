"""ChatPanel — v6-lite 对话面板（T07）

PanelBase 子类，自动发现。会话列表 + 消息时间线 + 工具调用折叠卡 + 审批入口 + 中断按钮。
QThread worker 跑 SessionRunner.run()，主线程 QTimer 轮询 EventStore 渲染中间状态。
首版非流式（v6-lite §3 W4）。

设计依据：
- v6-lite §3 W4：ChatPanel 最小可用 + 防死循环
- v6-lite §4.6：synthetic 消息 visible=false（面板不渲染）
- v6-02 §2.1：has_attempted_* retry 不重置（T07 预留字段，T08 触发逻辑）
- v6-02 §3.1：QThread worker + Signal，禁止 worker 直触 widget
- v6-lite §5：引擎是 GUI 进程内库（不起第三个进程）

架构：
- 主线程：_read_store（EventStore 只读，QTimer 200ms 轮询渲染）
- worker 线程：_ChatWorker 持有自己的 EventStore（写）+ SessionRunner
- 两个 EventStore 实例共享同一 DB 文件（SQLite WAL 支持并发读不阻塞写）
- 中断：主线程调 worker.interrupt() → worker loop 内 call_soon_threadsafe set interrupt_event
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from client.core.agent import (
    BuiltinToolExecutor,
    Compactor,
    EventStore,
    HttpClientToolExecutor,
    L0ArtifactStore,
    LLMPoolGateway,
    Message,
    RunnerConfig,
    RunnerDeps,
    SessionFacade,
    ToolRegistry,
    list_all_task_types,
    template_create,
    template_delete,
    template_load_all,
    template_update,
)
from client.core.agent.event_store import DEFAULT_DB_PATH
from client.core.agent.types import (
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_PENDING,
    TOOL_STATUS_RUNNING,
)
from client.core.constants import SERVER_URL
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import icon_button, tokens
from lib.ui.theme import set_kind, set_text_role

logger = logging.getLogger("localagent.panel.chat")


# ============================================================================
# 样式与常量
# ============================================================================

_TOOL_STATUS_COLOR = {
    TOOL_STATUS_PENDING: tokens.WARNING_TEXT,
    TOOL_STATUS_RUNNING: tokens.INFO_TEXT,
    TOOL_STATUS_COMPLETED: tokens.SUCCESS_TEXT,
    TOOL_STATUS_FAILED: tokens.DANGER_TEXT,
    "interrupted": tokens.TEXT_TERTIARY,
}

_TOOL_STATUS_TEXT = {
    TOOL_STATUS_PENDING: "待执行",
    TOOL_STATUS_RUNNING: "执行中",
    TOOL_STATUS_COMPLETED: "已完成",
    TOOL_STATUS_FAILED: "失败",
    "interrupted": "已中断",
}

_SESSION_STATUS_TEXT = {
    "idle": "空闲",
    "streaming": "生成中",
    "awaiting_tools": "等待工具",
    "compacting": "压缩中",
    "completed": "已完成",
    "interrupted": "已中断",
    "failed": "失败",
}

_SESSION_STATUS_COLOR = {
    "idle": tokens.TEXT_TERTIARY,
    "streaming": tokens.INFO_TEXT,
    "awaiting_tools": tokens.WARNING_TEXT,
    "compacting": tokens.WARNING_TEXT,
    "completed": tokens.SUCCESS_TEXT,
    "interrupted": tokens.TEXT_TERTIARY,
    "failed": tokens.DANGER_TEXT,
}

# headless-agent-session Ticket 10: 按 session.mode 渲染 badge
# - headless: 后端自主运行的会话（headless_session action 启动）
# - headless_judge: judge agent 判定会话
# - probe: 对话管理探针会话（chat-mgmt-probe，MockLLM 跑骨架）
# - dialogue（默认）: 普通 chat 会话，无 badge
_SESSION_MODE_BADGE = {
    "headless": "后端",
    "headless_judge": "判定",
    "probe": "探针",
}

_ROLE_TEXT = {
    "user": "你",
    "assistant": "Agent",
    "tool": "工具结果",
    "system": "系统",
}

# v6-lite-streaming-gui T03: 打字机三档（spec D07/D08/D09）
_TYPEWRITER_MODES = ("close", "fast", "normal")
_TYPEWRITER_MODE_TEXT = {"close": "关闭", "fast": "快速", "normal": "正常"}
# 各档位 _poll_timer 间隔（ms）：close 只看完整消息；fast 30ms 批量 append；normal 16ms 60fps
_TYPEWRITER_POLL_INTERVAL = {"close": 200, "fast": 30, "normal": 16}
# 单条消息 >4k 字符自动降级为一次性渲染（spec D07 Mitigation）
_LARGE_MESSAGE_THRESHOLD = 4000
# T05: content="" 的 assistant 消息占位文本（spec D6）
_EMPTY_ASSISTANT_PLACEHOLDER = "思考中..."

# T06: 动效时长（spec D5，淡入 150ms，工具卡展开 200ms）
_FADE_IN_DURATION_MS = 150
_TOOL_CARD_EXPAND_DURATION_MS = 200

# chat-panel-v2 T10（D47）：token 单位换算（B/K/M/B/T，整数 ≤3 位 + 1 位小数）
# < 1000 用原值；1000-999999 用 K；1M-999M 用 M；1B-999B 用 B；≥ 1T 用 T
_TOKEN_UNITS = [("", 1), ("K", 1_000), ("M", 1_000_000), ("B", 1_000_000_000), ("T", 1_000_000_000_000)]


def _format_token_count(tokens_count: int) -> str:
    """格式化 token 数为单位字符串（D47：整数 ≤3 位 + 1 位小数）。

    - 0 → "0"
    - 999 → "999"
    - 1500 → "1.5K"
    - 1200000 → "1.2M"
    """
    if tokens_count <= 0:
        return "0"
    # 找最大单位使 value >= 1
    for i in range(len(_TOKEN_UNITS) - 1, 0, -1):
        unit, divisor = _TOKEN_UNITS[i]
        value = tokens_count / divisor
        if value >= 1:
            # 整数 ≤3 位 + 1 位小数（>= 100 时取整，>= 10 时 1 位小数，否则 2 位小数）
            if value >= 100:
                return f"{int(value)}{unit}"
            elif value >= 10:
                return f"{value:.1f}{unit}"
            else:
                return f"{value:.1f}{unit}"
    # < 1000，用原值
    return str(int(tokens_count))


def _truncate_title(title: str, max_chars: int = 32) -> str:
    """D47: 标题超 max_chars 字符截断 + 省略号。"""
    if not title:
        return ""
    if len(title) <= max_chars:
        return title
    return title[:max_chars] + "…"

# T06: 会话标题长度（spec D7，前 20 字）
_SESSION_TITLE_MAX_LEN = 20

# chat-panel-v2 T09: 对话控制三模式（D34）
MODE_SEND = "send"      # 发送：中断 runner + start 新 run
MODE_QUEUE = "queue"    # 队列：存排队区，run 完成后自动 start
MODE_STEER = "steer"    # 引导：facade.steer，不打断
# 排队区消息超长截断阈值（D36：超长截断 + tooltip 全文）
_QUEUE_TEXT_TRUNCATE = 60

# T11: 导出格式 + 文件过滤器（D16）
_EXPORT_FILE_FILTER = "Markdown (*.md);;JSON (*.json);;两者都导 (*.md *.json)"
_EXPORT_FORMAT_MD = "md"
_EXPORT_FORMAT_JSON = "json"
_EXPORT_FORMAT_BOTH = "both"

# T11: 操作按钮 hover 显示延迟（D33）
_HOVER_BUTTON_FADE_MS = 100

# T04: ToolCategory 5 类染色（spec D3）
# execute黄 / read青(用 INFO 蓝) / edit紫(用 DANGER 红，无紫 token) / search蓝(用 ACCENT 青) / skill绿 / other灰
_TOOL_CATEGORY_COLOR = {
    "execute": tokens.WARNING_TEXT,
    "read": tokens.INFO_TEXT,
    "edit": tokens.DANGER_TEXT,
    "search": tokens.ACCENT,
    "skill": tokens.SUCCESS_TEXT,
    "other": tokens.TEXT_TERTIARY,
}


def classify_tool(tool_name: str) -> str:
    """按 ToolCategory 5 类映射工具名（spec D3）。

    - exec_*/exec_cmd → execute（黄）
    - read/docviewer/ocr → read（青/蓝）
    - apply_patch/edit/write/delete_file → edit（紫/红）
    - grep/glob/search → search（青/teal）
    - agent_guide/memory_*/skill → skill（绿）
    - 其余 → other（灰）
    """
    name = (tool_name or "").lower()
    if not name:
        return "other"
    # edit 类先判（exec_apply_patch 含 exec_ 前缀，要在 execute 之前判）
    if "apply_patch" in name or name in ("edit", "write", "delete_file", "delete"):
        return "edit"
    # execute 类
    if name.startswith("exec_") or name == "exec":
        return "execute"
    # read 类
    if "read" in name or "docviewer" in name or "ocr" in name:
        return "read"
    # search 类
    if name in ("grep", "glob", "search", "search_codebase") or "search" in name:
        return "search"
    # skill 类
    if "agent_guide" in name or name.startswith("memory_") or "skill" in name:
        return "skill"
    return "other"

_MONO_TEXT_STYLE = (
    f"QTextEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
    f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
    f" border: 1px solid {tokens.BG_CARD}; padding: 4px; }}"
)


def _safe_parse_json(s: str) -> dict:
    """安全解析 JSON 字符串，失败返回空 dict。"""
    try:
        return json.loads(s) if s else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _content_to_text(content) -> str:
    """把 Message.content（str 或 OpenAI content blocks list）转纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content) if content is not None else ""


# ============================================================================
# T11: 导出格式化（D16 + D17 + D32）
# ============================================================================


def _tool_call_to_md_quote(tool_call) -> str:
    """把 ToolCall 渲染为 MD 引用块（D32：tool_calls 引用块）。

    格式：
        > [tool_call] name(args_json)
        > status: pending / completed / failed
    """
    if hasattr(tool_call, "name"):
        name = tool_call.name
        args = tool_call.args if hasattr(tool_call, "args") else {}
        status = tool_call.status if hasattr(tool_call, "status") else "unknown"
        args_raw = tool_call.args_raw if hasattr(tool_call, "args_raw") else ""
    elif isinstance(tool_call, dict):
        fn = tool_call.get("function", {}) if "function" in tool_call else tool_call
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if args_raw else {}
        except Exception:
            args = {}
        status = "completed"
    else:
        name = str(tool_call)
        args = {}
        args_raw = ""
        status = "unknown"
    args_str = json.dumps(args, ensure_ascii=False, indent=2) if args else args_raw or "{}"
    return f"> [tool_call] {name}\n> args: {args_str}\n> status: {status}"


def _format_session_as_markdown(
    session, messages: list, tool_calls: list
) -> str:
    """D16 + D32：把会话渲染为 Markdown。

    结构：
        # {session.title}
        > mode: {mode} | status: {status} | created_at: {iso}

        ## user
        {content}

        ## assistant
        > [thinking] ...
        > [tool_call] name(args)
        {content}

    范围（D32）：用户消息后到下一条用户消息前的所有内容为整段 agent 回复，
    thinking/tool_calls 渲染为引用块。
    """
    # session 可能是 Session 对象或 dict
    if session is None:
        title = "未命名会话"
        meta = ""
    elif isinstance(session, dict):
        title = session.get("title", "") or "未命名会话"
        meta = (
            f"mode: {session.get('mode', 'dialogue')} | "
            f"status: {session.get('status', 'idle')}"
        )
    else:
        title = getattr(session, "title", "") or "未命名会话"
        meta = f"mode: {getattr(session, 'mode', 'dialogue')} | status: {getattr(session, 'status', 'idle')}"

    lines: list[str] = [f"# {title}", ""]
    if meta:
        lines.append(f"> {meta}")
        lines.append("")

    # tool_calls 按 seq 索引，便于按 message seq 查找
    tc_by_seq: dict[int, list] = {}
    for tc in tool_calls:
        seq = tc.seq if hasattr(tc, "seq") else tc.get("seq", 0) if isinstance(tc, dict) else 0
        tc_by_seq.setdefault(seq, []).append(tc)

    for msg in messages:
        # 跳过不可见 synthetic 消息（D32：不导出 invisible）
        visible = getattr(msg, "visible", True) if not isinstance(msg, dict) else msg.get("visible", True)
        if not visible:
            continue
        role = msg.role if hasattr(msg, "role") else msg.get("role", "unknown")
        content_text = _content_to_text(msg.content if hasattr(msg, "content") else msg.get("content", ""))
        thinking = msg.thinking if hasattr(msg, "thinking") else msg.get("thinking", "")
        msg_tool_calls = msg.tool_calls if hasattr(msg, "tool_calls") else msg.get("tool_calls", [])
        model = msg.model if hasattr(msg, "model") else msg.get("model", "")
        seq = msg.seq if hasattr(msg, "seq") else msg.get("seq", 0)

        if role == "user":
            lines.append("## user")
            lines.append("")
            lines.append(content_text)
            lines.append("")
        elif role == "assistant":
            lines.append("## assistant")
            if model:
                lines.append(f"*model: {model}*")
            lines.append("")
            # thinking 引用块
            if thinking:
                lines.append("> [thinking]")
                for tl in thinking.splitlines() or [""]:
                    lines.append(f"> {tl}")
                lines.append("")
            # tool_calls 引用块（message 自带的 + DB 中同 seq 的）
            all_tcs = list(msg_tool_calls) if msg_tool_calls else []
            for tc in tc_by_seq.get(seq, []):
                all_tcs.append(tc)
            for tc in all_tcs:
                lines.append(_tool_call_to_md_quote(tc))
                lines.append("")
            # 最终文本
            if content_text:
                lines.append(content_text)
                lines.append("")
        elif role == "tool":
            # tool_result 渲染为引用块（D32：tool_result 一并归到 agent 回复范围内）
            tc_id = msg.tool_call_id if hasattr(msg, "tool_call_id") else msg.get("tool_call_id", "")
            lines.append(f"> [tool_result] (call_id: {tc_id})")
            for tl in content_text.splitlines() or [""]:
                lines.append(f"> {tl}")
            lines.append("")
        elif role == "system":
            lines.append("## system")
            lines.append("")
            lines.append(content_text)
            lines.append("")
        # 其他 role（synthetic 等）跳过

    return "\n".join(lines)


def _format_session_as_json(
    session, messages: list, tool_calls: list, events: list | None = None
) -> str:
    """D16：把会话渲染为 JSON 完整备份（含 messages + tool_calls + events）。"""
    # session 序列化
    if session is None:
        sess_dict = {}
    elif isinstance(session, dict):
        sess_dict = dict(session)
    else:
        sess_dict = {
            "id": getattr(session, "id", ""),
            "title": getattr(session, "title", ""),
            "mode": getattr(session, "mode", "dialogue"),
            "status": getattr(session, "status", "idle"),
            "created_at": getattr(session, "created_at", 0.0),
            "updated_at": getattr(session, "updated_at", 0.0),
            "group_name": getattr(session, "group_name", None),
            "pinned": getattr(session, "pinned", False),
        }

    # messages 序列化
    msg_list = []
    for msg in messages:
        if hasattr(msg, "to_db"):
            msg_list.append(msg.to_db())
        elif isinstance(msg, dict):
            msg_list.append(dict(msg))
        else:
            msg_list.append({"content": str(msg)})

    # tool_calls 序列化
    tc_list = []
    for tc in tool_calls:
        if hasattr(tc, "to_db"):
            tc_list.append(tc.to_db())
        elif isinstance(tc, dict):
            tc_list.append(dict(tc))
        else:
            tc_list.append({"name": str(tc)})

    # events 序列化
    ev_list = []
    if events:
        for ev in events:
            if hasattr(ev, "__dict__"):
                ev_dict = {
                    "seq": getattr(ev, "seq", 0),
                    "session_id": getattr(ev, "session_id", ""),
                    "type": getattr(ev, "type", ""),
                    "payload": getattr(ev, "payload", {}),
                    "created_at": getattr(ev, "created_at", 0.0),
                    "trace_id": getattr(ev, "trace_id", None),
                    "prompt_index": getattr(ev, "prompt_index", None),
                    "invalidated_seq": getattr(ev, "invalidated_seq", None),
                    "parent_trace_id": getattr(ev, "parent_trace_id", None),
                    "depth": getattr(ev, "depth", 0),
                }
                ev_list.append(ev_dict)
            elif isinstance(ev, dict):
                ev_list.append(dict(ev))

    export = {
        "version": "chat-panel-v2-T11",
        "exported_at": time.time(),
        "session": sess_dict,
        "messages": msg_list,
        "tool_calls": tc_list,
        "events": ev_list,
    }
    return json.dumps(export, ensure_ascii=False, indent=2, default=str)


# ============================================================================
# chat-panel-v2 T07: 消息时间线块结构（5 块类 + _MessageTimeline 容器）
# Spec: D38（时间线独立块）/ D39（对齐与 bubble）/ D40（折叠规则）/
#       D43（流式中断）/ D44（system 样式）/ D45（5 块类拆分）/ D46（v6 复用）
# ============================================================================


class _TimelineBlock(QFrame):
    """时间线块基类（D45）：淡入动效（D46 #4）+ 弹性布局（D46 #9）。

    所有块共享：
    - QGraphicsOpacityEffect 0.0→1.0 150ms OutQuad 淡入（出错静默降级）
    - QSizePolicy.Expanding 水平方向，竖直方向 Preferred（不固定高度）
    - 不设 setMaximumHeight（D40：展开后高度自适应）

    T08 sticky 标题栏接口（D42/D42a）：
    - is_collapsible：是否可折叠（True=sticky 候选；False=不触发 sticky 标题）
    - title：sticky 标题文本（如 "thinking" / "exec_python"）
    - collapse()：折叠本块（sticky 标题点击时调用）
    - 默认不可折叠，子类按需覆盖
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("timelineBlock")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_fade_in()

    def _apply_fade_in(self) -> None:
        """D46 #4：淡入动效（150ms OutQuad，出错静默降级）。"""
        try:
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(0.0)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(_FADE_IN_DURATION_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutQuad)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        except Exception as e:
            logger.debug("fade-in animation failed (degraded to no anim): %s", e)

    # ------------------------------------------------------------------
    # T08 sticky 标题栏接口（D42/D42a）
    # ------------------------------------------------------------------

    @property
    def is_collapsible(self) -> bool:
        """是否可折叠（默认 False，子类按需覆盖）。

        - True：sticky 标题候选（thinking / tool_call / system 等可折叠块）
        - False：不触发 sticky 标题（user 消息 / agent 最终文本）
        """
        return False

    @property
    def title(self) -> str:
        """sticky 标题文本（默认空字符串，子类按需覆盖）。"""
        return ""

    def collapse(self) -> None:
        """折叠本块（sticky 标题点击时调用，默认 no-op）。"""
        return


class _UserBubble(_TimelineBlock):
    """user / steer / queue 消息块（D45 + D37 + D39 + D33 hover 按钮）。

    - user：右对齐 + ACCENT_WASH bubble（圆角 + 浅色背景）
    - steer：右对齐 + WARNING 色调小字"用户引导"标记
    - queue：右对齐 + INFO 色调小字"排队中"标记
    - T11 D33：hover 显示 Copy/Delete 按钮（仅 user 消息，steer/queue 不显示）
    """

    # T11 信号：ChatPanel 连接
    copy_requested = Signal(str)  # 参数 = message content text
    delete_requested = Signal(str)  # 参数 = message id

    def __init__(self, message: Message, parent=None):
        self._message = message
        super().__init__(parent)
        self._hover_button_row: QWidget | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        is_steer = self._message.source == "steer"
        is_queue = self._message.source == "queue"
        content_text = _content_to_text(self._message.content)
        # D33：只有 user（非 steer/queue）才显示 hover 按钮
        self._can_hover = not is_steer and not is_queue

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(2)

        # bubble 行（右对齐）
        bubble_row = QHBoxLayout()
        bubble_row.setContentsMargins(0, 0, 0, 0)
        bubble_row.setSpacing(4)
        bubble_row.addStretch(1)

        bubble = QFrame()
        bubble.setObjectName("userBubble")
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        bubble.setStyleSheet(
            f"QFrame {{ background: {tokens.ACCENT_WASH}; border: none;"
            f" border-radius: {tokens.RADIUS_MD}px; }}"
        )
        blayout = QVBoxLayout(bubble)
        blayout.setContentsMargins(12, 8, 12, 8)
        blayout.setSpacing(4)

        # 角色标签
        if is_steer:
            role_label = QLabel("用户引导")
            set_text_role(role_label, "warning")
        elif is_queue:
            role_label = QLabel("排队中")
            set_text_role(role_label, "secondary")
        else:
            role_label = QLabel("你")
            set_text_role(role_label, "accent")
        blayout.addWidget(role_label)

        # 内容（user 用 setPlainText，不 md 渲染）
        content_view = QTextEdit()
        content_view.setReadOnly(True)
        content_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_view.setPlainText(content_text)
        content_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_view.setStyleSheet(
            f"QTextEdit {{ background: transparent; color: {tokens.TEXT_PRIMARY};"
            f" border: none; padding: 0; font-size: {tokens.FONT_BODY}px; }}"
        )
        doc_height = content_view.document().size().height()
        content_view.setMinimumHeight(min(max(int(doc_height) + 12, 28), 520))
        blayout.addWidget(content_view)

        bubble_row.addWidget(bubble)
        outer.addLayout(bubble_row)

        # T11 D33：hover 按钮行（右对齐，默认隐藏）
        if self._can_hover:
            button_row = QHBoxLayout()
            button_row.setContentsMargins(0, 0, 0, 0)
            button_row.setSpacing(4)
            button_row.addStretch(1)  # 右对齐

            copy_btn = QPushButton("📋 复制")
            copy_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {tokens.TEXT_TERTIARY};"
                f" border: 1px solid {tokens.BORDER};"
                f" border-radius: {tokens.RADIUS_SM}px; padding: 2px 8px;"
                f" font-size: {tokens.FONT_SMALL}px; }}"
                f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
            )
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.clicked.connect(
                lambda: self.copy_requested.emit(_content_to_text(self._message.content))
            )
            button_row.addWidget(copy_btn)

            delete_btn = QPushButton("🗑 删除")
            delete_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {tokens.TEXT_TERTIARY};"
                f" border: 1px solid {tokens.BORDER};"
                f" border-radius: {tokens.RADIUS_SM}px; padding: 2px 8px;"
                f" font-size: {tokens.FONT_SMALL}px; }}"
                f"QPushButton:hover {{ background: {tokens.BG_HOVER};"
                f" color: {tokens.DANGER_TEXT}; }}"
            )
            delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            delete_btn.clicked.connect(
                lambda: self.delete_requested.emit(self._message.id or "")
            )
            button_row.addWidget(delete_btn)

            button_container = QWidget()
            button_container.setObjectName("userHoverButtons")
            button_container.setLayout(button_row)
            button_container.setVisible(False)
            outer.addWidget(button_container)
            self._hover_button_row = button_container

    def enterEvent(self, event) -> None:
        """D33：鼠标进入块时显示 hover 按钮。"""
        if self._hover_button_row is not None:
            self._hover_button_row.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """D33：鼠标离开块时隐藏 hover 按钮。"""
        if self._hover_button_row is not None:
            self._hover_button_row.setVisible(False)
        super().leaveEvent(event)


class _AssistantTextBlock(_TimelineBlock):
    """agent 最终文本块（D45 + D39 + D40 + D46 #6/#10 + D33 hover Copy 按钮）。

    - 左对齐 + 无 bubble（无背景色 / 无边框 / 直接展示在时间线上）
    - QTextBrowser setMarkdown 渲染（永不折叠，D40）
    - 支持流式增量（start_streaming / append_text_delta / finalize_text）
    - >4k 自动降级（D46 #6）
    - content="" 时显示"思考中..."占位（D46 #10）
    - finalize 后可设置 model 名（D31，气泡下方小字）
    - D43：流式中断 → finalize_as_interrupted 保留累积文本 + "已中断"小字
    - T11 D33：finalize 后 hover 显示 Copy 按钮（流式中不显示）
    """

    # T11 信号：ChatPanel 连接
    copy_requested = Signal(str)  # 参数 = accumulated text

    def __init__(self, content: str = "", parent=None):
        # 流式状态字段（需在 super().__init__ 前初始化）
        self._content_view: QTextBrowser | None = None
        self._accumulated_text: str = content
        self._is_streaming: bool = False
        self._degraded_to_close: bool = False
        self._showing_placeholder: bool = False
        self._model_label: QLabel | None = None
        self._model: str = ""
        self._interrupted: bool = False
        # T11: hover Copy 按钮（finalize 后才显示）
        self._hover_copy_btn: QPushButton | None = None
        self._finalized: bool = False
        super().__init__(parent)
        self._build_ui(content)

    def _build_ui(self, content: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(2)

        # 内容区（QTextBrowser setMarkdown）
        content_view = QTextBrowser()
        content_view.setOpenExternalLinks(True)
        content_view.setReadOnly(True)
        content_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; color: {tokens.TEXT_PRIMARY};"
            f" border: none; padding: 0; font-size: {tokens.FONT_BODY}px; }}"
        )
        if content:
            content_view.setMarkdown(content)
        else:
            content_view.setMarkdown(_EMPTY_ASSISTANT_PLACEHOLDER)
            self._showing_placeholder = True
        doc_height = content_view.document().size().height()
        content_view.setMinimumHeight(min(max(int(doc_height) + 12, 28), 520))
        outer.addWidget(content_view)
        self._content_view = content_view

        # model 名 + 中断标记（小字，finalize 后显示）
        self._model_label = QLabel()
        self._model_label.setStyleSheet(
            f"color: {tokens.TEXT_TERTIARY}; font-size: {tokens.FONT_SMALL}px;"
        )
        self._model_label.setVisible(False)
        outer.addWidget(self._model_label)

        # T11 D33：hover Copy 按钮（左对齐，默认隐藏，finalize 后才激活）
        copy_btn = QPushButton("📋 复制")
        copy_btn.setObjectName("assistantCopyBtn")
        copy_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {tokens.TEXT_TERTIARY};"
            f" border: 1px solid {tokens.BORDER};"
            f" border-radius: {tokens.RADIUS_SM}px; padding: 2px 8px;"
            f" font-size: {tokens.FONT_SMALL}px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setVisible(False)
        copy_btn.clicked.connect(
            lambda: self.copy_requested.emit(self._accumulated_text)
        )
        outer.addWidget(copy_btn)
        self._hover_copy_btn = copy_btn

    def start_streaming(self) -> None:
        """标记此块为流式状态（打字机增量 append 模式）。"""
        self._is_streaming = True
        self._degraded_to_close = False
        self._accumulated_text = ""
        if self._showing_placeholder and self._content_view is not None:
            self._content_view.setPlainText("")
            self._showing_placeholder = False

    def append_text_delta(self, delta: str) -> None:
        """增量 append 文本 delta（>4k 自动降级，D46 #6）。"""
        if not delta or not self._is_streaming or self._content_view is None:
            return
        if self._degraded_to_close:
            return
        if self._showing_placeholder:
            self._content_view.setPlainText("")
            self._showing_placeholder = False
        self._accumulated_text += delta
        if len(self._accumulated_text) > _LARGE_MESSAGE_THRESHOLD:
            self._degraded_to_close = True
            self._content_view.setPlainText("")
            return
        cursor = self._content_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(delta)
        self._content_view.setTextCursor(cursor)

    def finalize_text(self, full_text: str) -> None:
        """流结束，一次性 setMarkdown 渲染最终文本。"""
        self._is_streaming = False
        if self._content_view is None:
            return
        self._showing_placeholder = False
        if full_text:
            self._content_view.setMarkdown(full_text)
        else:
            self._content_view.setPlainText("")
        doc_height = self._content_view.document().size().height()
        self._content_view.setMinimumHeight(min(max(int(doc_height) + 12, 28), 520))
        self._update_model_label()
        # T11: finalize 后激活 hover Copy 按钮
        self._finalized = True

    def finalize_as_interrupted(self) -> None:
        """D43: 流式中断 → 把已累积文本转正为中断 assistant 块。"""
        self._is_streaming = False
        self._interrupted = True
        if self._content_view is None:
            return
        self._showing_placeholder = False
        # 用已累积的文本作为最终内容（即使未流完）
        if self._accumulated_text:
            self._content_view.setMarkdown(self._accumulated_text)
        else:
            self._content_view.setPlainText("（已中断）")
        doc_height = self._content_view.document().size().height()
        self._content_view.setMinimumHeight(min(max(int(doc_height) + 12, 28), 520))
        self._update_model_label()
        # T11: 中断也算 finalize，激活 hover Copy 按钮
        self._finalized = True

    def enterEvent(self, event) -> None:
        """D33：鼠标进入块时显示 Copy 按钮（仅 finalize 后）。"""
        if self._hover_copy_btn is not None and self._finalized:
            self._hover_copy_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """D33：鼠标离开块时隐藏 Copy 按钮。"""
        if self._hover_copy_btn is not None:
            self._hover_copy_btn.setVisible(False)
        super().leaveEvent(event)

    def set_model(self, model: str) -> None:
        """D31：设置模型名（finalize 后在气泡下方小字显示）。"""
        self._model = model or ""
        self._update_model_label()

    def _update_model_label(self) -> None:
        """更新 model 名 + 中断标记小字。"""
        if self._model_label is None:
            return
        parts = []
        if self._interrupted:
            parts.append("已中断")
        if self._model:
            parts.append(self._model)
        if parts:
            self._model_label.setText(" · ".join(parts))
            self._model_label.setVisible(True)
        else:
            self._model_label.setVisible(False)


class _ThinkingBlock(_TimelineBlock):
    """thinking 内容块（D45 + D39 + D40 + D46 #11）。

    - 左对齐 + 无 bubble
    - dashed border + "▶ thinking" toggle 按钮（D46 #11）
    - 默认折叠，展开后无滚动（D40：高度自适应）
    - 支持流式增量（append_delta / finalize）
    - D43：流式中断 → finalize_as_interrupted 保留累积内容
    """

    def __init__(self, content: str = "", parent=None):
        self._content: str = content
        self._view: QTextEdit | None = None
        self._toggle_btn: QPushButton | None = None
        self._expanded: bool = False
        self._accumulated: str = content
        self._is_streaming: bool = False
        self._interrupted: bool = False
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 4, 12, 4)
        outer.setSpacing(2)

        # thinking 容器（dashed border）
        frame = QFrame()
        frame.setObjectName("thinkingBlock")
        frame.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_BASE}; border: 1px dashed {tokens.BORDER};"
            f" border-radius: {tokens.RADIUS_SM}px; }}"
        )
        flayout = QVBoxLayout(frame)
        flayout.setContentsMargins(8, 4, 8, 4)
        flayout.setSpacing(2)

        # 标题行（可点击折叠）
        header = QHBoxLayout()
        header.setSpacing(4)
        toggle_btn = QPushButton("▶ thinking")
        toggle_btn.setFlat(True)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {tokens.TEXT_SECONDARY};"
            f" padding: 0; border: none; background: transparent;"
            f" font-size: {tokens.FONT_SMALL}px; }}"
        )
        toggle_btn.clicked.connect(self.toggle)
        header.addWidget(toggle_btn)
        header.addStretch()
        flayout.addLayout(header)

        # thinking 内容（默认折叠）
        view = QTextEdit()
        view.setReadOnly(True)
        view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        view.setStyleSheet(
            f"QTextEdit {{ background: transparent; color: {tokens.TEXT_SECONDARY};"
            f" border: none; padding: 0; font-size: {tokens.FONT_SMALL}px; }}"
        )
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        view.setVisible(False)
        if self._content:
            view.setPlainText(self._content)
        flayout.addWidget(view)

        outer.addWidget(frame)
        self._view = view
        self._toggle_btn = toggle_btn

    def toggle(self) -> None:
        """折叠/展开 thinking 内容。"""
        self._expanded = not self._expanded
        if self._view is not None:
            self._view.setVisible(self._expanded)
        if self._toggle_btn is not None:
            self._toggle_btn.setText(
                "▼ thinking" if self._expanded else "▶ thinking"
            )

    @property
    def is_collapsible(self) -> bool:
        """T08：thinking 块可折叠（sticky 候选）。"""
        return True

    @property
    def title(self) -> str:
        """T08：sticky 标题 = 'thinking'。"""
        return "thinking"

    def collapse(self) -> None:
        """T08：折叠（sticky 标题点击时调用）。"""
        if self._expanded:
            self.toggle()

    def append_delta(self, delta: str) -> None:
        """增量 append thinking delta（streaming_thinking_delta 事件）。"""
        if not delta:
            return
        self._accumulated += delta
        if self._view is not None and self._expanded:
            cursor = self._view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(delta)
            self._view.setTextCursor(cursor)

    def finalize(self, full_text: str) -> None:
        """流结束，替换完整 thinking 文本。"""
        self._is_streaming = False
        if self._view is not None:
            self._view.setPlainText(full_text)

    def finalize_as_interrupted(self) -> None:
        """D43: 流式中断 → 把已累积 thinking 转正为独立 thinking 块。"""
        self._is_streaming = False
        self._interrupted = True
        if self._view is not None and self._accumulated:
            self._view.setPlainText(self._accumulated)


class _ToolCallBlock(_TimelineBlock):
    """tool_call + tool_result 合并块（D45 + D41 + D41a + D46 #1/#2/#3）。

    T08 完整实现：
    - 折叠态（默认）：单行 - 类别色点 + 状态点 + ▶ tool 名 + 状态文本
    - 展开态：三部分垂直堆叠
      1. 原请求：tool 名（类别染色）+ args JSON 代码块
      2. 原结果：tool_result content 原文代码块（仅当 attach_tool_result 后）
      3. 格式解析：5 关键字段表 + 其余 JSON 折叠（仅当 result 是 JSON；非 JSON 兜底走原结果）
    """

    # D41a：5 关键字段提取顺序（chat-panel-v2 T07 _ToolCallBlock 用）
    _KEY_FIELDS = ("success", "exit_code", "stdout", "stderr", "error")
    _KEY_FIELD_LABEL = {
        "success": "成功",
        "exit_code": "退出码",
        "stdout": "stdout",
        "stderr": "stderr",
        "error": "错误",
    }

    def __init__(
        self,
        tool_call_dict: dict | None = None,
        status: str = TOOL_STATUS_PENDING,
        tool_result_message: Message | None = None,
        parent=None,
    ):
        self._tc_dict = tool_call_dict or {}
        self._status = status
        self._tool_result_message = tool_result_message
        self._tool_call_id = self._extract_tool_call_id()
        self._expanded: bool = False
        self._row_widget: QFrame | None = None
        self._toggle_btn: QPushButton | None = None
        self._expand_content: QFrame | None = None
        super().__init__(parent)
        self._build_ui()

    def _extract_tool_call_id(self) -> str:
        if isinstance(self._tc_dict, dict):
            return self._tc_dict.get("id", "")
        return ""

    @property
    def tool_call_id(self) -> str:
        return self._tool_call_id

    @property
    def tool_name(self) -> str:
        """工具名（用于 sticky 标题）。"""
        func = self._tc_dict.get("function", {}) if isinstance(self._tc_dict, dict) else {}
        name = func.get("name", "") if isinstance(func, dict) else ""
        return name or "(unknown)"

    @property
    def is_collapsible(self) -> bool:
        """_ToolCallBlock 是可折叠块（sticky 标题候选）。"""
        return True

    @property
    def title(self) -> str:
        """T08：sticky 标题 = tool 名（与 _ThinkingBlock.title='thinking' 一致接口）。"""
        return self.tool_name

    def collapse(self) -> None:
        """折叠（sticky 标题点击时调用）。"""
        if self._expanded:
            self.toggle()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 2, 12, 2)
        outer.setSpacing(0)

        # 单行摘要（点击展开）
        self._build_summary_row(outer)
        # 展开内容容器（默认隐藏）
        self._build_expand_content(outer)

    def _build_summary_row(self, parent_layout: QVBoxLayout) -> None:
        """单行摘要：类别色点 + 状态点 + ▶ tool 名 + 状态文本。"""
        name = self.tool_name
        category = classify_tool(name)
        category_color = _TOOL_CATEGORY_COLOR.get(category, tokens.TEXT_TERTIARY)
        status_color = _TOOL_STATUS_COLOR.get(self._status, tokens.TEXT_TERTIARY)

        row = QFrame()
        row.setObjectName("toolCallRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        cat_icon = QLabel("●")
        cat_icon.setStyleSheet(f"color: {category_color}; font-size: 13px; font-weight: bold;")
        row_layout.addWidget(cat_icon)

        status_icon = QLabel("◆")
        status_icon.setStyleSheet(f"color: {status_color}; font-size: 9px;")
        row_layout.addWidget(status_icon)

        arrow = "▼" if self._expanded else "▶"
        toggle_btn = QPushButton(f"{arrow} {name}")
        toggle_btn.setFlat(True)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {category_color};"
            f" padding: 0; border: none; background: transparent;"
            f" font-family: {tokens.FONT_MONO}; font-size: 12px; }}"
            f"QPushButton:hover {{ color: {tokens.TEXT_PRIMARY}; }}"
        )
        toggle_btn.clicked.connect(self.toggle)
        row_layout.addWidget(toggle_btn)
        row_layout.addStretch()

        status_text = QLabel(_TOOL_STATUS_TEXT.get(self._status, self._status))
        status_text.setStyleSheet(f"color: {status_color}; font-size: 11px;")
        row_layout.addWidget(status_text)

        parent_layout.addWidget(row)
        self._row_widget = row
        self._toggle_btn = toggle_btn

    def _build_expand_content(self, parent_layout: QVBoxLayout) -> None:
        """展开内容容器：三部分垂直堆叠。"""
        expand = QFrame()
        expand.setObjectName("toolCallExpand")
        expand.setVisible(self._expanded)
        ec_layout = QVBoxLayout(expand)
        ec_layout.setContentsMargins(8, 4, 8, 4)
        ec_layout.setSpacing(6)

        # 1. 原请求区
        self._build_request_section(ec_layout)
        # 2. 原结果区 + 3. 格式解析区（仅当 attach_tool_result 后渲染）
        self._build_result_section(ec_layout)

        parent_layout.addWidget(expand)
        self._expand_content = expand

    def _build_request_section(self, parent_layout: QVBoxLayout) -> None:
        """原请求：tool 名（类别染色）+ args JSON 代码块。"""
        func = self._tc_dict.get("function", {}) if isinstance(self._tc_dict, dict) else {}
        args_raw = func.get("arguments", "{}") if isinstance(func, dict) else "{}"
        # 美化 JSON
        try:
            args_obj = json.loads(args_raw) if args_raw else {}
            args_pretty = json.dumps(args_obj, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            args_pretty = args_raw or "{}"

        title = QLabel("原请求")
        title.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_TERTIARY};"
            f" font-size: 11px; font-weight: bold; padding: 0; }}"
        )
        parent_layout.addWidget(title)

        args_view = QTextEdit()
        args_view.setReadOnly(True)
        args_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        args_view.setPlainText(args_pretty)
        args_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        args_view.setStyleSheet(_MONO_TEXT_STYLE)
        line_count = args_pretty.count("\n") + 1
        args_view.setMinimumHeight(min(max(line_count * 16 + 12, 32), 200))
        parent_layout.addWidget(args_view)

    def _build_result_section(self, parent_layout: QVBoxLayout) -> None:
        """原结果：content 原文代码块 + 格式解析区（仅 result 是 JSON 时）。"""
        if self._tool_result_message is None:
            return
        content_text = _content_to_text(self._tool_result_message.content)

        title = QLabel("原结果")
        title.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_TERTIARY};"
            f" font-size: 11px; font-weight: bold; padding: 0; }}"
        )
        parent_layout.addWidget(title)

        result_view = QTextEdit()
        result_view.setReadOnly(True)
        result_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        result_view.setPlainText(content_text)
        result_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        result_view.setStyleSheet(_MONO_TEXT_STYLE)
        line_count = content_text.count("\n") + 1
        result_view.setMinimumHeight(min(max(line_count * 16 + 12, 32), 200))
        parent_layout.addWidget(result_view)

        # 格式解析区（仅当 content 是 JSON dict）
        parsed = _safe_parse_json(content_text)
        if isinstance(parsed, dict) and parsed:
            self._build_format_section(parent_layout, parsed)

    def _build_format_section(self, parent_layout: QVBoxLayout, parsed: dict) -> None:
        """格式解析：5 关键字段表（语义着色）+ 其余 JSON 折叠。"""
        title = QLabel("格式解析")
        title.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_TERTIARY};"
            f" font-size: 11px; font-weight: bold; padding: 0; }}"
        )
        parent_layout.addWidget(title)

        # 关键字段表
        key_fields = self._extract_key_fields(parsed)
        if key_fields:
            fields_frame = QFrame()
            fields_frame.setStyleSheet(
                f"QFrame {{ background: {tokens.BG_PANEL};"
                f" border-radius: {tokens.RADIUS_SM}px; }}"
            )
            fl = QVBoxLayout(fields_frame)
            fl.setContentsMargins(8, 4, 8, 4)
            fl.setSpacing(2)
            for field in self._KEY_FIELDS:
                if field not in key_fields:
                    continue
                val_str = key_fields[field]
                label_text = self._format_field_label(field, val_str)
                field_label = QLabel(label_text)
                field_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                color = self._field_color(field, val_str)
                field_label.setStyleSheet(
                    f"QLabel {{ color: {color};"
                    f" font-family: {tokens.FONT_MONO}; font-size: {tokens.FONT_SMALL}px;"
                    f" padding: 2px 6px; }}"
                )
                field_label.setWordWrap(True)
                fl.addWidget(field_label)
            parent_layout.addWidget(fields_frame)

    @classmethod
    def _extract_key_fields(cls, parsed: dict) -> dict:
        """D41a：从 parsed JSON 提取 5 关键字段。

        返回 {field: value_str} 字典，只含实际存在的字段。value_str 截断到 200 字符。
        """
        if not isinstance(parsed, dict) or not parsed:
            return {}
        result = {}
        for field in cls._KEY_FIELDS:
            if field in parsed:
                val = parsed[field]
                if isinstance(val, bool):
                    val_str = "true" if val else "false"
                elif val is None:
                    val_str = "null"
                else:
                    val_str = str(val)
                if len(val_str) > 200:
                    val_str = val_str[:197] + "..."
                result[field] = val_str
        return result

    def _format_field_label(self, field: str, val_str: str) -> str:
        """格式化关键字段标签文本。"""
        label = self._KEY_FIELD_LABEL.get(field, field)
        return f"{label}: {val_str}"

    def _field_color(self, field: str, val_str: str) -> str:
        """D41a 语义着色：
        - success true→绿 / false→红
        - error→红
        - exit_code 0→绿 / 非0→红
        - stderr 非空→WARNING 黄
        """
        if field == "success":
            return tokens.SUCCESS_TEXT if val_str == "true" else tokens.DANGER_TEXT
        if field == "error":
            return tokens.DANGER_TEXT if val_str else tokens.TEXT_SECONDARY
        if field == "exit_code":
            try:
                code = int(val_str)
                return tokens.SUCCESS_TEXT if code == 0 else tokens.DANGER_TEXT
            except (ValueError, TypeError):
                return tokens.TEXT_SECONDARY
        if field == "stderr":
            return tokens.WARNING_TEXT if val_str else tokens.TEXT_SECONDARY
        return tokens.TEXT_SECONDARY

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def toggle(self) -> None:
        """折叠/展开三部分内容。"""
        self._expanded = not self._expanded
        if self._expand_content is not None:
            self._expand_content.setVisible(self._expanded)
        if self._toggle_btn is not None:
            arrow = "▼" if self._expanded else "▶"
            self._toggle_btn.setText(f"{arrow} {self.tool_name}")

    def update_status(self, status: str) -> None:
        """更新工具调用状态（重建 UI 保持展开状态）。"""
        self._status = status
        self._rebuild_ui()

    def attach_tool_result(self, result_message: Message) -> None:
        """D41：附加 tool_result（重建展开内容含三部分）+ 更新状态为 completed。"""
        self._tool_result_message = result_message
        if result_message:
            self._status = TOOL_STATUS_COMPLETED
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """重建整个 UI（保持 _expanded 状态）。"""
        layout = self.layout()
        if layout is None:
            return
        # 清空现有子 widget（deleteLater 安全）
        while layout.count() > 0:
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._row_widget = None
        self._toggle_btn = None
        self._expand_content = None
        # 重新构建（_build_ui 会读取当前 _expanded 状态）
        # 复用 layout（不重新创建 QVBoxLayout）
        layout.setContentsMargins(12, 2, 12, 2)
        layout.setSpacing(0)
        self._build_summary_row(layout)
        self._build_expand_content(layout)


class _SystemBlock(_TimelineBlock):
    """system / system_reminder / summary 块（D44 + D45）。

    - system：灰色背景 + "系统" 标签 + 默认折叠
    - system_reminder：浅黄背景 + "系统提醒" 标签 + 默认折叠
    - summary：浅蓝背景 + "L2 摘要" 标签 + 默认折叠
    """

    # 样式映射（role → bg_color + label_text）
    # bg=None 时用 tokens.BG_PANEL；语义底色一律引用 tokens 常量（Anti-Cheat 禁硬编码）
    _STYLE_MAP = {
        "system": {"bg": None, "label": "系统"},
        "system_reminder": {"bg": "WARNING_WASH", "label": "系统提醒"},
        "summary": {"bg": "INFO_WASH", "label": "L2 摘要"},
    }

    def __init__(self, message: Message, parent=None):
        self._message = message
        self._expanded: bool = False
        self._view: QTextEdit | None = None
        self._toggle_btn: QPushButton | None = None
        self._label_text: str = "系统"
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        role = self._message.role
        content_text = _content_to_text(self._message.content)
        style = self._STYLE_MAP.get(role, self._STYLE_MAP["system"])
        # bg=None 用 BG_PANEL；否则按字符串名查 tokens 常量（Anti-Cheat 禁硬编码 hex）
        bg_ref = style["bg"]
        bg_color = tokens.BG_PANEL if bg_ref is None else getattr(tokens, bg_ref, tokens.BG_PANEL)
        self._label_text = style["label"]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 4, 12, 4)
        outer.setSpacing(2)

        frame = QFrame()
        frame.setObjectName("systemBlock")
        frame.setStyleSheet(
            f"QFrame {{ background: {bg_color}; border: none;"
            f" border-radius: {tokens.RADIUS_SM}px; }}"
        )
        flayout = QVBoxLayout(frame)
        flayout.setContentsMargins(8, 4, 8, 4)
        flayout.setSpacing(2)

        # 标题行（可点击折叠）
        header = QHBoxLayout()
        header.setSpacing(4)
        toggle_btn = QPushButton(f"▶ {self._label_text}")
        toggle_btn.setFlat(True)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {tokens.TEXT_SECONDARY};"
            f" padding: 0; border: none; background: transparent;"
            f" font-size: {tokens.FONT_SMALL}px; }}"
        )
        toggle_btn.clicked.connect(self.toggle)
        header.addWidget(toggle_btn)
        header.addStretch()
        flayout.addLayout(header)

        # 内容（默认折叠）
        view = QTextEdit()
        view.setReadOnly(True)
        view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        view.setStyleSheet(
            f"QTextEdit {{ background: transparent; color: {tokens.TEXT_SECONDARY};"
            f" border: none; padding: 0; font-size: {tokens.FONT_SMALL}px; }}"
        )
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        view.setPlainText(content_text)
        view.setVisible(False)
        flayout.addWidget(view)

        outer.addWidget(frame)
        self._view = view
        self._toggle_btn = toggle_btn

    def toggle(self) -> None:
        """折叠/展开 system 内容。"""
        self._expanded = not self._expanded
        if self._view is not None:
            self._view.setVisible(self._expanded)
        if self._toggle_btn is not None:
            self._toggle_btn.setText(
                f"▼ {self._label_text}" if self._expanded else f"▶ {self._label_text}"
            )

    @property
    def is_collapsible(self) -> bool:
        """T08：system 块可折叠（sticky 候选）。"""
        return True

    @property
    def title(self) -> str:
        """T08：sticky 标题 = 标签文本（'系统' / '系统提醒' / 'L2 摘要'）。"""
        return self._label_text

    def collapse(self) -> None:
        """T08：折叠（sticky 标题点击时调用）。"""
        if self._expanded:
            self.toggle()


class _MessageTimeline(QFrame):
    """消息时间线容器（D38 + D45）。

    管理按 seq 顺序追加各块类，追踪流式块状态。
    对外提供 append_block / append_user_block / append_assistant_blocks /
    append_tool_result / append_system_block / 流式方法 / clear 等接口。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageTimeline")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 8, 0, 8)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        # 流式状态
        self._streaming_assistant_block: _AssistantTextBlock | None = None
        self._streaming_thinking_block: _ThinkingBlock | None = None
        self._streaming_model: str = ""
        # tool_call_id → _ToolCallBlock 映射（用于 tool_result 附加）
        self._tool_call_blocks: dict[str, _ToolCallBlock] = {}

    # ------------------------------------------------------------------
    # 块追加接口
    # ------------------------------------------------------------------

    def append_block(self, block: _TimelineBlock) -> None:
        """追加块到时间线末尾（stretch 之前）。"""
        self._layout.insertWidget(self._layout.count() - 1, block)

    def append_user_block(self, message: Message) -> _UserBubble:
        """追加 user/steer/queue 消息块。"""
        block = _UserBubble(message)
        self.append_block(block)
        return block

    def append_assistant_blocks(self, message: Message) -> list:
        """追加 assistant 消息（可能产生多个块：thinking + tool_calls + text）。"""
        blocks: list = []
        # thinking 块（D38：独立块）
        if message.thinking:
            tb = _ThinkingBlock(content=message.thinking)
            self.append_block(tb)
            blocks.append(tb)
        # tool_call 块（D38：每个独立）
        if message.tool_calls:
            for tc in message.tool_calls:
                if not isinstance(tc, dict):
                    continue
                tcb = _ToolCallBlock(tool_call_dict=tc, status=TOOL_STATUS_PENDING)
                self.append_block(tcb)
                tc_id = tc.get("id", "")
                if tc_id:
                    self._tool_call_blocks[tc_id] = tcb
                blocks.append(tcb)
        # 文本块（D38：agent 最终文本独立块）
        text = _content_to_text(message.content)
        if text or not blocks:
            atb = _AssistantTextBlock(content=text)
            if message.model:
                atb.set_model(message.model)
            self.append_block(atb)
            blocks.append(atb)
        return blocks

    def append_tool_result(
        self, message: Message, tool_name: str = "工具结果", status: str = "unknown"
    ) -> _ToolCallBlock:
        """追加 tool_result（附加到匹配的 _ToolCallBlock 或创建独立块）。"""
        tc_id = message.tool_call_id or ""
        if tc_id and tc_id in self._tool_call_blocks:
            block = self._tool_call_blocks[tc_id]
            block.attach_tool_result(message)
            return block
        # 未找到匹配的 tool_call，创建独立块
        block = _ToolCallBlock(
            tool_call_dict={"id": tc_id, "function": {"name": tool_name, "arguments": "{}"}},
            status=status,
            tool_result_message=message,
        )
        self.append_block(block)
        return block

    def append_system_block(self, message: Message) -> _SystemBlock:
        """追加 system/system_reminder/summary 块。"""
        block = _SystemBlock(message)
        self.append_block(block)
        return block

    # ------------------------------------------------------------------
    # 流式接口
    # ------------------------------------------------------------------

    def start_streaming_assistant(self) -> _AssistantTextBlock:
        """开始流式 assistant 块（若无则创建）。"""
        if self._streaming_assistant_block is None:
            block = _AssistantTextBlock(content="")
            block.start_streaming()
            if self._streaming_model:
                block.set_model(self._streaming_model)
            self.append_block(block)
            self._streaming_assistant_block = block
        return self._streaming_assistant_block

    def append_text_delta(self, delta: str) -> None:
        """追加 streaming_text_delta 到流式 assistant 块。"""
        if self._streaming_assistant_block is not None:
            self._streaming_assistant_block.append_text_delta(delta)

    def append_thinking_delta(self, delta: str) -> None:
        """追加 streaming_thinking_delta 到流式 thinking 块（D38：独立块）。"""
        if self._streaming_thinking_block is None:
            self._streaming_thinking_block = _ThinkingBlock(content="")
            self._streaming_thinking_block._is_streaming = True
            self.append_block(self._streaming_thinking_block)
        self._streaming_thinking_block.append_delta(delta)

    def finalize_streaming_assistant(self, full_text: str, model: str = "") -> None:
        """流结束，finalize streaming assistant 块。"""
        if self._streaming_assistant_block is not None:
            self._streaming_assistant_block.finalize_text(full_text)
            if model:
                self._streaming_assistant_block.set_model(model)
            self._streaming_assistant_block = None

    def finalize_streaming_thinking(self, full_thinking: str) -> None:
        """流结束，finalize streaming thinking 块。"""
        if self._streaming_thinking_block is not None:
            self._streaming_thinking_block.finalize(full_thinking)
            self._streaming_thinking_block = None

    def finalize_streaming_on_interrupt(self, model: str = "") -> None:
        """D43: 流式中断 → streaming bubble 直接转正 + thinking 独立块 + "已中断"小字。"""
        # finalize streaming assistant block as interrupted
        if self._streaming_assistant_block is not None:
            self._streaming_assistant_block.finalize_as_interrupted()
            if model:
                self._streaming_assistant_block.set_model(model)
            self._streaming_assistant_block = None
        # finalize streaming thinking block as independent
        if self._streaming_thinking_block is not None:
            self._streaming_thinking_block.finalize_as_interrupted()
            self._streaming_thinking_block = None

    def set_streaming_model(self, model: str) -> None:
        """设置流式块的模型名。"""
        self._streaming_model = model or ""
        if self._streaming_assistant_block is not None:
            self._streaming_assistant_block.set_model(model)

    def has_streaming_assistant(self) -> bool:
        """是否有活跃的流式 assistant 块。"""
        return self._streaming_assistant_block is not None

    def update_tool_call_status(self, tool_call_status: dict) -> None:
        """更新所有 tool_call 块的状态（轮询时同步）。"""
        for tc_id, info in tool_call_status.items():
            block = self._tool_call_blocks.get(tc_id)
            if block is None:
                continue
            status = info.get("status", TOOL_STATUS_PENDING) if isinstance(info, dict) else info
            block.update_status(status)

    def clear(self) -> None:
        """清空时间线。"""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._streaming_assistant_block = None
        self._streaming_thinking_block = None
        self._tool_call_blocks.clear()

    def scroll_to_bottom(self, scroll_area: QScrollArea) -> None:
        """滚动到底部。"""
        sb = scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # T08 sticky 标题栏支持（D42/D42a）
    # ------------------------------------------------------------------

    def iter_blocks(self):
        """按顺序迭代时间线中所有块（不含 stretch）。"""
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, _TimelineBlock):
                yield widget

    def iter_collapsible_blocks(self):
        """按顺序迭代所有可折叠块（sticky 候选）。"""
        for block in self.iter_blocks():
            if block.is_collapsible:
                yield block

    def find_top_visible_collapsible(self, viewport_top: int, viewport_bottom: int):
        """D42：找到当前可见区域最顶部的可折叠块。

        viewport_top / viewport_bottom 是 _messages_container 坐标系下的可见区间。
        返回该块（_TimelineBlock）或 None（无可折叠块在可见区）。
        """
        for block in self.iter_collapsible_blocks():
            top = block.geometry().top()
            bottom = block.geometry().bottom()
            # 块底部超过 viewport_top → 该块有部分在可见区
            if bottom >= viewport_top:
                # 块顶部超过 viewport_bottom → 整块在可见区下方，跳过找下一个
                if top > viewport_bottom:
                    continue
                return block
        return None


# ============================================================================


class _ChatWorker(QThread):
    """后台线程跑 SessionRunner.run()（经 SessionFacade 封装，spec D13）。

    线程内创建 EventStore（写）+ LLMPoolGateway + ToolRegistry + HttpClientToolExecutor
    + SessionFacade。通过 Signal 通知主线程，禁止 worker 直接触发 widget（v6-02 §3.1）。

    中断：主线程调 interrupt()，通过 call_soon_threadsafe 在 worker loop 内 set interrupt_event。
    steer：主线程调 steer(text)，通过 call_soon_threadsafe 在 worker loop 内调 facade.steer。
    """

    finished_run = Signal(object)  # RunOutcome
    error = Signal(str)
    session_started = Signal(str)  # session_id

    def __init__(
        self,
        session_id: str,
        user_message: str,
        db_path: str,
        base_url: str,
        config: dict | None = None,
        model_override: str = "",
        template_skills: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._session_id = session_id
        self._user_message = user_message
        self._db_path = db_path
        self._base_url = base_url
        self._config = config or {}
        # T06 spec D10：前端模型选择器切换后下条消息生效
        self._model_override = model_override or ""
        # chat-panel-v2 T04: 模板 skills[] 注入（decisions D2）
        self._template_skills: list[str] | None = template_skills
        self._loop: asyncio.AbstractEventLoop | None = None
        self._interrupt_event: asyncio.Event | None = None
        # T05: facade 引用（worker 线程内创建，主线程通过 steer() 调用）
        self._facade: SessionFacade | None = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._interrupt_event = asyncio.Event()
        try:
            outcome = self._loop.run_until_complete(self._run_async())
            self.finished_run.emit(outcome)
        except Exception as e:
            logger.exception("ChatWorker failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _run_async(self):
        # 在 worker 线程内创建所有资源（SQLite connection thread-local）
        store = EventStore(db_path=self._db_path)
        store.init()
        try:
            # 构建 deps（facade 内部负责 create_session + append user message + runner.run）
            gateway = LLMPoolGateway(base_url=self._base_url)
            registry = ToolRegistry(base_url=self._base_url)
            try:
                registry.refresh()
            except Exception as e:
                logger.warning("ToolRegistry refresh failed (will run without tools): %s", e)
            # T02 修复（spec D13）：注入 L0ArtifactStore + Compactor
            # - L0: >8KB 工具结果落盘 data/client/artifacts/，模型只看 preview+path
            # - L2: 413 ContextOverflow 时 reactive_compact_retry，无 compactor 则直接失败
            l0_store = L0ArtifactStore()
            compactor = Compactor()  # 默认 max_context_tokens=32000, cheap_tier_model="" (gateway 默认)
            executor = HttpClientToolExecutor(
                registry=registry, base_url=self._base_url, l0_store=l0_store,
            )
            # T28（spec D8.1）：注入 BuiltinToolExecutor
            # - 10 个内置基本工具（file_read/write/edit/grep/glob/ls/delete + ask_user + web_search/fetch）
            # - 本地执行不经 HTTP，路径安全 4 重校验
            # - ask_callback 暂不注入（AskUserTool fail-closed，未注入时返回 error_variant）
            builtin_executor = BuiltinToolExecutor(base_url=self._base_url)
            try:
                registry.register_builtin_tools(builtin_executor)
            except Exception as e:
                logger.warning("register_builtin_tools failed (fail-open): %s", e)
            deps = RunnerDeps(
                llm_gateway=gateway,
                event_store=store,
                tool_executor=executor,
                tool_registry=registry,
                interrupt_event=self._interrupt_event,
                compactor=compactor,
                builtin_executor=builtin_executor,
            )
            config = RunnerConfig(
                session_id=self._session_id,
                model=self._config.get("model", ""),
                max_iterations=self._config.get("max_iterations", 50),
            )
            # T05: 用 SessionFacade 替代直接调 SessionRunner（spec D13）
            self._facade = SessionFacade(deps=deps, config=config)
            self.session_started.emit(self._session_id)
            # T06 spec D10：传 model_override 让前端模型选择器切换后下条消息生效
            # chat-panel-v2 T04: 传 template_skills 让模板 skills 注入 system prompt
            return await self._facade.start(
                self._session_id,
                self._user_message,
                model_override=self._model_override,
                template_skills=self._template_skills,
            )
        finally:
            store.close()
            self._facade = None  # 清理引用

    def interrupt(self) -> None:
        """主线程调用，触发中断。"""
        if self._loop and self._interrupt_event:
            self._loop.call_soon_threadsafe(self._interrupt_event.set)

    def steer(self, text: str) -> None:
        """主线程调用，注入引导消息（spec D13 steer UI）。

        通过 call_soon_threadsafe 在 worker loop 内调 facade.steer，
        runner 当前工具批次结束后下一轮读到。
        """
        if not text or not self._loop or not self._facade:
            return

        async def _do_steer():
            try:
                await self._facade.steer(self._session_id, text)
            except Exception as e:
                logger.warning("ChatWorker.steer failed: %s", e)

        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(_do_steer(), loop=self._loop)
        )


# ============================================================================
# _StartPage — chat-panel-v2 T04 开始页（decisions D1, D11, D12, D20）
# ============================================================================


class _StartPage(QFrame):
    """开始页：模板 tab + 输入框 + 最近 5 对话列表。

    布局（decisions D1）：
    - 顶部：模板 tab（默认空白 + "+" 号新建模板）
    - 中间：输入框（Ctrl+Enter 发送 + Enter 换行，D11）
    - 下方：最近 5 对话列表（标题 + 时间，单击切换，D20）

    信号：
    - send_requested(text, template_skills)：用户点击发送
    - session_selected(session_id)：最近 5 列表中单击某会话
    - manage_templates_requested()：点击 "+" 号新建模板
    """

    send_requested = Signal(str, list, str)  # text, template_skills[], group_name（"" = 未分组）
    session_selected = Signal(str)  # session_id
    manage_templates_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("startPage")
        self.setStyleSheet(f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # --- 顶部标题 + 分组归属选择器（D6: 新建会话时选分组）---
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        title = QLabel("开始新对话")
        title.setStyleSheet(f"font-size: {tokens.FONT_HEADING}px; font-weight: bold;")
        set_text_role(title, "primary")
        top_row.addWidget(title)
        top_row.addStretch()
        group_label = QLabel("分组：")
        set_text_role(group_label, "secondary")
        top_row.addWidget(group_label)
        # chat-panel-v2 T06（D6）：分组归属选择器
        self._group_combo = QComboBox()
        self._group_combo.setMinimumWidth(160)
        self._group_combo.setToolTip(
            "为新建会话选择分组归属（可选已有分组或新建）"
        )
        self._group_combo.setStyleSheet(
            f"QComboBox {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 2px 6px; font-size: 12px; }}"
        )
        # 占位项
        self._group_combo.addItem("（未分组）", "")
        self._group_combo.addItem("（新建分组...）", "__new__")
        top_row.addWidget(self._group_combo)
        layout.addLayout(top_row)

        # --- 模板 tab ---
        self._template_tabs = QTabWidget()
        self._template_tabs.setObjectName("templateTabs")
        # "+" 按钮放在 tab 栏右侧（corner widget）
        add_tab_btn = QPushButton("+")
        add_tab_btn.setFixedSize(24, 24)
        add_tab_btn.setToolTip("新建模板（打开模板管理面板）")
        add_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_tab_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" background: {tokens.BG_PANEL}; font-size: 14px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        add_tab_btn.clicked.connect(self.manage_templates_requested.emit)
        self._template_tabs.setCornerWidget(add_tab_btn)
        layout.addWidget(self._template_tabs)

        # --- 输入框 ---
        self._input_edit = QTextEdit()
        self._input_edit.setPlaceholderText("输入消息... (Ctrl+Enter 发送)")
        self._input_edit.setMinimumHeight(80)
        self._input_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        self._input_edit.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 8px; font-size: {tokens.FONT_BODY}px; }}"
        )
        self._input_edit.keyPressEvent = self._input_key_press
        layout.addWidget(self._input_edit)

        # --- 发送按钮 + 模型选择器（D30：模型选择器在发送按钮左侧）---
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        # chat-panel-v2 T10（D30）：开始页也加模型选择器
        self._model_selector = QComboBox()
        self._model_selector.setObjectName("startPageModelSelector")
        self._model_selector.setToolTip(
            "选择 LLM 模型（切换后下条消息生效，不打断当前 run）\n"
            "留空=用 config 默认 tier 选 model"
        )
        self._model_selector.setMinimumWidth(160)
        self._model_selector.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        self._model_selector.addItem("（默认）", "")
        btn_row.addWidget(self._model_selector)
        self._send_btn = QPushButton("发送")
        set_kind(self._send_btn, "primary")
        self._send_btn.setToolTip("发送消息 (Ctrl+Enter)")
        self._send_btn.clicked.connect(self._on_send)
        btn_row.addWidget(self._send_btn)
        layout.addLayout(btn_row)

        # --- 分隔线 + 最近 5 对话 ---
        recent_label = QLabel("最近对话")
        set_text_role(recent_label, "secondary")
        layout.addWidget(recent_label)

        self._recent_list = QListWidget()
        self._recent_list.setStyleSheet(
            f"QListWidget {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM}; }}"
            f"QListWidget::item {{ padding: 8px; border-bottom: 1px solid {tokens.BORDER}; }}"
            f"QListWidget::item:selected {{ background: {tokens.ACCENT_WASH}; }}"
            f"QListWidget::item:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        self._recent_list.itemClicked.connect(self._on_recent_clicked)
        layout.addWidget(self._recent_list)

        # 加载模板 tab
        self._templates: list = []  # list[Template]
        self.refresh_templates()

    # ------------------------------------------------------------------
    # 模板 tab 管理
    # ------------------------------------------------------------------

    def refresh_templates(self) -> None:
        """从 data/chat_templates.json 重新加载模板到 tab。"""
        # 缓存当前 tab 文本（按 template_id，存于 widget property）
        cached_texts: dict[str, str] = {}
        for i in range(self._template_tabs.count()):
            tab_widget = self._template_tabs.widget(i)
            if isinstance(tab_widget, QTextEdit):
                tpl_id = tab_widget.property("template_id") or ""
                cached_texts[tpl_id] = tab_widget.toPlainText()

        # 清空现有 tab
        self._template_tabs.clear()

        # 加载模板
        try:
            self._templates = template_load_all()
        except Exception as e:
            logger.warning("_StartPage.refresh_templates: load_all failed: %s", e)
            self._templates = []

        if not self._templates:
            # 兜底：至少有一个空白 tab
            self._add_template_tab("blank", "空白", "", [])

        for tpl in self._templates:
            # 恢复缓存的文本（如有），否则用模板预设 prompt
            text = cached_texts.get(tpl.id, tpl.prompt)
            self._add_template_tab(tpl.id, tpl.name, text, tpl.skills)

        # 默认选中第一个 tab
        if self._template_tabs.count() > 0:
            self._template_tabs.setCurrentIndex(0)

    def _add_template_tab(
        self, template_id: str, name: str, text: str, skills: list[str],
    ) -> None:
        """添加一个模板 tab，内部含 QTextEdit（独立缓存文本）。"""
        edit = QTextEdit()
        edit.setPlaceholderText(f"输入消息... ({name} 模板)")
        edit.setMinimumHeight(80)
        edit.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 8px; font-size: {tokens.FONT_BODY}px; }}"
        )
        edit.setPlainText(text)
        edit.keyPressEvent = self._input_key_press
        # template_id 存于 widget property（PySide6 QTabWidget 无 setTabData）
        edit.setProperty("template_id", template_id)
        idx = self._template_tabs.addTab(edit, name or template_id)
        self._template_tabs.setTabToolTip(
            idx,
            f"模板: {name}\nID: {template_id}\nSkills: {', '.join(skills) if skills else '(无)'}",
        )

    # ------------------------------------------------------------------
    # 输入框 + 发送
    # ------------------------------------------------------------------

    def _input_key_press(self, event) -> None:
        """Ctrl+Enter 发送，Enter 换行（D11）。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._on_send()
            return
        QTextEdit.keyPressEvent(self._current_input_edit(), event)

    def _current_input_edit(self) -> QTextEdit:
        """获取当前激活 tab 的 QTextEdit（或独立输入框）。"""
        # 优先取 tab 内的 input
        if self._template_tabs.count() > 0:
            widget = self._template_tabs.currentWidget()
            if isinstance(widget, QTextEdit):
                return widget
        return self._input_edit

    def _on_send(self) -> None:
        """发送按钮：取当前 tab 文本 + 模板 skills + 分组 → emit send_requested。"""
        edit = self._current_input_edit()
        text = edit.toPlainText().strip()
        if not text:
            return
        # 取当前 tab 对应模板的 skills（template_id 存于 widget property）
        skills: list[str] = []
        idx = self._template_tabs.currentIndex()
        if idx >= 0:
            tab_widget = self._template_tabs.widget(idx)
            tpl_id = (tab_widget.property("template_id") if tab_widget else "") or ""
            for tpl in self._templates:
                if tpl.id == tpl_id:
                    skills = list(tpl.skills)
                    break
        # chat-panel-v2 T06（D6）：取当前分组选择
        group_name = self._current_group_selection()
        self.send_requested.emit(text, skills, group_name)

    def _current_group_selection(self) -> str:
        """返回当前选中的分组名（"" = 未分组）。

        若选了"（新建分组...）" → 弹 QInputDialog 输入新分组名，
        取消则回退到"未分组"。选中后并把新分组名加入 combo 列表。
        """
        idx = self._group_combo.currentIndex()
        data = self._group_combo.itemData(idx) or ""
        if data == "__new__":
            # 弹新建分组对话框
            from PySide6.QtWidgets import QInputDialog
            new_name, ok = QInputDialog.getText(
                self, "新建分组", "分组名："
            )
            if not ok or not new_name.strip():
                # 取消：回退到未分组
                self._group_combo.setCurrentIndex(0)
                return ""
            new_group = new_name.strip()
            # 添加到 combo（防止重复）
            if self._group_combo.findData(new_group) < 0:
                self._group_combo.addItem(new_group, new_group)
                self._group_combo.setCurrentIndex(self._group_combo.count() - 1)
            else:
                self._group_combo.setCurrentIndex(self._group_combo.findData(new_group))
            return new_group
        # 已有分组或未分组：直接返回 data
        return data or ""

    def refresh_groups(self, groups: list[str]) -> None:
        """刷新分组选择器的已有分组清单（保留未分组 + 新建项）。"""
        current_data = self._group_combo.currentData() or ""
        # 清掉除"未分组"和"新建"之外的项
        while self._group_combo.count() > 2:
            self._group_combo.removeItem(2)
        for g in groups:
            if g and self._group_combo.findData(g) < 0:
                self._group_combo.addItem(g, g)
        # 还原选择
        idx = self._group_combo.findData(current_data)
        self._group_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def clear_input(self) -> None:
        """清空当前 tab 的输入框。"""
        edit = self._current_input_edit()
        edit.clear()

    def focus_input(self) -> None:
        """聚焦当前 tab 的输入框。"""
        self._current_input_edit().setFocus()

    # ------------------------------------------------------------------
    # 最近 5 对话
    # ------------------------------------------------------------------

    def refresh_recent_sessions(self, sessions: list) -> None:
        """刷新最近 5 对话列表。

        Args:
            sessions: list of Session 对象或 dict（含 id/title/updated_at）
        """
        self._recent_list.clear()
        for sess in sessions[:5]:
            sid = sess.get("id", "") if isinstance(sess, dict) else getattr(sess, "id", "")
            title = sess.get("title", "") if isinstance(sess, dict) else getattr(sess, "title", "")
            updated_at = (
                sess.get("updated_at", 0.0)
                if isinstance(sess, dict)
                else getattr(sess, "updated_at", 0.0)
            )
            if not sid:
                continue
            display = title or sid[:8]
            # 时间格式化
            import time as _time
            time_str = _time.strftime("%m-%d %H:%M", _time.localtime(updated_at)) if updated_at else ""
            item_text = f"{display}  ·  {time_str}" if time_str else display
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            self._recent_list.addItem(item)

    def _on_recent_clicked(self, item: QListWidgetItem) -> None:
        sid = item.data(Qt.ItemDataRole.UserRole)
        if sid:
            self.session_selected.emit(sid)


# ============================================================================
# TemplateManagerPanel — 模板管理面板（chat-panel-v2 T05）
# ============================================================================


class _TemplateManagerPanel(QFrame):
    """模板管理面板（decisions D5 / D7 / D14 / D28 / D29）。

    布局：
    - 左侧：模板 QListWidget（顶部 "+" 新建按钮，每项右侧三点菜单：删除/复制）
    - 右侧编辑区：
        - name QLineEdit
        - prompt QTextEdit
        - skills 复选框列表（QScrollArea + QCheckBox×N，全部 task_type，D7）
    - 失焦自动保存（focusOutEvent / editingFinished 信号触发 template_update）

    信号：
    - templates_changed()：模板列表变更（增删改后 emit，ChatPanel 可刷新开始页模板 tab）

    数据层复用 template_store CRUD（load_all / save_all / create / update / delete）。
    """

    templates_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("templateManagerPanel")
        self.setStyleSheet(f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}")

        # 当前编辑中的模板 id（None = 未选中）
        self._current_template_id: str | None = None
        # 内部加载 flag（避免初始化加载时触发 focusOut 保存）
        self._loading = False
        # 全部 task_type（D7：从 _index.md 解析）
        self._all_task_types: list[str] = []
        try:
            self._all_task_types = list_all_task_types()
        except Exception as e:
            logger.warning("_TemplateManagerPanel: list_all_task_types failed: %s", e)
            self._all_task_types = []

        self._build_ui()
        self.refresh_template_list()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # === 左侧：模板列表 + 顶部新建按钮 ===
        left = QFrame()
        left.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_PANEL}; border: none; border-right: 1px solid {tokens.BORDER}; }}"
        )
        left.setMinimumWidth(160)
        left.setMaximumWidth(400)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(4)
        title_label = QLabel("模板列表")
        set_text_role(title_label, "primary")
        header_row.addWidget(title_label, 1)

        new_btn = QPushButton("+")
        new_btn.setFixedSize(24, 24)
        new_btn.setToolTip("新建模板")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" background: {tokens.BG_PANEL}; font-size: 14px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        new_btn.clicked.connect(self._on_new_template)
        header_row.addWidget(new_btn)
        left_layout.addLayout(header_row)

        self._template_list = QListWidget()
        self._template_list.setStyleSheet(
            f"QListWidget {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_PRIMARY}; border: none; }}"
            f"QListWidget::item {{ padding: 6px; border-radius: {tokens.RADIUS_SM}; }}"
            f"QListWidget::item:selected {{ background: {tokens.ACCENT_WASH}; }}"
            f"QListWidget::item:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        self._template_list.itemClicked.connect(self._on_template_selected)
        # 三点菜单：右键 + 一个三点按钮（菜单按钮）
        self._template_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._template_list.customContextMenuRequested.connect(self._on_context_menu)
        left_layout.addWidget(self._template_list, 1)

        splitter.addWidget(left)
        splitter.setCollapsible(0, True)
        splitter.setStretchFactor(0, 1)

        # === 右侧：编辑区 ===
        right = QFrame()
        right.setStyleSheet(f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(8)

        # name 输入
        name_label = QLabel("模板名")
        set_text_role(name_label, "secondary")
        right_layout.addWidget(name_label)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("模板名（如：空白 / 问股）")
        self._name_edit.setStyleSheet(
            f"QLineEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 6px; font-size: {tokens.FONT_BODY}px; }}"
        )
        # editingFinished 信号：失焦或 Enter 触发自动保存
        self._name_edit.editingFinished.connect(self._auto_save_current)
        right_layout.addWidget(self._name_edit)

        # prompt 输入
        prompt_label = QLabel("Prompt 预设")
        set_text_role(prompt_label, "secondary")
        right_layout.addWidget(prompt_label)
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setPlaceholderText("预设 prompt（用户在开始页发送时可覆盖）")
        self._prompt_edit.setMinimumHeight(80)
        self._prompt_edit.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 6px; font-size: {tokens.FONT_BODY}px; }}"
        )
        right_layout.addWidget(self._prompt_edit, 1)

        # skills 复选框列表（QScrollArea + QCheckBox×N）
        skills_label = QLabel("Skills（task_type）")
        set_text_role(skills_label, "secondary")
        right_layout.addWidget(skills_label)

        self._skills_scroll = QScrollArea()
        self._skills_scroll.setWidgetResizable(True)
        self._skills_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._skills_scroll.setStyleSheet(
            f"QScrollArea {{ background: {tokens.BG_BASE}; border: 1px solid {tokens.BORDER};"
            f" border-radius: {tokens.RADIUS_SM}; }}"
        )
        self._skills_scroll.setMinimumHeight(120)
        self._skills_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )

        skills_container = QWidget()
        skills_layout = QVBoxLayout(skills_container)
        skills_layout.setContentsMargins(8, 8, 8, 8)
        skills_layout.setSpacing(2)

        # 渲染全部 task_type 复选框（D7）
        self._skill_checkboxes: dict[str, QCheckBox] = {}
        for task_type in self._all_task_types:
            cb = QCheckBox(task_type)
            cb.setStyleSheet(
                f"QCheckBox {{ color: {tokens.TEXT_PRIMARY}; padding: 2px; font-family: {tokens.FONT_MONO}; font-size: 12px; }}"
                f"QCheckBox::indicator {{ border: 1px solid {tokens.BORDER}; }}"
            )
            # stateChanged 信号触发自动保存
            cb.stateChanged.connect(lambda *_: self._auto_save_current())
            self._skill_checkboxes[task_type] = cb
            skills_layout.addWidget(cb)
        skills_layout.addStretch()

        self._skills_scroll.setWidget(skills_container)
        right_layout.addWidget(self._skills_scroll, 2)

        splitter.addWidget(right)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([240, 560])

        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # 模板列表刷新
    # ------------------------------------------------------------------

    def refresh_template_list(self) -> None:
        """重新加载模板列表（保留当前选中）。"""
        # 记住当前选中
        current_id = self._current_template_id

        self._template_list.clear()
        try:
            templates = template_load_all()
        except Exception as e:
            logger.warning("_TemplateManagerPanel.refresh_template_list: load_all failed: %s", e)
            templates = []

        for tpl in templates:
            display = tpl.name or tpl.id
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, tpl.id)
            self._template_list.addItem(item)
            if tpl.id == current_id:
                self._template_list.setCurrentItem(item)

        # 如果无选中且列表非空，默认选第一个
        if self._template_list.currentItem() is None and self._template_list.count() > 0:
            self._template_list.setCurrentRow(0)
            current_id = self._template_list.item(0).data(Qt.ItemDataRole.UserRole)

        # 加载选中模板到编辑区
        if current_id:
            self._load_template_to_editor(current_id)
        else:
            self._clear_editor()

    # ------------------------------------------------------------------
    # 选中模板 → 加载到编辑区
    # ------------------------------------------------------------------

    def _on_template_selected(self, item: QListWidgetItem) -> None:
        """点击列表项 → 加载到编辑区。"""
        tpl_id = item.data(Qt.ItemDataRole.UserRole)
        if not tpl_id:
            return
        # 切换前先保存当前编辑中的模板（D14 自动保存）
        self._auto_save_current()
        self._load_template_to_editor(tpl_id)

    def _load_template_to_editor(self, template_id: str) -> None:
        """从文件加载指定模板到编辑区。"""
        # 防止 loading 期间 focusOut 触发保存
        self._loading = True
        try:
            from client.core.agent import template_get
            tpl = template_get(template_id)
            if tpl is None:
                # 模板已被删除，清空编辑区
                self._clear_editor()
                return
            self._current_template_id = template_id
            self._name_edit.setText(tpl.name)
            self._prompt_edit.setPlainText(tpl.prompt)
            # 同步复选框
            for task_type, cb in self._skill_checkboxes.items():
                cb.setChecked(task_type in (tpl.skills or []))
        finally:
            self._loading = False

    def _clear_editor(self) -> None:
        """清空编辑区（无选中模板时）。"""
        self._loading = True
        try:
            self._current_template_id = None
            self._name_edit.clear()
            self._prompt_edit.clear()
            for cb in self._skill_checkboxes.values():
                cb.setChecked(False)
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # 自动保存（D14 失焦自动保存，无"是否保存"对话框）
    # ------------------------------------------------------------------

    def _auto_save_current(self) -> None:
        """失焦或编辑完成时自动保存当前模板（D14）。

        - _loading=True 时跳过（避免初始化加载触发）
        - _current_template_id=None 时跳过（无选中）
        """
        if self._loading:
            return
        if not self._current_template_id:
            return

        name = self._name_edit.text().strip()
        prompt = self._prompt_edit.toPlainText()
        skills = [
            task_type
            for task_type, cb in self._skill_checkboxes.items()
            if cb.isChecked()
        ]

        try:
            updated = template_update(
                self._current_template_id,
                name=name or "（未命名）",
                prompt=prompt,
                skills=skills,
            )
            if updated is not None:
                # 更新列表项显示名
                for i in range(self._template_list.count()):
                    item = self._template_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == self._current_template_id:
                        item.setText(updated.name or updated.id)
                        break
                self.templates_changed.emit()
        except Exception as e:
            logger.warning("_auto_save_current: update failed: %s", e)

    # ------------------------------------------------------------------
    # 新建模板（D14 顶部 + 按钮）
    # ------------------------------------------------------------------

    def _on_new_template(self) -> None:
        """点 + 新建模板 → 创建空白模板 + 选中新模板。"""
        try:
            new_tpl = template_create(name="新模板", prompt="", skills=[])
            self.refresh_template_list()
            # 选中新创建的项
            for i in range(self._template_list.count()):
                item = self._template_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == new_tpl.id:
                    self._template_list.setCurrentItem(item)
                    self._load_template_to_editor(new_tpl.id)
                    break
            self.templates_changed.emit()
            # 聚焦 name 输入框方便编辑
            self._name_edit.setFocus()
            self._name_edit.selectAll()
        except Exception as e:
            logger.warning("_on_new_template: create failed: %s", e)

    # ------------------------------------------------------------------
    # 三点菜单：删除 / 复制（D14）
    # ------------------------------------------------------------------

    def _on_context_menu(self, pos) -> None:
        """右键模板列表项 → 弹出三点菜单。"""
        item = self._template_list.itemAt(pos)
        if item is None:
            return
        tpl_id = item.data(Qt.ItemDataRole.UserRole)
        if not tpl_id:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_PRIMARY}; border: 1px solid {tokens.BORDER}; }}"
            f"QMenu::item {{ padding: 6px 16px; }}"
            f"QMenu::item:selected {{ background: {tokens.BG_HOVER}; }}"
        )

        # 复制
        copy_action = menu.addAction("复制")
        # 删除
        delete_action = menu.addAction("删除")

        action = menu.exec(self._template_list.mapToGlobal(pos))
        if action == copy_action:
            self._duplicate_template(tpl_id)
        elif action == delete_action:
            self._delete_template(tpl_id, item)

    def _delete_template(self, template_id: str, item: QListWidgetItem) -> None:
        """删除模板（D18 确认对话框）。"""
        name = item.text()
        reply = QMessageBox.question(
            self,
            "删除模板",
            f"确定删除模板 \"{name}\" 吗？\n此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = template_delete(template_id)
            if ok:
                # 若删除的是当前编辑中的模板，清空编辑区
                if self._current_template_id == template_id:
                    self._current_template_id = None
                    self._clear_editor()
                self.refresh_template_list()
                self.templates_changed.emit()
        except Exception as e:
            logger.warning("_delete_template: delete failed: %s", e)

    def _duplicate_template(self, template_id: str) -> None:
        """复制模板（新建一个同名 + 副本后缀）。"""
        try:
            from client.core.agent import template_get
            src = template_get(template_id)
            if src is None:
                return
            new_tpl = template_create(
                name=f"{src.name} 副本",
                prompt=src.prompt,
                skills=list(src.skills),
            )
            self.refresh_template_list()
            # 选中新副本
            for i in range(self._template_list.count()):
                item = self._template_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == new_tpl.id:
                    self._template_list.setCurrentItem(item)
                    self._load_template_to_editor(new_tpl.id)
                    break
            self.templates_changed.emit()
        except Exception as e:
            logger.warning("_duplicate_template: duplicate failed: %s", e)


# ============================================================================
# ChatPanel — 对话面板
# ============================================================================


class ChatPanel(PanelBase):
    """v6-lite 对话面板（T07）。

    功能：
    - 左侧会话列表（新建/切换）
    - 右侧消息时间线（user/assistant/tool 气泡 + 工具调用折叠卡）
    - 底部输入框 + 发送/中断按钮
    - 顶栏显示会话状态/cost/iterations
    - QTimer 200ms 轮询 EventStore 渲染中间状态
    - 中断即停（worker.interrupt → interrupt_event.set → runner 下一轮终止）
    """

    PANEL_META = PanelMeta(
        id="chat",
        title="对话",
        icon="message-square",
        order=10,
        category="main",
        requires_backend=True,
        requires_agent=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_url = SERVER_URL
        self._db_path = DEFAULT_DB_PATH
        self._read_store: EventStore | None = None  # 主线程只读 store
        self._worker: _ChatWorker | None = None
        self._current_session_id: str | None = None
        self._last_rendered_seq: int = 0  # 增量渲染锚点
        self._last_tool_call_ids: set[str] = set()  # 已渲染的 tool_call id
        self._pending_user_preview: str | None = None
        # T05: steer 预览去重（steer 可多次调用，用 set 记录已预渲染的文本）
        self._pending_steer_previews: set[str] = set()
        # T06 spec D10：前端模型选择器状态
        self._available_models: list[dict] = []  # [{model, tier, provider}, ...]
        self._current_model: str = ""  # 当前选中的 model（空=用 config 默认）

        # chat-panel-v2 T06（D15）：侧边栏搜索框状态
        self._session_filter: str = ""  # 实时按标题过滤（空=不过滤）
        # T06（D19）：当前正在行内重命名的会话 id（避免重渲染覆盖 LineEdit）
        self._renaming_sid: str | None = None
        self._rename_editor: QLineEdit | None = None
        # T06（D6）：开始页新建会话时选择的分组归属，session_started 后写入
        self._pending_group_for_new_session: str | None = None

        # T03: 打字机三档（config 默认 + UI 实时切换）
        self._typewriter_mode: str = self._load_typewriter_default()
        # T03: thinking 显示 toggle（默认关闭；T07 后 thinking 块默认折叠，此开关展开所有 thinking 块）
        self._show_thinking: bool = False
        # T07: 流式渲染状态由 _message_timeline 管理（_streaming_assistant_block / _streaming_thinking_block）
        # 保留 _last_streaming_seq 作为 streaming 事件增量锚点
        self._last_streaming_seq: int = 0  # streaming 事件增量锚点

        # 轮询定时器（worker 运行时启用）
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_TYPEWRITER_POLL_INTERVAL.get(self._typewriter_mode, 200))
        self._poll_timer.timeout.connect(self._poll_session_state)

        # 会话列表刷新定时器（低频）
        self._session_list_timer = QTimer(self)
        self._session_list_timer.setInterval(2000)
        self._session_list_timer.timeout.connect(self._refresh_session_list)

        # T08: sticky 标题栏状态（D42/D42a）
        self._sticky_bar: QFrame | None = None
        self._sticky_btn: QPushButton | None = None
        self._sticky_current_block: _TimelineBlock | None = None

        # T09: 对话控制三模式状态（D34/D36/D36a）
        self._mode_group: QButtonGroup | None = None
        self._mode_send_btn: QPushButton | None = None     # 发送 tab
        self._mode_queue_btn: QPushButton | None = None    # 队列 tab
        self._mode_steer_btn: QPushButton | None = None    # 引导 tab
        self._queue_bar: QFrame | None = None              # 排队区容器
        self._queue_label: QLabel | None = None            # 排队消息文本
        self._queue_edit_btn: QPushButton | None = None    # 编辑/取消排队按钮
        self._queued_text: str = ""                        # 排队消息内容（in-memory）
        self._queued_msg_id: str | None = None            # T09 持久化排队消息的 EventStore id
        self._is_running_state: bool = False              # worker 是否运行中

        self._build_ui()
        self._init_read_store()

    @staticmethod
    def _load_typewriter_default() -> str:
        """从 config.toml 读打字机默认档位（spec D08）。"""
        try:
            from lib.config_reader import load_config
            cfg = load_config()
            runner_cfg = cfg.get("runner", {}) if isinstance(cfg, dict) else {}
            mode = runner_cfg.get("typewriter_mode", "fast")
            if mode in _TYPEWRITER_MODES:
                return mode
        except Exception:
            pass
        return "fast"

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 左侧会话列表 + 右侧主区用 QSplitter 分隔（spec D4 弹性布局，去 setFixedWidth）
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setObjectName("chatSplitter")

        # 左侧会话列表
        left = QFrame()
        left.setStyleSheet(f"QFrame {{ background: {tokens.BG_PANEL}; border: none; border-right: 1px solid {tokens.BORDER}; }}")
        # 弹性：最小宽度防拖太窄，最大宽度防拖太宽（替代 setFixedWidth(200)）
        left.setMinimumWidth(120)
        left.setMaximumWidth(600)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        new_btn = QPushButton("✚  新对话")
        set_kind(new_btn, "primary")
        new_btn.setToolTip("开始新对话（点发送才创建会话，可换行）")
        new_btn.clicked.connect(self._on_new_session)
        left_layout.addWidget(new_btn)

        template_btn = QPushButton("▦  模板管理")
        set_kind(template_btn, "ghost")
        template_btn.setToolTip("管理对话模板（新建/编辑/删除/复制）")
        template_btn.clicked.connect(self._on_manage_templates)
        left_layout.addWidget(template_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {tokens.BORDER};")
        # 用 min/max 而非 setFixedHeight（spec Anti-Cheat 禁止 setFixedHeight）
        sep.setMinimumHeight(1)
        sep.setMaximumHeight(1)
        left_layout.addWidget(sep)

        # chat-panel-v2 T06（D15）：搜索框（实时按标题过滤）
        self._session_filter_input = QLineEdit()
        self._session_filter_input.setPlaceholderText("🔍 搜索对话标题...")
        self._session_filter_input.setClearButtonEnabled(True)
        self._session_filter_input.setStyleSheet(
            f"QLineEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 4px 8px; font-size: 12px; }}"
            f"QLineEdit:focus {{ border-color: {tokens.ACCENT}; }}"
        )
        self._session_filter_input.textChanged.connect(self._on_session_filter_changed)
        left_layout.addWidget(self._session_filter_input)

        # chat-panel-v2 T06（D4/D6）：会话树（置顶区 + 分组列表 + 未分组）
        self._session_tree = QTreeWidget()
        self._session_tree.setHeaderHidden(True)
        self._session_tree.setUniformRowHeights(True)
        self._session_tree.setAnimated(True)
        self._session_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._session_tree.setStyleSheet(
            f"QTreeWidget {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_PRIMARY};"
            f" border: none; }}"
            f"QTreeWidget::item {{ padding: 4px 2px; border-radius: {tokens.RADIUS_SM}; }}"
            f"QTreeWidget::item:selected {{ background: {tokens.ACCENT_WASH}; color: {tokens.TEXT_PRIMARY}; }}"
            f"QTreeWidget::item:hover {{ background: {tokens.BG_HOVER}; }}"
            f"QTreeWidget::branch {{ background: transparent; }}"
        )
        # 列定义（单列树，靠 setData 存 sid / group_name / pinned 等元信息）
        self._session_tree.setColumnCount(1)
        self._session_tree.itemClicked.connect(self._on_session_tree_clicked)
        self._session_tree.customContextMenuRequested.connect(self._on_session_context_menu)
        self._session_tree.itemDoubleClicked.connect(self._on_session_double_clicked)
        left_layout.addWidget(self._session_tree, 1)

        # 旧字段保留为 None（避免破坏外部引用，但实际上后续不再使用）
        self._session_list = None  # type: ignore[assignment]

        splitter.addWidget(left)
        splitter.setCollapsible(0, True)  # 左侧可折叠（spec D4）
        splitter.setStretchFactor(0, 1)

        # 右侧主区
        right = QFrame()
        right.setStyleSheet(f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # D26: 启动恢复 banner（默认隐藏，reconcile 恢复会话后显示）
        self._recovery_banner = QFrame()
        self._recovery_banner.setObjectName("recoveryBanner")
        self._recovery_banner.setVisible(False)
        self._recovery_banner.setMinimumHeight(0)
        self._recovery_banner.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        self._recovery_banner.setStyleSheet(
            f"QFrame {{ background: {tokens.ACCENT_WASH};"
            f" border: none; border-bottom: 1px solid {tokens.BORDER}; }}"
        )
        banner_layout = QHBoxLayout(self._recovery_banner)
        banner_layout.setContentsMargins(12, 6, 12, 6)
        banner_layout.setSpacing(8)
        self._recovery_label = QLabel("")
        set_text_role(self._recovery_label, "primary")
        banner_layout.addWidget(self._recovery_label, 1)
        self._recovery_close_btn = QPushButton("×")
        self._recovery_close_btn.setFixedSize(20, 20)
        self._recovery_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recovery_close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: inherit; font-size: 14px; }"
            "QPushButton:hover { background: rgba(0,0,0,0.1); border-radius: 2px; }"
        )
        self._recovery_close_btn.clicked.connect(self._hide_recovery_banner)
        banner_layout.addWidget(self._recovery_close_btn)
        right_layout.addWidget(self._recovery_banner)

        # 顶栏（setMinimumHeight + sizePolicy Fixed，去 setFixedHeight，spec D4 弹性）
        # chat-panel-v2 T10（D47）：last_updated + title(居中) + status + cost/token + 打字机 + refresh
        top_bar = QFrame()
        top_bar.setMinimumHeight(40)
        top_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_bar.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_PANEL}; border: none; border-bottom: 1px solid {tokens.BORDER}; }}"
        )
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 4, 12, 4)
        top_layout.setSpacing(8)

        # 1. last_updated 时间戳（最左，HH:MM，每分钟刷新，text_role=tertiary）
        self._last_updated_label = QLabel("")
        self._last_updated_label.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_TERTIARY}; font-size: {tokens.FONT_SMALL}px; }}"
        )
        top_layout.addWidget(self._last_updated_label)

        # 2. title_label 居中（用 addStretch 两侧推到中间，elideMode 截断超 32 字符）
        top_layout.addStretch()
        self._title_label = QLabel("")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_PRIMARY}; font-size: {tokens.FONT_BODY}px; font-weight: 500; }}"
        )
        # D47: 超 32 字符省略
        self._title_label.setMaximumWidth(400)
        # ElideRight 需在 resizeEvent 中手动 elideText（QLabel 不直接支持 elideMode 属性）
        # 简化：用 setWordWrap(False) + setMaximumWidth + 文本截断（超长加 …）
        self._title_label.setWordWrap(False)
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()

        # 3. status_label 状态文本（保留 _SESSION_STATUS_TEXT 映射 + 语义着色）
        self._status_label = QLabel("空闲")
        set_text_role(self._status_label, "secondary")
        top_layout.addWidget(self._status_label)

        # 4. cost + token 显示（D47：费用${cost:.4f} · token {value}{单位}，
        #    cost=0 两者都不显示，cost<0.01 只显示 token）
        self._cost_label = QLabel("")
        set_text_role(self._cost_label, "secondary")
        top_layout.addWidget(self._cost_label)

        # 5. 打字机效果：toggle 按钮（文字改为「打字机效果：{模式}」）
        self._typewriter_btn = QPushButton(
            f"打字机效果：{_TYPEWRITER_MODE_TEXT.get(self._typewriter_mode, '快速')}"
        )
        set_kind(self._typewriter_btn, "ghost")
        self._typewriter_btn.setToolTip(
            f"打字机效果（当前：{_TYPEWRITER_MODE_TEXT.get(self._typewriter_mode, '快速')}）\n"
            "点击切换：关闭→快速→正常→关闭"
        )
        self._typewriter_btn.clicked.connect(self._on_toggle_typewriter)
        top_layout.addWidget(self._typewriter_btn)

        # 6. refresh_btn 重载按钮
        self._refresh_btn = icon_button("refresh", "重新加载当前对话历史", parent=top_bar)
        self._refresh_btn.clicked.connect(self._reload_current_history)
        top_layout.addWidget(self._refresh_btn)

        # chat-panel-v2 T10（D47）：保留 _iter_label / _thinking_toggle_btn /
        # _copy_last_btn 作为隐藏 None 对象引用（向后兼容旧测试），不加入 layout
        # _model_selector 在输入区实例化（D30 移到发送按钮左侧）
        self._iter_label = None  # type: ignore[assignment]
        self._thinking_toggle_btn = None  # type: ignore[assignment]
        self._copy_last_btn = None  # type: ignore[assignment]
        # _model_selector 在下方输入区实例化（D30 移到发送按钮左侧）

        # last_updated QTimer 60s 刷新
        self._last_updated_timer = QTimer(self)
        self._last_updated_timer.setInterval(60000)  # 60s
        self._last_updated_timer.timeout.connect(self._refresh_last_updated)
        self._last_updated_timer.start()
        # 首次刷新
        self._refresh_last_updated()

        right_layout.addWidget(top_bar)

        # chat-panel-v2 T04: QStackedWidget 切换消息时间线 / 开始页 / 模板管理面板
        self._main_stack = QStackedWidget()
        right_layout.addWidget(self._main_stack, 1)

        # --- 消息时间线视图（stack index 0）---
        # 用 QFrame 容器包裹 scroll + empty_hint + input_bar，便于整体显隐
        self._messages_view = QFrame()
        self._messages_view.setStyleSheet(f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}")
        mv_layout = QVBoxLayout(self._messages_view)
        mv_layout.setContentsMargins(0, 0, 0, 0)
        mv_layout.setSpacing(0)

        # 消息时间线
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"QScrollArea {{ background: {tokens.BG_BASE}; border: none; }}")

        self._messages_container = QWidget()
        # chat-panel-v2 T07: 用 _MessageTimeline 替代裸 QVBoxLayout（5 块类 + 流式中断）
        self._message_timeline = _MessageTimeline(self._messages_container)
        tl_outer = QVBoxLayout(self._messages_container)
        tl_outer.setContentsMargins(0, 0, 0, 0)
        tl_outer.addWidget(self._message_timeline)

        self._scroll.setWidget(self._messages_container)
        mv_layout.addWidget(self._scroll, 1)

        # T08: sticky 标题栏（D42/D42a）—— 顶栏下方，显示当前可见可折叠块的标题
        # 插入到 _scroll 之上（mv_layout 中 _scroll 之前）。但 mv_layout 顺序是 _scroll 已加，
        # 这里在 _scroll 之前 insertWidget(0, sticky_bar)
        self._sticky_bar = QFrame()
        self._sticky_bar.setObjectName("stickyBar")
        self._sticky_bar.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_PANEL};"
            f" border: none; border-bottom: 1px solid {tokens.BORDER}; }}"
        )
        sticky_layout = QHBoxLayout(self._sticky_bar)
        sticky_layout.setContentsMargins(12, 4, 12, 4)
        sticky_layout.setSpacing(4)
        sticky_btn = QPushButton("")
        sticky_btn.setFlat(True)
        sticky_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sticky_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {tokens.TEXT_SECONDARY};"
            f" padding: 0; border: none; background: transparent;"
            f" font-size: {tokens.FONT_SMALL}px; }}"
            f"QPushButton:hover {{ color: {tokens.TEXT_PRIMARY}; }}"
        )
        sticky_btn.setToolTip("点击折叠当前块")
        sticky_btn.clicked.connect(self._on_sticky_clicked)
        sticky_layout.addWidget(sticky_btn)
        sticky_layout.addStretch()
        # 默认隐藏（无可折叠块时高度 0）
        self._sticky_bar.setVisible(False)
        self._sticky_bar.setMaximumHeight(0)
        self._sticky_btn = sticky_btn
        mv_layout.insertWidget(0, self._sticky_bar)

        # T08: 滚动监听 → 更新 sticky 标题
        sb = self._scroll.verticalScrollBar()
        sb.valueChanged.connect(self._update_sticky_bar)

        # 空状态提示
        self._empty_hint = QLabel("输入消息开始对话\n\n支持工具调用，需审批的工具会弹窗确认")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(self._empty_hint, "secondary")
        self._empty_hint.setVisible(True)
        mv_layout.addWidget(self._empty_hint)

        # 输入区
        input_bar = QFrame()
        input_bar.setObjectName("chatInputBar")
        input_bar.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_PANEL}; border: none; border-top: 1px solid {tokens.BORDER}; }}"
        )
        input_outer = QVBoxLayout(input_bar)
        input_outer.setContentsMargins(0, 0, 0, 0)
        input_outer.setSpacing(0)

        # T09: 3 tab widget（发送/队列/引导）+ 排队区
        self._build_mode_tabs_and_queue(input_outer)

        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(12, 8, 12, 8)
        input_layout.setSpacing(8)
        input_outer.addLayout(input_layout)

        self._input_edit = QTextEdit()
        self._input_edit.setPlaceholderText("输入消息... (Ctrl+Enter 发送)")
        # 弹性：setMinimumHeight + sizePolicy Fixed（去 setFixedHeight，spec D4 / ui_design_system 硬规则）
        self._input_edit.setMinimumHeight(60)
        self._input_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input_edit.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 4px; font-size: {tokens.FONT_BODY}px; }}"
        )
        self._input_edit.keyPressEvent = self._input_key_press
        input_layout.addWidget(self._input_edit, 1)

        self._interrupt_btn = QPushButton("停止")
        set_kind(self._interrupt_btn, "danger")
        self._interrupt_btn.setObjectName("interruptButton")
        self._interrupt_btn.setEnabled(False)
        self._interrupt_btn.setToolTip("停止当前会话（下一轮检查到即终止）")
        self._interrupt_btn.clicked.connect(self._on_interrupt)
        input_layout.addWidget(self._interrupt_btn)

        # chat-panel-v2 T10（D30）：模型选择器移到发送按钮左侧（顶栏已移除）
        # 仍用 objectName=modelSelector（_load_models / _populate_model_selector / 测试用 findChild 定位）
        self._model_selector = QComboBox()
        self._model_selector.setObjectName("modelSelector")
        self._model_selector.setToolTip(
            "选择 LLM 模型（切换后下条消息生效，不打断当前 run）\n"
            "留空=用 config 默认 tier 选 model"
        )
        self._model_selector.setMinimumWidth(160)
        self._model_selector.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._model_selector.addItem("（默认）", "")
        self._model_selector.currentIndexChanged.connect(self._on_model_changed)
        input_layout.addWidget(self._model_selector)

        self._send_btn = QPushButton("发送")
        set_kind(self._send_btn, "primary")
        self._send_btn.setToolTip("发送消息 (Ctrl+Enter)")
        self._send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self._send_btn)

        mv_layout.addWidget(input_bar)

        # --- 开始页（stack index 1，D1/D11/D12/D20）---
        self._start_page = _StartPage()
        self._start_page.send_requested.connect(self._on_start_page_send)
        self._start_page.session_selected.connect(self._switch_session)
        self._start_page.manage_templates_requested.connect(self._on_manage_templates)

        # --- 模板管理面板（stack index 2，T05 实现）---
        self._template_panel = _TemplateManagerPanel()
        # 模板变更 → 通知开始页刷新模板 tab（D5 联动）
        self._template_panel.templates_changed.connect(self._start_page.refresh_templates)

        # 加入 stack（index 0=消息时间线, 1=开始页, 2=模板管理面板）
        self._main_stack.addWidget(self._messages_view)        # index 0
        self._main_stack.addWidget(self._start_page)            # index 1
        self._main_stack.addWidget(self._template_panel)        # index 2

        # D12：首次启动且无活跃会话 → 显示开始页
        self._main_stack.setCurrentWidget(self._start_page)

        splitter.addWidget(right)
        splitter.setCollapsible(1, False)  # 右侧主区不可折叠（折叠了就没意义）
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([200, 800])  # 初始尺寸（spec D4 示例）

        outer.addWidget(splitter)

    def _reload_current_history(self) -> None:
        """重新从 EventStore 加载当前会话，避免刷新后仍停留在旧的增量锚点。"""
        if not self._current_session_id:
            self._refresh_session_list()
            return
        self._last_rendered_seq = 0
        self._last_tool_call_ids = set()
        self._pending_user_preview = None
        self._pending_steer_previews.clear()  # T05: 清理 steer 预览去重集合
        self._last_streaming_seq = 0  # T07: 流式状态由 _message_timeline 管理
        self._clear_messages()
        self._poll_session_state()

    def _copy_last_reply(self) -> None:
        """复制当前会话最后一条 Agent 文本回复。"""
        if not self._current_session_id or self._read_store is None:
            return
        messages = self._safe_read(self._read_store.load_messages(self._current_session_id)) or []
        for message in reversed(messages):
            if message.role != "assistant":
                continue
            text = _content_to_text(message.content).strip()
            if text:
                QApplication.clipboard().setText(text)
                return

    def _input_key_press(self, event) -> None:
        """Ctrl+Enter 发送消息。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._on_send()
            return
        # 调用原始 keyPressEvent
        QTextEdit.keyPressEvent(self._input_edit, event)

    # ------------------------------------------------------------------
    # PanelBase 钩子
    # ------------------------------------------------------------------

    def on_show(self) -> None:
        self._session_list_timer.start()
        self._refresh_session_list()
        # T06 spec D10：面板显示时拉模型清单（懒加载，避免启动时阻塞）
        if not self._available_models:
            self._load_models()

    def _load_models(self) -> None:
        """T06 spec D10：从 /llm/pool/models 拉取可用 model 清单填充 QComboBox。

        失败时静默（保留"（默认）"项，不阻断 chat 功能）。
        在 worker 线程跑 HTTP 请求，避免阻塞 UI。
        """
        import threading

        def _fetch():
            try:
                import httpx
                url = f"{self._base_url}/llm/pool/models"
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                if not data.get("initialized", False):
                    return
                grouped = data.get("grouped_by_tier", {})
                default_model = data.get("default_model", "")
                # 用信号回主线程更新 UI（避免跨线程操作 widget）
                models: list[dict] = []
                for _tier, items in grouped.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and item.get("model"):
                            models.append({
                                "model": item["model"],
                                "tier": item.get("tier", ""),
                                "provider": item.get("provider", ""),
                            })
                # 用 QTimer.singleShot(0, ...) 在主线程更新 UI
                QTimer.singleShot(0, lambda: self._populate_model_selector(models, default_model))
            except Exception as e:
                logger.warning("_load_models failed: %s", e)

        threading.Thread(target=_fetch, daemon=True).start()

    def _populate_model_selector(self, models: list[dict], default_model: str) -> None:
        """主线程填充模型选择器（_load_models 在线程中拿到数据后调此方法）。"""
        self._available_models = models
        # 临时断开信号，避免 addItem 触发 _on_model_changed
        self._model_selector.blockSignals(True)
        try:
            self._model_selector.clear()
            self._model_selector.addItem("（默认）", "")
            for m in models:
                label = m["model"]
                # 简短标注 tier/provider（避免过长）
                tier = m.get("tier", "")
                if tier:
                    label = f"{label}  [tier {tier}]"
                self._model_selector.addItem(label, m["model"])
            # 选中默认 model
            if default_model:
                idx = self._model_selector.findData(default_model)
                if idx >= 0:
                    self._model_selector.setCurrentIndex(idx)
                    self._current_model = default_model
        finally:
            self._model_selector.blockSignals(False)

    def _on_model_changed(self, _index: int) -> None:
        """T06 spec D10：用户切换模型时记录，下条消息生效（不打断当前 run）。"""
        self._current_model = self._model_selector.currentData() or ""
        logger.info("Model changed: %s", self._current_model or "(default)")

    def on_hide(self) -> None:
        self._session_list_timer.stop()
        self._poll_timer.stop()

    def on_refresh(self) -> None:
        self._refresh_session_list()
        if self._current_session_id:
            self._poll_session_state()

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._init_read_store()
            self._refresh_session_list()
        else:
            self._poll_timer.stop()
            self._session_list_timer.stop()

    # ------------------------------------------------------------------
    # EventStore（主线程只读）
    # ------------------------------------------------------------------

    def _init_read_store(self) -> None:
        """初始化主线程只读 EventStore，并运行启动恢复（D27）。

        D27: store.init() 后立即调 reconcile(store)，GUI 加载会话列表前先恢复，
        看到的是已恢复状态。banner 提示恢复会话数（D26）。
        """
        try:
            if self._read_store is not None:
                self._read_store.close()
            self._read_store = EventStore(db_path=self._db_path)
            self._read_store.init()
            logger.info("ChatPanel read store initialized: %s", self._db_path)
        except Exception as e:
            logger.warning("Failed to init read store: %s", e)
            self._read_store = None
            return

        # D27: store.init() 后调 reconcile()（启动恢复）
        self._run_reconcile()

    def _run_reconcile(self) -> None:
        """D27: 运行 reconciler，恢复崩溃残留会话，banner 提示恢复数（D26）。

        reconcile 是 async 但内部是 sqlite 本地 I/O（极快），用 new_event_loop
        同步执行。失败不阻塞 GUI 启动。
        """
        if self._read_store is None:
            return
        try:
            from client.core.agent.reconciler import reconcile
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(reconcile(self._read_store))
            finally:
                loop.close()
            recovered_count = len(result.sessions_interrupted)
            if recovered_count > 0:
                self._show_recovery_banner(recovered_count)
                logger.info(
                    "Reconcile recovered %d sessions: %s",
                    recovered_count, result.sessions_interrupted,
                )
        except Exception as e:
            logger.warning("Reconcile failed: %s", e)

        # T09: 恢复持久化的排队消息（重启后 banner 提示"有 N 条排队消息未发送"）
        self._recover_pending_queue()

    def _recover_pending_queue(self) -> None:
        """T09: 启动时检查持久化的排队消息，恢复到 UI + banner 提示。

        - 查找所有 source='queue' 的消息
        - 最新的恢复到 UI（切换到其所属会话 + _show_queue persist=False）
        - 其余的删除（UI 只支持单条排队，多余的视为孤儿）
        - banner 提示数量
        """
        if self._read_store is None:
            return
        try:
            loop = asyncio.new_event_loop()
            try:
                pending = loop.run_until_complete(
                    self._read_store.find_pending_queue_messages()
                )
            finally:
                loop.close()
        except Exception:
            logger.warning("Failed to find pending queue messages", exc_info=True)
            return

        if not pending:
            return

        # 最新的在最后（find_pending_queue_messages 按 created_at 升序）
        latest = pending[-1]
        orphans = pending[:-1]

        # 删除孤儿排队消息（UI 只支持单条）
        for orphan in orphans:
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self._read_store.delete_message_by_id(orphan.id)
                    )
                finally:
                    loop.close()
            except Exception:
                logger.warning("Failed to delete orphan queue message", exc_info=True)

        # 恢复最新的排队消息到 UI
        text = str(latest.content) if latest.content is not None else ""
        self._queued_msg_id = latest.id
        # 切换到排队消息所属的会话
        if latest.session_id:
            self._current_session_id = latest.session_id
        # 显示排队区（不重新持久化，已在库中）
        self._show_queue(text, persist=False)

        # banner 提示
        total = len(pending)
        if total > 0:
            self._show_queue_recovery_banner(total)
            logger.info("Recovered %d pending queue message(s)", total)

    def _show_queue_recovery_banner(self, count: int) -> None:
        """T09: 显示排队消息恢复提示 banner（复用 recovery banner）。"""
        if count <= 0:
            return
        self._recovery_label.setText(
            f"检测到 {count} 条排队消息未发送，已恢复到排队区"
        )
        self._recovery_banner.setVisible(True)

    def _show_recovery_banner(self, count: int) -> None:
        """D26: 显示恢复提示 banner。count <= 0 不显示。"""
        if count <= 0:
            return
        self._recovery_label.setText(
            f"检测到 {count} 个异常退出的会话，已自动恢复"
        )
        self._recovery_banner.setVisible(True)

    def _hide_recovery_banner(self) -> None:
        """D26: 隐藏恢复提示 banner（用户点 × 关闭）。"""
        self._recovery_banner.setVisible(False)

    def interrupt_active_session(self) -> None:
        """D24: closeEvent 调用 — 中断活跃 runner + 更新 session.status=interrupted。

        非阻塞，毫秒级完成（sqlite 本地 I/O 极快，interrupt 通过
        asyncio.Event.set 立即返回，不等待 runner 终止）。
        用 try/except 包裹防 closeEvent 失败。
        """
        # 1. 中断 runner（worker.interrupt 通过 call_soon_threadsafe set asyncio.Event）
        try:
            if self._worker is not None and self._worker.isRunning():
                self._worker.interrupt()
        except Exception:
            logger.warning(
                "interrupt_active_session: worker.interrupt() failed", exc_info=True,
            )

        # 2. 更新 session.status = interrupted（同步执行 sqlite 写）
        try:
            if self._current_session_id and self._read_store is not None:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self._read_store.update_session_status(
                            self._current_session_id, "interrupted",
                        )
                    )
                finally:
                    loop.close()
        except Exception:
            logger.warning(
                "interrupt_active_session: update_session_status failed", exc_info=True,
            )

        # 3. T07 D43: 流式中断 → streaming bubble 直接转正 + thinking 独立块 + "已中断"小字
        try:
            if self._message_timeline is not None:
                self._message_timeline.finalize_streaming_on_interrupt()
                self._last_streaming_seq = 0
        except Exception:
            logger.warning(
                "interrupt_active_session: finalize_streaming_on_interrupt failed",
                exc_info=True,
            )

    def _safe_read(self, coro):
        """在主线程同步执行 async read 方法（sqlite 本地 I/O 极快，阻塞可忽略）。"""
        if self._read_store is None:
            return None
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        except Exception as e:
            logger.debug("read store query failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # 会话列表
    # ------------------------------------------------------------------

    def _refresh_session_list(self) -> None:
        """chat-panel-v2 T06：刷新侧边栏会话树（置顶区 + 分组列表 + 未分组）。

        渲染顺序（D4/D6/D13/D15）：
        1. 置顶区（pinned=True），按 updated_at 倒序
        2. 各分组（group_name 非 NULL，UTF-8 顺序排序组名），组内按 updated_at 倒序
        3. 未分组（group_name 为 NULL/空），按 updated_at 倒序

        搜索过滤（D15）：标题包含 self._session_filter 子串（大小写不敏感）才展示；
        空的分区自动隐藏。

        行内重命名中（self._renaming_sid 非空）：保留该会话项不重新创建，
        由 _on_session_double_clicked / _commit_rename 控制 LineEdit 生命周期。
        """
        if self._read_store is None:
            return
        try:
            sessions = self._safe_read(self._read_store.list_sessions())
        except Exception:
            sessions = None
        if sessions is None:
            return

        # 统一转 dict 形式便于访问
        sessions_dicts: list[dict] = []
        for s in sessions:
            if isinstance(s, dict):
                d = {
                    "id": s.get("id", ""),
                    "title": s.get("title", "") or "",
                    "mode": s.get("mode", "") or "",
                    "status": s.get("status", "") or "",
                    "updated_at": s.get("updated_at", 0.0) or 0.0,
                    "group_name": s.get("group_name"),
                    "pinned": bool(s.get("pinned", False)),
                }
            else:
                d = {
                    "id": getattr(s, "id", ""),
                    "title": getattr(s, "title", "") or "",
                    "mode": getattr(s, "mode", "") or "",
                    "status": getattr(s, "status", "") or "",
                    "updated_at": getattr(s, "updated_at", 0.0) or 0.0,
                    "group_name": getattr(s, "group_name", None),
                    "pinned": bool(getattr(s, "pinned", False)),
                }
            if d["id"]:
                sessions_dicts.append(d)

        # D15：搜索过滤（标题 + sid 前 8 位兜底）
        filter_text = (self._session_filter or "").strip().lower()
        if filter_text:
            filtered = []
            for d in sessions_dicts:
                title_low = (d["title"] or d["id"][:8]).lower()
                if filter_text in title_low:
                    filtered.append(d)
            sessions_dicts = filtered

        # 三分类
        pinned_sessions = sorted(
            [d for d in sessions_dicts if d["pinned"]],
            key=lambda d: d["updated_at"],
            reverse=True,
        )
        ungrouped = sorted(
            [d for d in sessions_dicts if not d["pinned"] and not d["group_name"]],
            key=lambda d: d["updated_at"],
            reverse=True,
        )
        # 分组：group_name → list[sessions]（组内 updated_at 倒序）
        groups_map: dict[str, list[dict]] = {}
        for d in sessions_dicts:
            if d["pinned"] or not d["group_name"]:
                continue
            groups_map.setdefault(d["group_name"], []).append(d)
        for g_list in groups_map.values():
            g_list.sort(key=lambda d: d["updated_at"], reverse=True)

        # 重渲染树
        current_selected = self._current_session_id
        renaming_sid = self._renaming_sid

        # 若正在重命名，先取出 rename editor 的当前文本（避免重渲染丢失输入）
        renaming_text = ""
        if renaming_sid and self._rename_editor is not None:
            try:
                renaming_text = self._rename_editor.text()
            except Exception:
                renaming_text = ""

        self._session_tree.clear()

        def _build_session_item(d: dict) -> QTreeWidgetItem:
            sid = d["id"]
            display = d["title"] or sid[:8]
            status_text = _SESSION_STATUS_TEXT.get(d["status"], d["status"])
            badge = _SESSION_MODE_BADGE.get(d["mode"], "")
            text = (
                f"[{badge}] {display}  [{status_text}]"
                if badge
                else f"{display}  [{status_text}]"
            )
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, sid)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "session")  # 标记为会话项
            item.setData(0, Qt.ItemDataRole.UserRole + 2, d["pinned"])
            item.setData(0, Qt.ItemDataRole.UserRole + 3, d["group_name"])
            return item

        def _select_if_current(item: QTreeWidgetItem, sid: str) -> None:
            if sid == current_selected:
                self._session_tree.setCurrentItem(item)

        # 1. 置顶区
        if pinned_sessions:
            pinned_root = QTreeWidgetItem([f"📌 置顶 ({len(pinned_sessions)})"])
            pinned_root.setData(0, Qt.ItemDataRole.UserRole + 1, "header")
            pinned_root.setForeground(0, Qt.GlobalColor.transparent)  # 仅作分组视觉
            font = pinned_root.font(0)
            font.setBold(True)
            pinned_root.setFont(0, font)
            self._session_tree.addTopLevelItem(pinned_root)
            for d in pinned_sessions:
                child = _build_session_item(d)
                pinned_root.addChild(child)
                _select_if_current(child, d["id"])
            pinned_root.setExpanded(True)

        # 2. 分组列表（UTF-8 顺序排序）
        for group_name in sorted(groups_map.keys()):
            group_sessions = groups_map[group_name]
            group_root = QTreeWidgetItem([f"📁 {group_name} ({len(group_sessions)})"])
            group_root.setData(0, Qt.ItemDataRole.UserRole + 1, "header")
            group_root.setData(0, Qt.ItemDataRole.UserRole, group_name)  # 存分组名
            font = group_root.font(0)
            font.setBold(True)
            group_root.setFont(0, font)
            self._session_tree.addTopLevelItem(group_root)
            for d in group_sessions:
                child = _build_session_item(d)
                group_root.addChild(child)
                _select_if_current(child, d["id"])
            group_root.setExpanded(True)

        # 3. 未分组
        if ungrouped:
            ungrouped_root = QTreeWidgetItem([f"📪 未分组 ({len(ungrouped)})"])
            ungrouped_root.setData(0, Qt.ItemDataRole.UserRole + 1, "header")
            font = ungrouped_root.font(0)
            font.setBold(True)
            ungrouped_root.setFont(0, font)
            self._session_tree.addTopLevelItem(ungrouped_root)
            for d in ungrouped:
                child = _build_session_item(d)
                ungrouped_root.addChild(child)
                _select_if_current(child, d["id"])
            ungrouped_root.setExpanded(True)

        # 4. 重命名中：把 rename editor 重新挂到对应会话项上
        if renaming_sid:
            target_item = self._find_session_item(renaming_sid)
            if target_item is not None:
                self._attach_rename_editor(target_item, renaming_sid, renaming_text)
            else:
                # 该会话被过滤掉了，取消重命名
                self._cancel_rename()

        # chat-panel-v2 T04: 同步刷新开始页最近 5 对话（D8: 按 updated_at 倒序取前 5）
        try:
            sessions_sorted = sorted(
                sessions_dicts,
                key=lambda d: d["updated_at"],
                reverse=True,
            )
            # 还原成 refresh_recent_sessions 期望的对象形式（duck typing）
            self._start_page.refresh_recent_sessions(sessions_sorted)
        except Exception:
            logger.debug("refresh_recent_sessions (start page) failed", exc_info=True)

        # chat-panel-v2 T06（D6）：同步开始页分组选择器清单
        try:
            existing_groups: list[str] = []
            if self._read_store is not None:
                existing_groups = list(
                    self._safe_read(self._read_store.list_distinct_group_names()) or []
                )
            self._start_page.refresh_groups(existing_groups)
        except Exception:
            logger.debug("refresh_groups (start page) failed", exc_info=True)

    # ------------------------------------------------------------------
    # 侧边栏交互
    # ------------------------------------------------------------------

    def _on_session_filter_changed(self, text: str) -> None:
        """D15：搜索框 textChanged → 更新 filter → 实时重渲染。"""
        self._session_filter = text
        self._refresh_session_list()

    def _on_session_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """单击会话项 → 切换会话。点击分组标题无反应。"""
        item_kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if item_kind != "session":
            return
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if not sid:
            return
        # 重命名中点击其他会话：先提交当前重命名
        if self._renaming_sid and self._renaming_sid != sid:
            self._commit_rename()
        self._switch_session(sid)

    def _on_session_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """D19：双击会话项 → 进入行内重命名模式。"""
        item_kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if item_kind != "session":
            return
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if not sid:
            return
        self._begin_rename(item, sid)

    def _on_session_context_menu(self, pos) -> None:
        """D6/D17/D18/D19：会话项右键三点菜单。"""
        item = self._session_tree.itemAt(pos)
        if item is None:
            return
        item_kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if item_kind != "session":
            return
        sid = item.data(0, Qt.ItemDataRole.UserRole)
        if not sid:
            return
        pinned = bool(item.data(0, Qt.ItemDataRole.UserRole + 2))
        group_name = item.data(0, Qt.ItemDataRole.UserRole + 3)

        menu = QMenu(self)
        act_rename = menu.addAction("✏  重命名")
        act_pin = menu.addAction(
            "📌  取消置顶" if pinned else "📌  置顶"
        )
        act_move = menu.addAction("📁  移动到分组...")
        menu.addSeparator()
        act_export = menu.addAction("📤  导出...")
        act_delete = menu.addAction("🗑  删除...")

        action = menu.exec(self._session_tree.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action is act_rename:
            self._begin_rename(item, sid)
        elif action is act_pin:
            self._toggle_pin_session(sid, pinned)
        elif action is act_move:
            self._move_session_to_group(sid, current_group=group_name)
        elif action is act_export:
            self._export_session(sid)
        elif action is act_delete:
            self._delete_session_with_confirm(sid)

    # ------------------------------------------------------------------
    # 行内重命名（D19）
    # ------------------------------------------------------------------

    def _find_session_item(self, sid: str) -> QTreeWidgetItem | None:
        """遍历树找到指定 sid 的会话 QTreeWidgetItem。"""
        it = QTreeWidgetItemIterator(self._session_tree)
        while it.value() is not None:
            item = it.value()
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == "session":
                if item.data(0, Qt.ItemDataRole.UserRole) == sid:
                    return item
            it += 1
        return None

    def _begin_rename(self, item: QTreeWidgetItem, sid: str) -> None:
        """进入行内重命名模式：挂载 QLineEdit 到 item。"""
        # 若已在重命名其他会话，先提交
        if self._renaming_sid and self._renaming_sid != sid:
            self._commit_rename()
        self._renaming_sid = sid
        # 取当前标题作为初始值
        sess = self._safe_read(self._read_store.get_session(sid)) if self._read_store else None
        current_title = ""
        if sess is not None:
            current_title = (
                sess.get("title", "") if isinstance(sess, dict) else getattr(sess, "title", "")
            ) or ""
        self._attach_rename_editor(item, sid, current_title)

    def _attach_rename_editor(
        self, item: QTreeWidgetItem, sid: str, initial_text: str
    ) -> None:
        """把 QLineEdit 挂到 item 上，绑定 Enter/Esc + 失焦提交。"""
        if self._rename_editor is not None:
            try:
                self._rename_editor.deleteLater()
            except Exception:
                pass
        editor = QLineEdit(initial_text)
        editor.setStyleSheet(
            f"QLineEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.ACCENT}; border-radius: {tokens.RADIUS_SM};"
            f" padding: 1px 4px; font-size: 12px; }}"
        )
        editor.selectAll()
        editor.returnPressed.connect(self._commit_rename)
        editor.editingFinished.connect(self._commit_rename)
        # Esc 取消：keyPressEvent override
        editor.installEventFilter(self)
        self._session_tree.setItemWidget(item, 0, editor)
        editor.setFocus()
        self._rename_editor = editor
        self._renaming_sid = sid

    def _commit_rename(self) -> None:
        """提交重命名：取 LineEdit 文本，update_session_title，移除 editor。"""
        if self._renaming_sid is None or self._rename_editor is None:
            self._cancel_rename()
            return
        new_title = self._rename_editor.text().strip()
        sid = self._renaming_sid
        # 移除 editor
        try:
            item = self._find_session_item(sid)
            if item is not None:
                self._session_tree.removeItemWidget(item, 0)
        except Exception:
            pass
        try:
            self._rename_editor.deleteLater()
        except Exception:
            pass
        self._rename_editor = None
        self._renaming_sid = None
        # 写库（空标题不写，保留旧标题）
        if new_title and self._read_store is not None:
            try:
                self._safe_read(self._read_store.update_session_title(sid, new_title))
            except Exception:
                logger.warning("rename session failed: %s", sid, exc_info=True)
        self._refresh_session_list()

    def _cancel_rename(self) -> None:
        """取消重命名：丢弃 editor，不写库。"""
        if self._renaming_sid is None and self._rename_editor is None:
            return
        try:
            item = self._find_session_item(self._renaming_sid) if self._renaming_sid else None
            if item is not None:
                self._session_tree.removeItemWidget(item, 0)
        except Exception:
            pass
        if self._rename_editor is not None:
            try:
                self._rename_editor.deleteLater()
            except Exception:
                pass
        self._rename_editor = None
        self._renaming_sid = None
        self._refresh_session_list()

    def eventFilter(self, obj, event) -> bool:
        """Esc 取消重命名（QLineEdit 默认 Esc 不关闭，需手动接管）。"""
        from PySide6.QtCore import QEvent
        if obj is self._rename_editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._cancel_rename()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 三点菜单动作
    # ------------------------------------------------------------------

    def _toggle_pin_session(self, sid: str, currently_pinned: bool) -> None:
        """置顶/取消置顶。"""
        if self._read_store is None:
            return
        try:
            self._safe_read(
                self._read_store.update_session_pinned(sid, not currently_pinned)
            )
        except Exception:
            logger.warning("toggle pin failed: %s", sid, exc_info=True)
        self._refresh_session_list()

    def _move_session_to_group(self, sid: str, current_group: str | None) -> None:
        """移动到分组对话框：选择已有分组或新建分组，或移到未分组。"""
        if self._read_store is None:
            return
        # 取已有分组名
        existing_groups: list[str] = []
        try:
            existing_groups = list(
                self._safe_read(self._read_store.list_distinct_group_names()) or []
            )
        except Exception:
            existing_groups = []
        # 弹 QInputDialog，可选已有分组 / 新建 / 移到未分组
        items = ["（移到未分组）", "（新建分组...）"] + existing_groups
        chosen, ok = QInputDialog.getItem(
            self,
            "移动到分组",
            f"为会话选择分组（当前：{current_group or '未分组'}）：",
            items,
            0,
            False,
        )
        if not ok:
            return
        if chosen == "（移到未分组）":
            new_group: str | None = None
        elif chosen == "（新建分组...）":
            new_name, ok2 = QInputDialog.getText(
                self, "新建分组", "分组名："
            )
            if not ok2 or not new_name.strip():
                return
            new_group = new_name.strip()
        else:
            new_group = chosen
        try:
            self._safe_read(self._read_store.update_session_group(sid, new_group))
        except Exception:
            logger.warning("move to group failed: %s", sid, exc_info=True)
        self._refresh_session_list()

    def _delete_session_with_confirm(self, sid: str) -> None:
        """D18：删除会话（弹确认对话框，硬删 EventStore）。"""
        if self._read_store is None:
            return
        # 取标题用于确认文案
        title_for_msg = sid[:8]
        try:
            sess = self._safe_read(self._read_store.get_session(sid))
            if sess is not None:
                title_for_msg = (
                    sess.get("title", "") if isinstance(sess, dict) else getattr(sess, "title", "")
                ) or sid[:8]
        except Exception:
            pass
        reply = QMessageBox.question(
            self,
            "删除会话",
            f"确定删除会话 \"{title_for_msg}\" 吗？\n\n此操作会删除该会话的所有消息、工具调用和事件，不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._safe_read(self._read_store.delete_session(sid))
        except Exception:
            logger.warning("delete session failed: %s", sid, exc_info=True)
        # 若删的是当前会话，回到开始页
        if sid == self._current_session_id:
            self._on_new_session()
        else:
            self._refresh_session_list()

    # ------------------------------------------------------------------
    # T11: 导出（D16 + D17）
    # ------------------------------------------------------------------

    def _export_session(self, sid: str) -> None:
        """D16 + D17：导出当前会话（弹 QFileDialog 选路径 + 格式）。

        - Markdown：人类可读，tool_calls/thinking 以引用块呈现（D32）
        - JSON：结构化完整备份（含 messages + tool_calls + events）
        - 两者都导：同时保存 .md 和 .json 两个文件
        """
        if self._read_store is None:
            QMessageBox.warning(self, "导出失败", "存储未初始化")
            return
        # 加载会话数据
        session = self._safe_read(self._read_store.get_session(sid))
        if session is None:
            QMessageBox.warning(self, "导出失败", f"找不到会话 {sid[:8]}")
            return
        # include_invisible=True：导出全部消息（synthetic 等）
        messages = self._safe_read(self._read_store.load_messages(sid, include_invisible=True)) or []
        tool_calls = self._safe_read(self._read_store.load_tool_calls(sid)) or []
        events = self._safe_read(self._read_store.load_events(sid)) or []

        # 取标题作为默认文件名
        title = (
            session.get("title", "") if isinstance(session, dict) else getattr(session, "title", "")
        ) or sid[:8]
        # 文件名安全化：去除非法字符
        safe_title = "".join(c for c in title if c.isalnum() or c in "-_") or "session"

        # 弹 QFileDialog 选路径 + 格式
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出会话",
            safe_title,
            _EXPORT_FILE_FILTER,
        )
        if not file_path:
            return  # 用户取消

        # 根据选择的 filter 决定导出哪些格式
        # filter 字符串：MD="Markdown (*.md)" / JSON="JSON (*.json)" / Both="两者都导 (*.md *.json)"
        # 检测"两者"前缀最稳，避免 filter 名含 .md / .json 干扰
        if selected_filter and (
            selected_filter.startswith("两者") or "两者都导" in selected_filter
        ):
            fmt = _EXPORT_FORMAT_BOTH
        elif selected_filter and selected_filter.startswith("JSON"):
            fmt = _EXPORT_FORMAT_JSON
        else:
            # 默认 MD（含未选 filter 或选 Markdown 时）
            fmt = _EXPORT_FORMAT_MD

        try:
            if fmt in (_EXPORT_FORMAT_MD, _EXPORT_FORMAT_BOTH):
                md_path = file_path
                # 文件名没有 .md 后缀时自动补（both 模式也走同一路径）
                if not md_path.lower().endswith(".md"):
                    md_path = md_path + ".md"
                md_content = _format_session_as_markdown(session, messages, tool_calls)
                Path(md_path).write_text(md_content, encoding="utf-8")
            if fmt in (_EXPORT_FORMAT_JSON, _EXPORT_FORMAT_BOTH):
                json_path = file_path
                if fmt == _EXPORT_FORMAT_BOTH:
                    # both 模式：json 用同名 .json
                    base = file_path
                    if base.lower().endswith(".md"):
                        base = base[:-3]
                    json_path = base + ".json"
                elif not json_path.lower().endswith(".json"):
                    json_path = json_path + ".json"
                json_content = _format_session_as_json(session, messages, tool_calls, events)
                Path(json_path).write_text(json_content, encoding="utf-8")
        except Exception as e:
            logger.warning("export session failed: %s", sid, exc_info=True)
            QMessageBox.warning(self, "导出失败", f"写入文件失败：{e}")
            return

        # 成功提示
        QMessageBox.information(
            self,
            "导出成功",
            f"会话 \"{title}\" 已导出。\n\n格式：{fmt}\n路径：{file_path}",
        )

    def _on_session_selected(self, item: QListWidgetItem) -> None:
        """旧 QListWidget 选中槽（向后兼容，新代码用 _on_session_tree_clicked）。"""
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid:
            return
        self._switch_session(sid)

    def _switch_session(self, session_id: str) -> None:
        """切换到指定会话，重新渲染消息时间线。"""
        self._current_session_id = session_id
        self._last_rendered_seq = 0
        self._last_tool_call_ids = set()
        # optimistic preview 只属于发送它的会话，切换会话时必须丢弃，
        # 否则新会话中恰好出现同文案时会被错误去重。
        self._pending_user_preview = None
        self._pending_steer_previews.clear()  # T05: 清理 steer 预览去重集合
        self._clear_messages()
        # chat-panel-v2 T04: 切到消息时间线视图（D21）
        self._show_messages_view()
        self._poll_session_state()
        # T10: 刷新 last_updated（title 在 _poll_session_state 内刷新）
        self._refresh_last_updated()

    def _on_new_session(self) -> None:
        """chat-panel-v2 T04: 点"新对话" → 切到开始页（D12）。

        改造：不再立即创建 session_id。改为切到开始页 + 清空状态，
        session_id 在开始页发送时由 _on_start_page_send 创建。
        """
        self._current_session_id = None
        self._last_rendered_seq = 0
        self._last_tool_call_ids = set()
        self._pending_user_preview = None
        self._pending_steer_previews.clear()
        self._clear_messages()
        self._update_status("idle", 0)
        # T10: 清空 title + last_updated（开始页无活跃会话）
        self._update_title("")
        self._refresh_last_updated()
        self._show_start_page()

    def _show_start_page(self) -> None:
        """D12: 切到开始页视图，并刷新模板 tab + 最近 5 对话。"""
        self._start_page.refresh_templates()
        if self._read_store is not None:
            try:
                sessions = self._safe_read(self._read_store.list_sessions()) or []
                # D8: 全部按 updated_at 倒序取前 5
                sessions_sorted = sorted(
                    sessions,
                    key=lambda s: s.get("updated_at", 0.0) if isinstance(s, dict) else getattr(s, "updated_at", 0.0),
                    reverse=True,
                )
                self._start_page.refresh_recent_sessions(sessions_sorted)
            except Exception:
                logger.debug("refresh_recent_sessions failed", exc_info=True)
        self._main_stack.setCurrentWidget(self._start_page)
        self._start_page.focus_input()

    def _show_messages_view(self) -> None:
        """切到消息时间线视图（开始页发送后/选择会话后）。"""
        self._main_stack.setCurrentWidget(self._messages_view)

    def _on_manage_templates(self) -> None:
        """D5: 点"模板管理" → 切到模板管理面板（T05 实现）。"""
        self._template_panel.refresh_template_list()
        self._main_stack.setCurrentWidget(self._template_panel)

    def _on_start_page_send(self, text: str, template_skills: list, group_name: str = "") -> None:
        """D1/D11/D6: 开始页发送 → 创建新 session + 启动 worker（带 template_skills + group）。"""
        if self._worker is not None and self._worker.isRunning():
            return

        # 创建新 session_id（发送时才创建，D12）
        new_id = f"sess-{_uuid.uuid4().hex[:12]}"
        self._current_session_id = new_id
        self._last_rendered_seq = 0
        self._last_tool_call_ids = set()
        self._pending_user_preview = None
        self._pending_steer_previews.clear()
        # chat-panel-v2 T06（D6）：缓存新建会话的分组归属，等 session_started 后写入
        self._pending_group_for_new_session = (group_name or "").strip() or None
        self._clear_messages()
        self._empty_hint.setVisible(False)

        # 切到消息时间线视图
        self._show_messages_view()

        # 立即在 UI 显示用户消息（不等 worker）
        self._append_user_message_preview(text)

        # T07: 流式渲染状态由 _message_timeline 管理，仅重置 seq 锚点
        self._last_streaming_seq = 0

        # 启动 worker（带 template_skills）
        self._worker = _ChatWorker(
            session_id=new_id,
            user_message=text,
            db_path=self._db_path,
            base_url=self._base_url,
            config={
                "max_iterations": 50,
                # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost（缓存比例不明）
            },
            model_override=self._current_model,
            template_skills=template_skills if template_skills else None,
            parent=self,
        )
        self._worker.finished_run.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.session_started.connect(self._on_session_started)

        self._send_btn.setEnabled(False)
        self._interrupt_btn.setEnabled(True)
        self._update_status("streaming", 0)

        self._worker.start()
        self._poll_timer.start()

    # ------------------------------------------------------------------
    # 发送 / 中断
    # ------------------------------------------------------------------

    def _on_send(self) -> None:
        """T09: 单一发送按钮，按当前 mode dispatch（D34）。

        - 空闲态：mode=send（开始新 run）
        - 运行态：按当前 tab 触发 send/queue/steer
        """
        text = self._input_edit.toPlainText().strip()
        if not text:
            return

        mode = self._get_current_mode()
        if mode == MODE_SEND:
            # 空闲态发送 / 运行态"发送" tab（中断 + start 新 run）
            if self._worker is not None and self._worker.isRunning():
                # 运行态下选了"发送" tab → 中断 + start 新 run
                self._send_in_send_mode(text)
            else:
                # 空闲态 → 走 _start_new_run（避免 _send_in_send_mode 的 interrupt 分支）
                self._start_new_run(text)
        elif mode == MODE_QUEUE:
            self._send_in_queue_mode(text)
        elif mode == MODE_STEER:
            self._send_in_steer_mode(text)
        else:
            # 兜底：当作 send 处理
            if self._worker is not None and self._worker.isRunning():
                return
            self._start_new_run(text)

    def _on_interrupt(self) -> None:
        """中断当前会话。"""
        if self._worker is not None and self._worker.isRunning():
            self._interrupt_btn.setEnabled(False)
            self._worker.interrupt()
            self._update_status("interrupted", 0)

    def _on_steer(self) -> None:
        """T05/T09: 引导当前会话（legacy popup 路径）。

        新 UX 走 _send_in_steer_mode（直接用输入框文本，不弹 dialog）。
        本方法保留 popup 逻辑以兼容外部调用（如 _ChatWorker 信号连接）。
        """
        if self._worker is None or not self._worker.isRunning():
            return
        # 弹出多行输入框（兼容旧路径）
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getMultiLineText(
            self,
            "引导当前会话",
            "输入引导指令（不打断工具执行，下一轮读到）：",
            "",
        )
        if not ok or not text.strip():
            return
        # 调 worker.steer → worker loop 内 facade.steer 写入 EventStore
        self._worker.steer(text.strip())
        # 立即在 UI 显示引导消息预览（不等 worker 写库，避免延迟）
        self._append_steer_preview(text.strip())
        self._scroll_to_bottom()

    def _append_steer_preview(self, text: str) -> None:
        """T05: 立即在 UI 显示引导消息预览（source=steer，特殊样式）。"""
        msg = Message(role="user", content=text, source="steer", visible=True)
        self._append_message(msg, {})
        self._pending_steer_previews.add(text)  # 去重标记

    def _on_session_started(self, session_id: str) -> None:
        """worker 在 DB 创建会话后，刷新会话列表 + 应用 pending 分组。"""
        # T06（D6）：把开始页选的分组写入新建的会话
        if self._pending_group_for_new_session and self._read_store is not None:
            try:
                self._safe_read(
                    self._read_store.update_session_group(
                        session_id, self._pending_group_for_new_session
                    )
                )
            except Exception:
                logger.warning(
                    "apply pending group failed: %s", session_id, exc_info=True
                )
            self._pending_group_for_new_session = None
        self._refresh_session_list()

    def _on_worker_finished(self, outcome) -> None:
        """worker 完成，停止轮询，最终渲染。"""
        self._poll_timer.stop()
        self._send_btn.setEnabled(True)
        self._interrupt_btn.setEnabled(False)
        # T09: 切回空闲态（_set_running_state(False) 会检查排队区）
        self._set_running_state(False)

        status = getattr(outcome, "status", "unknown")
        # B2（spec D2/D3）：删 total_cost_usd 读取（字段已删，只记账 token）
        iters = getattr(outcome, "iterations", 0)
        # chat-panel-v2 T10（D47）：取累计 token
        total_tokens = getattr(outcome, "total_tokens", 0)
        self._update_status(status, iters, total_tokens=total_tokens)

        # 最终渲染一次确保完整
        self._poll_session_state()
        self._refresh_session_list()
        # T10: 刷新 last_updated + title
        self._refresh_last_updated()

        # 滚动到底部
        self._scroll_to_bottom()

    def _on_worker_error(self, error_msg: str) -> None:
        self._poll_timer.stop()
        self._send_btn.setEnabled(True)
        self._interrupt_btn.setEnabled(False)
        # 手动重置 mode tab（不走 _set_running_state，避免触发 _check_queue_after_run）
        if self._mode_queue_btn is not None:
            self._mode_queue_btn.setVisible(False)
        if self._mode_steer_btn is not None:
            self._mode_steer_btn.setVisible(False)
        if self._mode_send_btn is not None:
            self._mode_send_btn.setChecked(True)
        self._on_mode_tab_clicked(MODE_SEND)
        self._is_running_state = False
        self._update_status("failed", 0)
        QMessageBox.warning(self, "会话错误", f"会话执行失败：\n{error_msg}")

    # ------------------------------------------------------------------
    # 轮询渲染
    # ------------------------------------------------------------------

    # T08: sticky 标题栏（D42/D42a）
    def _update_sticky_bar(self) -> None:
        """滚动时更新 sticky 标题栏。

        D42：显示当前可见区域最顶部的可折叠块标题。
        D42a：无可见可折叠块时隐藏（高度 0）。
        """
        if (
            self._sticky_bar is None
            or self._sticky_btn is None
            or self._message_timeline is None
        ):
            return
        # 计算 viewport 在 _message_timeline 坐标系下的可见区间
        # _messages_container 是 scroll.widget()，_message_timeline 是其唯一子（layout 0 margin）
        # timeline_offset = _message_timeline 在 _messages_container 中的 y 偏移（通常 0）
        sb = self._scroll.verticalScrollBar()
        viewport_top = sb.value()
        viewport_height = self._scroll.viewport().height()
        viewport_bottom = viewport_top + viewport_height
        block = self._message_timeline.find_top_visible_collapsible(
            viewport_top, viewport_bottom
        )
        if block is None:
            # 无可见可折叠块 → 隐藏 sticky 栏
            if self._sticky_current_block is not None:
                self._sticky_current_block = None
                self._sticky_btn.setText("")
                self._sticky_bar.setVisible(False)
                self._sticky_bar.setMaximumHeight(0)
            return
        if block is self._sticky_current_block:
            # 同一块：仍需刷新箭头（块可能被 collapse/expand 后状态变化）
            arrow = "▼" if getattr(block, "_expanded", False) else "▶"
            self._sticky_btn.setText(f"{arrow} {block.title}")
            return
        self._sticky_current_block = block
        arrow = "▼" if getattr(block, "_expanded", False) else "▶"
        self._sticky_btn.setText(f"{arrow} {block.title}")
        # 显示 sticky 栏
        self._sticky_bar.setVisible(True)
        self._sticky_bar.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX

    def _on_sticky_clicked(self) -> None:
        """D42：点击 sticky 标题 = 折叠当前可见块。"""
        if self._sticky_current_block is not None:
            self._sticky_current_block.collapse()
            # 折叠后立即更新（块状态变化）
            self._update_sticky_bar()

    # ------------------------------------------------------------------
    # T09: 对话控制三模式（发送/队列/引导，D34/D35/D36/D36a/D37）
    # ------------------------------------------------------------------

    def _build_mode_tabs_and_queue(self, parent_layout: QVBoxLayout) -> None:
        """构建输入框上方的 3 mode tab + 排队区。

        - 3 个 checkable QPushButton 组成 QButtonGroup（exclusive）
        - 空闲态：只显示「发送」tab（D34）
        - 运行态：3 tab 全显示，默认选中「引导」（D34a）
        - 排队区：单条消息 + 编辑按钮，默认隐藏（D36）
        """
        # --- mode tab 容器 ---
        tabs_frame = QFrame()
        tabs_frame.setObjectName("modeTabsBar")
        tabs_frame.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_PANEL};"
            f" border: none; border-bottom: 1px solid {tokens.BORDER}; }}"
        )
        tabs_layout = QHBoxLayout(tabs_frame)
        tabs_layout.setContentsMargins(12, 4, 12, 4)
        tabs_layout.setSpacing(4)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self._mode_send_btn = QPushButton("发送")
        self._mode_send_btn.setCheckable(True)
        self._mode_send_btn.setChecked(True)  # 默认选中发送
        self._mode_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_send_btn.setStyleSheet(self._mode_tab_stylesheet(active=True))
        self._mode_send_btn.clicked.connect(lambda: self._on_mode_tab_clicked(MODE_SEND))
        tabs_layout.addWidget(self._mode_send_btn)
        self._mode_group.addButton(self._mode_send_btn, id=1)

        self._mode_queue_btn = QPushButton("队列")
        self._mode_queue_btn.setCheckable(True)
        self._mode_queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_queue_btn.setStyleSheet(self._mode_tab_stylesheet(active=False))
        self._mode_queue_btn.clicked.connect(lambda: self._on_mode_tab_clicked(MODE_QUEUE))
        # 空闲态隐藏
        self._mode_queue_btn.setVisible(False)
        tabs_layout.addWidget(self._mode_queue_btn)
        self._mode_group.addButton(self._mode_queue_btn, id=2)

        self._mode_steer_btn = QPushButton("引导")
        self._mode_steer_btn.setCheckable(True)
        self._mode_steer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_steer_btn.setStyleSheet(self._mode_tab_stylesheet(active=False))
        self._mode_steer_btn.clicked.connect(lambda: self._on_mode_tab_clicked(MODE_STEER))
        # 空闲态隐藏
        self._mode_steer_btn.setVisible(False)
        tabs_layout.addWidget(self._mode_steer_btn)
        self._mode_group.addButton(self._mode_steer_btn, id=3)

        tabs_layout.addStretch()
        parent_layout.addWidget(tabs_frame)

        # --- 排队区（默认隐藏）---
        self._queue_bar = QFrame()
        self._queue_bar.setObjectName("queueBar")
        self._queue_bar.setStyleSheet(
            f"QFrame {{ background: {tokens.WARNING_WASH};"
            f" border: none; border-bottom: 1px solid {tokens.BORDER}; }}"
        )
        queue_layout = QHBoxLayout(self._queue_bar)
        queue_layout.setContentsMargins(12, 4, 12, 4)
        queue_layout.setSpacing(8)

        queue_icon = QLabel("⏳")
        queue_icon.setStyleSheet(f"color: {tokens.WARNING_TEXT}; font-size: 14px;")
        queue_layout.addWidget(queue_icon)

        self._queue_label = QLabel("")
        self._queue_label.setStyleSheet(
            f"QLabel {{ color: {tokens.TEXT_SECONDARY};"
            f" font-size: {tokens.FONT_SMALL}px; }}"
        )
        self._queue_label.setWordWrap(False)
        queue_layout.addWidget(self._queue_label, 1)

        self._queue_edit_btn = QPushButton("编辑")
        set_kind(self._queue_edit_btn, "ghost")
        self._queue_edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._queue_edit_btn.setToolTip("取消排队，把消息内容追加到输入框")
        self._queue_edit_btn.clicked.connect(self._on_edit_queue)
        queue_layout.addWidget(self._queue_edit_btn)

        # 默认隐藏
        self._queue_bar.setVisible(False)
        self._queue_bar.setMaximumHeight(0)
        parent_layout.addWidget(self._queue_bar)

    def _mode_tab_stylesheet(self, active: bool) -> str:
        """mode tab 按钮样式（active/inactive）。"""
        bg = tokens.ACCENT_WASH if active else "transparent"
        color = tokens.TEXT_PRIMARY if active else tokens.TEXT_SECONDARY
        return (
            f"QPushButton {{ background: {bg}; color: {color};"
            f" border: none; padding: 4px 12px; border-radius: {tokens.RADIUS_SM}px;"
            f" font-size: {tokens.FONT_BODY}px; }}"
            f"QPushButton:hover {{ background: {tokens.ACCENT_WASH}; color: {tokens.TEXT_PRIMARY}; }}"
            f"QPushButton:disabled {{ color: {tokens.TEXT_TERTIARY}; background: transparent; }}"
        )

    def _on_mode_tab_clicked(self, mode: str) -> None:
        """点击 mode tab：更新样式（D34 运行态切 tab 不影响 runner）。"""
        # 更新所有 tab 的样式（active/inactive）
        for btn, m in [
            (self._mode_send_btn, MODE_SEND),
            (self._mode_queue_btn, MODE_QUEUE),
            (self._mode_steer_btn, MODE_STEER),
        ]:
            if btn is None:
                continue
            btn.setStyleSheet(self._mode_tab_stylesheet(active=(m == mode)))

    def _get_current_mode(self) -> str:
        """获取当前选中的 mode（D34）。"""
        if self._mode_group is None:
            return MODE_SEND
        checked = self._mode_group.checkedButton()
        if checked is self._mode_send_btn:
            return MODE_SEND
        if checked is self._mode_queue_btn:
            return MODE_QUEUE
        if checked is self._mode_steer_btn:
            return MODE_STEER
        return MODE_SEND

    def _set_running_state(self, is_running: bool) -> None:
        """切换空闲态/运行态（D34 + D34a）。

        - 空闲态：只显示「发送」tab，默认选中
        - 运行态：3 tab 全显示，默认选中「引导」（每次重置，不持久化）
        """
        self._is_running_state = is_running
        if is_running:
            # 运行态：显示全部 3 tab + 默认选中「引导」（D34a）
            if self._mode_queue_btn is not None:
                self._mode_queue_btn.setVisible(True)
            if self._mode_steer_btn is not None:
                self._mode_steer_btn.setVisible(True)
            # 重置到「引导」tab
            if self._mode_steer_btn is not None:
                self._mode_steer_btn.setChecked(True)
            self._on_mode_tab_clicked(MODE_STEER)
        else:
            # 空闲态：只显示「发送」tab + 选中「发送」
            if self._mode_queue_btn is not None:
                self._mode_queue_btn.setVisible(False)
            if self._mode_steer_btn is not None:
                self._mode_steer_btn.setVisible(False)
            if self._mode_send_btn is not None:
                self._mode_send_btn.setChecked(True)
            self._on_mode_tab_clicked(MODE_SEND)
            # 空闲时检查排队区，若有排队 → 自动 start
            self._check_queue_after_run()

    def _check_queue_after_run(self) -> None:
        """run 结束后检查排队区：若有排队消息 → 自动 start 新 run（D36）。"""
        if not self._queued_text:
            return
        if not self._current_session_id:
            # 无活跃会话，排队消息不丢失但需要用户手动触发
            return
        queued = self._queued_text
        self._clear_queue()
        # 立即在 UI 显示用户消息预览 + start 新 run
        self._input_edit.setPlainText(queued)
        # 直接触发发送（此时已是空闲态，走 MODE_SEND 路径）
        self._on_send()

    def _show_queue(self, text: str, *, persist: bool = True) -> None:
        """显示排队区（D36 + D36a：单条 + 编辑按钮）。

        Args:
            text: 排队消息内容
            persist: True=写入 EventStore 持久化（默认，用户主动排队时）；
                     False=不写库（启动恢复时从库读到，避免重复写入）
        """
        self._queued_text = text
        # 截断显示 + tooltip 全文
        display = text if len(text) <= _QUEUE_TEXT_TRUNCATE else text[:_QUEUE_TEXT_TRUNCATE] + "..."
        if self._queue_label is not None:
            self._queue_label.setText(display)
            self._queue_label.setToolTip(text)
        if self._queue_bar is not None:
            self._queue_bar.setVisible(True)
            self._queue_bar.setMaximumHeight(16777215)
        # D36a：已有排队时禁用「队列」tab
        if self._mode_queue_btn is not None:
            self._mode_queue_btn.setEnabled(False)
            # 切回「引导」tab（用户仍可发引导或中断后发送）
            if self._mode_steer_btn is not None and self._is_running_state:
                self._mode_steer_btn.setChecked(True)
                self._on_mode_tab_clicked(MODE_STEER)
        # T09 持久化：写入 EventStore source="queue" visible=0（不丢，重启可恢复）
        if persist and self._read_store is not None and self._current_session_id:
            self._persist_queue_message(text)

    def _persist_queue_message(self, text: str) -> None:
        """T09: 把排队消息写入 EventStore（source='queue', visible=False）。"""
        if self._read_store is None or not self._current_session_id:
            return
        # 先清掉旧的持久化行（避免重复）
        if self._queued_msg_id:
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self._read_store.delete_message_by_id(self._queued_msg_id)
                    )
                finally:
                    loop.close()
            except Exception:
                logger.warning("Failed to delete old queue message", exc_info=True)
        msg = Message(
            role="user",
            content=text,
            source="queue",
            visible=False,
        )
        try:
            loop = asyncio.new_event_loop()
            try:
                appended = loop.run_until_complete(
                    self._read_store.append_message(self._current_session_id, msg)
                )
            finally:
                loop.close()
            self._queued_msg_id = appended.id
        except Exception:
            logger.warning("Failed to persist queue message", exc_info=True)
            self._queued_msg_id = None

    def _delete_persisted_queue(self) -> None:
        """T09: 删除持久化的排队消息（清理时调用）。"""
        if not self._queued_msg_id or self._read_store is None:
            return
        msg_id = self._queued_msg_id
        self._queued_msg_id = None
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    self._read_store.delete_message_by_id(msg_id)
                )
            finally:
                loop.close()
        except Exception:
            logger.warning("Failed to delete persisted queue message", exc_info=True)

    def _clear_queue(self) -> None:
        """清空排队区。"""
        self._queued_text = ""
        # T09: 同步删除持久化的排队消息
        self._delete_persisted_queue()
        if self._queue_label is not None:
            self._queue_label.setText("")
            self._queue_label.setToolTip("")
        if self._queue_bar is not None:
            self._queue_bar.setVisible(False)
            self._queue_bar.setMaximumHeight(0)
        # 重新启用「队列」tab
        if self._mode_queue_btn is not None:
            self._mode_queue_btn.setEnabled(True)

    def _on_edit_queue(self) -> None:
        """点击「编辑」按钮：取消排队 + 消息内容追加到输入框末尾（D36）。"""
        if not self._queued_text:
            return
        existing = self._input_edit.toPlainText()
        if existing and not existing.endswith("\n"):
            # 输入框有内容时空行连接
            self._input_edit.setPlainText(existing + "\n" + self._queued_text)
        else:
            self._input_edit.setPlainText(existing + self._queued_text)
        self._clear_queue()
        self._input_edit.setFocus()

    # ------------------------------------------------------------------
    # T09: 三模式发送 dispatch（D34）
    # ------------------------------------------------------------------

    def _send_in_send_mode(self, text: str) -> None:
        """发送模式：中断当前 worker + start 新 run（D34 + D35）。

        - 若 worker 运行中：先 interrupt + finalize streaming 为中断消息（T07 已实现）
        - 然后正常 start 新 run（复用现有 _on_send 流程）
        """
        if self._worker is not None and self._worker.isRunning():
            # 中断当前 worker（D43 已在 T07 实现：streaming 转正为中断消息）
            self._worker.interrupt()
            # 等待 worker 实际结束（异步：下一轮 _on_worker_finished 会切回空闲态）
            # 这里直接走 _on_send 后续逻辑（start 新 run），不等待
        # 走现有 _on_send 的 session 创建 + worker 启动逻辑
        # 但避免递归调用 _on_send（会再次 dispatch），直接执行核心逻辑
        self._start_new_run(text)

    def _send_in_queue_mode(self, text: str) -> None:
        """队列模式：存排队区单条（D36 + D36a）。"""
        if self._queued_text:
            return  # 已有排队，不应到此（队列 tab 已禁用）
        self._show_queue(text)
        self._input_edit.clear()

    def _send_in_steer_mode(self, text: str) -> None:
        """引导模式：facade.steer + 显示预览（D34 + D37）。"""
        if self._worker is None or not self._worker.isRunning():
            return  # 无运行中的 worker，引导无意义
        # 调 worker.steer → worker loop 内 facade.steer 写入 EventStore
        self._worker.steer(text)
        # 立即在 UI 显示引导消息预览（不等 worker 写库）
        self._append_steer_preview(text)
        self._scroll_to_bottom()
        self._input_edit.clear()

    def _start_new_run(self, text: str) -> None:
        """启动新 run（提取自 _on_send，避免 dispatch 递归）。

        D34a：启动后切到运行态（3 tab 显示 + 默认「引导」）。
        """
        # chat-panel-v2 T04: 兜底创建 session_id
        if not self._current_session_id:
            self._current_session_id = f"sess-{_uuid.uuid4().hex[:12]}"
            self._last_rendered_seq = 0
            self._last_tool_call_ids = set()
            self._pending_user_preview = None
            self._pending_steer_previews.clear()
            self._clear_messages()
        self._show_messages_view()

        session_id = self._current_session_id
        self._input_edit.clear()
        self._empty_hint.setVisible(False)

        # 立即在 UI 显示用户消息（不等 worker）
        self._append_user_message_preview(text)

        # T07: 流式渲染状态由 _message_timeline 管理，仅重置 seq 锚点
        self._last_streaming_seq = 0

        # 启动 worker
        self._worker = _ChatWorker(
            session_id=session_id,
            user_message=text,
            db_path=self._db_path,
            base_url=self._base_url,
            config={
                "max_iterations": 50,
                # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost（缓存比例不明）
            },
            model_override=self._current_model,
            parent=self,
        )
        self._worker.finished_run.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.session_started.connect(self._on_session_started)

        self._send_btn.setEnabled(False)
        self._interrupt_btn.setEnabled(True)
        # T09: 切到运行态（3 tab 显示 + 默认「引导」）
        self._set_running_state(True)
        self._update_status("streaming", 0)

        self._worker.start()
        self._poll_timer.start()

    def _poll_session_state(self) -> None:
        """QTimer 触发，从 read_store 增量渲染当前会话。

        T03: 打字机三档模式控制 streaming 事件读取：
        - close：不读 streaming 事件，等流结束一次性渲染
        - fast/normal：读 streaming 事件增量 append
        """
        if self._read_store is None or self._current_session_id is None:
            return

        sid = self._current_session_id

        # T03: streaming 事件增量渲染（close 档跳过）
        if self._typewriter_mode != "close":
            self._poll_streaming_events(sid)

        # 加载所有消息（visible=true，已过滤 synthetic）
        messages = self._safe_read(self._read_store.load_messages(sid))
        if messages is None:
            return

        # 加载所有 tool_calls（用于更新卡片状态）
        tool_calls = self._safe_read(self._read_store.load_tool_calls(sid))
        tool_call_status: dict[str, dict[str, str]] = {}
        if tool_calls:
            for tc in tool_calls:
                tc_id = tc.id if hasattr(tc, "id") else tc.get("id", "")
                tc_status = tc.status if hasattr(tc, "status") else tc.get("status", "pending")
                tc_name = tc.name if hasattr(tc, "name") else tc.get("name", "")
                if tc_id:
                    tool_call_status[tc_id] = {
                        "name": tc_name or "工具结果",
                        "status": tc_status,
                    }

        # 加载会话状态
        session = self._safe_read(self._read_store.get_session(sid))
        if session is not None:
            status = session.status if hasattr(session, "status") else session.get("status", "")
            # chat-panel-v2 T10（D47）：刷新 title_label
            sess_title = (
                getattr(session, "title", "")
                if not isinstance(session, dict)
                else session.get("title", "")
            )
            self._update_title(sess_title)
            # 只在非 running 时更新顶栏状态（running 时保留 streaming）
            if status and status != "streaming":
                cur_text = self._status_label.text()
                if cur_text != _SESSION_STATUS_TEXT.get(status, status):
                    self._update_status(status, 0)

        # 增量渲染：找 seq > _last_rendered_seq 的新消息
        new_messages = [m for m in messages if m.seq > self._last_rendered_seq]
        if new_messages:
            new_messages.sort(key=lambda m: m.seq)
            for m in new_messages:
                is_preview = (
                    self._pending_user_preview is not None
                    and m.role == "user"
                    and _content_to_text(m.content).strip() == self._pending_user_preview
                )
                # T05: steer 预览去重（_append_steer_preview 已渲染，跳过）
                is_steer_preview = (
                    m.role == "user"
                    and m.source == "steer"
                    and _content_to_text(m.content).strip() in self._pending_steer_previews
                )
                if is_preview:
                    self._pending_user_preview = None
                elif is_steer_preview:
                    # 已预渲染，从去重集合移除（避免内存增长）
                    self._pending_steer_previews.discard(_content_to_text(m.content).strip())
                elif m.role == "assistant" and self._message_timeline.has_streaming_assistant():
                    # T07: 流结束，finalize streaming assistant 块 + thinking 块
                    self._message_timeline.finalize_streaming_assistant(
                        _content_to_text(m.content), model=m.model or ""
                    )
                    if m.thinking:
                        self._message_timeline.finalize_streaming_thinking(m.thinking)
                    self._last_streaming_seq = 0
                else:
                    self._append_message(m, tool_call_status)
                self._last_rendered_seq = m.seq
            self._scroll_to_bottom()

        # 更新已有 tool_call 卡片的状态
        self._update_tool_card_statuses(tool_call_status)

        self._empty_hint.setVisible(len(messages) == 0)

    # ------------------------------------------------------------------
    # T03: streaming 事件增量渲染
    # ------------------------------------------------------------------

    def _poll_streaming_events(self, sid: str) -> None:
        """T07: 读 streaming 事件并增量渲染到 _message_timeline。"""
        if self._read_store is None:
            return
        streaming_events = self._safe_read(self._read_store.load_streaming_events(sid))
        if not streaming_events:
            return

        # 过滤出 seq > _last_streaming_seq 的新事件
        new_events = [e for e in streaming_events if e.seq > self._last_streaming_seq]
        if not new_events:
            return
        new_events.sort(key=lambda e: e.seq)

        # 处理每个新事件（timeline 自动创建/复用 streaming 块）
        for ev in new_events:
            if ev.type == "streaming_text_delta":
                delta = ev.payload.get("delta", "")
                # 首个 delta 时创建 streaming assistant 块
                if not self._message_timeline.has_streaming_assistant():
                    streaming_block = self._message_timeline.start_streaming_assistant()
                    # T11: 连接 hover Copy 信号（finalize 后才激活）
                    if isinstance(streaming_block, _AssistantTextBlock):
                        streaming_block.copy_requested.connect(self._on_block_copy_requested)
                self._message_timeline.append_text_delta(delta)
            elif ev.type == "streaming_thinking_delta":
                delta = ev.payload.get("delta", "")
                # D38：thinking 始终作为独立块（不再依赖 _show_thinking 开关）
                self._message_timeline.append_thinking_delta(delta)
            self._last_streaming_seq = ev.seq

        self._scroll_to_bottom()

    def _append_user_message_preview(self, text: str) -> None:
        """发送时立即在 UI 显示用户消息预览（不等 worker 写库）。"""
        self._pending_user_preview = text
        msg = Message(role="user", content=text, source="user")
        self._append_message(msg, {})
        self._scroll_to_bottom()

    def _append_message(self, message: Message, tool_call_status: dict) -> None:
        """T07: 追加一条消息到时间线（按 role 派发到对应块类）。

        T11: user/assistant 块追加后连接 hover 按钮信号（D33）。
        """
        role = message.role
        if role == "user":
            block = self._message_timeline.append_user_block(message)
            self._connect_block_hover_signals(block, message)
        elif role == "assistant":
            blocks = self._message_timeline.append_assistant_blocks(message)
            for b in blocks:
                if isinstance(b, _AssistantTextBlock):
                    self._connect_block_hover_signals(b, message)
        elif role == "tool":
            tool_info = tool_call_status.get(message.tool_call_id, {})
            if isinstance(tool_info, dict):
                tool_name = tool_info.get("name", "工具结果")
                tool_status = tool_info.get("status", "unknown")
            else:
                tool_name = "工具结果"
                tool_status = tool_info or "unknown"
            self._message_timeline.append_tool_result(message, tool_name, tool_status)
        elif role in ("system", "system_reminder", "summary"):
            self._message_timeline.append_system_block(message)
        else:
            # 兜底：作为 assistant 文本块追加
            blocks = self._message_timeline.append_assistant_blocks(message)
            for b in blocks:
                if isinstance(b, _AssistantTextBlock):
                    self._connect_block_hover_signals(b, message)

    def _connect_block_hover_signals(self, block, message: Message) -> None:
        """T11 D33：连接 hover 按钮信号（user=Copy/Delete, assistant=Copy）。"""
        if isinstance(block, _UserBubble):
            block.copy_requested.connect(self._on_block_copy_requested)
            if message.id:  # 只在有 id 时连 delete（否则 delete 无意义）
                block.delete_requested.connect(self._on_block_delete_requested)
        elif isinstance(block, _AssistantTextBlock):
            block.copy_requested.connect(self._on_block_copy_requested)

    def _on_block_copy_requested(self, text: str) -> None:
        """T11 D33：hover Copy 按钮 → 复制到剪贴板。"""
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def _on_block_delete_requested(self, message_id: str) -> None:
        """T11 D32/D33：hover Delete 按钮 → 弹确认对话框 + 删除消息。

        D32：删除 user 消息时连带后续 tool_calls 和 tool_result 一起删。
        实现简化：只删单条消息（用户确认后调 EventStore.delete_message_by_id）。
        整段删除（D32 完整范围）留待后续增强。
        """
        if not message_id or self._read_store is None:
            return
        reply = QMessageBox.question(
            self,
            "删除消息",
            "确定删除这条消息吗？\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._safe_read(self._read_store.delete_message_by_id(message_id))
        except Exception:
            logger.warning("delete message failed: %s", message_id, exc_info=True)
            return
        # 刷新当前会话的消息时间线
        if self._current_session_id:
            self._last_rendered_seq = 0
            self._last_tool_call_ids = set()
            self._clear_messages()
            self._poll_session_state()

    def _update_tool_card_statuses(self, tool_call_status: dict) -> None:
        """T07: 更新所有 tool_call 块的状态（委托给 _message_timeline）。"""
        self._message_timeline.update_tool_call_status(tool_call_status)

    def _clear_messages(self) -> None:
        """T07: 清空消息时间线（委托给 _message_timeline）。"""
        self._message_timeline.clear()

    def _scroll_to_bottom(self) -> None:
        """T07: 滚动到底部（委托给 _message_timeline）。"""
        self._message_timeline.scroll_to_bottom(self._scroll)

    def _update_status(self, status: str, iterations: int, total_tokens: int = 0) -> None:
        """更新顶栏状态显示（B2 后：只显示 token，不算 cost）。

        - total_tokens=0 → 不显示
        - total_tokens>0 → 显示「token {value}{单位}」
        - iterations 信息合并到 status_label tooltip
        """
        color = _SESSION_STATUS_COLOR.get(status, tokens.TEXT_TERTIARY)
        self._status_label.setText(_SESSION_STATUS_TEXT.get(status, status))
        self._status_label.setStyleSheet(f"color: {color};")
        # tooltip 含 iterations（D47：取消 iter_label，合并到 tooltip）
        if iterations > 0:
            self._status_label.setToolTip(f"{iterations} 轮")
        else:
            self._status_label.setToolTip("")

        # B2（spec D2/D3）：删 cost 显示，只显示 token
        if total_tokens <= 0:
            self._cost_label.setText("")
            self._cost_label.setToolTip("")
        else:
            token_str = _format_token_count(total_tokens)
            self._cost_label.setText(f"token {token_str}")
            self._cost_label.setToolTip(f"token {total_tokens}")

    # ------------------------------------------------------------------
    # T03: 打字机 + thinking toggle 回调
    # ------------------------------------------------------------------

    def _on_toggle_typewriter(self) -> None:
        """打字机三档 toggle：close→fast→normal→close 循环切换（D47：按钮文字加前缀）。"""
        idx = _TYPEWRITER_MODES.index(self._typewriter_mode)
        next_idx = (idx + 1) % len(_TYPEWRITER_MODES)
        self._typewriter_mode = _TYPEWRITER_MODES[next_idx]
        self._apply_typewriter_mode()
        mode_text = _TYPEWRITER_MODE_TEXT.get(self._typewriter_mode, self._typewriter_mode)
        self._typewriter_btn.setText(f"打字机效果：{mode_text}")
        self._typewriter_btn.setToolTip(
            f"打字机效果（当前：{mode_text}）\n点击切换：关闭→快速→正常→关闭"
        )

    def _apply_typewriter_mode(self) -> None:
        """应用打字机模式（调整 _poll_timer interval）。"""
        interval = _TYPEWRITER_POLL_INTERVAL.get(self._typewriter_mode, 200)
        self._poll_timer.setInterval(interval)
        logger.info("Typewriter mode: %s (poll interval %dms)", self._typewriter_mode, interval)

    def _on_toggle_thinking(self) -> None:
        """T07: thinking toggle — 展开/折叠所有 _ThinkingBlock 块。

        T10 后 thinking_toggle_btn 已从顶栏移除（D47），此方法保留为 no-op stub，
        供旧测试调用 _on_toggle_thinking 不报错（_thinking_toggle_btn=None 时直接 return）。
        新 UX：thinking 块自带折叠 toggle（点击 ▶ thinking 标题切换）。
        """
        if self._thinking_toggle_btn is None:
            # T10 后按钮已移除，_show_thinking 状态由各块独立管理
            self._show_thinking = not self._show_thinking
            if not hasattr(self, "_message_timeline") or self._message_timeline is None:
                return
            tl_layout = self._message_timeline._layout
            for i in range(tl_layout.count()):
                item = tl_layout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, _ThinkingBlock):
                    if widget._expanded != self._show_thinking:
                        widget.toggle()
            logger.info("Show thinking (toggled): %s", self._show_thinking)
            return
        # 兼容旧测试：_thinking_toggle_btn 存在时走原逻辑
        self._show_thinking = self._thinking_toggle_btn.isChecked()
        if not hasattr(self, "_message_timeline") or self._message_timeline is None:
            return
        tl_layout = self._message_timeline._layout
        for i in range(tl_layout.count()):
            item = tl_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, _ThinkingBlock):
                if widget._expanded != self._show_thinking:
                    widget.toggle()
        logger.info("Show thinking: %s", self._show_thinking)

    # ------------------------------------------------------------------
    # T10: 顶栏 last_updated / title 刷新
    # ------------------------------------------------------------------

    def _refresh_last_updated(self) -> None:
        """D47: 刷新 last_updated 标签（HH:MM 格式，用会话 updated_at 时间）。"""
        if self._last_updated_label is None:
            return
        if self._current_session_id is None or self._read_store is None:
            # 无活跃会话 → 显示当前系统时间
            import time as _time
            self._last_updated_label.setText(_time.strftime("%H:%M", _time.localtime()))
            return
        try:
            session = self._safe_read(self._read_store.get_session(self._current_session_id))
            if session is not None:
                updated_at = (
                    getattr(session, "updated_at", 0.0)
                    if not isinstance(session, dict)
                    else session.get("updated_at", 0.0)
                )
                if updated_at:
                    import time as _time
                    self._last_updated_label.setText(
                        _time.strftime("%H:%M", _time.localtime(updated_at))
                    )
                else:
                    self._last_updated_label.setText("")
            else:
                self._last_updated_label.setText("")
        except Exception:
            self._last_updated_label.setText("")

    def _update_title(self, title: str) -> None:
        """D47: 更新 title_label（截断超 32 字符 + 省略号）。"""
        if self._title_label is None:
            return
        display = _truncate_title(title or "", max_chars=32)
        self._title_label.setText(display)
        self._title_label.setToolTip(title or "")
