"""Agent Guide 语义嵌入器 — 向量化补强关键词匹配

复用 server.memory.embeddings.EmbeddingEngine 本地 ONNX/ST 引擎（BAAI/bge-small-zh-v1.5，
512 维，免费无需 API key），对 GUIDE_REGISTRY 所有 task_type 摘要做语义嵌入并缓存，
提供 match(question) 接口返回按余弦相似度排序的 task_type 列表。

设计原则（仿 wisdom_embedder.py）：
- 容错：embedding 引擎不可用 / cache 读取失败 / numpy 未安装 等场景，全部返回空列表，
  让调用方（agent_guide.match_task_candidates）走关键词 fallback，**绝不抛异常**
- 幂等：cache 文件存在且 registry hash 未变 → 跳过重建
- 单例：同一进程内复用实例，避免重复加载
- 轻量：直接 new EmbeddingEngine，不走 MemoryManager（避免触发 SQLite/Recent 全套初始化）
- 动态：build_index 时实时读取 GUIDE_REGISTRY（含 workspace 可选组件运行时合并的条目），
  不在模块加载时一次性固化

集成点（agent_guide.match_task_candidates）：
- 关键词匹配完成后，若 top-1 是弱匹配（score < WEAK_MATCH_THRESHOLD 或 not strong_match）
  → 调本模块 match() 拿语义分数
  → semantic_bonus = cosine * 20（cosine 0.5 → +10，0.7 → +14）
  → cosine > 0.6 → 设 strong_match=True
  → matched_reasons 加 "semantic:{cosine:.2f}"

强匹配场景不触发本模块（性能优化 + 不影响现有测试）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("localagent.agent_guide_embedder")

# === 路径 ===
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = _PROJECT_ROOT / "data" / "agent_guide" / "embeddings_cache.json"

# === 配置 ===
EMBEDDING_ENABLED = True  # 可改为 False 完全禁用 embedding，方便调试
SUMMARY_MAX_CHARS = 500  # 每个摘要截取长度（模型 max_length=512 token ≈ 340 中文字）
DEFAULT_TOP_K = 10
EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# 语义强匹配阈值：cosine ≥ 此值视为语义强相关，可强制 strong_match=True
# 0.6 是经验值——同义改写（如"我今天都干了啥"vs"今日工作总结"）通常 0.4-0.6，
# 0.6+ 表示高度语义相关（同主题 + 同意图），可放心 strong_match
SEMANTIC_STRONG_THRESHOLD = 0.6


class AgentGuideEmbedder:
    """Agent Guide 语义嵌入器（单例）

    通过 EmbeddingEngine 对 GUIDE_REGISTRY 所有 task_type 摘要做嵌入并缓存，
    提供 match(question) 接口返回按余弦相似度排序的 task_type 列表。

    失败容忍：所有异常捕获并返回空列表，绝不抛异常（让调用方走关键词 fallback）。

    用法：
        embedder = AgentGuideEmbedder()
        results = embedder.match("我今天都干了啥，帮我捋一下时间线")
        # 失败时 results == []，调用方走关键词匹配
    """

    _instance: "AgentGuideEmbedder | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "AgentGuideEmbedder":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        self._entries: list[dict] = []  # [{"task_type": ..., "summary": ...}, ...]
        self._embeddings: list[list[float]] = []  # 与 _entries 一一对应
        self._embedding_engine: Any = None
        self._engine_tried: bool = False
        self._registry_hash: str = ""  # 上次构建时的 registry hash，用于检测变更

        if not EMBEDDING_ENABLED:
            logger.info("AgentGuideEmbedder: EMBEDDING_ENABLED=False，已禁用 embedding 匹配")
            return

    # ─── 从 GUIDE_REGISTRY 提取摘要 ─────────────────────────────

    def _load_registry_summaries(self) -> list[dict]:
        """从 GUIDE_REGISTRY 提取每个 task_type 的摘要文本。

        摘要构成：name + 触发关键词 + description + first_action
        （first_action 含丰富语义，如 daily_summary 的"确定今天日期、读 hourly、写 daily"）

        运行时动态读取 GUIDE_REGISTRY（含 workspace 可选组件合并的条目），
        不在模块加载时固化。
        """
        try:
            # 函数级 import 避免循环依赖（agent_guide 顶部 import 本模块会形成环）
            from server.agent_guide import GUIDE_REGISTRY
        except Exception as e:
            logger.warning("AgentGuideEmbedder: 导入 GUIDE_REGISTRY 失败: %s", e)
            return []

        entries: list[dict] = []
        for task_type, entry in GUIDE_REGISTRY.items():
            name = entry.get("name", task_type)
            keywords = entry.get("keywords", [])
            description = entry.get("description", "")
            first_action = entry.get("first_action", "")

            # 摘要文本：name + keywords + description + first_action
            kw_str = "、".join(keywords) if keywords else ""
            summary = (
                f"任务名：{name}\n"
                f"触发关键词：{kw_str}\n"
                f"描述：{description}\n"
                f"首步：{first_action}"
            )
            # 截断到模型 max_length 内
            summary = summary[:SUMMARY_MAX_CHARS]

            entries.append({
                "task_type": task_type,
                "name": name,
                "summary": summary,
            })

        return entries

    # ─── registry hash（cache 幂等校验）────────────────────

    def _compute_registry_hash(self) -> str:
        """对 registry entries 的 task_type + summary 算 md5，作为 cache 失效依据"""
        h = hashlib.md5()
        for e in self._entries:
            h.update(e["task_type"].encode("utf-8"))
            h.update(b"\x00")
            h.update(e["summary"].encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    # ─── 获取 EmbeddingEngine（懒加载）─────────────────────────

    def _get_embedding_engine(self) -> Any:
        """直接 new EmbeddingEngine 实例（不走 MemoryManager，避免全套初始化）

        失败返回 None。不主动调 initialize()（避免首次 match 卡很久下载模型），
        由 build_index() 显式调 initialize()。
        """
        if self._engine_tried:
            return self._embedding_engine
        self._engine_tried = True
        try:
            from server.config import get_models_config
            from server.memory.embeddings import EmbeddingEngine
            # 复用后端统一模型目录（[models] external_dir）
            cache_dir = get_models_config()["embeddings_dir"]
            self._embedding_engine = EmbeddingEngine(
                model_name=EMBEDDING_MODEL_NAME,
                cache_dir=cache_dir,
            )
        except Exception as e:
            logger.warning("AgentGuideEmbedder: 创建 EmbeddingEngine 失败: %s", e)
            self._embedding_engine = None
        return self._embedding_engine

    # ─── build_index ───────────────────────────────────────────

    def build_index(self) -> dict:
        """构建嵌入索引并写入 cache 文件

        幂等：若 cache 文件存在且 registry hash 未变 → 跳过重建，直接载入 cache 的向量。
        若 registry hash 变化（新增/修改 task_type）→ 重建。

        Returns:
            {"ok": bool, "indexed": int, "skipped": bool, "error": str}
        """
        # 每次都重新加载 registry summaries（workspace 可选组件可能动态增减）
        self._entries = self._load_registry_summaries()
        if not self._entries:
            return {"ok": False, "indexed": 0, "skipped": False, "error": "no registry entries"}

        registry_hash = self._compute_registry_hash()

        # 幂等：cache 存在且 hash 匹配 → 直接载入
        cached = self._load_cache()
        if cached and cached.get("registry_hash") == registry_hash:
            try:
                self._embeddings = [item["vector"] for item in cached.get("embeddings", [])]
                if len(self._embeddings) == len(self._entries):
                    self._registry_hash = registry_hash
                    logger.info(
                        "AgentGuideEmbedder: cache 命中（hash=%s...），跳过重建，共 %d 条",
                        registry_hash[:8], len(self._embeddings),
                    )
                    return {"ok": True, "indexed": 0, "skipped": True, "error": ""}
                else:
                    logger.warning(
                        "AgentGuideEmbedder: cache 条目数 %d != registry 数 %d，重建",
                        len(self._embeddings), len(self._entries),
                    )
                    self._embeddings = []
            except Exception as e:
                logger.warning("AgentGuideEmbedder: cache 载入失败: %s，重建", e)
                self._embeddings = []

        # 拿 embedding engine
        engine = self._get_embedding_engine()
        if engine is None:
            return {"ok": False, "indexed": 0, "skipped": False, "error": "embedding engine unavailable"}

        # engine 未就绪则尝试 initialize（首次构建时会触发模型加载）
        if not engine.ready:
            try:
                ok = engine.initialize()
                if not ok:
                    return {"ok": False, "indexed": 0, "skipped": False, "error": "embedding engine not ready"}
            except Exception as e:
                logger.warning("AgentGuideEmbedder: engine.initialize() 失败: %s", e)
                return {"ok": False, "indexed": 0, "skipped": False, "error": f"init failed: {e}"}

        # 批量嵌入
        try:
            summaries = [e["summary"] for e in self._entries]
            vecs = engine.embed(summaries)  # np.ndarray (N, dim)
            # 转 list[float]
            self._embeddings = [list(map(float, v)) for v in vecs]
        except Exception as e:
            logger.warning("AgentGuideEmbedder: embed 失败: %s", e)
            return {"ok": False, "indexed": 0, "skipped": False, "error": f"embed failed: {e}"}

        # 写 cache
        try:
            self._write_cache(registry_hash, getattr(engine, "model_name", EMBEDDING_MODEL_NAME))
            self._registry_hash = registry_hash
        except Exception as e:
            logger.warning("AgentGuideEmbedder: 写 cache 失败: %s", e)
            # cache 写失败不影响内存中的 embeddings

        logger.info("AgentGuideEmbedder: 构建完成，共 %d 条", len(self._embeddings))
        return {"ok": True, "indexed": len(self._embeddings), "skipped": False, "error": ""}

    def _load_cache(self) -> dict | None:
        """读取 cache 文件"""
        if not CACHE_PATH.exists():
            return None
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("AgentGuideEmbedder: 读 cache 失败: %s", e)
            return None

    def _write_cache(self, registry_hash: str, model_name: str) -> None:
        """写 cache 文件"""
        cache_data = {
            "version": "1.0",
            "model": model_name,
            "registry_hash": registry_hash,
            "embeddings": [
                {"task_type": e["task_type"], "name": e["name"], "summary": e["summary"], "vector": v}
                for e, v in zip(self._entries, self._embeddings, strict=False)
            ],
        }
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)

    # ─── match ────────────────────────────────────────────────

    def match(self, question: str, top_k: int = DEFAULT_TOP_K) -> list[tuple[str, float]]:
        """语义匹配，返回 [(task_type, cosine_similarity), ...] 按相似度降序

        失败返回空列表（让调用方走关键词 fallback）。
        """
        if not EMBEDDING_ENABLED:
            return []

        # 确保 embeddings 就绪（首次会触发 build_index → engine.initialize）
        if not self._embeddings:
            result = self.build_index()
            if not result.get("ok"):
                return []

        if not self._embeddings:
            return []

        # 嵌入 query
        engine = self._get_embedding_engine()
        if engine is None:
            return []
        # cache 命中时 build_index 会跳过 engine 初始化，这里兜底初始化
        if not engine.ready:
            try:
                if not engine.initialize():
                    return []
            except Exception as e:
                logger.warning("AgentGuideEmbedder: match() 中 engine.initialize() 失败: %s", e)
                return []

        try:
            query_vec = engine.embed([question])[0]  # np.ndarray (dim,)
        except Exception as e:
            logger.warning("AgentGuideEmbedder: query embed 失败: %s", e)
            return []

        # 计算余弦相似度（embeddings 已 L2 归一化，query 也需归一化）
        try:
            import numpy as np
            skill_matrix = np.array(self._embeddings, dtype=np.float32)
            q_norm = float(np.linalg.norm(query_vec))
            if q_norm < 1e-9:
                return []
            query_vec = query_vec / q_norm
            sims = skill_matrix @ query_vec  # (N,)

            # 按相似度降序取 top_k
            indices = np.argsort(-sims)[:top_k]
            return [(self._entries[i]["task_type"], float(sims[i])) for i in indices]
        except Exception as e:
            logger.warning("AgentGuideEmbedder: 相似度计算失败: %s", e)
            return []

    def match_or_fallback(self, question: str, top_k: int = DEFAULT_TOP_K) -> list[str]:
        """尝试 embedding 匹配，失败返回空列表（让调用方走关键词 fallback）"""
        try:
            results = self.match(question, top_k=top_k)
            return [task_type for task_type, _ in results]
        except Exception as e:
            logger.warning("AgentGuideEmbedder: match_or_fallback 异常: %s", e)
            return []


# === 模块级便捷函数（供 agent_guide.match_task_candidates 调用）===

_embedder_instance: AgentGuideEmbedder | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> AgentGuideEmbedder:
    """获取 AgentGuideEmbedder 单例（线程安全）"""
    global _embedder_instance
    if _embedder_instance is None:
        with _embedder_lock:
            if _embedder_instance is None:
                _embedder_instance = AgentGuideEmbedder()
    return _embedder_instance


def semantic_match(task: str, top_k: int = DEFAULT_TOP_K) -> list[tuple[str, float]]:
    """便捷函数：语义匹配，返回 [(task_type, cosine), ...]

    失败返回空列表，调用方走关键词 fallback。
    """
    return get_embedder().match(task, top_k=top_k)
