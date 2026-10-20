from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest

from sentinel.security import (
    AddressBlockedError,
    ConnectFailedError,
    ConnectTimeoutError,
    FetchPolicy,
    HostLimiter,
    OutboundClient,
    OutboundError,
    ReadTimeoutError,
    ResponseTooLargeError,
    Timeouts,
    TLSError,
    TooManyRedirectsError,
    URLPolicyError,
    build_user_agent,
)
from sentinel.security.addresses import IPAddress, Resolver

UA = build_user_agent("https://status.example.invalid/", "bot@status.example.invalid")
Handler = Callable[[httpx.Request], httpx.Response]


class Resolutions:
    """Resolver that answers from a table and records every lookup."""

    def __init__(self, table: dict[str, list[list[str]]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> Sequence[IPAddress]:
        self.calls.append(host)
        answers = self.table[host]
        current = answers.pop(0) if len(answers) > 1 else answers[0]
        return [ipaddress.ip_address(a) for a in current]


class Chunks(httpx.AsyncByteStream):
    """A body without Content-Length, delivered in chunks."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk, self.count = chunk, count

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            yield self.chunk


def client(
    handler: Handler, resolver: Resolver | None = None, limiter: HostLimiter | None = None
) -> OutboundClient:
    return OutboundClient(
        user_agent=UA,
        resolver=resolver or Resolutions({"portal.example": [["1.1.1.1"]]}),
        transport=httpx.MockTransport(handler),
        limiter=limiter or HostLimiter(rate_per_s=1000, burst=100),
    )


def test_user_agent_points_at_the_bot_page() -> None:
    assert UA.startswith("EntegratorStatus/")
    assert UA.endswith("(+https://status.example.invalid/hakkinda/bot; bot@status.example.invalid)")


async def test_request_is_pinned_to_the_validated_ip() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    async with client(handler) as c:
        result = await c.fetch("https://portal.example/login?x=1")

    request = seen[0]
    assert request.url.host == "1.1.1.1"
    assert request.headers["host"] == "portal.example"
    assert request.extensions["sni_hostname"] == "portal.example"
    assert request.headers["user-agent"] == UA
    assert (result.status_code, result.body, result.peer_ip) == (200, b"ok", "1.1.1.1")
    assert result.url == "https://portal.example/login?x=1"
    assert result.redirects == ()


async def test_dns_rebinding_cannot_reach_a_private_address() -> None:
    # First answer is public, the next one private: the classic rebinding pattern.
    resolver = Resolutions({"portal.example": [["1.1.1.1"], ["127.0.0.1"]]})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200)

    async with client(handler, resolver) as c:
        await c.fetch("https://portal.example/")
        with pytest.raises(AddressBlockedError):
            await c.fetch("https://portal.example/")

    assert resolver.calls == ["portal.example", "portal.example"]  # once per fetch
    assert seen == ["1.1.1.1"]  # the private answer was never connected to


async def test_redirect_to_a_private_address_is_blocked() -> None:
    resolver = Resolutions({"portal.example": [["1.1.1.1"]], "internal.example": [["10.0.0.5"]]})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://internal.example/admin"})

    async with client(handler, resolver) as c:
        with pytest.raises(AddressBlockedError):
            await c.fetch("https://portal.example/")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "location", ["https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/"]
)
async def test_redirect_to_a_private_ip_literal_is_blocked(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": location})

    async with client(handler) as c:
        with pytest.raises(AddressBlockedError):
            await c.fetch("https://portal.example/")


async def test_redirect_downgrade_to_http_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://portal.example/"})

    async with client(handler) as c:
        with pytest.raises(URLPolicyError):
            await c.fetch("https://portal.example/")


async def test_relative_redirect_is_followed_and_revalidated() -> None:
    resolver = Resolutions({"portal.example": [["1.1.1.1"]]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(307, headers={"Location": "/tr/giris"})
        return httpx.Response(200, content=b"giris")

    async with client(handler, resolver) as c:
        result = await c.fetch("https://portal.example/")
    assert result.body == b"giris"
    assert result.redirects == ("https://portal.example/tr/giris",)
    assert resolver.calls == ["portal.example", "portal.example"]


@pytest.mark.parametrize(("hops", "ok"), [(3, True), (4, False)])
async def test_at_most_three_redirects(hops: int, ok: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.strip("/") or 0)
        if step < hops:
            return httpx.Response(302, headers={"Location": f"/{step + 1}"})
        return httpx.Response(200)

    async with client(handler) as c:
        if ok:
            assert len((await c.fetch("https://portal.example/")).redirects) == hops
        else:
            with pytest.raises(TooManyRedirectsError):
                await c.fetch("https://portal.example/")


async def test_malformed_redirect_location_is_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://[::1"})

    async with client(handler) as c:
        with pytest.raises(OutboundError):
            await c.fetch("https://portal.example/")
    assert len(calls) == 1


async def test_declared_oversized_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    async with client(handler) as c:
        with pytest.raises(ResponseTooLargeError, match="declared"):
            await c.fetch("https://portal.example/", FetchPolicy(max_bytes=1024))


async def test_streamed_oversized_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Chunks(b"x" * 64 * 1024, count=40))  # 2.5 MB

    async with client(handler) as c:
        with pytest.raises(ResponseTooLargeError, match="exceeded"):
            await c.fetch("https://portal.example/")


async def test_body_at_the_cap_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Chunks(b"x" * 1024, count=4))

    async with client(handler) as c:
        result = await c.fetch("https://portal.example/", FetchPolicy(max_bytes=4096))
    assert len(result.body) == 4096


async def test_head_returns_no_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"X-Thing": "1"})

    async with client(handler) as c:
        result = await c.fetch("https://portal.example/", FetchPolicy(method="HEAD"))
    assert (result.body, result.headers["x-thing"]) == (b"", "1")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectTimeout("t"), ConnectTimeoutError),
        (httpx.ReadTimeout("t"), ReadTimeoutError),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad cert"), TLSError),
        (httpx.ConnectError("connection refused"), ConnectFailedError),
        (httpx.RemoteProtocolError("garbage"), ConnectFailedError),
    ],
)
async def test_transport_errors_are_translated(
    exc: httpx.HTTPError, expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with client(handler) as c:
        with pytest.raises(expected):
            await c.fetch("https://portal.example/")


async def test_tls_failure_detected_from_the_cause_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("handshake failed") from ssl.SSLError(1, "wrong version")

    async with client(handler) as c:
        with pytest.raises(TLSError):
            await c.fetch("https://portal.example/")


async def test_read_timeout_while_streaming_the_body() -> None:
    class Stalls(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            raise httpx.ReadTimeout("stalled")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stalls())

    async with client(handler) as c:
        with pytest.raises(ReadTimeoutError):
            await c.fetch("https://portal.example/")


async def test_total_time_budget_is_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    async with client(handler) as c:  # type: ignore[arg-type]  # async handler
        with pytest.raises(ReadTimeoutError, match="total"):
            await c.fetch("https://portal.example/", FetchPolicy(timeouts=Timeouts(total_s=0.05)))


async def test_one_request_per_host_at_a_time() -> None:
    resolver = Resolutions({"a.example": [["1.1.1.1"]], "b.example": [["8.8.8.8"]]})
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        active[host] = active.get(host, 0) + 1
        peak[host] = max(peak.get(host, 0), active[host])
        await asyncio.sleep(0.01)
        active[host] -= 1
        return httpx.Response(200)

    async with client(handler, resolver) as c:  # type: ignore[arg-type]  # async handler
        urls = ["https://a.example/"] * 4 + ["https://b.example/"] * 4
        await asyncio.gather(*(c.fetch(u) for u in urls))
    assert peak == {"a.example": 1, "b.example": 1}


@pytest.mark.parametrize(
    "kwargs",
    [{"max_redirects": 4}, {"max_redirects": -1}, {"max_bytes": 0}, {"max_bytes": 6 << 20}],
)
def test_policy_limits_cannot_be_loosened(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be between"):
        FetchPolicy(**kwargs)  # type: ignore[arg-type]


async def test_conditional_headers_are_sent_but_ours_cannot_be_replaced() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    policy = FetchPolicy(headers={"If-None-Match": '"abc"', "Accept": "application/rss+xml"})
    async with client(handler) as c:
        result = await c.fetch("https://portal.example/feed", policy)
    assert result.status_code == 304
    assert seen[0].headers["if-none-match"] == '"abc"'
    assert seen[0].headers["accept"] == "application/rss+xml"
    assert seen[0].headers["user-agent"] == UA


@pytest.mark.parametrize(
    "headers",
    [{"User-Agent": "spoofed"}, {"Host": "elsewhere"}, {"If-None-Match": "a\r\nX-Evil: 1"}],
)
def test_only_safe_headers_with_safe_values(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="header"):
        FetchPolicy(headers=headers)


async def test_read_query_posts_json_and_never_follows_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    policy = FetchPolicy(method="POST", json_body=b'{"q":1}', read_query=True, max_redirects=0)
    async with client(handler) as c:
        with pytest.raises(TooManyRedirectsError):
            await c.fetch("https://portal.example/v1/query", policy)
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].content == b'{"q":1}'
    assert seen[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "POST", "json_body": b"{}", "max_redirects": 0},  # not flagged
        {"method": "POST", "read_query": True, "max_redirects": 0},  # no body
        {"method": "POST", "json_body": b"{}", "read_query": True},  # would follow redirects
        {"json_body": b"{}"},  # a GET with a body
    ],
)
def test_post_is_only_a_flagged_read_query(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"POST|read query|body"):
        FetchPolicy(**kwargs)  # type: ignore[arg-type]
