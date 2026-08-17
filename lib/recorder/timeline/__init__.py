"""L2 时间轴层（spec-l2.md）

L2 时间轴层入口：build_timeline。输入录制包目录，
合并 L1 产出（blocks.json + keyframes.json + transcript.json）为统一 timeline.json。

模块结构（D019 公用库分层）：
- builder.py：build_timeline seam（T1 时间轴引擎，合并四类块按时间戳排序）
- chapterizer.py：T2 章节切分器（focus_change + idle 切章节）
- annotator.py：T3 重点标注器（初始化空 annotations.json）

L2 不走后端 API（延续 D018），L3 编辑器启动时自动调 build_timeline。

决策：D039（音频时间戳映射）/ D040（keyframes 转独立块）/ D041（块 ID 前缀）
/ D042（章节切分粒度）/ D043（annotations 只初始化不标注）
"""

from lib.recorder.timeline.annotator import init_annotations
from lib.recorder.timeline.builder import TimelineResult, build_timeline

__all__ = ["build_timeline", "TimelineResult", "init_annotations"]
