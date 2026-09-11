"""T19: web_fetch 工具（抓取网页转 markdown）

B2（SECURITY-RISKS D16/D17，2026-08-06）：已接入反爬基础设施。
- Chrome 131 Win10 UA + Accept/Accept-Language
- robots.txt 遵循（fail-open）
- 同域名 2s 频率限制
- 429/503 指数退避（1/2/4s 最多 3 次，尊重 Retry-After）
"""

from __future__ import annotations

from urllib.parse import urlparse

from client.core.agent.builtin_tools._web_anti_crawl import (
    check_robots,
    fetch_with_backoff,
    wait_if_needed,
)
from client.core.agent.builtin_tools.base import BuiltinTool
from client.core.agent.builtin_tools.ssrf_guard import check_url
from client.core.agent.types import ToolCall, ToolResult, ToolResultVariant


class WebFetchTool(BuiltinTool):
    """抓取网页内容并转为 markdown。"""

    operation_id = "web_fetch"

    def __init__(self, base_url: str = "http://127.0.0.1:8766"):
        self._base_url = base_url.rstrip("/")

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        url = args.get("url", "")

        if not url:
            return self._error(tool_call, "url is required", "invalid_argument")
        if not (url.startswith("http://") or url.startswith("https://")):
            return self._error(
                tool_call, f"Invalid URL (must start with http/https): {url}",
                "invalid_url",
            )

        # D1（spec D15）：SSRF 防护——校验 URL 不含内部网络 IP
        ok, err = check_url(url)
        if not ok:
            return self._error(
                tool_call,
                f"SSRF blocked: {err}",
                "ssrf_blocked",
            )

        # B2：robots.txt 遵循（fail-open）
        allowed, robots_reason = check_robots(url)
        if not allowed:
            return self._error(
                tool_call,
                f"robots.txt disallowed: {robots_reason}",
                "robots_blocked",
            )

        try:
            import requests
            # 调后端 web fetch 端点（如有 markdown 转换能力）
            # 注：后端调用走 X-Agent-Caller，不经过反爬基础设施（后端自身处理）
            try:
                resp = requests.get(
                    f"{self._base_url}/web/fetch",
                    params={"url": url},
                    timeout=20,
                    headers={"X-Agent-Caller": "v6-lite-agent"},
                )
                if resp.status_code == 200:
                    return self._success(tool_call, resp.text)
            except Exception:
                pass  # 后端不可用，直接抓取

            # Fallback: 直接抓取（接入反爬基础设施）
            host = urlparse(url).hostname or ""
            wait_if_needed(host)
            success, resp, err_msg = fetch_with_backoff(url, timeout=20)
            if not success:
                err_type = "backoff_exhausted" if "backoff" in err_msg else "fetch_error"
                return self._error(tool_call, f"Web fetch failed: {err_msg}", err_type)

            # 契约（fetch_with_backoff）：success=True ⟹ resp 非 None
            assert resp is not None

            if resp.status_code >= 400:
                return self._error(
                    tool_call,
                    f"HTTP {resp.status_code}: {resp.text[:500]}",
                    "http_error",
                )

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                # 简易 HTML → text 转换（去标签）
                import re
                text = resp.text
                # 去 script/style
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                # 去标签
                text = re.sub(r"<[^>]+>", " ", text)
                # 压缩空白
                text = re.sub(r"\s+", " ", text).strip()
                # 截断保护
                if len(text) > 20000:
                    text = text[:20000] + "\n\n[truncated at 20000 chars]"
                    return self._success(
                        tool_call, text, output_truncated=True,
                        next_action_hint="内容被截断，如需完整内容请分段获取或用 file_write 落盘",
                    )
                return self._success(tool_call, text)
            else:
                # 非 HTML，原样返回文本
                text = resp.text[:20000]
                truncated = len(resp.text) > 20000
                return self._success(
                    tool_call, text, output_truncated=truncated,
                )
        except requests.exceptions.Timeout:
            return ToolResult(
                tool_call_id=tool_call.id,
                content="web_fetch timed out after 20s",
                variant=ToolResultVariant.TIMEOUT,
                timeout_seconds=20.0,
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Web fetch failed: {type(e).__name__}: {e}",
                "fetch_error",
            )
