"""记忆压缩管线

将旧消息压缩为摘要，节省上下文窗口。
支持 LLM 压缩（需 API key）和规则压缩（无需 API key）两种模式。
"""

import json
import logging

from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class CompressionPipeline:
    """记忆压缩管线"""

    def __init__(self, store: MemoryStore, threshold: int = 200,
                 batch_size: int = 50):
        """初始化压缩管线。

        Args:
            store: MemoryStore 实例
            threshold: 触发批处理的阈值数量
            batch_size: 每次批处理的数据数量
        """
        self.store = store
        self.threshold = threshold
        self.batch_size = batch_size

    def should_compress(self) -> bool:
        """检查是否需要压缩"""
        return self.store.count_uncompressed() >= self.threshold

    async def compress(self, force: bool = False) -> dict:
        """执行压缩

        Returns:
            {"compressed": int, "summaries": int, "method": str}
        """
        uncompressed = self.store.count_uncompressed()
        if not force and uncompressed < self.threshold:
            return {"compressed": 0, "summaries": 0, "method": "skipped"}

        # 获取未压缩的消息
        messages = self.store.query_messages(compressed=0, limit=self.batch_size)
        if not messages:
            return {"compressed": 0, "summaries": 0, "method": "none"}

        # 按时间排序
        messages.sort(key=lambda m: m["timestamp"])

        # 尝试 LLM 压缩
        summary_text = await self._llm_compress(messages)
        used_llm = bool(summary_text)

        if not summary_text:
            # 降级到规则压缩
            summary_text = self._rule_compress(messages)

        if not summary_text:
            return {"compressed": 0, "summaries": 0, "method": "failed"}

        # 保存摘要
        self.store.insert_summary(
            start_time=messages[0]["timestamp"],
            end_time=messages[-1]["timestamp"],
            summary=summary_text,
            message_count=len(messages),
            model="memory_compress" if used_llm else "rule",
        )

        # 标记消息为已压缩
        msg_ids = [m["id"] for m in messages]
        self.store.mark_compressed(msg_ids)

        method = "llm" if used_llm else "rule"
        logger.info(f"压缩完成: {len(messages)} 条消息 → 1 条摘要 ({method})")

        return {"compressed": len(messages), "summaries": 1, "method": method}

    async def _llm_compress(self, messages: list[dict]) -> str | None:
        """使用 LLM 压缩消息

        v15：统一走 pool.call_simple(use_case="memory_compress", retries=1)。
        pool 内部已实现 model 级降级 / cooldown / 429 处理 / 统计记录，
        本函数无需再处理 OpenAI SDK 直连和 429 熔断逻辑。
        """
        try:
            from server.llm_pool.singleton import get_pool

            # 构建压缩 prompt
            conversation = []
            for msg in messages:
                role = msg.get("role", "assistant")
                source = msg.get("source", "agent")
                content = msg.get("content", "")
                conversation.append(f"[{source}/{role}] {content[:300]}")

            prompt = (
                "请将以下对话记录压缩为简洁的摘要，保留关键信息、决策和结论。"
                "忽略寒暄和重复内容。用中文输出，不超过200字。\n\n"
                "防幻觉规则：\n"
                "1. 只总结对话中实际出现的内容，不要编造未提及的信息、因果关系或结论\n"
                "2. 不确定时如实说明'对话中提及X但细节不明'\n"
                "3. 保留原文中的专有名词/名称，不要替换或衍生\n\n"
                + "\n".join(conversation)
            )

            system_prompt = "你是一个对话摘要助手。"

            # 同步调用 pool.call_simple（在线程池中避免阻塞事件循环）
            # asyncio.to_thread 不支持 kwargs → 用 lambda 包装
            import asyncio
            response = await asyncio.to_thread(
                lambda: get_pool().call_simple(
                    prompt,
                    system_prompt,
                    temperature=0.3,
                    max_tokens=16384,
                    retries=1,
                    use_case="memory_compress",
                )
            )
            return response

        except Exception as e:
            logger.debug(f"LLM 压缩失败: {e}")
            return None

    def _rule_compress(self, messages: list[dict]) -> str:
        """规则压缩（无需 API key 的降级方案）

        策略：
        1. 提取每条消息的关键句子
        2. 去重
        3. 拼接为摘要
        """
        key_points = []

        for msg in messages:
            content = msg.get("content", "")
            source = msg.get("source", "agent")
            role = msg.get("role", "assistant")

            # 跳过太短或太长的内容
            if len(content) < 5 or len(content) > 2000:
                continue

            # 提取关键信息
            if source == "tool" or role == "tool":
                # 工具调用结果：提取关键数据
                point = self._extract_tool_result(content)
                if point:
                    key_points.append(f"[工具] {point}")
            elif source == "user" or role == "user":
                # 用户消息：保留意图
                point = content[:100].strip()
                if point:
                    key_points.append(f"[用户] {point}")
            else:
                # Agent 回复：提取结论
                point = self._extract_conclusion(content)
                if point:
                    key_points.append(point)

        if not key_points:
            # 最终降级：只保留时间范围和消息数
            return f"({len(messages)}条交互记录，{messages[0].get('timestamp', '')} ~ {messages[-1].get('timestamp', '')})"

        # 去重
        seen = set()
        unique = []
        for p in key_points:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        # 限制总长度
        result = "；".join(unique[:20])
        if len(result) > 500:
            result = result[:500] + "..."

        return result

    def _extract_tool_result(self, content: str) -> str | None:
        """从工具调用结果中提取关键信息"""
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                # 提取 status/summary/result 等关键字段
                for key in ["summary", "result", "status", "error", "message"]:
                    if key in data:
                        val = str(data[key])[:100]
                        return val
                # 提取第一个非元数据字段
                for k, v in data.items():
                    if not k.startswith("_") and isinstance(v, (str, int, float, bool)):
                        return f"{k}: {str(v)[:80]}"
            elif isinstance(data, str):
                return data[:100]
        except (json.JSONDecodeError, TypeError):
            return content[:100].strip()
        return None

    def _extract_conclusion(self, content: str) -> str | None:
        """从文本中提取结论性语句"""
        # 寻找结论性关键词
        conclusion_markers = ["因此", "所以", "结论", "结果", "总结", "完成", "成功", "失败"]
        sentences = content.replace("。", "。\n").replace("！", "！\n").replace("？", "？\n").split("\n")

        for s in sentences:
            s = s.strip()
            if not s or len(s) < 5:
                continue
            for marker in conclusion_markers:
                if marker in s:
                    return s[:100]

        # 没有明确结论，取第一句
        for s in sentences:
            s = s.strip()
            if len(s) >= 10:
                return s[:100]

        return None
