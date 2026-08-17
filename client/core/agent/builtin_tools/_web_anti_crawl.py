"""B2（SECURITY-RISKS D16/D17）：Web 工具共享反爬基础设施。

设计依据：
- 用户 2026-08-06 mini-grill 确认：Chrome 131 Win10 UA / 1次/2秒/域名 / 1-2-4s 退避最多3次 / robots.txt fail-open
- web_fetch.py 和 web_search.py 共用本模块，避免重复实现

提供：
- CHROME_UA：Chrome 131 Win10 真实 UA 字符串
- DEFAULT_HEADERS：完整请求头（UA + Accept + Accept-Language）
- DomainRateLimiter：进程级同域名频率限制器（默认 2s/域名）
- RobotsCache：urllib.robotparser 缓存（5min TTL，fail-open）
- fetch_with_backoff：requests.get 封装，429/503 指数退避（尊重 Retry-After）

非目标：
- 不实现 JS 渲染（重量级，按需切 playwright）
- 不实现 IP 代理池（基础设施太重）
- 不做 UA 随机轮换（用户选了固定 Chrome 131）
"""

from __future__ import annotations

import logging
import time
from datetime import UTC
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger("localagent.agent.web_anti_crawl")

# ============================================================================
# 常量
# ============================================================================

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 同域名最小请求间隔（秒）—— mini-grill 选"保守：1 次/2 秒/域名"
MIN_DOMAIN_INTERVAL = 2.0

# robots.txt 缓存 TTL（秒）—— 避免每次请求都重新获取
ROBOTS_CACHE_TTL = 300.0  # 5 分钟

# 退避策略：1/2/4 秒，最多 3 次重试
BACKOFF_DELAYS = [1.0, 2.0, 4.0]
BACKOFF_MAX_RETRIES = 3


# ============================================================================
# DomainRateLimiter：进程级同域名频率限制器
# ============================================================================


class DomainRateLimiter:
    """同域名请求间隔限制器。

    进程级单例（模块全局 _RATE_LIMITER）。同域名两次请求间隔小于
    MIN_DOMAIN_INTERVAL 时，自动 sleep 补齐。

    线程安全：用 threading.Lock 保护 _last_request_ts 字典。
    非异步：sleep 在 asyncio 上下文会阻塞事件循环——但 web_fetch/web_search
    本身是同步 requests.get，整段 execute 在 asyncio.to_thread 之外，
    所以同步 sleep 是正确选择（与现有 timeout 处理一致）。
    """

    def __init__(self, min_interval: float = MIN_DOMAIN_INTERVAL):
        self._min_interval = min_interval
        self._last_request_ts: dict[str, float] = {}
        self._lock = None  # lazy init，避免 import 时副作用

    def wait_if_needed(self, host: str) -> float:
        """若同域名距上次请求不足 min_interval，sleep 补齐。

        Returns:
            实际 sleep 秒数（0 表示无需等待）
        """
        if not host:
            return 0.0
        now = time.monotonic()
        last = self._last_request_ts.get(host, 0.0)
        elapsed = now - last
        if elapsed >= self._min_interval:
            self._last_request_ts[host] = now
            return 0.0
        sleep_sec = self._min_interval - elapsed
        logger.debug("rate_limit: host=%s sleep=%.2fs", host, sleep_sec)
        time.sleep(sleep_sec)
        self._last_request_ts[host] = time.monotonic()
        return sleep_sec

    def _get_lock(self):
        if self._lock is None:
            import threading
            self._lock = threading.Lock()
        return self._lock


# 模块级单例
_RATE_LIMITER = DomainRateLimiter()


def wait_if_needed(host: str) -> float:
    """模块级便捷函数：调单例 wait_if_needed。"""
    return _RATE_LIMITER.wait_if_needed(host)


# ============================================================================
# RobotsCache：robots.txt 缓存（fail-open）
# ============================================================================


