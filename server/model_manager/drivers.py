"""Model Lifecycle Manager — 驱动适配器（design §5）

驱动只是"适配器"：把管理器统一接口翻译到各模块既有单例，不改变模块内部实现。
所有对模块单例的引用都走函数内懒导入，避免注册链上的循环依赖。

当前驱动：
- OcrDriver（T2）：PaddleOCR，GPU，可逐出（唯一 GPU 模型）
- MemoryEmbeddingDriver（T3）：记忆系统 embedding，CPU，默认钉住
- GuideEmbeddingDriver（T3）：agent_guide embedder，CPU，默认钉住
- MindForgeSearcherDriver（T4 追加）：MindForge 搜索引擎，CPU，可逐出
"""

from __future__ import annotations

import logging
from typing import Literal

from server.model_manager.types import LoadFailureError

logger = logging.getLogger("localagent.model_manager.drivers")


class OcrDriver:
    """PaddleOCR 驱动（design §5.1）。

    load → models.get_ocr()（含管理器准入门控，拒绝时抛 ModelUnavailableError）；
    unload → 增强卸载（排空 + del + gc + empty_cache + forced 延迟补释放）；
    loaded_at/load_duration_ms → ocr.py 现成数据源（四驱动中唯一有）。
    """

    model_id = "ocr"
    footprint_mb = 600        # 实测稳态 ~484-597MiB（design §2 盘点）
    priority = 80             # 核心能力，高保留意愿（当前唯一 GPU 模型）
    reload_cost_sec = 27.0    # 冷启动估计（热重载 ~2s，取保守值）
    evictable = True

    def _models(self):
        from server.ocr import models
        return models

    @property
    def resource(self) -> Literal["gpu", "cpu"]:
        """动态资源归属：跟随 OCR 当前实际设备（server/ocr.py:_decide_device 决策）。

        降级到 CPU 后：
        1. of_resource("gpu") 不再包含 OCR → GPU 逐出器不会误选（否则会死循环：
           卸载 CPU 版释放不了显存 → 水位不降 → 继续 refusing → 再次降级 → 再被逐出）；
        2. check_admission 走 cpu monitor（默认 disabled → monitor_disabled 放行），
           降级后的 OCR 能正常加载。
        """
        return self._models().active_device

    def is_loaded(self) -> bool:
        return self._models().ocr_loaded

    def load(self) -> None:
        # get_ocr 内部已 admit("ocr")；加载失败异常原样上抛（管理器记失败）
        self._models().get_ocr()

    def unload(self) -> None:
        result = self._models().unload_ocr()
        if result.get("forced"):
            logger.warning("OcrDriver 卸载为强制模式（排空超时，延迟补释放已安排）")

    def in_flight(self) -> int:
        return self._models().in_flight_count()

    def loaded_at(self) -> float | None:
        return self._models()._loaded_at

    def load_duration_ms(self) -> int | None:
        return self._models()._load_elapsed_ms


# ============================================================================
# EmbeddingDriver ×2（design §5.2，T3）
# ============================================================================

# 共享参数（两实例 footprint 相同 ~90MB，reload 秒级；优先级 memory > guide，因
# memory 在每个 MCP 请求路径上，guide 仅在关键词弱匹配时才用）
_EMBEDDING_FOOTPRINT_MB = 90
_EMBEDDING_RELOAD_COST_SEC = 2.0
_MEMORY_EMBEDDING_PRIORITY = 80    # 同 OCR，热路径
_GUIDE_EMBEDDING_PRIORITY = 70     # 仅弱匹配时触发，可稍低


