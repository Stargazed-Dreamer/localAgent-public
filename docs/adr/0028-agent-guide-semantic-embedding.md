# Agent Guide 语义向量化匹配补强关键词

Agent Guide 关键词匹配对口语化查询（如"我今天都干了啥，帮我捋一下时间线"）仅得分 6（未达 strong_match 阈值 15），因 daily_summary 的 keywords 无"干了啥/捋/时间线"等口语变体。加口语化关键词易误判（用户明确拒绝此方向）。决策：复用 `EmbeddingEngine`（BAAI/bge-small-zh-v1.5，本地 ONNX/ST）对 GUIDE_REGISTRY 所有 task_type 摘要做语义嵌入并缓存（`agent_guide_embedder.py`），在 `match_task_candidates` 弱匹配时用余弦相似度加分（cosine×20），双条件 strong_match（cosine≥0.6 OR score≥15 AND cosine≥0.4），空匹配时向量化兜底。替代方案：① 纯关键词加口语化（易误判，如"保存一下"误匹配"试一下"）；② 纯向量化替代关键词（首次加载模型 3-5s 延迟，且关键词精确匹配仍有价值）；③ 单阈值 cosine≥0.6（太严，漏召回 0.4-0.6 的同义改写如"我今天都干了啥"cosine=0.51）。
