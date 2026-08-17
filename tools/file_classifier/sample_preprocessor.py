"""sample_preprocessor.py — F4 样本冲突查证（ticket J）

预测前样本预处理（已确认默认全开，对每次预测执行）：

1. **A2 多样性下采样**：每类送入 prompt 的样本上限 MAX_PER_CAT=25；
   超出时用 embedding 余弦相似度贪心分群（阈值 SIMILARITY_THRESHOLD），
   每群挑代表，保证送入 prompt 的样本彼此不同（不做随机抽样）。
   下限增强（A3）不做，MIN_PER_CAT 概念已删除。
2. **B 冲突检测**：最终样本集中，文件名余弦 ≥ 阈值但分属不同分类 → 冲突对。
   C2 提示式处理：冲突样本照常送入 prompt，另在 prompt 末尾列出冲突对，
   要求 LLM"相似文件名分到不同类时附 reason 并降 confidence"。
3. **跨会话借补**（会话内为主 + 跨会话补充）：某类本地样本不足 MAX_PER_CAT 时，
   从其它会话的同名分类借补凑满配额，优先借语义差异更大的样本（最远点采样）。
   借补样本必须标注来源目录（如「来自会话：微信文件夹」），prompt 中按来源分段展示。
4. **源目录上下文**：源目录仅作 prompt 文字信息（如「文件来自：下载」），
   不参与余弦计算（embedding 逻辑不变）。

模型：BAAI/bge-m3 本地 ONNX（multilingual）。真实模型目录由 localAgent 根
config.toml [models] external_dir 推导（external_dir + embeddings），读不到时
回退 <localAgent根>/weights/embeddings；bge-m3 的 ONNX 在 bge-m3/onnx 子目录，
model_dir 必须指向该子目录（指向根目录会走更重的 sentence-transformers 路径）。

全部功能 fail-soft：引擎不可用（无权重/依赖缺失）时降级为原始行为——
每类截断前 MAX_PER_CAT 条、无冲突检测、无借补，预测照常进行。
"""
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# ── 参数（阈值以参数形式暴露，不硬编码） ──
MAX_PER_CAT = 25                     # 每类送入 prompt 的样本上限
# 余弦阈值初值取 0.6（与 AgentGuide SEMANTIC_STRONG_THRESHOLD 一致），
# 但 2026-08-13 文件名单独实测：bge-m3 对不相关短文件名的基线相似度可达 ~0.67
#（如"设计稿.png"↔"年度总结.txt"=0.67），真正相似的文件名对 ≥0.82
#（"设计稿.png"↔"设计稿v2.png"=0.90）。按规格"最终取值按实际测试调整"，
# 默认值校准为 0.7，可在调用处以参数覆盖。
DEFAULT_SIMILARITY_THRESHOLD = 0.7
MAX_CONFLICT_PAIRS = 20              # prompt 中列出的冲突对上限（防 prompt 膨胀）
EMBED_MODEL_NAME = "BAAI/bge-m3"

# ── 引擎单例（懒加载；首次 initialize 约数秒，之后进程内复用） ──
_ENGINE = None
_ENGINE_INIT_TRIED = False