class _RobotsEntry:
    """单个 host 的 robots.txt 缓存条目。"""

    __slots__ = ("parser", "fetched_at", "fetch_failed")

    def __init__(self, parser: RobotFileParser | None, fetched_at: float, fetch_failed: bool):
        self.parser = parser
        self.fetched_at = fetched_at
        self.fetch_failed = fetch_failed


class RobotsCache:
    """urllib.robotparser 的 LRU 缓存。

    - 缓存每个 host 的 RobotFileParser 实例（5min TTL）
    - 获取失败（超时/404/网络错误）→ fail-open，返回 (True, "robots fetch failed: ...")
    - 明确 Disallow → 返回 (False, "robots.txt disallows: <url>")
    """

    def __init__(self, ttl: float = ROBOTS_CACHE_TTL, fetch_timeout: float = 5.0):
        self._ttl = ttl
        self._fetch_timeout = fetch_timeout
        self._cache: dict[str, _RobotsEntry] = {}

    def is_allowed(self, url: str) -> tuple[bool, str]:
        """检查 url 是否被 robots.txt 允许。

        Returns:
            (allowed, reason):
                (True, ""): 允许（robots.txt 明确允许或获取失败 fail-open）
                (False, reason): 拒绝（reason 含说明）
        """
        if not url:
            return True, ""
        try:
            parsed = urlparse(url)
        except Exception as e:
            return True, f"url parse failed (fail-open): {e}"
        host = parsed.hostname
        if not host:
            return True, ""
        scheme = parsed.scheme or "http"
        robots_url = f"{scheme}://{host}/robots.txt"

        entry = self._cache.get(host)
        now = time.monotonic()
        if entry is None or (now - entry.fetched_at) > self._ttl:
            entry = self._fetch_robots(host, robots_url)
            self._cache[host] = entry

        if entry.fetch_failed or entry.parser is None:
            return True, f"robots fetch failed (fail-open): {host}"

        try:
            # RobotFileParser.can_fetch(user_agent, url)
            if entry.parser.can_fetch(CHROME_UA, url):
                return True, ""
            return False, f"robots.txt disallows: {url}"
        except Exception as e:
            return True, f"robots check error (fail-open): {e}"

    def _fetch_robots(self, host: str, robots_url: str) -> _RobotsEntry:
        """获取并解析 robots.txt。失败返回 fail-open 条目。"""
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            import requests
            resp = requests.get(
                robots_url,
                headers=DEFAULT_HEADERS,
                timeout=self._fetch_timeout,
            )
            # 404/403 等无 robots.txt → 视为全允许（标准行为）
            if resp.status_code == 404:
                logger.debug("robots 404 (allowed): %s", robots_url)
                return _RobotsEntry(parser=None, fetched_at=time.monotonic(), fetch_failed=True)
            if resp.status_code >= 400:
                logger.warning("robots fetch %d (fail-open): %s", resp.status_code, robots_url)
                return _RobotsEntry(parser=None, fetched_at=time.monotonic(), fetch_failed=True)
            parser.parse(resp.text.splitlines())
            return _RobotsEntry(parser=parser, fetched_at=time.monotonic(), fetch_failed=False)
        except Exception as e:
            logger.warning("robots fetch error (fail-open): %s: %s", robots_url, e)
            return _RobotsEntry(parser=None, fetched_at=time.monotonic(), fetch_failed=True)


# 模块级单例
_ROBOTS_CACHE = RobotsCache()


def check_robots(url: str) -> tuple[bool, str]:
    """模块级便捷函数：调单例 is_allowed。"""
    return _ROBOTS_CACHE.is_allowed(url)


# ============================================================================
# fetch_with_backoff：requests.get + 指数退避
# ============================================================================


