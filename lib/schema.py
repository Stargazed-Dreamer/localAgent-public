"""Pydantic BaseSchema：全局 extra='forbid' 基类。

所有新 Pydantic 模型应继承 BaseSchema，传多余字段时立即报 ValidationError
而非静默丢弃（Pydantic v2 默认 extra='ignore'）。

现有模型渐进迁移：记入 wip，逐步将 `class Foo(BaseModel)` 改为 `class Foo(BaseSchema)`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    """全局 Pydantic 基类，强制 extra='forbid'。

    传未定义字段时抛 ValidationError，而非静默丢弃。
    防止拼写错误/旧字段名导致的"传了但不生效"问题。
    """

    model_config = ConfigDict(extra="forbid")