class _BaseEmbeddingDriver:
    """EmbeddingDriver 共享骨架（design §5.2）

    通用约束：
    - resource="cpu"，evictable=False（默认钉住，design §5.2 — 逐出收益为负：
      记忆 embedding 在每个 MCP 请求路径上，逐出后必然立即被下一请求拉回，
      且卸载窗口内写入的消息永久缺失向量索引）
    - 懒绑定：每次调用经动态解引用拿 EmbeddingEngine 实例；目标未创建时
      is_loaded()=False、load() 抛 LoadFailureError、unload() 无操作（不假成功）
    - initialize() 返回 False（吞异常路径，embeddings.py:71）→ LoadFailureError
    - in_flight 恒 0（无排空能力，协议允许；竞态由 embed() 局部快照兜底）
    - loaded_at/load_duration_ms 恒 None（无现成数据源，design §5.5 可空）
    """

    model_id: str = ""  # 子类覆盖；base 声明以满足 ModelDriver 协议（management 层读取）
    resource: Literal["gpu", "cpu"] = "cpu"
    evictable = False
    footprint_mb = _EMBEDDING_FOOTPRINT_MB
    reload_cost_sec = _EMBEDDING_RELOAD_COST_SEC

    def _engine(self):
        """子类实现：返回当前 EmbeddingEngine 实例，未创建返回 None（不触发懒创建）"""
        raise NotImplementedError

    def _engine_for_load(self):
        """子类实现：load 路径用的解引用，允许触发懒创建/初始化（返回 None 视为失败）"""
        raise NotImplementedError

    def is_loaded(self) -> bool:
        engine = self._engine()
        return engine is not None and engine.ready

    def load(self) -> None:
        engine = self._engine_for_load()
        if engine is None:
            # 目标未创建 → 不 no-op 假成功（design §5.2）
            raise LoadFailureError(
                f"{self.model_id}: embedding engine unavailable (target not initialized)"
            )
        ok = engine.initialize()
        if not ok:
            # EmbeddingEngine.initialize() 吞异常返回 False（embeddings.py:71）→
            # 必须转 LoadFailureError，否则管理器无法进入冷却/降级（design §7）
            raise LoadFailureError(
                f"{self.model_id}: EmbeddingEngine.initialize() returned False"
            )

    def unload(self) -> None:
        engine = self._engine()
        if engine is None:
            # 目标未创建：无资源可释放；is_loaded 已 False，manual_unload 走
            # already_unloaded 分支，evictor 也不会选中（is_loaded=False）→ 安全 no-op
            return
        engine.unload()

    def in_flight(self) -> int:
        return 0

    def loaded_at(self) -> float | None:
        return None

    def load_duration_ms(self) -> int | None:
        return None


class MemoryEmbeddingDriver(_BaseEmbeddingDriver):
    """记忆系统 embedding 驱动（design §5.2）

    适配 `server.memory.manager.MemoryManager.embedding`（EmbeddingEngine 实例）。
    该模型是热路径模型：每个 `/mcp` tools/call 都会经 recorder → semantic → embed
    （middleware.py:123-166；recorder.py:190）。默认钉住，仅手动卸载可达。
    """

    model_id = "memory_embedding"
    priority = _MEMORY_EMBEDDING_PRIORITY

    def _engine(self):
        """读 MemoryManager._instance.embedding（不触发 get_memory_manager 重初始化）

        get_memory_manager() 首次调用会触发 SQLite + recent + embedding 全套初始化，
        对 is_loaded() 这种每采样周期都调用的查询代价过高。直接 peek _instance。
        """
        import server.memory.manager as mm

        with mm._instance_lock:
            return mm._instance.embedding if mm._instance is not None else None

    def _engine_for_load(self):
        """load 路径：经 get_memory_manager() 确保单例已创建并初始化

        手动 load 是显式动作，触发 MemoryManager 全套初始化是可接受的（且
        lifecycle 启动时已调用过，正常情况下是幂等 no-op）。
        """
        from server.memory.manager import get_memory_manager

        try:
            mgr = get_memory_manager()
        except Exception as e:
            logger.warning(f"{self.model_id}: MemoryManager 初始化失败: {e}")
            return None
        return mgr.embedding


class GuideEmbeddingDriver(_BaseEmbeddingDriver):
    """agent_guide embedder 驱动（design §5.2）

    适配 `server.agent_guide_embedder.AgentGuideEmbedder._embedding_engine`。
    与 MemoryEmbeddingDriver 共享同一 ONNX 模型文件却各持一份 session（~90MB×2
    冗余，design §12 已记录合并机会，本期不合并）。

    重载廉价：embeddings_cache.json（964KB）幂等，registry hash 不变时跳过重建。
    """

    model_id = "guide_embedding"
    priority = _GUIDE_EMBEDDING_PRIORITY

    def _embedder(self):
        """读类级单例 AgentGuideEmbedder._instance（不触发模块级 get_embedder 创建）

        AgentGuideEmbedder 用 __new__ 做类级单例 + 模块级 get_embedder 双层，
        两者返回同一对象。直接读类级 _instance 避免触发模块级缓存的副作用。
        """
        from server.agent_guide_embedder import AgentGuideEmbedder

        return AgentGuideEmbedder._instance

    def _engine(self):
        """读 ._embedding_engine（可能为 None：首次 match 前 _get_embedding_engine 未调用）"""
        embedder = self._embedder()
        if embedder is None:
            return None
        return embedder._embedding_engine

    def _engine_for_load(self):
        """load 路径：经 get_embedder()._get_embedding_engine() 触发懒创建"""
        from server.agent_guide_embedder import get_embedder

        try:
            embedder = get_embedder()
            # _get_embedding_engine 首次调用会尝试创建 EmbeddingEngine（不下载模型，
            # 仅 new 实例）；失败返回 None 并设 _engine_tried=True 阻止重试
            return embedder._get_embedding_engine()
        except Exception as e:
            logger.warning(f"{self.model_id}: AgentGuideEmbedder 引擎获取失败: {e}")
            return None


