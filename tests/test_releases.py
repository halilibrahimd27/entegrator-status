"""Release sources: each ecosystem's front door, robots.txt, and release changes."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from probe_support import env_for, job
from sentinel.db.base import utcnow
from sentinel.engine.changes import run_changes
from sentinel.models import Change, Component, Service, Snapshot, Source
from sentinel.security.addresses import AddressPolicy, IPAddress
from sentinel.security.http import OutboundClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.sources.adapters import adapter_for
from sentinel.sources.snapshots import snapshot_hash
from sentinel.web.services import AppServices

PYPI_ROBOTS = "User-agent: *\nDisallow: /simple/\nDisallow: /pypi/*/json\n"


class Registry:
    """Fake registries: answers by host and path, records what was asked."""

    def __init__(self, routes: dict[str, Any], robots: dict[str, str] | None = None) -> None:
        self.routes = routes
        self.robots = robots or {}
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"].split(":")[0]
        if request.url.path == "/robots.txt":
            text = self.robots.get(host)
            return httpx.Response(200, text=text) if text else httpx.Response(404)
        key = f"{host}{request.url.raw_path.decode()}"
        self.asked.append(key)
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404)
        if isinstance(answer, bytes):
            return httpx.Response(200, content=answer)
        return httpx.Response(200, json=answer, headers={"ETag": '"e1"'})


async def anywhere(host: str, port: int) -> Sequence[IPAddress]:
    """Every registry host "resolves" to loopback; MockTransport never connects."""
    return [ipaddress.ip_address("127.0.0.1")]


async def releases(registry: Registry, ecosystem: str, name: str) -> Any:
    adapter = adapter_for("release")
    assert adapter is not None
    http = OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=anywhere,
        policy=AddressPolicy.for_tests(),
        transport=httpx.MockTransport(registry),
        limiter=HostLimiter(rate_per_s=100, burst=10),
    )
    async with http:
        return await adapter.run(
            job(
                kind="release", url="https://x.test/", config={"ecosystem": ecosystem, "name": name}
            ),
            env_for(http),
        )


def versions(outcome: Any) -> list[str]:
    assert outcome.ok, outcome
    return [item["version"] for item in outcome.snapshot.items]


async def test_github_releases_and_tag_fallback() -> None:
    registry = Registry(
        {
            "api.github.com/repos/acme/sdk/releases?per_page=30": [
                {"tag_name": "v2.0.0", "name": "SDK 2.0", "html_url": "https://github.com/acme/sdk/releases/tag/v2.0.0",
                 "published_at": "2026-10-01T10:00:00Z", "body": "BREAKING CHANGE: auth moved"},
                {"tag_name": "v2.1.0-rc.1", "prerelease": True},
                {"tag_name": "v1.9.0", "draft": True},
                {"tag_name": "nightly"},
            ],
            "api.github.com/repos/acme/empty/releases?per_page=30": [],
            "api.github.com/repos/acme/empty/tags?per_page=30": [
                {"name": "v0.3.0"}, {"name": "v0.2.0"}],
        }
    )  # fmt: skip
    outcome = await releases(registry, "github", "acme/sdk")
    assert versions(outcome) == ["v2.1.0-rc.1", "v2.0.0"]  # drafts and non-versions dropped
    stable = outcome.snapshot.items[1]
    assert stable["breaking"] is True  # flagged from the notes; the notes are not kept
    assert "body" not in stable
    assert stable["published"] == "2026-10-01T10:00:00+00:00"
    assert outcome.snapshot.items[0]["prerelease"] is True
    fallback = await releases(registry, "github", "acme/empty")
    assert versions(fallback) == ["v0.3.0", "v0.2.0"]
    assert fallback.snapshot.items[0]["link"] == "https://github.com/acme/empty/releases/tag/v0.3.0"


async def test_npm_abbreviated_metadata() -> None:
    registry = Registry(
        {
            "registry.npmjs.org/@acme%2Fclient": {
                "dist-tags": {"latest": "3.1.0"},
                "versions": {"3.1.0": {}, "3.0.0": {"deprecated": "use 3.1"}, "4.0.0-beta.1": {}},
            }
        }
    )
    outcome = await releases(registry, "npm", "@acme/client")
    assert versions(outcome) == ["4.0.0-beta.1", "3.1.0", "3.0.0"]
    by_version = {i["version"]: i for i in outcome.snapshot.items}
    assert by_version["3.1.0"]["latest"] is True
    assert by_version["3.0.0"]["deprecated"] is True
    assert outcome.snapshot.validators == {"etag": '"e1"'}


async def test_pypi_uses_the_rss_feed_robots_allow() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>2.32.0</title><link>https://pypi.org/project/acme/2.32.0/</link>
        <pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate></item>
      <item><title>2.31.0</title><link>https://pypi.org/project/acme/2.31.0/</link></item>
    </channel></rss>"""
    registry = Registry(
        {"pypi.org/rss/project/acme/releases.xml": feed}, robots={"pypi.org": PYPI_ROBOTS}
    )
    outcome = await releases(registry, "pypi", "Acme")
    assert versions(outcome) == ["2.32.0", "2.31.0"]
    assert registry.asked == ["pypi.org/rss/project/acme/releases.xml"]  # never /pypi/*/json