def get_localagent_root() -> str:
    """localAgent 根目录 = 本文件上两级（tools/file_classifier → localAgent）"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_embeddings_dir() -> str:
    """解析真实 embeddings 目录：config.toml [models] external_dir + /embeddings，
    读不到或目录不存在时回退 <localAgent根>/weights/embeddings"""
    root = get_localagent_root()
    try:
        import tomllib
        cfg_path = os.path.join(root, "config.toml")
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        ext = (cfg.get("models") or {}).get("external_dir") or ""
        if ext:
            ext_abs = ext if os.path.isabs(ext) else os.path.join(root, ext)
            cand = os.path.join(ext_abs, "embeddings")
            if os.path.isdir(cand):
                return cand
    except Exception as e:
        logger.warning(f"读取 config.toml external_dir 失败，回退默认目录: {e}")
    return os.path.join(root, "weights", "embeddings")


def get_embedding_engine():
    """懒加载 bge-m3 ONNX 引擎（进程内单例）；失败返回 None（fail-soft）"""
    global _ENGINE, _ENGINE_INIT_TRIED
    if _ENGINE is not None or _ENGINE_INIT_TRIED:
        return _ENGINE
    _ENGINE_INIT_TRIED = True
    try:
        from embeddings import EmbeddingEngine
        emb_dir = resolve_embeddings_dir()
        model_dir = os.path.join(emb_dir, "bge-m3", "onnx")
        # ONNX 在 bge-m3/onnx 子目录；缺失时不走引擎（避免触发 ST/torch 重路径或下载）
        if not os.path.exists(os.path.join(model_dir, "model.onnx")):
            logger.warning(f"bge-m3 ONNX 缺失: {model_dir}，样本预处理降级")
            return None
        engine = EmbeddingEngine(model_name=EMBED_MODEL_NAME, cache_dir=emb_dir)
        engine.model_dir = model_dir  # 覆盖派生路径，指向 onnx 子目录
        if not engine.initialize():
            logger.warning("bge-m3 引擎初始化失败，样本预处理降级")
            return None
        _ENGINE = engine
        logger.info(f"bge-m3 引擎就绪（dim={engine.dim}, dir={model_dir}）")
    except Exception as e:
        logger.warning(f"bge-m3 引擎加载异常，样本预处理降级: {e}")
    return _ENGINE


def reset_engine_cache():
    """测试钩子：重置单例状态"""
    global _ENGINE, _ENGINE_INIT_TRIED
    _ENGINE = None
    _ENGINE_INIT_TRIED = False


# ── 分群 / 多样性选取 ──

def greedy_cluster(emb: np.ndarray, threshold: float) -> list:
    """贪心余弦分群：按顺序把每个样本放入第一个与其代表相似度 ≥ threshold 的群，
    否则新开一群。返回 [{"rep": idx, "members": [idx, ...]}, ...]"""
    clusters = []
    for i in range(len(emb)):
        placed = False
        for c in clusters:
            if float(emb[i] @ emb[c["rep"]]) >= threshold:
                c["members"].append(i)
                placed = True
                break
        if not placed:
            clusters.append({"rep": i, "members": [i]})
    return clusters


def farthest_first_selection(emb: np.ndarray, count: int,
                             base_emb: np.ndarray = None) -> list:
    """最远点采样：从候选 emb 中选 count 个语义差异最大的行索引。

    base_emb 为已选定的参照集（如本地已有样本）：优先选与参照集差异大的候选。
    """
    n = len(emb)
    if count >= n:
        return list(range(n))
    if n == 0:
        return []

    # 每个候选与参照集的最大相似度（越小越远）；无参照集时为 -inf
    if base_emb is not None and len(base_emb) > 0:
        max_sim_to_base = (emb @ base_emb.T).max(axis=1)
    else:
        max_sim_to_base = np.full(n, -np.inf)

    selected = []
    forbidden = np.zeros(n, dtype=bool)
    # 首个种子：与参照集最远的候选（无参照集时取第 0 个）
    first = int(np.argmin(np.where(np.isinf(max_sim_to_base), 1.0, max_sim_to_base))) \
        if base_emb is not None and len(base_emb) > 0 else 0
    selected.append(first)
    forbidden[first] = True

    # 迭代：选"与已选集合最大相似度"最小者（最远点）
    cur = emb[first]
    while len(selected) < count:
        sims = emb @ cur
        max_sim_to_base = np.maximum(max_sim_to_base, sims)
        masked = np.where(forbidden, np.inf, max_sim_to_base)
        pick = int(np.argmin(masked))
        if np.isinf(masked[pick]):
            break  # 所有候选已选
        selected.append(pick)
        forbidden[pick] = True
        cur = emb[pick]
    return selected


def _group_indices_by_category(sample_files: list) -> dict:
    """按分类聚合样本下标（保持原顺序）"""
    grouped = {}
    for i, s in enumerate(sample_files):
        cat = s.get("category", "") if isinstance(s, dict) else ""
        if not cat:
            continue
        grouped.setdefault(cat, []).append(i)
    return grouped


def _truncate_by_category(sample_files: list, max_per_cat: int) -> list:
    """无引擎降级路径：每类保留前 max_per_cat 条"""
    grouped = _group_indices_by_category(sample_files)
    keep = set()
    for idxs in grouped.values():
        keep.update(idxs[:max_per_cat])
    return [s for i, s in enumerate(sample_files) if i in keep]


# ── 跨会话借补 ──

def collect_borrow_candidates(local_samples: list, other_sessions: list,
                              categories: list) -> dict:
    """为每个本地样本数不足 MAX_PER_CAT 的分类，从其它会话收集同名分类的候选样本

    Args:
        local_samples: 本地样本 [{"filename","category",...}]
        other_sessions: [{"session_id","title","source_dir","current_categories":{fn:cat}}]
        categories: 当前会话分类配置（限定借补范围为本会话存在的分类）

    Returns:
        {category: [{"filename","category","_source": "来自会话：<标题>"}]}
    """
    if not other_sessions:
        return {}
    cat_names = {c.get("name", "") for c in categories if isinstance(c, dict)}
    grouped = _group_indices_by_category(local_samples)
    local_names = {s.get("filename", "") for s in local_samples if isinstance(s, dict)}

    candidates = {}
    for sess in other_sessions:
        cc = sess.get("current_categories") or {}
        if not cc:
            continue
        label = (sess.get("title")
                 or os.path.basename(os.path.normpath(sess.get("source_dir", "")))
                 or sess.get("session_id") or "未知会话")
        for fn, cat in cc.items():
            # 仅借补本地已有样本的分类（会话内为主，F4-A-cross）；
            # 本地零样本的分类不引入外来约定
            if (cat not in cat_names or cat not in grouped
                    or not fn or fn in local_names):
                continue
            deficit = MAX_PER_CAT - len(grouped.get(cat, [])) - len(candidates.get(cat, []))
            if deficit <= 0:
                continue
            candidates.setdefault(cat, []).append(
                {"filename": fn, "category": cat, "_source": f"来自会话：{label}"})
    return candidates


# ── 冲突检测 ──

def detect_conflicts(samples: list, emb: np.ndarray, threshold: float,
                     max_pairs: int = MAX_CONFLICT_PAIRS) -> list:
    """跨分类冲突检测：余弦 ≥ threshold 且分属不同分类的样本对

    Returns:
        [{"file_a","category_a","file_b","category_b","similarity"}]，按相似度降序，截断 max_pairs
    """
    conflicts = []
    n = len(samples)
    for i in range(n):
        for j in range(i + 1, n):
            if samples[i].get("category") != samples[j].get("category"):
                sim = float(emb[i] @ emb[j])
                if sim >= threshold:
                    conflicts.append({
                        "file_a": samples[i].get("filename", ""),
                        "category_a": samples[i].get("category", ""),
                        "file_b": samples[j].get("filename", ""),
                        "category_b": samples[j].get("category", ""),
                        "similarity": round(sim, 4),
                    })
    conflicts.sort(key=lambda c: -c["similarity"])
    return conflicts[:max_pairs]


# ── 主入口 ──

def preprocess_samples(sample_files: list, categories: list,
                       source_dir: str = "",
                       other_sessions: list = None,
                       similarity_threshold: float = None,
                       max_per_cat: int = None,
                       engine=None) -> dict:
    """预测前样本预处理（默认全开；引擎不可用时 fail-soft 降级）

    Args:
        sample_files: 本地（当前会话）手动分类样本 [{"filename","category","size"?}]
        categories: 当前会话分类配置
        source_dir: 当前会话源目录（仅写入 prompt 文字上下文）
        other_sessions: 其它会话摘要（跨会话借补用）
        similarity_threshold: 余弦阈值（None → DEFAULT_SIMILARITY_THRESHOLD）
        max_per_cat: 每类样本上限（None → MAX_PER_CAT）
        engine: 可注入引擎（测试用；None 时走单例）

    Returns:
        {
            "samples": [...],        # 最终样本（可能带 _source 标注；内部字段）
            "conflicts": [...],      # 冲突对列表
            "source_context": str,   # "文件来自：xxx" 或 ""
            "engine_used": bool,
            "borrowed_count": int,
            "_emb": np.ndarray|None, # 最终样本对应向量（供预测冲突标记复用）
        }
    """
    threshold = (DEFAULT_SIMILARITY_THRESHOLD if similarity_threshold is None
                 else float(similarity_threshold))
    cap = int(max_per_cat or MAX_PER_CAT)

    result = {
        "samples": list(sample_files or []),
        "conflicts": [],
        "source_context": "",
        "engine_used": False,
        "borrowed_count": 0,
        "_emb": None,
    }
    if source_dir:
        base = os.path.basename(os.path.normpath(source_dir)) or source_dir
        result["source_context"] = f"文件来自：{base}"

    if not sample_files:
        return result

    eng = engine if engine is not None else get_embedding_engine()
    if eng is None or not getattr(eng, "ready", False):
        # 降级：每类截断，无冲突检测/借补
        result["samples"] = _truncate_by_category(sample_files, cap)
        return result

    try:
        # 1) 本地样本向量化
        local_names = [s.get("filename", "") for s in sample_files]
        emb_local = np.asarray(eng.embed(local_names), dtype=np.float32)

        # 2) A2 多样性下采样（每类独立分群取代表）
        grouped = _group_indices_by_category(sample_files)
        chosen_idxs = []
        for cat, idxs in grouped.items():
            if len(idxs) <= cap:
                chosen_idxs.extend(idxs)
                continue
            sub_emb = emb_local[idxs]
            clusters = greedy_cluster(sub_emb, threshold)
            reps = [idxs[c["rep"]] for c in clusters]
            if len(reps) > cap:
                rep_emb = emb_local[reps]
                picks = farthest_first_selection(rep_emb, cap)
                reps = [reps[p] for p in picks]
            chosen_idxs.extend(reps)
        chosen_idxs = sorted(set(chosen_idxs))
        chosen = [dict(sample_files[i]) for i in chosen_idxs]
        chosen_emb = emb_local[chosen_idxs] if chosen_idxs else np.zeros((0, emb_local.shape[1]), dtype=np.float32)

        # 3) 跨会话借补（凑满配额，优先语义差异大的，标注来源）
        borrowed = []
        borrowed_emb_rows = []
        candidates_by_cat = collect_borrow_candidates(chosen, other_sessions or [], categories)
        for cat, cands in candidates_by_cat.items():
            if not cands:
                continue
            local_cat_idx = [i for i, s in enumerate(chosen) if s.get("category") == cat]
            deficit = cap - len(local_cat_idx)
            if deficit <= 0:
                continue
            cand_emb = np.asarray(eng.embed([c["filename"] for c in cands]), dtype=np.float32)
            base_emb = chosen_emb[local_cat_idx] if local_cat_idx else None
            picks = farthest_first_selection(cand_emb, deficit, base_emb=base_emb)
            for p in picks:
                borrowed.append(dict(cands[p]))
                borrowed_emb_rows.append(cand_emb[p])

        final_samples = chosen + borrowed
        if borrowed_emb_rows:
            final_emb = np.vstack([chosen_emb, np.vstack(borrowed_emb_rows)]) \
                if len(chosen_emb) else np.vstack(borrowed_emb_rows)
        else:
            final_emb = chosen_emb

        # 4) B 冲突检测（在最终样本集上）
        conflicts = detect_conflicts(final_samples, final_emb, threshold) if len(final_samples) >= 2 else []

        result.update({
            "samples": final_samples,
            "conflicts": conflicts,
            "engine_used": True,
            "borrowed_count": len(borrowed),
            "_emb": final_emb,
        })
    except Exception as e:
        # 预处理任何异常都不阻塞预测主流程
        logger.warning(f"样本预处理失败，降级原始样本: {e}")
        result["samples"] = list(sample_files)
        result["conflicts"] = []
        result["_emb"] = None
        result["engine_used"] = False
    return result


def flag_prediction_conflicts(predictions: list, final_samples: list,
                              final_emb: np.ndarray, engine=None,
                              similarity_threshold: float = None) -> None:
    """对 LLM 预测结果标记冲突（R1 高亮依据）：
    预测文件与某样本余弦 ≥ 阈值且分类不同 → pred["conflict"]=True + conflict_with

    就地修改 predictions；引擎不可用/final_emb 缺失时静默跳过。
    """
    if not predictions or final_samples is None or not len(final_samples):
        return
    eng = engine if engine is not None else get_embedding_engine()
    if eng is None or not getattr(eng, "ready", False):
        return
    threshold = (DEFAULT_SIMILARITY_THRESHOLD if similarity_threshold is None
                 else float(similarity_threshold))
    try:
        names = [p.get("filename", "") for p in predictions]
        emb = np.asarray(eng.embed(names), dtype=np.float32)
        sims = emb @ final_emb.T
        for i, pred in enumerate(predictions):
            row = sims[i]
            order = np.argsort(-row)
            for j in order:
                if row[j] < threshold:
                    break
                if final_samples[j].get("category") != pred.get("category"):
                    pred["conflict"] = True
                    pred["conflict_with"] = final_samples[j].get("filename", "")
                    break
    except Exception as e:
        logger.warning(f"预测冲突标记失败（不影响预测结果）: {e}")
