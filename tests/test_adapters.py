"""Availability adapters against local fake servers (loopback only, no real network)."""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import trustme
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from sentinel.probe.nodes import ERROR_CLASSES, PROBED_KINDS
from sentinel.probe.robots import RobotsCache
from sentinel.security.addresses import AddressPolicy, IPAddress
from sentinel.security.errors import DNSResolutionError
from sentinel.security.http import OutboundClient
from sentinel.security.tls import fetch_certificate
from sentinel.sources.adapters import REGISTRY, AdapterEnv, ProbeJob, adapter_for

HOST = "portal.test"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


async def loopback(host: str, port: int) -> Sequence[IPAddress]:
    if host.endswith(".test"):
        return [ipaddress.ip_address("127.0.0.1")]
    raise DNSResolutionError("unknown test host")


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture(scope="module")
def trust(ca: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return context


def server_context(ca: trustme.CA, **cert_args: Any) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(HOST, **cert_args).configure_cert(context)
    return context


@pytest.fixture
def plain() -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0)
    server.start()
    yield server
    server.clear()
    server.stop()


@pytest.fixture
def secure(ca: trustme.CA) -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0, ssl_context=server_context(ca))
    server.start()
    yield server
    server.clear()
    server.stop()


def make_env(
    trust_ctx: ssl.SSLContext | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> AdapterEnv:
    http = OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=loopback,
        policy=AddressPolicy.for_tests(),
        transport=transport
        or httpx.AsyncHTTPTransport(verify=trust_ctx or ssl.create_default_context(), retries=0),
    )

    async def tls(host: str, port: int, limit_s: float) -> Any:
        return await fetch_certificate(
            host,
            port,
            timeout_s=limit_s,
            resolver=loopback,
            policy=AddressPolicy.for_tests(),
            trust=trust_ctx,
        )

    async def dns(host: str, record_type: str, limit_s: float) -> Sequence[str]:
        return {"ok.test": ["192.0.2.10", "192.0.2.11"], "empty.test": []}.get(host) or []

    return AdapterEnv(http=http, robots=RobotsCache(http), tls=tls, dns=dns, now=lambda: NOW)


@pytest.fixture
async def env() -> AsyncIterator[AdapterEnv]:
    environment = make_env()
    yield environment
    await environment.http.aclose()


def job(kind: str, config: dict[str, Any], **overrides: Any) -> ProbeJob:
    values: dict[str, Any] = {
        "key": "k",
        "kind": kind,
        "config": config,
        "interval_s": 60,
        "timeout_s": 2,
        "allow_http": True,
        "ignores_robots": True,
        "source_ids": ("s",),
    }
    values.update(overrides)
    return ProbeJob(**values)


def url(server: HTTPServer, path: str = "/", scheme: str = "http") -> str:
    return f"{scheme}://{HOST}:{server.port}{path}"


async def run(kind: str, config: dict[str, Any], env: AdapterEnv, **overrides: Any) -> Any:
    adapter = adapter_for(kind)
    assert adapter is not None
    return await adapter.run(job(kind, config, **overrides), env)


# --- http -------------------------------------------------------------------------


async def test_http_ok_with_latency_and_headers(plain: HTTPServer, env: AdapterEnv) -> None:
    plain.expect_request("/").respond_with_data(
        "merhaba", headers={"Deprecation": "@1735689600", "Sunset": "Wed, 01 Jul 2026 00:00:00 GMT"}
    )
    outcome = await run("http", {"url": url(plain), "body_must_contain": "merhaba"}, env)
    assert outcome.ok
    assert outcome.status_code == 200
    assert outcome.latency_ms is not None
    assert outcome.details["deprecation"] == "@1735689600"
    assert outcome.details["sunset"].startswith("Wed, 01 Jul 2026")


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, "http_4xx"), (401, "http_4xx"), (500, "http_5xx"), (302, "assertion")],
)
async def test_http_status_classes(
    plain: HTTPServer, env: AdapterEnv, status: int, error: str
) -> None:
    plain.expect_request("/").respond_with_data("x", status=status)
    outcome = await run("http", {"url": url(plain)}, env)
    assert (outcome.ok, outcome.error_class, outcome.status_code) == (False, error, status)


