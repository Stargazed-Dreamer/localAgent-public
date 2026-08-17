"""T02: SSRF 防护测试——loopback + redirect + IPv6。"""

from __future__ import annotations

import ipaddress
from unittest.mock import MagicMock, patch

from client.core.agent.builtin_tools.ssrf_guard import check_url, is_ip_blocked


class TestLoopbackBlocked:
    """T02: 127.0.0.0/8 和 ::1/128 必须被拦。"""

    def test_127_0_0_1_blocked(self):
        """127.0.0.1 必须被拦（防探测本地 Redis/后端 API）。"""
        ok, err = check_url("http://127.0.0.1:6379/")
        assert not ok, f"127.0.0.1 应被拦，但返回 ok={ok}"
        assert "blocked CIDR" in err

    def test_127_0_0_1_ip_blocked(self):
        """is_ip_blocked 对 127.0.0.1 返回 True。"""
        assert is_ip_blocked(ipaddress.ip_address("127.0.0.1"))

    def test_127_255_255_255_blocked(self):
        """127.0.0.0/8 整个网段都被拦。"""
        assert is_ip_blocked(ipaddress.ip_address("127.255.255.255"))

    def test_ipv6_loopback_blocked(self):
        """::1 必须被拦。"""
        ok, err = check_url("http://[::1]:8080/")
        assert not ok, f"::1 应被拦，但返回 ok={ok}"
        assert "blocked CIDR" in err


class TestIPv6PublicAllowed:
    """T02: 移除 ::/0 后，公网 IPv6 地址应放行。"""

    def test_public_ipv6_not_blocked(self):
        """公网 IPv6 地址（如 2606:4700::6810:85e5 Cloudflare）不应被拦。"""
        assert not is_ip_blocked(ipaddress.ip_address("2606:4700::6810:85e5"))

    def test_ipv4_mapped_ipv6_still_checked(self):
        """IPv4-mapped IPv6 中的 127.0.0.1 仍被拦。"""
        assert is_ip_blocked(ipaddress.ip_address("::ffff:127.0.0.1"))

    def test_ipv4_mapped_public_allowed(self):
        """IPv4-mapped IPv6 中的公网 IP 放行。"""
        assert not is_ip_blocked(ipaddress.ip_address("::ffff:8.8.8.8"))


class TestRedirectSSRF:
    """T02: fetch_with_backoff 跟随 redirect 时每跳重新 SSRF 校验。"""

    def test_redirect_to_metadata_blocked(self):
        """302 → 169.254.169.254 必须被拦（经典 SSRF 绕过）。"""
        from client.core.agent.builtin_tools._web_anti_crawl import fetch_with_backoff

        # 构造 mock response：第一次 302 → 169.254.169.254
        redirect_resp = MagicMock()
        redirect_resp.status_code = 302
        redirect_resp.is_redirect = True
        redirect_resp.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with patch("requests.get", return_value=redirect_resp):
            success, resp, err = fetch_with_backoff("http://example.com/redirect")
            assert not success, f"redirect 到 169.254 应被拦，但 success={success}"
            assert "SSRF blocked" in err or "blocked CIDR" in err

    def test_redirect_to_loopback_blocked(self):
        """302 → 127.0.0.1 必须被拦。"""
        from client.core.agent.builtin_tools._web_anti_crawl import fetch_with_backoff

        redirect_resp = MagicMock()
        redirect_resp.status_code = 302
        redirect_resp.is_redirect = True
        redirect_resp.headers = {"Location": "http://127.0.0.1:8080/"}

        with patch("requests.get", return_value=redirect_resp):
            success, resp, err = fetch_with_backoff("http://example.com/redirect")
            assert not success
            assert "SSRF blocked" in err or "blocked CIDR" in err

    def test_no_allow_redirects_true(self):
        """fetch_with_backoff 不应传 allow_redirects=True（默认 True 也不行）。"""
        import inspect

        from client.core.agent.builtin_tools._web_anti_crawl import fetch_with_backoff

        source = inspect.getsource(fetch_with_backoff)
        assert "allow_redirects=False" in source, "fetch_with_backoff 必须显式传 allow_redirects=False"
