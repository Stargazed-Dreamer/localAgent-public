"""T18: web_search 工具（调后端 WebSearch 或直接 HTTP）

B2（SECURITY-RISKS D16/D17，2026-08-06）：已接入反爬基础设施。
- Chrome 131 Win10 UA + Accept/Accept-Language
- robots.txt 遵循（fail-open）
- 同域名 2s 频率限制（DuckDuckGo 域名也受限）
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
from client.core.agent.types import ToolCall, ToolResult


class WebSearchTool(BuiltinTool):
    """网络搜索。"""

    operation_id = "web_search"

    def __init__(self, base_url: str = "http://127.0.0.1:8766"):
        self._base_url = base_url.rstrip("/")

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        # 暂时禁用：DuckDuckGo 搜索源不稳定（国内多失败，e2e 7 连败）。
        # 推荐替代：1) web_fetch 抓已知 URL；2) browser_navigate 访问搜索引擎 + browser_snapshot 读取结果。
        return self._error(
            tool_call,
            "web_search 已暂时禁用（搜索源 DuckDuckGo 不稳定，国内多失败）。替代方案："
            "1) web_fetch 抓取已知 URL；"
            "2) browser_navigate 访问搜索引擎（如 https://www.bing.com/search?q=<query>）+ browser_snapshot 读取结果页",
            "disabled",
        )

    async def _execute_impl(self, tool_call: ToolCall) -> ToolResult:
        """实际搜索逻辑（禁用期间不调用，保留以便后续恢复）。"""
        args = tool_call.args or {}
        query = args.get("query", "")
        num = int(args.get("num", 5))

        if not query:
            return self._error(tool_call, "query is required", "invalid_argument")

        try:
            import requests
            # 调后端 web search 端点（如有），否则用 DuckDuckGo HTML
            # 注：后端调用走 X-Agent-Caller，不经过反爬基础设施（后端自身处理）
            try:
                resp = requests.get(
                    f"{self._base_url}/web/search",
                    params={"query": query, "num": num},
                    timeout=15,
                    headers={"X-Agent-Caller": "v6-lite-agent"},
                )
                if resp.status_code == 200:
                    return self._success(tool_call, resp.text)
            except Exception:
                pass  # 后端不可用，fallback 到 DuckDuckGo

            # Fallback: DuckDuckGo HTML（无 API key 需求）
            ddg_url = "https://html.duckduckgo.com/html/"
            # B2：robots.txt 遵循（fail-open）
            allowed, robots_reason = check_robots(ddg_url)
            if not allowed:
                return self._error(
                    tool_call,
                    f"robots.txt disallowed: {robots_reason}",
                    "robots_blocked",
                )
            # B2：同域名频率限制
            host = urlparse(ddg_url).hostname or ""
            wait_if_needed(host)
            # B2：429/503 退避
            success, resp, err_msg = fetch_with_backoff(
                ddg_url, timeout=15, params={"q": query},
            )
            if not success:
                err_type = "backoff_exhausted" if "backoff" in err_msg else "search_error"
                return self._error(tool_call, f"Web search failed: {err_msg}", err_type)

            if resp.status_code != 200:
                return self._error(
                    tool_call,
                    f"Web search failed: HTTP {resp.status_code}",
                    "http_error",
                )
            # 简单解析 HTML（提取 result links + titles）
            import re
            results = []
            # DuckDuckGo HTML 结果格式：<a class="result__a" href="...">title</a>
            matches = re.findall(
                r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
                resp.text,
            )
            for url, title in matches[:num]:
                results.append(f"- {title.strip()}\n  {url}")
            output = "\n\n".join(results) if results else "(no results)"
            return self._success(tool_call, output)
        except requests.exceptions.Timeout:
            from client.core.agent.types import ToolResultVariant
            return ToolResult(
                tool_call_id=tool_call.id,
                content="web_search timed out after 15s",
                variant=ToolResultVariant.TIMEOUT,
                timeout_seconds=15.0,
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Web search failed: {type(e).__name__}: {e}",
                "search_error",
            )