def fetch_with_backoff(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict | None = None,
    params: dict | None = None,
    max_redirects: int = 5,
) -> tuple[bool, object, str]:
    """带指数退避的 GET 请求（SSRF 安全的 redirect 跟随）。

    Args:
        url: 目标 URL（已过 SSRF 校验）
        timeout: 请求超时秒数
        headers: 自定义请求头（None 时用 DEFAULT_HEADERS）
        params: query 参数
        max_redirects: 最大 redirect 跳数（每跳重新 SSRF 校验）

    Returns:
        (success, response, error_msg):
            (True, requests.Response, ""): 成功（status < 400 或已耗尽重试后最后一次响应）
            (False, None, error_msg): 失败（网络错误/超时/退避耗尽/SSRF 拦截）

    退避策略（mini-grill 确认）：
        1. 遇 429/503 → 看 Retry-After 头，无则用 BACKOFF_DELAYS[i]
        2. sleep 后重试，最多 BACKOFF_MAX_RETRIES 次
        3. 退避耗尽仍 429/503 → 返回 (False, None, "backoff exhausted: HTTP xxx")
        4. 4xx（非 429）/5xx（非 503）→ 不重试，直接返回 (True, response, "")，
           由调用方决定如何处理（如 404 不需要重试）

    SSRF redirect 防护（T02 修复）：
        - allow_redirects=False，手动跟随 3xx
        - 每跳重新 check_url，防止 302 → 169.254.169.254 绕过
        - 超过 max_redirects 则拒绝
    """
    import requests

    from client.core.agent.builtin_tools.ssrf_guard import check_url

    final_headers = dict(DEFAULT_HEADERS)
    if headers:
        final_headers.update(headers)

    current_url = url

    for attempt in range(BACKOFF_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                current_url,
                headers=final_headers,
                params=params,
                timeout=timeout,
                allow_redirects=False,  # T02: 手动跟随，每跳 SSRF 校验
            )
        except requests.exceptions.Timeout:
            return False, None, f"timeout after {timeout}s"
        except requests.exceptions.RequestException as e:
            return False, None, f"request error: {type(e).__name__}: {e}"

        # T02: 3xx redirect → 手动跟随，每跳重新 SSRF 校验
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            if max_redirects <= 0:
                return False, None, f"max redirects exceeded for {url}"
            location = resp.headers.get("Location", "")
            if not location:
                return True, resp, ""  # 无 Location 头，返回原响应
            # 构造绝对 URL
            from urllib.parse import urljoin
            redirect_url = urljoin(current_url, location)
            # T02 核心：每跳重新 SSRF 校验
            ok, err = check_url(redirect_url)
            if not ok:
                return False, None, f"SSRF blocked on redirect to {redirect_url}: {err}"
            current_url = redirect_url
            max_redirects -= 1
            continue  # 不消耗退避次数，直接请求新 URL

        # 429/503 → 退避重试
        if resp.status_code in (429, 503) and attempt < BACKOFF_MAX_RETRIES:
            # 优先用 Retry-After 头
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is None:
                retry_after = BACKOFF_DELAYS[attempt]
            # 限制单次退避最大 30s，避免服务器要求过大值
            retry_after = min(retry_after, 30.0)
            logger.warning(
                "backoff: HTTP %d, attempt %d/%d, sleep %.2fs",
                resp.status_code, attempt + 1, BACKOFF_MAX_RETRIES, retry_after,
            )
            time.sleep(retry_after)
            continue

        # 退避耗尽仍 429/503
        if resp.status_code in (429, 503):
            return False, None, f"backoff exhausted: HTTP {resp.status_code}"

        # 其他情况（2xx/4xx 非 429/5xx 非 503）→ 返回让调用方处理
        return True, resp, ""

    # 理论上不可达（最后一次循环必然 return）
    return False, None, "backoff loop exhausted unexpectedly"


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头。

    支持两种格式：
    - 数字（秒）："120" → 120.0
    - HTTP-date："Wed, 21 Oct 2026 07:28:00 GMT" → 距现在的秒数

    解析失败返回 None（调用方回退到 BACKOFF_DELAYS）。
    """
    if not value:
        return None
    value = value.strip()
    # 尝试数字
    try:
        return float(value)
    except ValueError:
        pass
    # 尝试 HTTP-date
    try:
        from datetime import datetime
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = (dt - now).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None
