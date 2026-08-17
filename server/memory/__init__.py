"""
三层记忆系统

架构：
  L1 Recent  — 近期对话滑动窗口（内存 + JSON 文件）
  L2 Time-indexed — SQLite 时间索引（原始消息 + 压缩摘要）
  L3 Semantic — 混合语义检索（向量嵌入 + BM25）

对外接口通过 router.py 暴露为 FastAPI 路由，完全向后兼容 /memory/* 端点。
"""

from server.memory.bm25 import BM25Index
from server.memory.compress import CompressionPipeline
from server.memory.config import MemoryConfig, get_memory_config
from server.memory.embeddings import EmbeddingEngine
from server.memory.evidence import EvidenceLedger
from server.memory.maintainer import MemoryMaintainer
from server.memory.manager import MemoryManager, get_memory_manager
from server.memory.recent import RecentMemory
from server.memory.recorder import InteractionRecorder
from server.memory.search_tracer import SearchTracer
from server.memory.semantic import SemanticSearch
from server.memory.store import MemoryStore

__all__ = [
    "MemoryConfig",
    "get_memory_config",
    "MemoryStore",
    "RecentMemory",
    "SemanticSearch",
    "EmbeddingEngine",
    "BM25Index",
    "CompressionPipeline",
    "InteractionRecorder",
    "MemoryMaintainer",
    "MemoryManager",
    "get_memory_manager",
    # v3 可观测性组件
    "SearchTracer",
    "EvidenceLedger",
]
