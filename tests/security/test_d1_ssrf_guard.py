"""D1（spec D15）：SSRF 防护红绿测试。

红测试：证明 bug 存在（修复前 web_fetch 直连 169.254.169.254）
绿测试：证明 bug 修好（修复后返回 ssrf_blocked）

测试覆盖：
1. v6 标准 9 条 CIDR 全部拦截
2. IPv4-mapped IPv6 绕过防护
3. 公网 IP 通过
4. 域名解析后命中 CIDR 拦截（用 mock）
5. web_fetch 集成测试：SSRF IP 返回 ssrf_blocked
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.builtin_tools.ssrf_guard import (  # noqa: E402
    BLOCKED_CIDRS,
    check_url,
    extract_mapped_ipv4,
    is_ip_blocked,
    ssrf_guarded_lookup,
)
from client.core.agent.builtin_tools.web_fetch import WebFetchTool  # noqa: E402
from client.core.agent.types import ToolCall  # noqa: E402

# ============================================================================
# Part 1: BLOCKED_CIDRS 覆盖测试
# ============================================================================


class TestBlockedCidrs:
    """10 条 BLOCKED_CIDRS 全部能命中对应 IP（T02 修复后：+127.0.0.0/8 +::1/128，-::/0）。"""

    def test_all_10_cidrs_defined(self):
        """应有 10 条 CIDR（T02：+127.0.0.0/8 +::1/128，-::/0）。"""
        assert len(BLOCKED_CIDRS) == 10, (
            f"D1: Expected 10 BLOCKED_CIDRS, got {len(BLOCKED_CIDRS)}"
        )

    def test_0000_8_blocked(self):
        """0.0.0.0/8：当前网络。"""
        assert is_ip_blocked(ipaddress.ip_address("0.0.0.0"))
        assert is_ip_blocked(ipaddress.ip_address("0.1.2.3"))

    def test_10_0_0_0_8_blocked(self):
        """10.0.0.0/8：私有网络 RFC1918。"""
        assert is_ip_blocked(ipaddress.ip_address("10.0.0.1"))
        assert is_ip_blocked(ipaddress.ip_address("10.255.255.255"))

    def test_100_64_0_0_10_blocked(self):
        """100.64.0.0/10：CGNAT。"""
        assert is_ip_blocked(ipaddress.ip_address("100.64.0.1"))
        assert is_ip_blocked(ipaddress.ip_address("100.127.255.255"))

    def test_169_254_0_0_16_blocked(self):
        """169.254.0.0/16：链路本地（含 metadata 169.254.169.254）。"""
        assert is_ip_blocked(ipaddress.ip_address("169.254.169.254"))
        assert is_ip_blocked(ipaddress.ip_address("169.254.0.1"))

    def test_172_16_0_0_12_blocked(self):
        """172.16.0.0/12：私有网络 RFC1918。"""
        assert is_ip_blocked(ipaddress.ip_address("172.16.0.1"))
        assert is_ip_blocked(ipaddress.ip_address("172.31.255.255"))

    def test_192_168_0_0_16_blocked(self):
        """192.168.0.0/16：私有网络 RFC1918。"""
        assert is_ip_blocked(ipaddress.ip_address("192.168.0.1"))
        assert is_ip_blocked(ipaddress.ip_address("192.168.1.1"))

    def test_127_0_0_0_8_blocked(self):
        """127.0.0.0/8：loopback（T02 新增，防探测本地 Redis/后端 API）。"""
        assert is_ip_blocked(ipaddress.ip_address("127.0.0.1"))
        assert is_ip_blocked(ipaddress.ip_address("127.255.255.255"))

    def test_ipv6_loopback_blocked(self):
        """::1/128：IPv6 loopback（T02 新增，等价 127.0.0.0/8）。

        注：T02 移除了 ::/0（曾拦截所有纯 IPv6，导致双栈网站被拒），
        改为只拦 ::1/128 + fc00::/7 + fe80::/10，公网 IPv6 放行。
        """
        assert is_ip_blocked(ipaddress.ip_address("::1"))

    def test_ipv6_public_not_blocked(self):
        """公网 IPv6 不应被拦（T02 移除 ::/0 后的回归断言）。"""
        assert not is_ip_blocked(ipaddress.ip_address("2606:4700::6810:85e5"))

    def test_fc00_7_blocked(self):
        """fc00::/7：IPv6 ULA。"""
        assert is_ip_blocked(ipaddress.ip_address("fd00::1"))

    def test_fe80_10_blocked(self):
        """fe80::/10：IPv6 链路本地。"""
        assert is_ip_blocked(ipaddress.ip_address("fe80::1"))


# ============================================================================
# Part 2: 公网 IP 通过
# ============================================================================


class TestPublicIpAllowed:
    """公网 IP 不应被拦。"""

    def test_8_8_8_8_allowed(self):
        """Google DNS 8.8.8.8 是公网 IP，应通过。"""
        assert not is_ip_blocked(ipaddress.ip_address("8.8.8.8"))

    def test_1_1_1_1_allowed(self):
        """Cloudflare 1.1.1.1 是公网 IP，应通过。"""
        assert not is_ip_blocked(ipaddress.ip_address("1.1.1.1"))

    def test_203_0_113_1_allowed(self):
        """203.0.113.0/24 是 TEST-NET-3（公网文档用），不在 BLOCKED_CIDRS。"""
        assert not is_ip_blocked(ipaddress.ip_address("203.0.113.1"))


# ============================================================================
# Part 3: IPv4-mapped IPv6 绕过防护
# ============================================================================


class TestIpv4MappedIpv6:
    """IPv4-mapped IPv6 应被提取后校验，防绕过。"""

    def test_extract_mapped_ipv4_basic(self):
        """::ffff:a9fe:a9fe → 169.254.169.254。"""
        addr = ipaddress.ip_address("::ffff:a9fe:a9fe")
        mapped = extract_mapped_ipv4(addr)
        assert mapped is not None
        assert str(mapped) == "169.254.169.254"

    def test_extract_mapped_ipv4_none_for_pure_ipv6(self):
        """纯 IPv6（非 mapped）→ 返回 None。"""
        addr = ipaddress.ip_address("2001:db8::1")
        assert extract_mapped_ipv4(addr) is None

    def test_mapped_ipv6_169_254_169_254_blocked(self):
        """::ffff:a9fe:a9fe 应命中 169.254.0.0/16（通过 mapped 提取）。"""
        # 不做 mapped 提取时，::ffff:a9fe:a9fe 是 IPv6，不在任何 IPv6 CIDR 内
        # 做 mapped 提取后，等价 169.254.169.254，命中 169.254.0.0/16
        assert is_ip_blocked(ipaddress.ip_address("::ffff:a9fe:a9fe"))

    def test_mapped_ipv6_10_0_0_1_blocked(self):
        """::ffff:0a00:0001 应命中 10.0.0.0/8（通过 mapped 提取）。"""
        # 10.0.0.1 = 0a00:0001
        assert is_ip_blocked(ipaddress.ip_address("::ffff:0a00:0001"))

    def test_mapped_ipv6_8_8_8_8_allowed(self):
        """::ffff:0808:0808 应通过（mapped 后是 8.8.8.8，公网）。"""
        assert not is_ip_blocked(ipaddress.ip_address("::ffff:0808:0808"))


# ============================================================================
# Part 4: check_url 集成测试
# ============================================================================


class TestCheckUrl:
    """check_url 函数测试（IP 字面量 + 域名）。"""

    def test_metadata_ip_blocked(self):
        """http://169.254.169.254/latest/meta-data/ 应被拦。"""
        ok, err = check_url("http://169.254.169.254/latest/meta-data/")
        assert not ok, f"D1: 169.254.169.254 should be blocked, got ok={ok}"
        assert "blocked CIDR" in err or "SSRF" in err

    def test_10_0_0_1_blocked(self):
        """http://10.0.0.1/ 应被拦。"""
        ok, err = check_url("http://10.0.0.1/")
        assert not ok
        assert "blocked CIDR" in err or "SSRF" in err

    def test_192_168_1_1_blocked(self):
        """http://192.168.1.1/ 应被拦。"""
        ok, err = check_url("http://192.168.1.1/")
        assert not ok
        assert "blocked CIDR" in err or "SSRF" in err

    def test_ipv6_metadata_blocked(self):
        """http://[::ffff:a9fe:a9fe]/ 应被拦（IPv4-mapped IPv6）。"""
        ok, err = check_url("http://[::ffff:a9fe:a9fe]/")
        assert not ok, (
            f"D1: ::ffff:a9fe:a9fe (mapped 169.254.169.254) should be blocked, "
            f"got ok={ok}"
        )

    def test_public_ip_allowed(self):
        """http://8.8.8.8/ 应通过。"""
        ok, err = check_url("http://8.8.8.8/")
        assert ok, f"D1: 8.8.8.8 should be allowed, got err={err}"

    def test_invalid_scheme_blocked(self):
        """ftp:// 应被拦（非 http/https）。"""
        ok, err = check_url("ftp://example.com/")
        assert not ok
        assert "scheme" in err.lower()

    def test_empty_url_blocked(self):
        """空 URL 应被拦。"""
        ok, err = check_url("")
        assert not ok
        assert "empty" in err.lower()

    def test_domain_resolves_to_blocked_ip(self):
        """域名 DNS 解析返回内部 IP → 应被拦（mock getaddrinfo）。"""
        # 模拟域名 internal.example.com 解析到 10.0.0.1
        def mock_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))]

        import socket
        with patch.object(socket, "getaddrinfo", side_effect=mock_getaddrinfo):
            ok, err = check_url("http://internal.example.com/")
        assert not ok, (
            f"D1: Domain resolving to 10.0.0.1 should be blocked, got ok={ok}"
        )
        assert "10.0.0.1" in err or "blocked CIDR" in err


