"""L4 consumer 公用库数据结构。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecordingSummary:
    """录制包摘要（agent 发现录制包时用的轻量信息）。

    字段：
    - recording_id: 录制包 ID（如 rec_20260723_141608）
    - created_at: 创建时间（ISO 8601）
    - duration_seconds: 有效时长（秒）
    - finalized: 是否已标记就绪（D053）
    - block_count: 块总数（timeline.json 的 blocks 数量）
    - chapter_count: 章节数
    - marked_key_count: 标记为关键的块数
    - marked_anomaly_count: 标记为异常的块数
    - has_transcript: 是否有转写（transcript.json 存在）
    - stt_model: STT 模型名（如 large-v3），无转写时为 None
    - package_path: 录制包根目录绝对路径
    """

    recording_id: str
    created_at: str
    duration_seconds: float
    finalized: bool
    block_count: int
    chapter_count: int
    marked_key_count: int
    marked_anomaly_count: int
    has_transcript: bool
    stt_model: str | None
    package_path: str


@dataclass
class VLCandidate:
    """VL 候选帧（agent 调 understand_image 时的入参封装，Ticket 30）。

    字段：
    - block_id: 关联块 ID（如 b001/f001/c001）
    - frame_path: 帧图片路径（package_path 提供时为绝对路径，否则为录制包内相对路径）
    - timestamp: 帧时间戳（秒，录制包时间轴）
    - reason: 选择原因（marked_key/marked_anomaly/marked_automatable/
      screenshot_frame/trimmed；去重时多个 reason 用 + 连接）
    - suggested_question: VL question 模板（调 build_vl_question 生成）
    """

    block_id: str
    frame_path: str
    timestamp: float
    reason: str
    suggested_question: str
