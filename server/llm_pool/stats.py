"""per-project token 统计持久化路径解析"""

from pathlib import Path

try:
    from server.config import get_llm_storage_config
except Exception:
    get_llm_storage_config = None

# 项目根目录（stats.py 在 server/llm_pool/，需 3 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_default_stats_file() -> str:
    """从 [llm.storage] 配置读取默认 stats 文件路径，失败时 fallback 到硬编码。

    相对路径相对于项目根目录解析，不依赖 cwd。
    """
    stats_dir = "data/llm/stats"
    if get_llm_storage_config is not None:
        try:
            stats_dir = get_llm_storage_config().get("stats_dir", stats_dir)
        except Exception:
            pass
    # 相对路径相对于项目根目录解析
    p = Path(stats_dir)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return str(p / "llm_pool_stats.json")
