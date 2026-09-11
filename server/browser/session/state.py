"""TabSession 数据类：单个标签页的会话句柄。

从 server/browser_session.py 迁移至 server/browser/session/ 包（Ticket 02）。
代码与原定义完全一致，仅调整 import 路径。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Dialog, Download, FileChooser, Page


@dataclass
class TabSession:
    """单个标签页的会话句柄。"""
    session_id: str  # uuid4 hex，调用方持有
    tab_id: str  # CDP target id，跨调用稳定
    page: Page  # Playwright Page 对象（TYPE_CHECKING 下标注，运行时仅字符串注解）
    url: str  # 创建时的 URL（运行时 page.url 可能变化）
    title: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # 控制台日志缓冲（评估文档 P1 第 2 项：since_cursor 增量返回）
    # P1-2 修复：环形缓冲 + dropped_count 计数
    # - console_logs_max_len: 缓冲上限（默认 1000，溢出时丢弃最旧的）
    # - console_logs_dropped_count: 因溢出被丢弃的累计日志数
    console_logs: list[dict] = field(default_factory=list)
    console_log_cursor: int = 0  # 已返回给客户端的日志数
    console_logs_max_len: int = 1000
    console_logs_dropped_count: int = 0
    # P2-1: 站点经验（match_site_for_url 命中的 .agents/skills/browser_lessons/sites/*.md）
    site_lessons: list[dict] = field(default_factory=list)
    # P2-2: snapshot 缓存，支持 node_id 重放 + STALE_NODE 检测
    last_snapshot_nodes: list[dict] = field(default_factory=list)
    last_snapshot_at: float = 0.0
    last_snapshot_url: str = ""
    last_snapshot_dom_hash: str = ""  # DOM 版本签名，用于判断 snapshot 是否过期
    # P1-2 修复：DOM mutation 计数（基于 MutationObserver），用于 STALE_NODE 精细化判定
    # snapshot 时记录当时的 mutation 计数；action 用 node_id 时如果计数变化则返回 STALE_NODE
    last_snapshot_dom_version: int = 0  # snapshot 时的 dom 版本号
    _dom_version_probe: object = field(default=None, repr=False)  # MutationObserver 句柄（page.evaluate 返回的 handler dict）
    # P0-1 修复：dialog/popup/filechooser 持久事件监听器
    # Playwright expect_* context manager 跨 HTTP 请求时序不可靠（enter→exit 之间
    # 无法被另一个 HTTP 请求触发），改为在 session 创建时挂持久 page.on 监听器，
    # 把事件存到队列，wait_for 时消费队列或等待新事件。
    # - dialog: page.on('dialog') 触发时存事件，Playwright 默认会自动 dismiss
    #   （我们不在监听器里 accept/dismiss，让 wait_for 决定如何处理；超时则自动 dismiss）
    # - popup: page.on('popup') 触发时存事件（2026-09-03 从 context 级改为 page 级，
    #   只收本 session 页面 spawn 的 popup，不再串扰其他 session 的新建页）
    # - filechooser: page.on('filechooser') 触发时存事件
    # 队列上限（防恶意页面持续触发导致内存泄漏，参考 console_logs_max_len）：
    # 超出时丢弃最旧的并累计 dropped_count。
    pending_dialogs: list[dict] = field(default_factory=list)
    pending_popups: list[dict] = field(default_factory=list)
    pending_filechoosers: list[dict] = field(default_factory=list)
    pending_<data_drive>:/Downloads: list[dict] = field(default_factory=list)
    pending_events_max_len: int = 100
    pending_dialogs_dropped_count: int = 0
    pending_popups_dropped_count: int = 0
    pending_filechoosers_dropped_count: int = 0
    pending_<data_drive>:/Downloads_dropped_count: int = 0
    # asyncio.Event 用于 wait_for 阻塞等待新事件（仅 session 模式有效）
    dialog_event: asyncio.Event = field(default_factory=asyncio.Event)
    popup_event: asyncio.Event = field(default_factory=asyncio.Event)
    filechooser_event: asyncio.Event = field(default_factory=asyncio.Event)
    download_event: asyncio.Event = field(default_factory=asyncio.Event)
    # _on_popup 异步收集 task 的 strong reference（避免 asyncio.ensure_future 创建的 task 被 GC）
    _popup_tasks: set = field(default_factory=set)
    # 2026-09-03 popup 归属修复：本 session 页面 spawn 的 popup Page 强引用集合。
    # close_session(close_page=True) 连坐关闭它们（否则 window.open 的子页成为
    # 孤儿 tab 泄漏）；popup 自身关闭时经 once("close") 自动移除。
    popup_pages: set = field(default_factory=set)
    # _on_dialog 回调中存放最新的 Dialog 对象引用（不被自动 dismiss，wait_for 可拿到）。
    # dialog 生命周期：触发 → 存引用 + 启动 5 分钟超时兜底 task；超时前 handle_dialog
    # 可调 dlg.accept(prompt_text)/dlg.dismiss()，超时后兜底 task 自动 dismiss 并清空。
    last_dialog_obj: Dialog | None = field(default=None, repr=False)
    # 标记最新 dialog 是否已被超时兜底自动 dismiss（handle_dialog 据此返回 DIALOG_ALREADY_DISMISSED）
    dialog_auto_dismissed: bool = False
    # 超时兜底 task 的 strong reference（session close 时全部取消，避免 asyncio task 泄漏）
    _dialog_timeout_tasks: set = field(default_factory=set)
    last_filechooser_obj: FileChooser | None = field(default=None, repr=False)
    # _on_download 回调中存放最新的 Download 对象引用（wait_for 可调 save_as 保存到指定路径）
    last_download_obj: Download | None = field(default=None, repr=False)

    def touch(self) -> None:
        self.last_used_at = time.time()

    def _append_pending(self, queue_name: str, item: dict) -> None:
        """向 pending_* 队列追加事件，超出上限时丢弃最旧的并累计 dropped_count。

        queue_name: "pending_dialogs" / "pending_popups" / "pending_filechoosers" / "pending_<data_drive>:/Downloads"
        """
        q: list[dict] = getattr(self, queue_name)
        q.append(item)
        max_len = self.pending_events_max_len
        if len(q) > max_len:
            overflow = len(q) - max_len
            del q[:overflow]
            attr = queue_name + "_dropped_count"
            setattr(self, attr, getattr(self, attr) + overflow)

    def pop_pending_dialog(self) -> dict | None:
        """取出一个未消费的 dialog 事件。"""
        if self.pending_dialogs:
            return self.pending_dialogs.pop(0)
        return None

    def peek_pending_dialog(self) -> dict | None:
        """查看队首 dialog 事件但不消费（2026-09-03 修复设计断点）。

        wait_for(dialog) 只报告不处理，改用 peek；handle_dialog 是队列的
        唯一消费者（pop + 处理 last_dialog_obj）。若 wait_for 用 pop 会把
        handle_dialog 要消费的事件提前弹掉，导致文档工作流
        「wait_for 报告 → handle_dialog 收尾」必然 DIALOG_NOT_FOUND。
        """
        if self.pending_dialogs:
            return self.pending_dialogs[0]
        return None

    def pop_pending_popup(self) -> dict | None:
        """取出一个未消费的 popup 事件。"""
        if self.pending_popups:
            return self.pending_popups.pop(0)
        return None

    def pop_pending_filechooser(self) -> dict | None:
        """取出一个未消费的 filechooser 事件。"""
        if self.pending_filechoosers:
            return self.pending_filechoosers.pop(0)
        return None

    def pop_pending_download(self) -> dict | None:
        """取出一个未消费的 download 事件。"""
        if self.pending_<data_drive>:/Downloads:
            return self.pending_<data_drive>:/Downloads.pop(0)
        return None

    def has_pending_dialog(self) -> bool:
        """是否有 dialog 阻塞 page 操作（操作端点预检用）。

        返回 True 的条件：有 Dialog 引用（last_dialog_obj 非 None）且未被超时兜底自动 dismiss。
        - dialog 触发时：last_dialog_obj=Dialog, dialog_auto_dismissed=False → True
        - handle_dialog 处理后：last_dialog_obj=None → False
        - 超时兜底 dismiss 后：last_dialog_obj=None, dialog_auto_dismissed=True → False
        - 新 dialog 覆盖旧 dialog：重置 dialog_auto_dismissed=False → True
        """
        return self.last_dialog_obj is not None and not self.dialog_auto_dismissed
