"""D1（spec D15）：SSRF 防护层。

设计依据：
- v6 标准 9 条 BLOCKED_CIDRS（防内部网络探测）
- IPv4-mapped IPv6 绕过防护（::ffff:a9fe:a9fe → 169.254.169.254）
- DNS rebinding 防护（解析后校验 IP，socket 连 validated IP）

使用方式：
    from client.core.agent.builtin_tools.ssrf_guard import check_url
    ok, err = check_url("http://169.254.169.254/latest/meta-data/")
    if not ok:
        return ToolResult(variant=ERROR, error_type="ssrf_blocked", content=err)

注：当前 web_fetch/web_search 已禁用（D16/D17），但 SSRF 防护逻辑写好，
恢复时生效。所有外部 HTTP 工具调用前都应过 check_url。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger("localagent.agent.ssrf_guard")

# SSRF 防护 CIDR 黑名单（10 条）
# IPv4：
# - 0.0.0.0/8：当前网络（不应在公网出现）
# - 10.0.0.0/8：私有网络 RFC1918
# - 100.64.0.0/10：CGNAT（运营商级 NAT，可能被用来探测内网）
# - 127.0.0.0/8：loopback（防探测本地 Redis/后端 API 等）
# - 169.254.0.0/16：链路本地（含 AWS/GCP/Azure metadata 服务 169.254.169.254）
# - 172.16.0.0/12：私有网络 RFC1918
# - 192.168.0.0/16：私有网络 RFC1918
# IPv6：
# - ::1/128：loopback（等价 127.0.0.0/8）
# - fc00::/7：IPv6 唯一本地地址（ULA，等价 RFC1918）
# - fe80::/10：IPv6 链路本地（等价 169.254.0.0/16）
#
# 注：不再使用 ::/0（曾拦截所有纯 IPv6，导致双栈网站如 Google/GitHub 被拒）。
# 公网 IPv6 地址现在放行，只拦 loopback + ULA + 链路本地。
BLOCKED_CIDRS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def extract_mapped_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """提取 IPv4-mapped IPv6 地址中的 IPv4 部分。

    IPv4-mapped IPv6 格式：::ffff:a.b.c.d（前 80 位 0，中间 16 位 1，最后 32 位 IPv4）
    例：::ffff:a9fe:a9fe → 169.254.169.254

    攻击者可能用此格式绕过 IPv4 CIDR 校验：
    - 输入 http://[::ffff:a9fe:a9fe]/ → DNS 解析返回 ::ffff:a9fe:a9fe
    - 不做 mapped 提取 → 不命中 169.254.0.0/16（IPv6 CIDR）
    - 提取后 → 169.254.169.254 → 命中 169.254.0.0/16 → 拦截
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    # IPv4-mapped IPv6 的 ipv4_mapped 属性返回 IPv4Address，否则返回 None
    return addr.ipv4_mapped


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否在 BLOCKED_CIDRS 内（含 IPv4-mapped IPv6 处理）。

    逻辑分层（D1 修正 + T02 修复）：
    - IPv4：仅校验 IPv4 CIDRs
    - IPv4-mapped IPv6（::ffff:a.b.c.d）：仅校验 mapped IPv4 部分 against IPv4 CIDRs
      （不命中 IPv6 CIDRs，否则所有 mapped IPv6 都会被拦——违反 mapped 公网 IP 应放行）
    - 纯 IPv6：校验 IPv6 CIDRs（::1/128 + fc00::/7 + fe80::/10）
      （T02 修复：移除 ::/0，不再过度拦截公网 IPv6；双栈网站 Google/GitHub 可正常访问）
    """
    # IPv4：仅校验 IPv4 CIDRs
    if isinstance(ip, ipaddress.IPv4Address):
        for cidr in BLOCKED_CIDRS:
            if cidr.version == 4 and ip in cidr:
                return True
        return False

    # IPv6：先看是否 IPv4-mapped
    if isinstance(ip, ipaddress.IPv6Address):
        mapped_v4 = extract_mapped_ipv4(ip)
        if mapped_v4 is not None:
            # IPv4-mapped：仅校验 IPv4 部分针对 IPv4 CIDRs（防 ::/0 误拦公网 mapped）
            for cidr in BLOCKED_CIDRS:
                if cidr.version == 4 and mapped_v4 in cidr:
                    return True
            return False
        # 纯 IPv6：校验 IPv6 CIDRs（含 ::/0 → fail-closed）
        for cidr in BLOCKED_CIDRS:
            if cidr.version == 6 and ip in cidr:
                return True
        return False

    return False


def check_url(url: str) -> tuple[bool, str]:
    """校验 URL 是否安全（不含内部网络 IP）。

    Returns:
        (True, ""): URL 安全
        (False, reason): URL 不安全，reason 含拒绝原因

    校验流程：
    1. 解析 URL，提取 host
    2. host 是 IP 字面量 → 直接校验（含 IPv4-mapped IPv6 提取）
    3. host 是域名 → DNS 解析后校验所有返回 IP
       - 任一 IP 命中 BLOCKED_CIDRS → 拒绝（防 DNS rebinding：解析后校验）

    注：此函数只做 IP 校验，不防 DNS rebinding 的 TOCTOU（解析后连接时
    DNS 返回不同 IP）。完整防 rebinding 需 ssrf_guarded_lookup（见下），
    它解析后用 validated IP 直接 socket 连接，绕过二次 DNS 查询。
    """
    if not url or not isinstance(url, str):
        return False, "empty url"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse failed: {e}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid scheme (only http/https allowed): {parsed.scheme}"

    host = parsed.hostname
    if not host:
        return False, "no hostname in URL"

    # 尝试解析为 IP 字面量（含 IPv6）
    try:
        # ip_address 能识别 IPv4 和 IPv6（含 IPv4-mapped）
        ip = ipaddress.ip_address(host)
        if is_ip_blocked(ip):
            return False, f"IP {ip} is in blocked CIDR (SSRF protection)"
        # IP 字面量安全 → 通过（不需要 DNS 解析）
        return True, ""
    except ValueError:
        # 不是 IP 字面量 → 是域名，继续 DNS 解析
        pass

    # DNS 解析后校验所有返回 IP
    try:
        # getaddrinfo 返回 list of (family, type, proto, canonname, sockaddr)
        # sockaddr 是 (host, port) for IPv4 或 (host, port, flowinfo, scope_id) for IPv6
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for {host}: {e}"

    for _family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # 不应发生，但兜底
            continue
        if is_ip_blocked(ip):
            return False, (
                f"DNS resolved {host} to {ip} which is in blocked CIDR "
                f"(SSRF protection)"
            )

    return True, ""


def ssrf_guarded_lookup(host: str, port: int) -> tuple[bool, str, str | None]:
    """SSRF 防护的 DNS 查询（防 rebinding）。

    Returns:
        (True, "", validated_ip): 校验通过，validated_ip 可用于 socket 直连
        (False, reason, None): 校验失败

    流程：
    1. DNS 解析 host → 获取所有 IP
    2. 校验所有 IP 都不在 BLOCKED_CIDRS
    3. 任一不通过 → 拒绝
    4. 全通过 → 返回第一个 validated_ip，调用方用它直接 socket 连接
       （不再二次 DNS 查询，防 rebinding TOCTOU）

    用法：
        ok, err, ip = ssrf_guarded_lookup("example.com", 443)
        if not ok:
            return error
        # 用 ip 直连，不用 host（防 rebinding）
        sock = socket.create_connection((ip, port))
    """
    if not host:
        return False, "empty host", None

    # 先尝试作为 IP 字面量
    try:
        ip = ipaddress.ip_address(host)
        if is_ip_blocked(ip):
            return False, f"IP {ip} is in blocked CIDR", None
        return True, "", host
    except ValueError:
        pass

    # DNS 解析
    try:
        addrs = socket.getaddrinfo(host, port)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for {host}: {e}", None

    validated_ip: str | None = None
    for _family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if is_ip_blocked(ip):
            return False, (
                f"DNS resolved {host} to {ip} which is in blocked CIDR"
            ), None
        if validated_ip is None:
            validated_ip = ip_str

    if validated_ip is None:
        return False, f"No valid IP for {host}", None

    return True, "", validated_ip