async def test_retry_after_seconds_and_date(plain: HTTPServer, env: AdapterEnv) -> None:
    plain.expect_request("/a").respond_with_data("busy", status=503, headers={"Retry-After": "120"})
    later = format_datetime(NOW + timedelta(seconds=90), usegmt=True)
    plain.expect_request("/b").respond_with_data(
        "slow down", status=429, headers={"Retry-After": later}
    )
    first = await run("http", {"url": url(plain, "/a")}, env)
    second = await run("http", {"url": url(plain, "/b")}, env)
    assert (first.error_class, first.retry_after_s) == ("http_5xx", 120.0)
    assert second.error_class == "http_4xx"
    assert second.retry_after_s == pytest.approx(90, abs=1)


async def test_body_assertion(plain: HTTPServer, env: AdapterEnv) -> None:
    plain.expect_request("/").respond_with_data("bakımdayız")
    outcome = await run("http", {"url": url(plain), "body_must_contain": "Giriş"}, env)
    assert outcome.error_class == "assertion"


async def test_read_timeout(plain: HTTPServer, env: AdapterEnv) -> None:
    def slow(request: Request) -> Response:
        time.sleep(1.5)
        return Response("late")

    plain.expect_request("/").respond_with_handler(slow)
    outcome = await run("http", {"url": url(plain)}, env, timeout_s=1)
    assert outcome.error_class == "read_timeout"


async def test_connection_refused(env: AdapterEnv) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    outcome = await run("http", {"url": f"http://{HOST}:{free_port}/"}, env)
    assert outcome.error_class == "connect"


async def test_dns_failure(env: AdapterEnv) -> None:
    outcome = await run("http", {"url": "http://unknown.invalid/"}, env)
    assert outcome.error_class == "dns"


async def test_connect_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow network")

    environment = make_env(transport=httpx.MockTransport(handler))
    outcome = await run("http", {"url": "https://portal.test/"}, environment)
    assert outcome.error_class == "connect_timeout"


async def test_redirect_limit_and_size_cap(plain: HTTPServer, env: AdapterEnv) -> None:
    for i in range(5):
        plain.expect_request(f"/r{i}").respond_with_data(
            "", status=302, headers={"Location": f"/r{i + 1}"}
        )
    plain.expect_request("/big").respond_with_data("x" * (2 * 1024 * 1024 + 1))
    assert (await run("http", {"url": url(plain, "/r0")}, env)).error_class == "too_many_redirects"
    assert (await run("http", {"url": url(plain, "/big")}, env)).error_class == "response_too_large"


async def test_private_address_is_blocked_in_production_policy(plain: HTTPServer) -> None:
    http = OutboundClient(user_agent="test", resolver=loopback)  # strict policy
    environment = AdapterEnv(
        http=http, robots=RobotsCache(http), tls=make_env().tls, dns=make_env().dns
    )
    outcome = await run("http", {"url": url(plain)}, environment)
    assert outcome.error_class == "blocked_address"
    await http.aclose()


# --- robots.txt -----------------------------------------------------------------------


async def test_robots_disallow_and_crawl_delay(plain: HTTPServer, env: AdapterEnv) -> None:
    plain.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nCrawl-delay: 7\nDisallow: /private\n"
    )
    plain.expect_request("/public").respond_with_data("ok")
    plain.expect_request("/private/x").respond_with_data("fetched anyway")
    blocked = await run("http", {"url": url(plain, "/private/x")}, env, ignores_robots=False)
    allowed = await run("http", {"url": url(plain, "/public")}, env, ignores_robots=False)
    ignored = await run("http", {"url": url(plain, "/private/x")}, env, ignores_robots=True)
    assert blocked.error_class == "blocked_by_robots"
    assert allowed.ok
    assert ignored.ok  # the per-source flag skips robots.txt for single-URL availability probes
    decision = await env.robots.check(url(plain, "/public"), allow_http=True)
    assert decision.crawl_delay_s == 7.0
    robots_hits = [r for r, _ in plain.log if r.path == "/robots.txt"]
    assert len(robots_hits) == 1  # cached


@pytest.mark.parametrize(
    ("status", "allowed"), [(404, True), (410, True), (500, False), (503, False)]
)
async def test_robots_status_rules(
    plain: HTTPServer, env: AdapterEnv, status: int, allowed: bool
) -> None:
    plain.expect_request("/robots.txt").respond_with_data("", status=status)
    decision = await env.robots.check(url(plain, "/page"), allow_http=True)
    assert decision.allowed is allowed


# --- TLS ---------------------------------------------------------------------------------


async def test_https_with_trusted_and_untrusted_certificates(
    secure: HTTPServer, trust: ssl.SSLContext
) -> None:
    secure.expect_request("/").respond_with_data("ok")
    trusted = make_env(trust)
    untrusted = make_env()
    assert (await run("http", {"url": url(secure, scheme="https")}, trusted, allow_http=False)).ok
    outcome = await run("http", {"url": url(secure, scheme="https")}, untrusted, allow_http=False)
    assert outcome.error_class == "tls"
    await trusted.http.aclose()
    await untrusted.http.aclose()