# ============================================================================
# Part 5: web_fetch 集成测试
# ============================================================================


class TestWebFetchSsrfBlocked:
    """web_fetch 工具应返回 ssrf_blocked 而非直连。"""

    def _make_tool_call(self, url: str) -> ToolCall:
        return ToolCall(id="tc1", name="web_fetch", args={"url": url})

    def test_metadata_ip_returns_ssrf_blocked(self):
        """web_fetch 抓 169.254.169.254 → 返回 ssrf_blocked（不发 HTTP）。"""
        tool = WebFetchTool()
        tc = self._make_tool_call("http://169.254.169.254/latest/meta-data/")
        result = asyncio.run(tool.execute(tc))
        assert result.is_error, (
            f"D1: web_fetch on 169.254.169.254 should return error, "
            f"got content={result.content[:200]}"
        )
        assert result.error_type == "ssrf_blocked", (
            f"D1: error_type should be 'ssrf_blocked', "
            f"got {result.error_type}"
        )
        assert "SSRF" in result.content or "blocked" in result.content

    def test_10_0_0_1_returns_ssrf_blocked(self):
        """web_fetch 抓 10.0.0.1 → 返回 ssrf_blocked。"""
        tool = WebFetchTool()
        tc = self._make_tool_call("http://10.0.0.1/")
        result = asyncio.run(tool.execute(tc))
        assert result.is_error
        assert result.error_type == "ssrf_blocked"

    def test_ipv6_mapped_returns_ssrf_blocked(self):
        """web_fetch 抓 ::ffff:a9fe:a9fe → 返回 ssrf_blocked（防绕过）。"""
        tool = WebFetchTool()
        tc = self._make_tool_call("http://[::ffff:a9fe:a9fe]/")
        result = asyncio.run(tool.execute(tc))
        assert result.is_error, (
            "D1: web_fetch on ::ffff:a9fe:a9fe should be blocked (mapped 169.254.169.254)"
        )
        assert result.error_type == "ssrf_blocked"


