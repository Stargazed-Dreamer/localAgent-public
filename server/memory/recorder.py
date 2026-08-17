"""交互自动记录器 + 触发器

自动记录 Agent 交互历史，每 N 次交互触发记忆维护。
"""

import asyncio
import json
import logging
import threading

from server.memory.compress import CompressionPipeline
from server.memory.config import MemoryConfig
from server.memory.recent import RecentMemory
from server.memory.semantic import SemanticSearch
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class InteractionRecorder:
    """Agent 交互自动记录器 + 触发器"""

    def __init__(self, store: MemoryStore, recent: RecentMemory,
                 semantic: SemanticSearch, compress: CompressionPipeline,
                 config: MemoryConfig, maintainer=None):
        """初始化内存管理器实例。

        功能：设置记忆存储、最近记忆、语义搜索、压缩管道和配置，用于管理内存交互。
        参数：
            store (MemoryStore): 记忆存储对象，负责数据持久化。
            recent (RecentMemory): 最近记忆对象，管理短期记忆。
            semantic (SemanticSearch): 语义搜索对象，处理基于语义的检索。
            compress (CompressionPipeline): 压缩管道对象，用于优化内存。
            config (MemoryConfig): 内存配置对象，存储相关设置。
            maintainer: 记忆维护器实例（可选），用于定期老化+验证。
        返回值：None（__init__方法不返回值）。
        """
        self.store = store  # 存储记忆的存储对象
        self.recent = recent  # 最近记忆管理对象
        self.semantic = semantic  # 语义搜索对象
        self.compress = compress  # 压缩管道对象
        self.config = config  # 内存配置对象
        self.maintainer = maintainer  # 记忆维护器（可选）

        self._interaction_count = 0  # 交互计数器，记录交互次数
        self._pending_messages: list[dict] = []  # 挂起的消息列表，存储待处理的消息字典
        self._pending_lock = threading.Lock()  # 保护 _pending_messages 的并发访问

    def record_tool_call(self, tool_name: str, arguments: dict,
                         result: str | None = None, duration_ms: float = 0) -> None:
        """记录工具调用

        Args:
            tool_name: 工具名称
            arguments: 调用参数
            result: 返回结果（截断）
            duration_ms: 耗时（毫秒）
        """
        if not self.config.auto_record:
            return

        # 排除不需要记录的工具
        if tool_name in self.config.exclude_tools:
            return

        if "tool" not in self.config.record_sources:
            return

        content = {
            "tool": tool_name,
            "args_summary": self._summarize_args(arguments),
            "result_summary": self._truncate(result, 200) if result else None,
            "duration_ms": round(duration_ms, 1),
        }

        self._record(
            content=json.dumps(content, ensure_ascii=False),
            source="tool",
            role="tool",
            metadata={"tool_name": tool_name},
        )

    def record_agent_response(self, content: str, session_id: str | None = None) -> None:
        """记录 Agent 回复"""
        if not self.config.auto_record:
            return
        if "agent" not in self.config.record_sources:
            return

        self._record(
            content=content,
            source="agent",
            role="assistant",
            session_id=session_id,
        )

    def record_user_input(self, content: str, session_id: str | None = None) -> None:
        """记录用户输入"""
        if not self.config.auto_record:
            return
        if "user" not in self.config.record_sources:
            return

        self._record(
            content=content,
            source="user",
            role="user",
            session_id=session_id,
        )

    def _record(self, content: str, source: str, role: str,
                session_id: str | None = None, metadata: dict | None = None) -> None:
        """内部记录方法"""
        # 写入 Recent 层
        self.recent.add(content, source=source, role=role, metadata=metadata)

        # 缓存待写入 SQLite 的消息
        with self._pending_lock:
            self._pending_messages.append({
                "content": content,
                "source": source,
                "role": role,
                "session_id": session_id,
                "metadata": metadata,
            })

        # 增加交互计数
        self._interaction_count += 1

        # 检查是否触发维护
        if self._interaction_count % self.config.maintenance_interval == 0:
            self._trigger_maintenance()

    def _trigger_maintenance(self) -> None:
        """触发记忆维护（同步部分）"""
        try:
            # 1. 保存待写入的消息到 SQLite
            self._flush_pending()

            # 2. 异步执行其余维护任务
            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(self._async_maintenance())
            except RuntimeError:
                # 没有运行中的事件循环，只做同步部分
                pass

        except Exception as e:
            logger.warning(f"记忆维护失败: {e}")

    async def _async_maintenance(self) -> None:
        """异步维护任务"""
        for task in self.config.maintenance_tasks:
            try:
                if task == "compress":
                    if self.compress.should_compress():
                        result = await self.compress.compress()
                        logger.info(f"记忆压缩: {result}")
                elif task == "cleanup_vectors":
                    self._cleanup_old_vectors()
                elif task == "update_bm25":
                    self.semantic.bm25.rebuild_stats()
                elif task == "maintain":
                    if self.maintainer and self.maintainer.should_run():
                        result = await self.maintainer.run()
                        logger.info(f"记忆维护器: aging={result.get('aging', {}).get('stale_count', 0)} "
                                    f"validate={result.get('validate', {})}")
            except Exception as e:
                logger.warning(f"维护任务 {task} 失败: {e}")

    def _flush_pending(self) -> int:
        """将缓存的消息写入 SQLite + 建立索引"""
        with self._pending_lock:
            if not self._pending_messages:
                return 0
            pending = self._pending_messages[:]
            self._pending_messages.clear()

        count = 0
        for msg in pending:
            try:
                msg_id = self.store.insert_message(
                    content=msg["content"],
                    source=msg["source"],
                    role=msg["role"],
                    session_id=msg.get("session_id"),
                    metadata=msg.get("metadata"),
                )
                # 建立语义索引
                self.semantic.index_message(msg_id, msg["content"])
                count += 1
            except Exception as e:
                logger.warning(f"消息写入失败: {e}")

        return count

    def _cleanup_old_vectors(self) -> int:
        """清理已压缩消息的向量索引

        T06：DELETE + commit 包入 _write_lock，避免并发写交叉。
        """
        # T06：包入 _write_lock
        with self.store._write_lock:
            cur = self.store.conn.execute("""
                DELETE FROM vector_index
                WHERE message_id IN (
                    SELECT id FROM messages WHERE compressed = 1
                )
            """)
            self.store.conn.commit()
            return cur.rowcount

    @staticmethod
    def _summarize_args(args: dict, max_len: int = 200) -> str:
        """摘要工具参数"""
        try:
            s = json.dumps(args, ensure_ascii=False)
            return s[:max_len] + "..." if len(s) > max_len else s
        except Exception:
            return str(args)[:max_len]

    @staticmethod
    def _truncate(text: str, max_len: int = 200) -> str:
        """截断文本"""
        if not text:
            return ""
        return text[:max_len] + "..." if len(text) > max_len else text

    @property
    def interaction_count(self) -> int:
        return self._interaction_count

    def force_flush(self) -> int:
        """强制保存所有待写入消息"""
        return self._flush_pending()
