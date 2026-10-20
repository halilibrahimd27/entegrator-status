"""The only outbound HTTP client.

Every hop is validated (scheme, credentials, blocked ranges), resolved once and the
connection is pinned to the validated IP, so a DNS answer that changes between check
and connect (rebinding) cannot redirect the request. TLS still verifies the original
host name (SNI + certificate). Redirects are followed by hand and re-validated.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Literal, Self

import httpx

from sentinel import __version__
from sentinel.security.addresses import (
    STRICT,
    AddressPolicy,
    IPAddress,
    Resolver,
    Target,
    parse_target,
    resolve_pinned,
    system_resolver,
)
from sentinel.security.errors import (
    ConnectFailedError,
    ConnectTimeoutError,
    OutboundError,
    ReadTimeoutError,
    ResponseTooLargeError,
    TLSError,
    TooManyRedirectsError,
    URLPolicyError,
)
from sentinel.security.ratelimit import Clock, HostLimiter

PAGE_MAX_BYTES = 2 * 1024 * 1024
CONTRACT_MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
# Headers a fetch may add; everything else (Host, User-Agent, encodings) is ours.
REQUEST_HEADERS = frozenset({"accept", "if-none-match", "if-modified-since"})
MAX_HEADER_VALUE = 256
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def build_user_agent(public_base: str, contact_email: str, version: str = __version__) -> str:
    """The User-Agent every request sends, pointing providers at our bot page."""
    return f"EntegratorStatus/{version} (+{public_base.rstrip('/')}/hakkinda/bot; {contact_email})"


@dataclass(frozen=True)
class Timeouts:
    connect_s: float = 5.0
    read_s: float = 10.0
    total_s: float = 20.0  # whole fetch, including DNS, redirects and body


@dataclass(frozen=True)
class FetchPolicy:
    """How one fetch may behave. ``POST`` exists only for read queries (ADR 0003 rule 2):
    it needs ``read_query=True`` and a JSON body, and never follows a redirect."""

    method: Literal["GET", "HEAD", "POST"] = "GET"
    allow_http: bool = False
    max_redirects: int = MAX_REDIRECTS
    max_bytes: int = PAGE_MAX_BYTES
    timeouts: Timeouts = field(default_factory=Timeouts)
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: bytes | None = None
    read_query: bool = False

    def __post_init__(self) -> None:
        if self.method == "POST":
            if not self.read_query or self.json_body is None:
                raise ValueError("POST is only for flagged read queries with a JSON body")
            if self.max_redirects != 0:
                raise ValueError("a read query never follows redirects")
        elif self.json_body is not None or self.read_query:
            raise ValueError("only a POST read query carries a body")
        for name, value in self.headers.items():
            if name.lower() not in REQUEST_HEADERS:
                raise ValueError(f"header {name!r} may not be set by a fetch")
            if len(value) > MAX_HEADER_VALUE or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"invalid value for header {name!r}")
        if not 0 <= self.max_redirects <= MAX_REDIRECTS:
            raise ValueError(f"max_redirects must be between 0 and {MAX_REDIRECTS}")
        if not 0 < self.max_bytes <= CONTRACT_MAX_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {CONTRACT_MAX_BYTES}")


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL, host name form
    status_code: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes
    elapsed_s: float
    peer_ip: str
    redirects: tuple[str, ...] = ()


class OutboundClient:
    """Async client; one instance per process, closed on shutdown."""

    def __init__(
        self,
        *,
        user_agent: str,
        limiter: HostLimiter | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        policy: AddressPolicy = STRICT,
    ) -> None:
        self._user_agent = user_agent
        self._policy = policy
        self._limiter = limiter or HostLimiter()
        self._resolver = resolver
        self._clock = clock
        # No keep-alive: pooled connections are keyed by IP, so a reused connection
        # could carry a request for a host name whose certificate it never checked.
        transport = transport or httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_keepalive_connections=0), retries=0
        )
        # trust_env=False: proxy environment variables would bypass IP pinning.
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def address_policy(self) -> AddressPolicy:
        return self._policy

    @property
    def resolver(self) -> Resolver:
        return self._resolver

    async def fetch(self, url: str, policy: FetchPolicy | None = None) -> FetchResult:
        """Fetch ``url`` under ``policy``. Raises :class:`OutboundError` subclasses only."""
        policy = policy or FetchPolicy()
        started = self._clock()
        redirects: list[str] = []
        current = url
        try:
            async with asyncio.timeout(policy.timeouts.total_s):
                while True:
                    target = parse_target(
                        current, allow_http=policy.allow_http, policy=self._policy
                    )
                    ip = await resolve_pinned(
                        target, self._resolver, policy.timeouts.connect_s, self._policy
                    )
                    async with self._limiter.slot(target.host):
                        status, headers, body = await self._send(target, ip, policy)
                    location = headers.get("location")
                    if status in REDIRECT_STATUSES and location:
                        if len(redirects) >= policy.max_redirects:
                            raise TooManyRedirectsError(f"more than {policy.max_redirects}")
                        current = _join(target, location)
                        redirects.append(current)
                        continue
                    return FetchResult(
                        url=str(target.url),
                        status_code=status,
                        headers=MappingProxyType(headers),
                        body=body,
                        elapsed_s=self._clock() - started,
                        peer_ip=str(ip),
                        redirects=tuple(redirects),
                    )
        except TimeoutError as exc:
            raise ReadTimeoutError("request exceeded its total time budget") from exc

    async def _send(
        self, target: Target, ip: IPAddress, policy: FetchPolicy
    ) -> tuple[int, dict[str, str], bytes]:
        request = self._client.build_request(
            policy.method,
            target.url.copy_with(host=str(ip)),
            content=policy.json_body,
            headers={
                **policy.headers,
                **({"Content-Type": "application/json"} if policy.json_body is not None else {}),
                "Host": target.host_header,
                "User-Agent": self._user_agent,
                # Size caps count decoded bytes; asking for identity keeps them honest.
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": target.host},
            timeout=httpx.Timeout(policy.timeouts.read_s, connect=policy.timeouts.connect_s),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise _translate(exc) from exc
        try:
            headers = {name.lower(): value for name, value in response.headers.items()}
            if response.status_code in REDIRECT_STATUSES or policy.method == "HEAD":
                return response.status_code, headers, b""
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > policy.max_bytes:
                raise ResponseTooLargeError(f"declared {declared} bytes > {policy.max_bytes}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > policy.max_bytes:
                    raise ResponseTooLargeError(f"body exceeded {policy.max_bytes} bytes")
            return response.status_code, headers, bytes(body)
        except httpx.HTTPError as exc:
            raise _translate(exc) from exc
        finally:
            await response.aclose()


def _join(target: Target, location: str) -> str:
    try:
        return str(target.url.join(location))
    except httpx.InvalidURL as exc:  # pragma: no cover - httpx rejects bad Location first
        raise URLPolicyError("malformed redirect location") from exc


def _translate(exc: httpx.HTTPError) -> OutboundError:
    if isinstance(exc, httpx.ConnectTimeout):
        return ConnectTimeoutError("connect timed out")
    if isinstance(exc, httpx.TimeoutException):
        return ReadTimeoutError("read timed out")
    if isinstance(exc, httpx.ConnectError) and _is_tls_failure(exc):
        return TLSError("TLS handshake or certificate verification failed")
    return ConnectFailedError(type(exc).__name__)


def _is_tls_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    text = str(exc).upper()
    return "SSL" in text or "CERTIFICATE" in text