# ============================================================================
# MindForgeSearcherDriver（design §5.3，T4）
# ============================================================================


class MindForgeSearcherDriver:
    """MindForge 搜索引擎驱动（design §5.3）

    适配 `server.mindforge.search_engine`（SearchEngineManager 单例）。
    HybridSearcher 加载 FAISS 索引 + sentence-transformers 模型，首次约 10-30s，
    稳态 ~1-2GB。索引 mtime 变化时 SearchEngineManager 自动重载（逻辑保持不变）。

    load() 无参协议与 `get_searcher(index_dir)` 必填参数的冲突解决（design §5.3）：
    - unload() 前自留 `last_index_dir` 快照（unload 会清空 _index_dir，mindforge.py:126）
    - load() 优先用 last_index_dir；从未加载过则复刻 /mindforge/preload 解析逻辑
      （_resolve_mindforge_dir() + kb_search/index_store + meta.json 存在性检查）
    - mindforge 未启用/索引缺失 → LoadFailureError（结构化错误，不抛裸异常）

    已知局限（design §5.3/§16）：搜索端点持局部引用，unload() 只删属性，1-2GB
    实际要等在途搜索结束才释放；本期不实现 in_flight 计数（恒 0）。

    自然热路径恢复不受影响：/mindforge/search 每次从配置重算 index_dir 传入
    （mindforge.py:482-487），get_searcher 检测 _searcher is None 自动重载——
    "下次真实请求"自愈成立，管理器只介入主动预热/逐出分支。
    """

    model_id = "mindforge_searcher"
    resource: Literal["gpu", "cpu"] = "cpu"
    footprint_mb = 1500          # 实测 1-2GB（design §2 盘点）
    priority = 30                # 低：可容忍冷启动 10-30s，逐出性价比最高
    reload_cost_sec = 20.0        # 冷启动估计（10-30s 取中值）
    evictable = True              # 与 embedding 不同，可逐出

    def __init__(self):
        self._last_index_dir = None  # unload 前的快照，供下次 load 复用

    def _search_engine(self):
        from server.mindforge import search_engine
        return search_engine

    def is_loaded(self) -> bool:
        return self._search_engine().loaded

    def load(self) -> None:
        from pathlib import Path

        from server.mindforge import _resolve_mindforge_dir, search_engine

        # 1. 优先用 last_index_dir 快照
        index_dir = self._last_index_dir
        if index_dir is None:
            # 2. 从未加载过 → 复刻 /mindforge/preload 的解析逻辑
            mf_dir = _resolve_mindforge_dir()
            if mf_dir is None:
                raise LoadFailureError(
                    f"{self.model_id}: MindForge 项目目录不存在或未配置"
                )
            index_dir = mf_dir / "kb_search" / "index_store"
            if not (index_dir / "meta.json").exists():
                raise LoadFailureError(
                    f"{self.model_id}: 知识库索引不存在，请先运行 build-index"
                )

        # get_searcher 同步加载（10-30s 冷启动）；管理器在 asyncio.to_thread 里调
        try:
            search_engine.get_searcher(Path(index_dir))
        except Exception as e:
            # 加载失败不清 last_index_dir（下次仍可尝试同一路径或重新解析）
            raise LoadFailureError(f"{self.model_id}: 加载失败: {e}") from e
        # 加载成功后更新快照（get_searcher 内部可能因 mtime 变化重载，路径不变）
        self._last_index_dir = Path(search_engine.index_dir) if search_engine.index_dir else Path(index_dir)

    def unload(self) -> None:
        se = self._search_engine()
        # unload 会清空 _index_dir，先快照（仅在已加载时更新，避免 None 覆盖）
        if se.index_dir is not None:
            from pathlib import Path
            self._last_index_dir = Path(se.index_dir)
        se.unload()

    def in_flight(self) -> int:
        # 已知局限：无 in_flight 计数（design §5.3，可选改进记录在案）
        return 0

    def loaded_at(self) -> float | None:
        return None

    def load_duration_ms(self) -> int | None:
        # SearchEngineManager 当前不暴露耗时字段，本期不补
        return None