async def test_tls_cert_adapter(secure: HTTPServer, trust: ssl.SSLContext) -> None:
    trusted = make_env(trust)
    outcome = await run("tls_cert", {"host": HOST, "port": secure.port}, trusted)
    assert outcome.ok
    assert outcome.details["verified"] is True
    assert outcome.details["days_left"] > 300
    assert "trustme" in outcome.details["issuer"]

    untrusted = await run("tls_cert", {"host": HOST, "port": secure.port}, make_env())
    assert untrusted.error_class == "tls"
    assert untrusted.details["verified"] is False
    assert untrusted.details["days_left"] > 300  # still read without verification


async def test_tls_cert_expired(ca: trustme.CA, trust: ssl.SSLContext) -> None:
    expired = HTTPServer(
        host="127.0.0.1",
        port=0,
        ssl_context=server_context(
            ca,
            not_before=datetime.now(UTC) - timedelta(days=60),
            not_after=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    expired.start()
    try:
        environment = make_env(trust)
        environment.now = lambda: datetime.now(UTC)
        outcome = await run("tls_cert", {"host": HOST, "port": expired.port}, environment)
        assert outcome.error_class == "tls"
        assert outcome.details["days_left"] < 0
        assert outcome.details["expiring_soon"] is True
    finally:
        expired.stop()


# --- dns and domain expiry ----------------------------------------------------------------


async def test_dns_adapter(env: AdapterEnv) -> None:
    ok = await run("dns", {"host": "ok.test"}, env)
    assert ok.ok
    assert ok.details["answers"] == 2
    assert (await run("dns", {"host": "empty.test"}, env)).error_class == "dns"


def rdap_transport(
    domain_status: int = 200, events: list[dict[str, str]] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "data.iana.org":
            return httpx.Response(
                200, json={"services": [[["test", "invalid"], ["https://rdap.registry.test/"]]]}
            )
        if request.headers["host"] == "rdap.registry.test":
            body = {
                "events": events
                if events is not None
                else [{"eventAction": "expiration", "eventDate": "2027-01-15T00:00:00Z"}]
            }
            return httpx.Response(domain_status, content=json.dumps(body))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def rdap_resolver(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("93.184.215.14")]  # any public address: the mock answers


def rdap_env(**kwargs: Any) -> AdapterEnv:
    http = OutboundClient(
        user_agent="test", resolver=rdap_resolver, transport=rdap_transport(**kwargs)
    )
    return AdapterEnv(
        http=http, robots=RobotsCache(http), tls=make_env().tls, dns=make_env().dns, now=lambda: NOW
    )


async def test_domain_expiry() -> None:
    outcome = await run("domain_expiry", {"domain": "example.test"}, rdap_env())
    assert outcome.ok
    assert outcome.details["days_left"] == 86
    assert (
        await run("domain_expiry", {"domain": "example.com"}, rdap_env())
    ).error_class == "unsupported"
    assert (
        await run("domain_expiry", {"domain": "example.test"}, rdap_env(events=[]))
    ).error_class == "unsupported"
    assert (
        await run("domain_expiry", {"domain": "example.test"}, rdap_env(domain_status=404))
    ).error_class == "http_4xx"
    expired = rdap_env(events=[{"eventAction": "expiration", "eventDate": "2026-10-01T00:00:00Z"}])
    assert (await run("domain_expiry", {"domain": "example.test"}, expired)).details[
        "reason"
    ] == "expired"


# --- the contract --------------------------------------------------------------------------


def test_every_probed_kind_has_an_adapter() -> None:
    assert set(REGISTRY) >= set(PROBED_KINDS)
    assert adapter_for("no-such-kind") is None


@pytest.mark.parametrize("kind", sorted(k.value for k in PROBED_KINDS))
async def test_adapters_never_raise(kind: str) -> None:
    class Broken:
        async def fetch(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        async def check(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

    async def broken(*args: Any) -> Any:
        raise RuntimeError("boom")

    environment = AdapterEnv(http=Broken(), robots=Broken(), tls=broken, dns=broken)  # type: ignore[arg-type]
    config = {"url": "https://x.test/", "host": "x.test", "domain": "x.test"}
    outcome = await adapter_for(kind).run(job(kind, config, ignores_robots=False), environment)  # type: ignore[union-attr]
    assert outcome.ok is False
    assert outcome.error_class in ERROR_CLASSES
