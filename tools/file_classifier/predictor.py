"""文件分类预测器 - 调用后端 LLM 池预测文件归属分类

通过 HTTP 调用后端 /llm/pool/call-simple 端点，将用户手动分类的样例 + 全部文件信息
发给 LLM，让其预测每个文件应该归入哪个分类，并给出置信度。

当后端不可用时，自动降级为基于扩展名的规则匹配。

用法:
    from predictor import predict_categories
    predictions = predict_categories(sample_files, all_files, categories)
"""
import json
import os
import re

import requests

# 后端 LLM 池端点
BACKEND_URL = "http://127.0.0.1:8766"
LLM_CALL_ENDPOINT = f"{BACKEND_URL}/llm/pool/call-simple"

# 每批最多处理的文件数（避免单次 prompt 过长）
BATCH_SIZE = 50

# 项目标签（用于 per-project token 统计）
PROJECT_TAG = "file_classifier"


def check_backend_available(timeout: int = 5) -> bool:
    """检查后端 LLM 池是否可用"""
    try:
        r = requests.get(f"{BACKEND_URL}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _render_sample_sections(sample_files: list) -> str:
    """样本按来源分段展示（ticket J）：本会话样本在前，借补样本按来源分段

    样本条目可带内部字段 "_source"（借补标注，如 "来自会话：微信文件夹"），
    渲染时剥离所有下划线前缀的内部字段。
    """
    local = [s for s in sample_files if not s.get("_source")]
    borrowed_groups = {}
    for s in sample_files:
        src = s.get("_source")
        if src:
            borrowed_groups.setdefault(src, []).append(s)

    def _dump(entries: list) -> str:
        clean = [{k: v for k, v in e.items() if not str(k).startswith("_")}
                 for e in entries]
        return json.dumps(clean, ensure_ascii=False, indent=2)

    sections = []
    if local:
        sections.append(f"我手动分类的样例（本会话）:\n{_dump(local)}")
    for src, entries in borrowed_groups.items():
        sections.append(
            f"借补参考样例（{src}）——这些样例来自其它文件夹，文件名可能与本会话"
            f"同名不同源，请结合上下文判断:\n{_dump(entries)}")
    if not sections:
        sections.append("（无参考样例）")
    return "\n\n".join(sections)


def _render_conflict_section(conflicts: list) -> str:
    """冲突对提示段（ticket J F4-C2 提示式）"""
    if not conflicts:
        return ""
    lines = [
        f'- "{c["file_a"]}"（{c["category_a"]}）↔ "{c["file_b"]}"'
        f'（{c["category_b"]}）相似度 {c["similarity"]}'
        for c in conflicts
    ]
    return (
        "\n\n【相似文件名冲突提醒】以下样例对的文件名高度相似但被分到了不同分类:\n"
        + "\n".join(lines)
        + "\n预测时若将形态相似的文件名分到不同分类，必须在 reason 字段附上理由说明，"
          "并适当降低 confidence。"
    )


def _build_prompt(sample_files: list, all_files: list, categories: list,
                  content_map: dict | None = None,
                  conflicts: list = None, source_context: str = "") -> str:
    """构建发送给 LLM 的提示词

    Args:
        sample_files: 样例 [{"filename": ..., "category": ..., "_source"?: ...}, ...]
            带 "_source" 的为跨会话借补样本（ticket J），按来源分段展示
        all_files: 待预测的文件列表 [{"filename": ..., "size": ...}, ...]
        categories: 分类名称列表 ["文档", "图片", ...]
        content_map: 可选，文件名 → 内容摘要。若提供，对应文件条目会附加 "content" 字段
        conflicts: 可选，冲突对列表（ticket J）——prompt 末尾列出，要求 LLM 附 reason 降 confidence
        source_context: 可选，源目录文字上下文（如 "文件来自：下载"，ticket J F4-loc）
    """
    sample_sections = _render_sample_sections(sample_files)
    # 若有内容摘要，注入到文件条目中
    files_with_content = []
    for f in all_files:
        entry = dict(f) if isinstance(f, dict) else {"filename": f, "size": 0}
        fname = entry.get("filename", "")
        if content_map and fname in content_map and content_map[fname]:
            entry["content"] = content_map[fname]
        files_with_content.append(entry)
    files_json = json.dumps(files_with_content, ensure_ascii=False, indent=2)
    categories_str = ", ".join(categories)

    source_line = f"待预测文件{source_context}（仅作背景信息）。\n" if source_context else ""
    conflict_section = _render_conflict_section(conflicts or [])

    prompt = f"""你是一个文件分类助手。{source_line}
{sample_sections}

请预测以下文件应该归入哪个分类，返回JSON数组。部分文件提供了 content 字段（文件内容摘要），请结合文件名和内容综合判断：
{files_json}

分类选项: {categories_str}

返回格式: [{{"filename": "xxx", "category": "文档", "confidence": 0.9, "reason": "可选，仅冲突时需要"}}, ...]
只返回JSON，不要其他文字。{conflict_section}"""
    return prompt


def _parse_llm_response(content: str, expected_files: list) -> list:
    """解析 LLM 返回的 JSON 预测结果

    Args:
        content: LLM 返回的文本
        expected_files: 期望预测的文件名列表（用于校验和补全）

    Returns:
        预测结果列表 [{"filename": ..., "category": ..., "confidence": ...}, ...]
    """
    if not content:
        return []

    # 尝试从文本中提取 JSON 数组（LLM 可能包裹在 markdown 代码块中）
    json_str = content.strip()

    # 去除可能的 markdown 代码块标记
    if json_str.startswith("```"):
        # 移除开头的 ```json 或 ```
        json_str = re.sub(r"^```(?:json)?\s*\n?", "", json_str)
        json_str = re.sub(r"\n?```\s*$", "", json_str)

    # 尝试找到第一个 [ 到最后一个 ]
    start = json_str.find("[")
    end = json_str.rfind("]")
    if start != -1 and end != -1 and end > start:
        json_str = json_str[start:end + 1]

    try:
        predictions = json.loads(json_str)
        if not isinstance(predictions, list):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # 校验并规范化每条预测
    valid_categories_set = set()
    result = []
    seen_files = set()

    for pred in predictions:
        if not isinstance(pred, dict):
            continue
        filename = pred.get("filename")
        category = pred.get("category")
        confidence = pred.get("confidence", 0.5)

        if not filename or not category:
            continue

        # 置信度规范化到 [0, 1]
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5

        # ticket J：冲突场景 LLM 会附 reason，保留供审核窗口展示
        entry = {
            "filename": filename,
            "category": category,
            "confidence": confidence,
        }
        reason = pred.get("reason")
        if reason:
            entry["reason"] = str(reason)
        result.append(entry)
        seen_files.add(filename)
        valid_categories_set.add(category)

    # 对 LLM 漏掉的文件补全为 "其他"（低置信度）
    for f in expected_files:
        fname = f if isinstance(f, str) else f.get("filename")
        if fname and fname not in seen_files:
            result.append({
                "filename": fname,
                "category": "其他",
                "confidence": 0.1,
            })

    return result


def _rule_based_predict(files: list, categories: list) -> list:
    """基于扩展名的规则匹配降级方案（后端不可用时使用）

    Args:
        files: 文件列表 [{"filename": ..., "size": ...}, ...]
        categories: 分类配置列表 [{"name": ..., "extensions": [...]}, ...]
    """
    # 构建扩展名 -> 分类名 映射
    ext_map = {}
    for cat in categories:
        for ext in cat.get("extensions", []):
            ext_map[ext.lower()] = cat["name"]

    result = []
    for f in files:
        fname = f["filename"] if isinstance(f, dict) else f
        ext = os.path.splitext(fname)[1].lower()
        category = ext_map.get(ext, "其他")
        # 规则匹配给中等置信度
        confidence = 0.7 if ext and category != "其他" else 0.3
        result.append({
            "filename": fname,
            "category": category,
            "confidence": confidence,
        })
    return result


def _call_llm(prompt: str, timeout: int = 180, model: str | None = None) -> str | None:
    """调用后端 LLM 池（简化版）

    Args:
        prompt: 提示词
        timeout: HTTP 超时秒数
        model: 指定模型（如 "ZhipuAI/GLM-5.2"），None 时走 default tier

    Returns:
        LLM 生成的文本，失败返回 None
    """
    payload = {
        "prompt": prompt,
        "temperature": 0.2,  # 分类任务用低温度保证稳定
        "max_tokens": 8192,
        "timeout": 120,
        "retries": 3,
        "project": PROJECT_TAG,
        "use_case": "download_watcher",  # 敏感用途：处理用户下载文件内容，排除免费 key
    }
    if model:
        payload["model"] = model
    try:
        r = requests.post(LLM_CALL_ENDPOINT, json=payload, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("ok"):
            return data.get("content", "")
        return None
    except Exception:
        return None


def predict_categories(sample_files: list, all_files: list, categories: list,
                       use_llm: bool = True,
                       progress_callback=None,
                       model: str | None = None,
                       content_map: dict | None = None,
                       item_callback=None,
                       should_stop=None,
                       source_dir: str = "",
                       other_sessions: list = None,
                       similarity_threshold: float = None,
                       max_per_cat: int = None) -> list:
    """预测文件归属分类

    Args:
        sample_files: 用户手动分类的样例 [{"filename": ..., "category": ..., "size": ...}, ...]
        all_files: 待预测的文件列表 [{"filename": ..., "size": ...}, ...]
        categories: 分类配置列表 [{"name": ..., "extensions": [...], "path": ...}, ...]
        use_llm: 是否使用 LLM 预测（False 则只用规则匹配）
        progress_callback: 进度回调函数 (current, total, message) -> None
        model: 指定模型（如 "ZhipuAI/GLM-5.2"），None 时走 default tier
        content_map: 可选，文件名 → 内容摘要，注入到 prompt 供 LLM 综合判断
        item_callback: 可选，单条预测完成回调 (pred_dict) -> None（ticket F 流式审核：
            每确定一条预测立即回调，供 GUI 实时渲染到审核窗口）
        should_stop: 可选，中断检查函数 () -> bool（ticket F：返回 True 时尽快退出，
            已产出的预测照常返回）
        source_dir: 可选，当前会话源目录（ticket J：仅作 prompt 文字上下文，不参与余弦）
        other_sessions: 可选，其它会话摘要列表（ticket J 跨会话样本借补）
            [{"session_id","title","source_dir","current_categories":{fn:cat}}]
        similarity_threshold: 可选，余弦相似度阈值（ticket J：None → 默认 0.6）
        max_per_cat: 可选，每类送入 prompt 的样本上限（None → 默认 25）

    Returns:
        预测结果列表 [{"filename": ..., "category": ..., "confidence": ...}, ...]
    """
    if not all_files:
        return []

    def _stopped() -> bool:
        return bool(should_stop and should_stop())

    def _emit(pred: dict) -> None:
        if item_callback:
            try:
                item_callback(pred)
            except Exception:
                pass  # GUI 回调异常不影响预测主流程

    category_names = [c["name"] for c in categories]

    # 后端不可用或主动禁用 LLM 时，使用规则匹配
    if not use_llm or not check_backend_available():
        if progress_callback:
            progress_callback(0, len(all_files), "后端不可用，使用规则匹配...")
        rule_preds = _rule_based_predict(all_files, categories)
        for pred in rule_preds:
            if _stopped():
                break
            _emit(pred)
        return rule_preds

    # 分批处理准备
    total = len(all_files)
    all_predictions = []

    # ticket J：F4 样本预处理（多样性选取 + 跨会话借补 + 冲突检测；fail-soft）
    if progress_callback:
        progress_callback(0, total, "样本预处理（F4：多样性选取/冲突检测）...")
    try:
        from sample_preprocessor import preprocess_samples
        prep = preprocess_samples(sample_files, categories,
                                  source_dir=source_dir,
                                  other_sessions=other_sessions,
                                  similarity_threshold=similarity_threshold,
                                  max_per_cat=max_per_cat)
    except Exception:
        prep = {"samples": list(sample_files), "conflicts": [],
                "source_context": "", "engine_used": False, "_emb": None}
    processed_samples = prep.get("samples") or list(sample_files)
    conflicts = prep.get("conflicts") or []
    source_context = prep.get("source_context", "")

    # 分批处理
    for i in range(0, total, BATCH_SIZE):
        if _stopped():
            break
        batch = all_files[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        if progress_callback:
            progress_callback(i, total, f"正在预测第 {batch_num}/{total_batches} 批...")

        prompt = _build_prompt(processed_samples, batch, category_names,
                               content_map=content_map,
                               conflicts=conflicts, source_context=source_context)
        content = _call_llm(prompt, model=model)

        if content:
            batch_predictions = _parse_llm_response(content, batch)
            # ticket J：预测冲突标记（R1 高亮依据；失败静默跳过不影响结果）
            if prep.get("_emb") is not None and batch_predictions:
                try:
                    from sample_preprocessor import flag_prediction_conflicts
                    flag_prediction_conflicts(batch_predictions, processed_samples,
                                              prep["_emb"],
                                              similarity_threshold=similarity_threshold)
                except Exception:
                    pass
        else:
            # LLM 调用失败，该批降级为规则匹配
            batch_predictions = _rule_based_predict(batch, categories)

        # ticket F：逐条流式回调（批内也检查中断）
        for pred in batch_predictions:
            if _stopped():
                break
            _emit(pred)

        all_predictions.extend(batch_predictions)

    if progress_callback and not _stopped():
        progress_callback(total, total, "预测完成")

    return all_predictions


def load_categories(config_path: str) -> list:
    """从 JSON 文件加载分类配置

    Args:
        config_path: categories.json 路径

    Returns:
        分类配置列表
    """
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("categories", [])


def save_categories(config_path: str, categories: list) -> None:
    """保存分类配置到 JSON 文件

    Args:
        config_path: categories.json 路径
        categories: 分类配置列表
    """
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"categories": categories}, f, ensure_ascii=False, indent=2)


def save_sample(sample_path: str, sample_files: list, categories: list,
                source_dir: str = "") -> None:
    """保存样例配置到 JSON 文件（供未来复用）

    Args:
        sample_path: 样例文件路径
        sample_files: 样例文件列表
        categories: 分类配置
        source_dir: 源目录
    """
    data = {
        "source_dir": source_dir,
        "categories": categories,
        "sample_files": sample_files,
    }
    with open(sample_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sample(sample_path: str) -> dict:
    """加载样例配置

    Returns:
        {"source_dir": ..., "categories": ..., "sample_files": ...}
    """
    with open(sample_path, encoding="utf-8") as f:
        return json.load(f)


# ==================== disposition 扩展（供 Loop 下载监控调用）====================

# 模糊文件名模式：信息不足，需用户审查
_VAGUE_PATTERNS = [
    r"新建文件夹",
    r"未命名",
    r"Untitled",
    r"无标题",
    r"\(\d+\)",      # (1), (2) 等自动编号
    r"^新建\b",
    r"^Screenshot",  # 截图自动命名
]


def _is_vague_filename(filename: str) -> bool:
    """判断文件名是否信息不足（模糊）"""
    return any(re.search(p, filename, re.IGNORECASE) for p in _VAGUE_PATTERNS)


def classify_with_disposition(sample_files: list, all_files: list, categories: list,
                               user_preferences: dict = None,
                               model: str | None = None,
                               content_map: dict | None = None) -> list:
    """预测文件分类并附加 disposition 字段（供 Loop 下载监控调用）

    在 predict_categories 基础上，为每个预测附加 disposition：
        - "move":    置信度 ≥ 0.85 且该分类有非空 path（用户配置了移动目标）
        - "inspect": 置信度 ≥ 0.5 但无 path 或文件名模糊，需用户审查
        - "unknown": 置信度 < 0.5，无法分类

    Args:
        sample_files: 用户手动分类样例（Loop 自动触发时可为空）
        all_files: 待预测文件列表 [{"filename": ..., "size": ...}, ...]
        categories: 分类配置 [{"name", "extensions", "path"}, ...]
        user_preferences: 用户历史归类习惯（category name → path），覆盖 categories 的 path
        model: 指定模型（如 "ZhipuAI/GLM-5.2"），None 时走 default tier
        content_map: 可选，文件名 → 内容摘要，注入到 prompt 供 LLM 综合判断（Phase 2 用）

    Returns:
        预测结果列表 [{"filename", "category", "confidence", "disposition", "target_path"?}, ...]
    """
    predictions = predict_categories(sample_files, all_files, categories,
                                     model=model, content_map=content_map)

    # 构建 category name → path 映射
    cat_path_map = {c["name"]: c.get("path", "") for c in categories}
    if user_preferences and isinstance(user_preferences, dict):
        for k, v in user_preferences.items():
            if isinstance(v, str):
                cat_path_map[k] = v

    for pred in predictions:
        conf = pred.get("confidence", 0)
        cat = pred.get("category", "")
        fname = pred.get("filename", "")
        path = cat_path_map.get(cat, "")
        is_vague = _is_vague_filename(fname)

        if conf < 0.5:
            pred["disposition"] = "unknown"
        elif conf >= 0.85 and path and not is_vague:
            pred["disposition"] = "move"
            pred["target_path"] = path
        else:
            pred["disposition"] = "inspect"

    return predictions


# ==================== 文件夹分类协议（类交互式无状态，ticket 06）====================

# 协议参数上限
FOLDER_MAX_CALLS = 15                # 最大调用次数
FOLDER_MAX_FILES = 1500              # 累计最大文件数
FOLDER_MAX_FILENAME_LEN = 150        # 单文件名最大字符
FOLDER_MAX_NEED_MORE = 10            # 单次请求文件夹数上限
FOLDER_TOKEN_BUDGET = 180_000        # token 预算监控阈值
FOLDER_PROMPT_HARD_LIMIT = 195_000   # 单次 prompt 硬上限（字符数近似 token）

# 文件名采样上限（每层文件夹最多采样的文件名数）
FOLDER_SAMPLE_PER_LEVEL = 30


def truncate_filename(name: str, max_len: int = FOLDER_MAX_FILENAME_LEN) -> str:
    """截断超长文件名，附加截断提示

    Args:
        name: 原始文件名
        max_len: 最大字符数（默认 150）

    Returns:
        截断后的文件名（超长时末尾加 "…(截断)"）
    """
    if not name or len(name) <= max_len:
        return name
    return name[:max_len] + "…(截断)"


def collect_folder_info(folder_path: str,
                        max_files: int = FOLDER_MAX_FILES,
                        sample_per_level: int = FOLDER_SAMPLE_PER_LEVEL) -> dict:
    """递归收集文件夹信息（供 LLM 分类）

    纯逻辑函数，无 LLM 依赖，便于单元测试。

    Args:
        folder_path: 文件夹路径
        max_files: 累计文件数上限
        sample_per_level: 每层文件夹最多采样的文件名数

    Returns:
        dict: {
            "folder_name": str,          # 根文件夹名
            "file_count": int,           # 累计文件数
            "ext_distribution": dict,    # {ext_lower: count}
            "samples": list[str],        # 文件名采样（含相对路径，截断后）
            "truncated": bool,           # 是否因达上限截断
            "error": str | None,
        }
    """
    result = {
        "folder_name": os.path.basename(folder_path.rstrip(os.sep)) or folder_path,
        "file_count": 0,
        "ext_distribution": {},
        "samples": [],
        "truncated": False,
        "error": None,
    }
    if not folder_path or not os.path.isdir(folder_path):
        result["error"] = "路径不是文件夹或不存在"
        return result

    ext_counter = {}
    try:
        for root, dirs, filenames in os.walk(folder_path):
            # 计算相对路径前缀
            rel_root = os.path.relpath(root, folder_path)
            if rel_root == ".":
                rel_prefix = ""
            else:
                rel_prefix = rel_root + os.sep
            # 每层只采样前 N 个文件名
            sampled_count = 0
            for fname in filenames:
                if result["file_count"] >= max_files:
                    result["truncated"] = True
                    break
                result["file_count"] += 1
                # 扩展名分布
                _, ext = os.path.splitext(fname)
                ext_lower = ext.lower().lstrip(".")
                ext_counter[ext_lower] = ext_counter.get(ext_lower, 0) + 1
                # 采样（每层最多 sample_per_level 个）
                if sampled_count < sample_per_level:
                    display_name = truncate_filename(rel_prefix + fname)
                    result["samples"].append(display_name)
                    sampled_count += 1
            if result["truncated"]:
                break
    except OSError as e:
        result["error"] = str(e)

    result["ext_distribution"] = ext_counter
    return result


def build_folder_prompt(folder_name: str, folder_info: dict, categories: list,
                        call_count: int, need_more_paths: list = None) -> str:
    """构建文件夹分类 prompt（无状态，每次都重新构建）

    Args:
        folder_name: 根文件夹名
        folder_info: collect_folder_info 返回的 dict
        categories: 分类配置列表
        call_count: 当前调用次数（1-based）
        need_more_paths: 上次 LLM 请求补全的子文件夹路径列表（用于在 prompt 中提示）

    Returns:
        prompt 字符串
    """
    cat_names = [c["name"] if isinstance(c, dict) else c for c in categories]
    cat_str = ", ".join(cat_names)

    # 扩展名分布格式化（按 count 降序）
    ext_items = sorted(folder_info["ext_distribution"].items(),
                       key=lambda x: (-x[1], x[0]))
    ext_str = ", ".join(f"{e or '无扩展名'}: {c}" for e, c in ext_items[:15])
    if not ext_str:
        ext_str = "（无文件）"

    samples_str = "\n".join(f"  - {s}" for s in folder_info["samples"][:30])
    if not samples_str:
        samples_str = "  （无文件）"

    need_more_hint = ""
    if need_more_paths:
        need_more_hint = f"\n\n注意：你上次请求了以下子文件夹的详细信息，现已补全：{', '.join(need_more_paths)}"

    prompt = f"""你是一个文件夹分类助手。请根据文件夹名、扩展名分布、文件名采样预测这个文件夹应该归入哪个分类。

文件夹名: {folder_name}
文件数: {folder_info['file_count']}
扩展名分布: {ext_str}
文件名采样（含相对路径）:
{samples_str}

调用次数: {call_count}/{FOLDER_MAX_CALLS}
分类选项: {cat_str}{need_more_hint}

返回 JSON: {{"classification": "分类名", "confidence": 0.0-1.0, "need_more": ["相对子文件夹路径"], "reason": "理由"}}
- need_more 为空数组表示已有足够信息分类
- need_more 非空时列出需要查看内容的子文件夹相对路径（最多 {FOLDER_MAX_NEED_MORE} 个）
- 如果调用次数已达上限或信息不足，请直接给出最佳猜测分类或返回 "未知"
只返回 JSON，不要其他文字。"""
    return prompt


def parse_folder_response(content: str) -> dict:
    """解析 LLM 文件夹分类响应

    Returns:
        dict: {
            "classification": str,
            "confidence": float,
            "need_more": list[str],
            "reason": str,
            "valid": bool,  # 是否成功解析
        }
    """
    result = {
        "classification": "",
        "confidence": 0.0,
        "need_more": [],
        "reason": "",
        "valid": False,
    }
    if not content:
        return result

    json_str = content.strip()
    # 去除 markdown 代码块
    if json_str.startswith("```"):
        json_str = re.sub(r"^```(?:json)?\s*\n?", "", json_str)
        json_str = re.sub(r"\n?```\s*$", "", json_str)
    # 提取第一个 { 到最后一个 }
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start != -1 and end != -1 and end > start:
        json_str = json_str[start:end + 1]
    else:
        return result

    try:
        data = json.loads(json_str)
        if not isinstance(data, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        return result

    result["classification"] = str(data.get("classification", "")).strip()
    try:
        result["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        result["confidence"] = 0.0

    need_more = data.get("need_more", [])
    if isinstance(need_more, list):
        # 截断到 FOLDER_MAX_NEED_MORE 个，过滤非字符串
        result["need_more"] = [str(p) for p in need_more if p][:FOLDER_MAX_NEED_MORE]

    result["reason"] = str(data.get("reason", "")).strip()
    result["valid"] = bool(result["classification"])
    return result


def _rule_based_folder_predict(folder_info: dict, categories: list) -> dict:
    """基于扩展名分布的文件夹分类降级方案

    取出现次数最多的扩展名对应的分类作为文件夹分类。
    """
    if not categories or not folder_info["ext_distribution"]:
        return {
            "classification": "未知",
            "confidence": 0.1,
            "need_more": [],
            "reason": "无扩展名分布或无分类配置",
            "valid": True,
        }

    # 构建 ext → category 映射
    ext_map = {}
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        for ext in cat.get("extensions", []):
            ext_map[ext.lower()] = cat["name"]

    # 找出能匹配到分类的、count 最高的扩展名
    best_cat = ""
    best_count = 0
    for ext, count in folder_info["ext_distribution"].items():
        cat = ext_map.get(ext)
        if cat and count > best_count:
            best_cat = cat
            best_count = count

    if best_cat:
        total = sum(folder_info["ext_distribution"].values())
        confidence = min(0.8, best_count / max(total, 1) + 0.3)
        return {
            "classification": best_cat,
            "confidence": confidence,
            "need_more": [],
            "reason": f"规则匹配：{best_count} 个文件匹配 {best_cat}",
            "valid": True,
        }
    return {
        "classification": "未知",
        "confidence": 0.2,
        "need_more": [],
        "reason": "无扩展名匹配到分类",
        "valid": True,
    }


def classify_folder(folder_path: str, categories: list,
                    llm_caller=None,
                    model: str | None = None,
                    progress_callback=None) -> dict:
    """文件夹分类主入口（类交互式无状态协议）

    协议流程：
    1. collect_folder_info 收集根文件夹信息
    2. build_folder_prompt 构建 prompt（含调用次数计数）
    3. llm_caller 调用 LLM
    4. parse_folder_response 解析响应
    5. 若 need_more 非空且未达上限：补全子文件夹信息，重新调用（调用次数+1）
    6. 达上限或无 need_more：返回最终分类

    Args:
        folder_path: 文件夹路径
        categories: 分类配置列表
        llm_caller: 可注入的 LLM 调用函数 (prompt, model) -> content_str|None。
                    默认用 _call_llm。测试时可传 mock。
        model: 指定模型
        progress_callback: 进度回调 (call_count, max_calls, message) -> None

    Returns:
        dict: {
            "classification": str,
            "confidence": float,
            "reason": str,
            "calls_made": int,        # 实际调用次数
            "degraded": bool,         # 是否降级（LLM 不可用或达上限）
            "degrade_reason": str,    # 降级原因
            "folder_info": dict,      # collect_folder_info 结果
        }
    """
    if llm_caller is None:
        llm_caller = _call_llm

    # 1. 收集根文件夹信息
    folder_info = collect_folder_info(folder_path)
    if folder_info["error"]:
        return {
            "classification": "未知",
            "confidence": 0.0,
            "reason": folder_info["error"],
            "calls_made": 0,
            "degraded": True,
            "degrade_reason": "collect_folder_info 失败",
            "folder_info": folder_info,
        }

    folder_name = folder_info["folder_name"]
    call_count = 0
    cumulative_chars = 0  # 累计 prompt 字符数（近似 token）
    need_more_paths = []

    while call_count < FOLDER_MAX_CALLS:
        call_count += 1

        # 2. 构建 prompt
        prompt = build_folder_prompt(
            folder_name, folder_info, categories, call_count, need_more_paths
        )

        # 检查单次 prompt 硬上限
        if len(prompt) > FOLDER_PROMPT_HARD_LIMIT:
            # 截断 prompt
            prompt = prompt[:FOLDER_PROMPT_HARD_LIMIT]

        cumulative_chars += len(prompt)

        if progress_callback:
            progress_callback(call_count, FOLDER_MAX_CALLS,
                              f"文件夹分类第 {call_count}/{FOLDER_MAX_CALLS} 次调用")

        # 3. 调用 LLM
        content = llm_caller(prompt, model=model) if model else llm_caller(prompt)

        # 4. LLM 不可用 → 规则匹配降级
        if content is None:
            rule_result = _rule_based_folder_predict(folder_info, categories)
            return {
                "classification": rule_result["classification"],
                "confidence": rule_result["confidence"],
                "reason": f"LLM 不可用，降级规则匹配：{rule_result['reason']}",
                "calls_made": call_count,
                "degraded": True,
                "degrade_reason": "llm_unavailable",
                "folder_info": folder_info,
            }

        # 5. 解析响应
        parsed = parse_folder_response(content)
        if not parsed["valid"]:
            # 解析失败，重试一次（不递增 call_count，因为本次无效）
            continue

        # 6. 检查 need_more
        need_more = parsed["need_more"]
        # token 预算检查：达 180k 强制停止扩展
        if cumulative_chars >= FOLDER_TOKEN_BUDGET:
            if progress_callback:
                progress_callback(call_count, FOLDER_MAX_CALLS, "token 预算达上限，停止扩展")
            return {
                "classification": parsed["classification"],
                "confidence": parsed["confidence"] * 0.7,  # 降低置信度
                "reason": f"token 预算达上限强制分类：{parsed['reason']}",
                "calls_made": call_count,
                "degraded": True,
                "degrade_reason": "token_budget_exceeded",
                "folder_info": folder_info,
            }

        if not need_more:
            # 无需更多信息，返回最终分类
            return {
                "classification": parsed["classification"],
                "confidence": parsed["confidence"],
                "reason": parsed["reason"],
                "calls_made": call_count,
                "degraded": False,
                "degrade_reason": "",
                "folder_info": folder_info,
            }

        # need_more 非空：补全子文件夹信息后重新调用
        # 限制单次请求文件夹数
        need_more_paths = need_more[:FOLDER_MAX_NEED_MORE]
        # 合并子文件夹信息到 folder_info（追加 samples 和 ext_distribution）
        for sub_path in need_more_paths:
            sub_full = os.path.join(folder_path, sub_path)
            sub_info = collect_folder_info(sub_full, max_files=FOLDER_MAX_FILES - folder_info["file_count"])
            if sub_info["error"]:
                continue
            # 追加采样（带子路径前缀）
            for s in sub_info["samples"][:10]:
                folder_info["samples"].append(f"{sub_path}/{s}")
            # 合并扩展名分布
            for ext, cnt in sub_info["ext_distribution"].items():
                folder_info["ext_distribution"][ext] = \
                    folder_info["ext_distribution"].get(ext, 0) + cnt
            folder_info["file_count"] += sub_info["file_count"]
            if folder_info["file_count"] >= FOLDER_MAX_FILES:
                folder_info["truncated"] = True
                break

    # 达调用次数上限
    if progress_callback:
        progress_callback(call_count, FOLDER_MAX_CALLS, "调用次数达上限，标记未知")
    return {
        "classification": "未知",
        "confidence": 0.1,
        "reason": f"调用次数达上限 {FOLDER_MAX_CALLS} 仍未确定分类",
        "calls_made": call_count,
        "degraded": True,
        "degrade_reason": "max_calls_exceeded",
        "folder_info": folder_info,
    }

