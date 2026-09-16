"""近期记忆 — 滑动窗口

L1 层：保留最近 N 条消息在内存中，快速访问。
同时持久化到 JSON 文件，重启后自动加载。
"""

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class RecentMemory:
    """近期记忆滑动窗口"""

    def __init__(self, window_size: int = 50, save_dir: str = "data/memory/recent",
                     save_interval: int = 5):
            """
            功能：初始化一个带有内存窗口的实例，用于缓冲数据并定期保存。

            参数：
                window_size (int): 缓冲区的最大大小，默认为50。
                save_dir (str): 保存目录路径，默认为 "data/memory/recent"。
                save_interval (int): 保存间隔，默认为5。

            返回值：None
            """
            self.window_size = window_size  # 设置缓冲区窗口大小
            # 5-7: 相对路径基于项目根解析（不依赖 cwd），同 manager.py db_path 的处理
            save_dir_path = Path(save_dir)
            if not save_dir_path.is_absolute():
                # recent.py 在 server/memory/ 下，3 级 parent 到项目根
                save_dir_path = Path(__file__).resolve().parent.parent.parent / save_dir_path
            self.save_dir = str(save_dir_path)  # 设置保存目录
            self.save_interval = save_interval  # 设置保存间隔
            self._buffer: deque = deque(maxlen=window_size)  # 创建一个最大长度为window_size的双端队列，用于缓冲数据
            self._pending_saves = 0  # 初始化待保存计数为0
            self._lock = threading.Lock()  # 创建线程锁以支持并发访问，确保操作线程安全
            self._session_id: str | None = None  # 初始化会话ID为None，用于标识当前会话

    def initialize(self) -> None:
        """从文件加载近期记忆"""
        os.makedirs(self.save_dir, exist_ok=True)
        self._load_from_file()
        self._session_id = f"session_{int(time.time())}"

    def add(self, content: str, source: str = "agent", role: str = "assistant",
            key: str | None = None, metadata: dict | None = None) -> dict:
        """添加一条消息到近期记忆"""
        entry = {
            "timestamp": time.time(),
            "source": source,
            "role": role,
            "content": content,
            "key": key,
            "metadata": metadata or {},
            "session_id": self._session_id,
        }

        with self._lock:
            self._buffer.append(entry)
            self._pending_saves += 1

            # 达到保存间隔时持久化
            if self._pending_saves >= self.save_interval:
                self._save_to_file()
                self._pending_saves = 0

        return entry

    def get_recent(self, limit: int | None = None, source: str | None = None) -> list[dict]:
        """获取近期消息"""
        with self._lock:
            messages = list(self._buffer)

        if source:
            messages = [m for m in messages if m["source"] == source]

        if limit:
            messages = messages[-limit:]

        return messages

    def get_context_for_prompt(self, max_chars: int = 4000) -> str:
        """生成用于 LLM prompt 的近期上下文"""
        messages = self.get_recent()
        if not messages:
            return ""

        lines = []
        total_chars = 0
        for msg in reversed(messages):
            role = msg.get("role", "assistant")
            source = msg.get("source", "agent")
            content = msg.get("content", "")
            line = f"[{source}/{role}] {content[:200]}"
            if total_chars + len(line) > max_chars:
                break
            lines.append(line)
            total_chars += len(line)

        lines.reverse()
        return "\n".join(lines)

    def search_recent(self, keyword: str, limit: int = 10) -> list[dict]:
        """在近期记忆中搜索关键词"""
        keyword_lower = keyword.lower()
        results = []
        with self._lock:
            for msg in reversed(self._buffer):
                if keyword_lower in msg.get("content", "").lower():
                    results.append(msg)
                    if len(results) >= limit:
                        break
        return results

    @property
    def count(self) -> int:
        """返回缓冲区中当前的元素数量。

        功能：计算并返回实例内部缓冲区中元素的个数。
        参数：无（self 是实例自身）。
        返回值：整数，表示缓冲区中的元素数量。
        """
        with self._lock:  # 使用锁来保证多线程环境下的数据一致性
            return len(self._buffer)  # 计算缓冲区长度并返回

    def clear(self) -> None:
        """清空近期记忆"""
        with self._lock:
            self._buffer.clear()
            self._pending_saves = 0
            self._save_to_file()

    def flush(self) -> None:
        """强制保存到文件"""
        with self._lock:
            self._save_to_file()
            self._pending_saves = 0

    def _save_to_file(self) -> None:
        """保存到 JSON 文件"""
        filepath = os.path.join(self.save_dir, "recent.json")
        try:
            data = {
                "session_id": self._session_id,
                "window_size": self.window_size,
                "messages": list(self._buffer),
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"近期记忆保存失败: {e}")

    def _load_from_file(self) -> None:
        """从 JSON 文件加载"""
        filepath = os.path.join(self.save_dir, "recent.json")
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])
            self._session_id = data.get("session_id")

            with self._lock:
                self._buffer.clear()
                for msg in messages[-self.window_size:]:
                    self._buffer.append(msg)

            logger.info(f"加载了 {len(self._buffer)} 条近期记忆")
        except Exception as e:
            logger.warning(f"近期记忆加载失败: {e}")
