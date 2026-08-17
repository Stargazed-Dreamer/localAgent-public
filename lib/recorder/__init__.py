"""L0 采集层核心逻辑

模块：
- types: 事件类型协议
- timestamp: TimestampService（time.time() 基准 + time.monotonic() 偏移）
- package: RecordingPackage 目录 schema
- controller: RecordingController 单一 seam
- sensors/: 传感器封装（base / mock / keyboard / mouse / screen / audio / window）
"""