async def test_docker_hub_keeps_plain_version_tags() -> None:
    tags = ("1.27.2", "1.27-alpine", "latest", "mainline", "1.26.2")
    path = "hub.docker.com/v2/repositories/library/nginx/tags?page_size=50&ordering=last_updated"
    registry = Registry({path: {"results": [{"name": n} for n in tags]}})
    outcome = await releases(registry, "oci", "nginx")
    assert versions(outcome) == ["1.27.2", "1.26.2"]
    assert outcome.snapshot.items[0]["link"] == "https://hub.docker.com/_/nginx"


async def test_robots_unsupported_and_broken_answers() -> None:
    blocked = await releases(
        Registry({}, robots={"api.github.com": "User-agent: *\nDisallow: /repos/\n"}),
        "github",
        "acme/sdk",
    )
    assert blocked.error_class == "blocked_by_robots"
    garbled = await releases(Registry({"registry.npmjs.org/acme": {"name": "acme"}}), "npm", "acme")
    assert garbled.error_class == "parse_error"
    missing = await releases(Registry({}), "npm", "nope")
    assert missing.error_class == "http_4xx"
    odd = await releases(Registry({}), "cargo", "x")
    assert odd.error_class == "unsupported"


# --- release changes ------------------------------------------------------------------


async def release_source(services: AppServices) -> Source:
    async with services.db.system("test setup") as s:
        service = Service(slug="acme-sdk", name="Acme SDK", kind="package", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="sdk", name="SDK", tags=[])
        s.add(component)
        await s.flush()
        source = Source(
            component_id=component.id,
            kind="release",
            identity="release:github:acme/sdk",
            config={"ecosystem": "github", "name": "acme/sdk"},
            interval_s=21600,
            timeout_s=10,
        )
        s.add(source)
        return source


def item(version: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"v:{version}",
        "version": version,
        "title": version,
        "link": None,
        "published": None,
        "prerelease": "-" in version,
        "breaking": False,
        **extra,
    }


async def snap(
    services: AppServices, source: Source, items: list[dict[str, Any]], minutes: int
) -> None:
    async with services.db.system("test setup") as s:
        s.add(
            Snapshot(
                source_id=source.id,
                taken_at=utcnow() - timedelta(hours=2) + timedelta(minutes=minutes),
                hash=snapshot_hash(items),
                items=items,
            )
        )


async def test_release_changes(services: AppServices) -> None:
    source = await release_source(services)
    await snap(services, source, [item("1.9.4"), item("1.8.0")], 0)
    await snap(
        services,
        source,
        [
            item("2.0.0", breaking=True),
            item("2.0.1"),
            item("1.9.5"),
            item("2.1.0-rc.1"),
            item("1.9.4"),
        ],
        10,
    )
    report = await run_changes(services.db)
    assert report.changes == 4
    async with services.db.system("test check") as s:
        rows = {c.detail["version"]: c for c in (await s.scalars(select(Change))).all()}
    assert {v: c.category for v, c in rows.items()} == {
        "2.0.0": "major",
        "2.0.1": "patch",
        "1.9.5": "patch",  # a backport of the 1.9 line, not "other"
        "2.1.0-rc.1": "prerelease",
    }
    assert rows["2.0.0"].detail == {
        "version": "2.0.0",
        "previous": "1.9.5",  # the closest older stable version, even from the same batch
        "breaking_notes": True,
        "deprecated": False,
    }
    assert rows["2.0.0"].title == "acme/sdk 2.0.0"
    assert all(c.kind == "release" and c.org_id is None for c in rows.values())
    assert uuid.UUID(str(rows["2.0.1"].component_id)) == source.component_id
