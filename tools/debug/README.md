# Debug 调试脚本

通用调试脚本，用于排查 OCR 识别、截图坐标链路、DPI 偏移等屏幕操控相关问题。

## 前置条件

- LocalAgent 后端正在运行（`http://127.0.0.1:8766`）
- 目标窗口可见且未最小化
- 大部分脚本需要先修改窗口名参数后再使用

## 脚本列表

| 脚本 | 用途 | 说明 |
|------|------|------|
| `verify_bbox_affine.py` | OCR bbox 回归 | 更新 PaddleOCR/PaddleX 后第一步运行；三档分辨率目标 RMS 1-3px、max≤5px |
| `sample_bbox_offset.py` | detector affine 调查 | 仅在直接 detector A/B 也有系统误差时使用，禁止补偿 UVDoc |
| `ocr_scale_debugger.py` | 历史交互式缩放调试 | 旧 sx/sy 思路，仅作历史辅助，不用于当前默认配置 |
| `draw_all_ocr_boxes.py` | 标注 OCR 文本框 | 在截图上绘制所有 OCR 识别框，验证识别结果和坐标 |
| `debug_resolution.py` | 分辨率对比 | 对比截图分辨率 vs 窗口分辨率，排查 DPI 缩放导致的偏移 |
| `debug_ocr_data.py` | OCR 数据查看 | OCR 结果排序输出到文件，查看完整识别数据 |
| `scale_test.py` | 批量缩放标注 | 批量生成不同缩放参数的标注图，非交互式版缩放调试 |
| `debug_coords.py` | 坐标转换验证 | 验证窗口坐标 vs OCR 坐标的转换是否正确 |

## 注意事项

- 这些脚本仅用于开发调试，不是生产工具
- 截图 OCR 必须保持 `use_doc_unwarping=False` 和恒等 affine；升级先跑 verify，再做 detector/pipeline A/B
- 过于特定于某个游戏/应用的调试脚本，完成调试后应删除
- 新增调试脚本放入本目录，不要放在 `tools/` 根目录
