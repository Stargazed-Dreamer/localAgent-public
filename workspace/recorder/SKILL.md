---
name: recorder
description: >
  操作录制与 agent 辅助消费系统。录制键鼠/屏幕/音频/窗口焦点生成可回放录制包，
  经 L1 处理（STT/VL/事件聚合）→ L2 时间轴 → L3 编辑 → L4 agent 消费。
  触发词：录制、看一下录制、最近的录制、消费录制、分析录制、录制包、recording。
task_type: recording.consume
---

# 操作录制与 agent 辅助消费 Skill

## 概述

五层架构：
- **L0 采集**：`lib/recorder/sensors/` + `workspace/recorder/tools/`（GUI 入口）— 键鼠/屏幕/音频/窗口焦点传感器
- **L1 处理**：`lib/recorder/processor/` — STT (Whisper) + UIA + VL + 事件聚合 + 关键帧
- **L2 时间轴**：`lib/recorder/timeline/` — 合并 L1 三 JSON 为 timeline.json + 章节切分
- **L3 编辑**：`lib/recorder/editor/`（核心数据层）+ `workspace/recorder/tools/editor/`（GUI）— 标注/裁剪/音频调参
- **L4 消费**：`workspace/recorder/consumer/` — agent 读录制包 API（list_recordings / get_merged_view / select_vl_candidates / log_consumption）

录制包存储在 `workspace/recorder/recordings/rec_{YYYYMMDD}_{HHMMSS}/`，不压缩本地直读（D009）。

## 触发词

- "录制" / "看一下录制" / "最近的录制" / "列出录制" / "发现录制"
- "消费录制" / "分析录制" / "看录制" / "处理录制" / "录制总结"
- "录制 转脚本" / "录制 蒸馏" / "录制 triage" / "录制 bug"

## 启动录制器

```bash
# 小模式（悬浮条）+ 默认配置
python -m workspace.recorder.tools.main

# 大模式（双屏监控面板）
python -m workspace.recorder.tools.main --mode large

# 跳过配置对话框，直接开始录制
python -m workspace.recorder.tools.main --range fullscreen --detail-level detailed --autostart
```

详细 CLI 参数见 [docs/recorder-guide.md](../../docs/recorder-guide.md) 第二章。

## 工作流程

### 路由 1：发现录制包（recording.discover）

1. `from workspace.recorder.consumer import list_recordings; list_recordings()`
   - 返回每个录制包的摘要（recording_id/created_at/duration_seconds/finalized/block_count/chapter_count/has_transcript）
2. 展示 recording_id + 时长 + finalized 给用户选
3. 用户选定后路由到 `recording.consume`

### 路由 2：消费录制包（recording.consume）

1. `from workspace.recorder.consumer import get_merged_view; get_merged_view(package_path)`
   - 读 merge 后的最终视图（timeline + annotations 合并，自动过滤 is_trimmed 块）
2. agent 自主决定后续场景（处理 bug / 蒸馏方法论 / 生成自动化脚本 / 总结等），不预设路由
3. 多轮 VL 协议（D057）：
   - Round 1：`get_merged_view` 建立认知
   - Round 2：`select_vl_candidates` + `build_vl_question` + `understand_image` 调 VL
   - Round 3：收敛
4. 每次 VL 调用后 `log_consumption(package_path, 'vl_call', {block_id, question, frame})` 记录统计

## 关键约束

- **不预存 VL 结果到录制包**（D005）：VL 按需调用，结果只在会话内存中
- **录制包不压缩本地直读**（D009）：不要尝试 zip 解压
- **VL 调用次数推荐上限 15-30 次/次消费**（D054 统计预警，不 enforce）
- **未 finalized 的录制包**：表示用户还未在 L3 编辑器标记就绪，消费前提示用户先标记
- **supplements 路径**：在 `get_block_detail` 中已解析为绝对路径，直接用 `understand_image` 调 VL

## 详细参考

完整设计决策（D001-D057）、CLI 参数、配置项、数据格式、L3 编辑器操作等详见 [docs/recorder-guide.md](../../docs/recorder-guide.md)。
