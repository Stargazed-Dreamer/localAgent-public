"""opencode.ai Zen/Go 端点适配：会话亲和 header 族（2026-09-06 起上游强制）

背景（上游 2026-09-03 公告，09-06 起执行）：不带 x-opencode-session 的请求返回
400 MissingSessionID（实测证据：inbound_calls id 632-637，09-04 成功 → 09-08 全 400）。

header 语义（官方文档 opencode.ai/docs/go/#where-can-i-use-it + 官方客户端 LLM.stream
源码 + Hermes PR #101864 参考实现）：
- x-opencode-session: 会话 ID。同一会话所有请求（含辅助调用）稳定不变；上游据此做
  会话亲和路由 + prompt cache 命中（同会话导到同一后端，cached read 计费远低于全价）。
- x-opencode-request: 每请求唯一，请求级 trace。
- x-opencode-client / x-opencode-project: 客户端 / 项目标识（官方 cli 发 "cli"/project id）。
- User-Agent: 自有客户端身份，不能用 python-httpx 之类通用库名。

判定口径：按 base_url host 判定（与 Hermes URL-only 检测一致），非 opencode 端点
返回空 dict，一字节不多发。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import urlsplit

_HOST_SUFFIX = "opencode.ai"
CLIENT_ID = "localagent"
# 项目标识用常量：仅作上游侧区分，不外泄内部项目名
PROJECT_ID = "localagent"


def is_opencode_endpoint(base_url: str) -> bool:
    """base_url 是否指向 opencode.ai（含子域）"""
    host = (urlsplit(base_url).hostname or "").lower()
    return host == _HOST_SUFFIX or host.endswith("." + _HOST_SUFFIX)


def _first_user_text(messages: list[dict]) -> str:
    """首条 user 消息的文本（append-only 历史下跨轮稳定，是对话指纹的锚点）"""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif isinstance(p, str):
                    parts.append(p)
            return "".join(parts)
        return ""
    return ""


def derive_session_id(messages: list[dict], namespace: str = "") -> str:
    """内容派生的稳定会话 ID：sha256(namespace + 首条 user 文本)[:16]

    同一对话跨轮稳定（历史 append-only，首条 user 不变）；不同对话（首条 user
    不同 / 不同 namespace）得到不同 ID。无 user 消息时退化为全消息哈希（仍确定性）。
    """
    text = _first_user_text(messages)
    if not text:
        text = json.dumps(messages or [], ensure_ascii=False, default=str)
    digest = hashlib.sha256(f"{namespace}\x00{text}".encode("utf-8")).hexdigest()[:16]
    return f"ocs-{digest}"


def _user_agent() -> str:
    try:
        from server.main import VERSION  # 延迟导入避免循环依赖
        return f"localagent/{VERSION}"
    except Exception:
        return CLIENT_ID


def build_opencode_headers(base_url: str, session_id: str) -> dict[str, str]:
    """opencode 端点的会话亲和 header 族；非 opencode 端点返回 {}（零多余 header）"""
    if not is_opencode_endpoint(base_url):
        return {}
    return {
        "x-opencode-session": session_id,
        "x-opencode-request": uuid.uuid4().hex,
        "x-opencode-client": CLIENT_ID,
        "x-opencode-project": PROJECT_ID,
        "User-Agent": _user_agent(),
    }
