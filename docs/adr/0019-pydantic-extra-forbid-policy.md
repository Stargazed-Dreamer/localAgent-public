# Pydantic extra='forbid' 全项目策略

Pydantic v2 默认 `extra='ignore'`，传错字段名静默丢弃，导致超时不生效 + 误报成功（batch 4 C2 根因）。决策：全项目 BaseModel 继承 `lib.schema.BaseSchema`（`model_config = ConfigDict(extra='forbid')`），独立服务内联 `ConfigDict(extra='forbid')`。严格优于容错——传错字段名应立即报错而非静默吞，避免调试时误报成功。
