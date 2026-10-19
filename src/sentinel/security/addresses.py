"""URL policy, blocked address ranges and DNS resolution for outbound requests."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import httpx

from sentinel.security.errors import AddressBlockedError, DNSResolutionError, URLPolicyError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

# Explicit list on top of ``is_global`` so the policy is readable and does not
# depend on the Python version's view of the IANA special-purpose registries.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, cloud metadata
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64 (embeds IPv4)
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments, incl. Teredo
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds IPv4)
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
    )
)

DEFAULT_PORTS = {"https": 443, "http": 80}

# Strict LDH host names (after IDNA). httpx accepts some hosts that resolvers or
# other parsers read differently (e.g. with spaces); reject anything unusual.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def is_blocked_address(ip: IPAddress) -> bool:
    """True when ``ip`` must never be contacted."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_blocked_address(ip.ipv4_mapped)
    if any(ip.version == net.version and ip in net for net in BLOCKED_NETWORKS):
        return True
    return not ip.is_global


@dataclass(frozen=True)
class Target:
    """A validated URL, split into the parts the pinned connection needs."""

    url: httpx.URL
    host: str  # ASCII (IDNA) host name or IP literal, lower case
    port: int

    @property
    def host_header(self) -> str:
        literal = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == DEFAULT_PORTS[self.url.scheme]:
            return literal
        return f"{literal}:{self.port}"


def parse_target(raw_url: str, *, allow_http: bool = False) -> Target:
    """Validate scheme, credentials and host. Does not touch the network."""
    try:
        url = httpx.URL(raw_url)
        host = url.raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, UnicodeError, TypeError) as exc:
        raise URLPolicyError("malformed URL") from exc

    allowed = {"https", "http"} if allow_http else {"https"}
    if url.scheme not in allowed:
        raise URLPolicyError(f"scheme '{url.scheme}' is not allowed")
    if url.userinfo:
        raise URLPolicyError("credentials in URLs are not allowed")
    if not host:
        raise URLPolicyError("URL has no host")

    literal = _ip_literal(host)
    if literal is None and not _HOSTNAME_RE.match(host.rstrip(".")):
        raise URLPolicyError("host is not a valid host name")
    if literal is not None and is_blocked_address(literal):
        raise AddressBlockedError("address is in a blocked range")

    port = url.port or DEFAULT_PORTS[url.scheme]
    return Target(url=url.copy_with(fragment=None), host=host, port=port)


async def system_resolver(host: str, port: int) -> Sequence[IPAddress]:
    """Resolve ``host`` with the OS resolver (all A/AAAA answers, in order)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DNSResolutionError("name could not be resolved") from exc
    addresses: list[IPAddress] = []
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if ip not in addresses:
            addresses.append(ip)
    return addresses


async def resolve_pinned(target: Target, resolver: Resolver, timeout_s: float) -> IPAddress:
    """Resolve once and return the address to connect to.

    Every answer must be allowed: one blocked answer rejects the host, so a
    resolver that mixes public and private records cannot steer the request.
    """
    literal = _ip_literal(target.host)
    if literal is not None:
        return literal
    try:
        async with asyncio.timeout(timeout_s):
            addresses = list(await resolver(target.host, target.port))
    except TimeoutError as exc:
        raise DNSResolutionError("name resolution timed out") from exc
    if not addresses:
        raise DNSResolutionError("name has no addresses")
    if any(is_blocked_address(ip) for ip in addresses):
        raise AddressBlockedError("host resolves to a blocked range")
    return addresses[0]


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
