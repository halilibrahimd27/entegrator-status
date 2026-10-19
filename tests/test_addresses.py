from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence

import pytest

from sentinel.security.addresses import (
    IPAddress,
    Resolver,
    is_blocked_address,
    parse_target,
    resolve_pinned,
    system_resolver,
)
from sentinel.security.errors import AddressBlockedError, DNSResolutionError, URLPolicyError

BLOCKED = [
    "0.0.0.0",
    "10.1.2.3",
    "100.64.0.1",  # CGNAT
    "100.127.255.254",
    "127.0.0.1",
    "169.254.169.254",  # cloud metadata
    "172.16.0.1",
    "172.31.255.255",
    "192.0.0.8",
    "192.0.2.10",
    "192.168.1.1",
    "198.18.0.1",
    "198.51.100.7",
    "203.0.113.5",
    "224.0.0.251",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",  # NAT64 → 10.0.0.1
    "2001::1",  # Teredo
    "2001:db8::1",
    "2002:a00:1::1",  # 6to4 → 10.0.0.1
    "fc00::1",
    "fd12:3456::1",
    "fe80::1",
    "fec0::1",
    "ff02::1",
]
PUBLIC = ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "2a00:1450:4001::1"]


def static(*answers: str) -> Resolver:
    async def resolver(host: str, port: int) -> Sequence[IPAddress]:
        return [ipaddress.ip_address(a) for a in answers]

    return resolver


@pytest.mark.parametrize("address", BLOCKED)
def test_blocked_ranges(address: str) -> None:
    assert is_blocked_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_allowed(address: str) -> None:
    assert not is_blocked_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", BLOCKED)
async def test_host_resolving_to_blocked_range_is_rejected(address: str) -> None:
    target = parse_target("https://provider.example/")
    with pytest.raises(AddressBlockedError):
        await resolve_pinned(target, static(address), 1.0)


@pytest.mark.parametrize(
    "url", ["https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data"]
)
def test_blocked_ip_literals_are_rejected_before_any_lookup(url: str) -> None:
    with pytest.raises(AddressBlockedError):
        parse_target(url)


async def test_mixed_answers_are_rejected() -> None:
    target = parse_target("https://provider.example/")
    with pytest.raises(AddressBlockedError):
        await resolve_pinned(target, static("1.1.1.1", "10.0.0.1"), 1.0)


async def test_first_allowed_answer_is_pinned() -> None:
    target = parse_target("https://provider.example/")
    ip = await resolve_pinned(target, static("1.1.1.1", "8.8.8.8"), 1.0)
    assert str(ip) == "1.1.1.1"


async def test_public_ip_literal_needs_no_lookup() -> None:
    target = parse_target("https://1.1.1.1/")
    assert str(await resolve_pinned(target, static(), 1.0)) == "1.1.1.1"


async def test_empty_answer_is_a_dns_error() -> None:
    with pytest.raises(DNSResolutionError):
        await resolve_pinned(parse_target("https://x.example/"), static(), 1.0)


async def test_slow_resolver_is_a_dns_error() -> None:
    async def slow(host: str, port: int) -> Sequence[IPAddress]:
        await asyncio.sleep(5)
        return []

    with pytest.raises(DNSResolutionError, match="timed out"):
        await resolve_pinned(parse_target("https://x.example/"), slow, 0.01)


@pytest.mark.parametrize(
    "url",
    ["http://provider.example/", "ftp://provider.example/", "file:///etc/passwd", "gopher://x/"],
)
def test_only_https_by_default(url: str) -> None:
    with pytest.raises(URLPolicyError):
        parse_target(url)


def test_http_needs_the_explicit_flag() -> None:
    assert parse_target("http://provider.example/", allow_http=True).port == 80
    with pytest.raises(URLPolicyError):
        parse_target("ftp://provider.example/", allow_http=True)


@pytest.mark.parametrize(
    "url", ["https://user:pass@provider.example/", "https://user@provider.example/"]
)
def test_credentials_in_url_are_rejected(url: str) -> None:
    with pytest.raises(URLPolicyError, match="credentials"):
        parse_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https:///path",
        "https://exa mple.com/",
        "https://[::1/",
        "https://[not-an-ip]/",
        "https://bad_host.example/",
        "https://-lead.example/",
        "https://a..b.example/",
        "https://" + "a" * 64 + ".example/",
    ],
)
def test_malformed_urls_are_rejected(url: str) -> None:
    with pytest.raises(URLPolicyError):
        parse_target(url)


def test_target_parts() -> None:
    target = parse_target("https://Portal.Example:8443/a?b=1#frag")
    assert (target.host, target.port, target.host_header) == (
        "portal.example",
        8443,
        "portal.example:8443",
    )
    assert "#" not in str(target.url)
    assert parse_target("https://portal.example/").host_header == "portal.example"
    assert parse_target("https://[2606:4700::1]:8443/").host_header == "[2606:4700::1]:8443"


def test_idna_host_is_ascii() -> None:
    assert parse_target("https://münchen.example/").host == "xn--mnchen-3ya.example"


async def test_system_resolver_returns_unique_addresses() -> None:
    addresses = await system_resolver("localhost", 443)
    assert addresses
    assert len(addresses) == len(set(addresses))
    assert all(ip.is_loopback for ip in addresses)


async def test_system_resolver_maps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise socket.gaierror(socket.EAI_NONAME, "not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(DNSResolutionError):
        await system_resolver("nowhere.example", 443)