# ============================================================================
# Part 6: ssrf_guarded_lookup 测试
# ============================================================================


import socket  # noqa: E402


class TestSsrfGuardedLookup:
    """ssrf_guarded_lookup 测试（防 DNS rebinding）。"""

    def test_ip_literal_blocked(self):
        """IP 字面量 169.254.169.254 → 拦截。"""
        ok, err, ip = ssrf_guarded_lookup("169.254.169.254", 80)
        assert not ok
        assert ip is None

    def test_ip_literal_allowed(self):
        """IP 字面量 8.8.8.8 → 通过，返回 IP。"""
        ok, err, ip = ssrf_guarded_lookup("8.8.8.8", 80)
        assert ok
        assert ip == "8.8.8.8"

    def test_domain_blocked_via_dns(self):
        """域名解析到 10.0.0.1 → 拦截（mock）。"""
        def mock_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))]

        with patch.object(socket, "getaddrinfo", side_effect=mock_getaddrinfo):
            ok, err, ip = ssrf_guarded_lookup("internal.example.com", 80)
        assert not ok
        assert ip is None
        assert "10.0.0.1" in err

    def test_domain_allowed_returns_validated_ip(self):
        """域名解析到公网 IP → 通过，返回 validated_ip（mock）。"""
        def mock_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch.object(socket, "getaddrinfo", side_effect=mock_getaddrinfo):
            ok, err, ip = ssrf_guarded_lookup("example.com", 80)
        assert ok, f"D1: example.com should be allowed, got err={err}"
        assert ip == "93.184.216.34", (
            f"D1: validated_ip should be 93.184.216.34, got {ip}"
        )
