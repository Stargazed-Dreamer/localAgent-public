"""记忆系统配置"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import toml

logger = logging.getLogger(__name__)


@dataclass
class MemoryConfig:
    """三层记忆系统配置"""

    # --- 基础路径 ---
    db_path: str = "data/memory/memory.db"
    recent_dir: str = "data/memory/recent"

    # --- Recent 层 ---
    recent_window_size: int = 50  # 滑动窗口保留最近 N 条消息
    recent_save_interval: int = 5  # 每 N 条消息保存一次到文件

    # --- Time-indexed 层 ---
    compress_threshold: int = 200  # 超过 N 条未压缩消息时触发压缩
    compress_batch_size: int = 50  # 每次压缩处理 N 条消息

    # --- Semantic 层 ---
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    embedding_cache_dir: str = "weights/embeddings"
    hf_mirror: str = ""  # HuggingFace 镜像源，如 "https://hf-mirror.com"
    enable_semantic: bool = True  # 可关闭语义检索（无模型时自动降级）
    bm25_top_k: int = 10
    vector_top_k: int = 10
    hybrid_alpha: float = 0.5  # 向量检索权重（1-alpha 为 BM25 权重）

    # --- 自动记录 ---
    auto_record: bool = True  # 是否自动记录 agent 交互
    record_sources: list = field(default_factory=lambda: ["tool", "agent", "user"])
    exclude_tools: list = field(default_factory=lambda: [
        "memory_status", "memory_list", "memory_get", "memory_set", "memory_delete",
        "mcp_stats", "exec_status",
    ])

    # --- 触发器 ---
    maintenance_interval: int = 10  # 每 N 次 agent 交互触发一次维护
    maintenance_tasks: list = field(default_factory=lambda: [
        "compress", "cleanup_vectors", "update_bm25", "maintain",
    ])

    # --- 维护器 (maintainer) ---
    maintain_interval_hours: int = 6          # 维护器运行间隔（小时）
    maintain_stale_days: int = 7              # 超过 N 天未更新视为 stale
    maintain_enable_validate: bool = True     # 是否启用 LLM 验证 stale 记忆

    # --- 清理 ---
    orphan_cleanup_days: int = 30  # 孤立记忆清理阈值（已压缩消息）
    summary_cleanup_days: int = 180  # 旧摘要清理阈值
    max_db_size_mb: int = 500  # 数据库最大大小（MB）

    # --- Evidence Ledger (A) ---
    enable_evidence_ledger: bool = True        # 是否启用证据组织层
    evidence_top_k: int = 10                   # 返回给 agent 的证据数量
    evidence_corroboration_threshold: float = 0.7  # Jaccard 相似度阈值，超过视为互相佐证
    evidence_conflict_window_minutes: int = 5  # 冲突检测时间窗口（分钟）
    evidence_audit_keep_days: int = 30         # evidence_ledger 表保留天数

    # --- Search Tracer (C) ---
    enable_search_tracer: bool = True           # 是否启用检索可观测性
    search_tracer_retention_days: int = 30     # search_traces 表保留天数
    search_tracer_sample_rate: float = 1.0     # 采样率 0-1，1.0=全采样
    search_tracer_candidates_to_store: int = 20  # 每次 trace 存储前 N 条候选


def get_memory_config() -> MemoryConfig:
    """从 config.toml 加载记忆配置，缺失字段使用默认值"""
    config_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
    cfg = MemoryConfig()

    if not os.path.exists(config_path):
        return cfg

    try:
        data = toml.load(config_path)
        mem = data.get("memory", {})

        if "db_path" in mem:
            cfg.db_path = mem["db_path"]
        if "recent_dir" in mem:
            cfg.recent_dir = mem["recent_dir"]
        if "recent_window_size" in mem:
            cfg.recent_window_size = int(mem["recent_window_size"])
        if "compress_threshold" in mem:
            cfg.compress_threshold = int(mem["compress_threshold"])
        if "embedding_model" in mem:
            cfg.embedding_model = mem["embedding_model"]
        if "enable_semantic" in mem:
            cfg.enable_semantic = bool(mem["enable_semantic"])
        if "hf_mirror" in mem:
            cfg.hf_mirror = mem["hf_mirror"]
        if "hybrid_alpha" in mem:
            cfg.hybrid_alpha = float(mem["hybrid_alpha"])
        if "auto_record" in mem:
            cfg.auto_record = bool(mem["auto_record"])
        if "maintenance_interval" in mem:
            cfg.maintenance_interval = int(mem["maintenance_interval"])
        if "orphan_cleanup_days" in mem:
            cfg.orphan_cleanup_days = int(mem["orphan_cleanup_days"])
        if "summary_cleanup_days" in mem:
            cfg.summary_cleanup_days = int(mem["summary_cleanup_days"])
        if "max_db_size_mb" in mem:
            cfg.max_db_size_mb = int(mem["max_db_size_mb"])

        # 维护器子配置 [memory.maintain]
        maintain = mem.get("maintain", {})
        if "interval_hours" in maintain:
            cfg.maintain_interval_hours = int(maintain["interval_hours"])
        if "stale_days" in maintain:
            cfg.maintain_stale_days = int(maintain["stale_days"])
        if "enable_validate" in maintain:
            cfg.maintain_enable_validate = bool(maintain["enable_validate"])

        # Evidence Ledger 子配置（A）
        if "enable_evidence_ledger" in mem:
            cfg.enable_evidence_ledger = bool(mem["enable_evidence_ledger"])
        if "evidence_top_k" in mem:
            cfg.evidence_top_k = int(mem["evidence_top_k"])
        if "evidence_corroboration_threshold" in mem:
            cfg.evidence_corroboration_threshold = float(mem["evidence_corroboration_threshold"])
        if "evidence_conflict_window_minutes" in mem:
            cfg.evidence_conflict_window_minutes = int(mem["evidence_conflict_window_minutes"])
        if "evidence_audit_keep_days" in mem:
            cfg.evidence_audit_keep_days = int(mem["evidence_audit_keep_days"])

        # Search Tracer 子配置（C）
        if "enable_search_tracer" in mem:
            cfg.enable_search_tracer = bool(mem["enable_search_tracer"])
        if "search_tracer_retention_days" in mem:
            cfg.search_tracer_retention_days = int(mem["search_tracer_retention_days"])
        if "search_tracer_sample_rate" in mem:
            cfg.search_tracer_sample_rate = float(mem["search_tracer_sample_rate"])
        if "search_tracer_candidates_to_store" in mem:
            cfg.search_tracer_candidates_to_store = int(mem["search_tracer_candidates_to_store"])
    except Exception as e:
        logger.warning(f"加载 config.toml [memory] 失败，使用默认配置: {e}")

    # 环境变量覆盖（测试隔离用）：LOCALAGENT_MEMORY_DB_PATH / LOCALAGENT_MEMORY_RECENT_DIR
    # 优先级高于 config.toml，确保测试进程可重定向到临时 DB，不污染生产 memory.db
    env_db_path = os.environ.get("LOCALAGENT_MEMORY_DB_PATH", "").strip()
    if env_db_path:
        cfg.db_path = env_db_path
    env_recent_dir = os.environ.get("LOCALAGENT_MEMORY_RECENT_DIR", "").strip()
    if env_recent_dir:
        cfg.recent_dir = env_recent_dir

    # 统一模型路径：如果 [models] external_dir 已设置，覆盖 embedding_cache_dir
    # 这样发布时 external_dir 留空则回退到 weights/embeddings，个人部署则用 <data_drive>:\ai_models\embeddings
    try:
        from server.config import load_config
        models = load_config().get("models", {})
        external_dir = str(models.get("external_dir", "")).strip()
        if external_dir:
            cfg.embedding_cache_dir = os.path.join(external_dir, "embeddings")
    except Exception:
        pass

    return cfg
