# OmniParser 移除：远程 VL + PaddleOCR bbox 替代

OmniParser（YOLOv8 + Florence2 + PaddleOCR 本地 UI 元素解析，2.06 GB 模型 + 5 个专用依赖含 AGPL-3.0 的 ultralytics）唯一入口 `parse_screen` 在 `data/mcp_stats.json` 中显示调用次数为 0。2026-07-31 整体移除该模块，Computer Use 文字定位主路径改为 PaddleOCR bbox（三档分辨率回归误差 1-3px），远程 VL（ModelScope Qwen3-VL-235B-A22B-Instruct）只做文档解析和图像描述，纯图标场景由远程 VL 兜底。同时消除 ultralytics AGPL-3.0 传染风险。

## Status

accepted (2026-07-31)

## Context

OmniParser 是项目初期 Computer Use 模块的视觉 AI 组件，由 YOLOv8（图标检测）+ Florence2（图标描述）+ PaddleOCR（文字识别）三段管线组成，承担"截图→UI 元素结构化"职责，模型目录 `<models_root>\omniparser` 占 2.06 GB，依赖 `ultralytics`/`supervision`/`torchvision`/`timm`/`accelerate` 5 个专用包。

移除的强证据：
- `data/mcp_stats.json` 显示 `parse_screen`（OmniParser 唯一入口）MCP 调用次数为 **0**——功能存在但无人用。
- PaddleOCR bbox 已修复：`server/ocr.py` 显式关闭 `use_doc_unwarping`（UVDoc 去畸变会改变几何且不逆映射 bbox），三档分辨率（800x600 / 1920x1080 / 2560x1440）网格回归误差 1-3px，可承担 Computer Use 文字定位主路径。
- 远程 VL（ModelScope Qwen3-VL-235B-A22B-Instruct）已上线，覆盖文档解析和图像描述，定位优先级表更新为"DOM > UIA > OCR bbox > `vision_locate`"。
- `ultralytics` 是 AGPL-3.0 许可证，存在传染风险（public-release roadmap H1）。

## Decision

1. **整体移除 OmniParser 模块**：删除 `OmniParserManager` 类、`_detect_icons`/`_caption_icons`/`_ocr_text`/`_merge_ocr_into_elements` 函数、`parse_screen` 端点、`/vision/models/*` 端点、`ParseRequest`/`ParseResponse`/`ParseElement` Pydantic 模型。`VisionStatusResponse` 移除 `omniparser_enabled`/`icon_detect_loaded`/`icon_caption_loaded`/`keep_models`/`weights_dir` 5 个字段。
2. **Computer Use 文字定位主路径改为 PaddleOCR bbox**：截图管线关闭 UVDoc 去畸变（`use_doc_unwarping=False`），bbox 三档回归误差 1-3px。坐标转换：`screen_ocr(mode="window")` 返回窗口截图内 bbox，中心点加 `list_windows` 返回的窗口 `left/top` 后再传 `execute_action`。
3. **远程 VL 只做文档解析和图像描述**，不承担普通文字坐标定位（避免消耗昂贵且慢的 VL 配额）。纯图标/无文字元素场景由远程 VL 兜底：`understand_image` 描述 + `vision_locate` 坐标定位。
4. **移除 5 个 OmniParser 专用依赖**（`ultralytics`/`supervision`/`torchvision`/`timm`/`accelerate`），保留 `transformers` 和 `torch`（embeddings + STT 仍用）。`<models_root>\omniparser` 2.06 GB 模型目录物理删除。
5. **消除 ultralytics AGPL 传染风险**：public-release roadmap H1 标记为 ✅ 已解决，H7（OmniParser 模型权重不分发）状态更新为"模块整体移除，权重已物理删除"。

## Considered Options

1. **保留 OmniParser 作为 fallback**——被拒：调用 0 次证明无真实需求；2.06 GB 模型常驻磁盘 + 5 个专用依赖（含 AGPL）维护成本高；PaddleOCR bbox + 远程 VL 已覆盖全部实际场景。
2. **引入新的目标检测模型替代**——被拒：无真实需求驱动（调用 0 次），引入新模型等于制造新的维护负担和许可证风险，违反"有真实痛点再回收"。
3. **纯 OCR（无 VL 兜底）**——被拒：纯图标/无文字元素场景 OCR 无法处理，远程 VL 兜底是必要补充，且远程 VL 已上线无额外成本。

## Consequences

**正面**：
- 2.06 GB 模型物理删除 + 5 个专用依赖移除，启动更快、磁盘释放、依赖树精简。
- 消除 ultralytics AGPL-3.0 传染风险，项目许可证（现 Apache-2.0）不再受 AGPL 争议影响（public-release H1 关闭）。
- 定位链路简化为"DOM > UIA > OCR bbox > vision_locate"，单一职责清晰。

**负面**：
- 纯图标无文字场景依赖远程 VL，网络/配额受限时降级（VL provider 级 fallback 已在 [0.31.0] 引入缓解）。
- 主后端不再加载 Florence2 processor，仅 `tools/image_organizer/` 工具从 HF cache 加载（孤立工具，影响可控）。

**回退路径**：OmniParser 代码在 git 历史（[0.30.0] 移除提交）中可恢复，但 2.06 GB 模型权重需重新下载；AGPL 风险会随恢复回归。鉴于调用 0 次的强证据，回退无实际意义。若未来出现"远程 VL 不可用 + 有图标检测需求"的场景，优先评估 ONNX 格式的轻量图标检测路径（绕开 ultralytics/AGPL），而非恢复完整 OmniParser 管线（2026-09-16 记忆归档时从 `omniparser_removal` 记忆并入的重新启用条件）。

## References

- 移除条目：`docs/changelog-archive.md` [0.30.0] Changed "移除 OmniParser 模块" + Security "消除 ultralytics AGPL 传染风险"
- 项目指引：`AGENTS.md` 技术栈段（"OmniParser 已于 2026-07-31 移除（调用 0 次，远程 VL + OCR bbox 已覆盖全部实际场景）"）
- 环境约束：`docs/environment-constraints.md`（"OmniParser 已于 2026-07-31 移除"；Florence2 兼容性段）
- 关键代码路径：[server/vl/vision.py](file:///<project_root>/server/vl/vision.py)（OmniParser 已删除，仅留远程 VL 集成）、[server/ocr.py](file:///<project_root>/server/ocr.py)（PaddleOCR bbox，`use_doc_unwarping=False`）、[server/vl/remote_vl.py](file:///<project_root>/server/vl/remote_vl.py)（远程 VL + provider 级 fallback）
- 注：`temp/sdd/omniparser-removal/spec.md` 在 changelog 中被引用，但该 SDD 产物已被清理（temp/ 目录 gitignore），决策细节以 changelog 0.30.0 条目 + AGENTS.md 为准
- 相关 ADR：无（独立模块移除决策）
