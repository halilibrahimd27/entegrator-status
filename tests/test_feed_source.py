"""Feed sources end to end: adapter, one node per change source, snapshots, changes, UI."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from org_support import create_org, create_service
from probe_support import Clock, Midpoint, client_for, env_for, job, outbound, two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.engine.changes import run_changes
from sentinel.engine.retention import run_retention
from sentinel.models import Change, Component, ProbeNode, Service, Snapshot, Source, SourceWatch
from sentinel.probe.buffer import ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.prober import Prober
from sentinel.probe.scheduler import Scheduler
from sentinel.sources.adapters import adapter_for
from sentinel.sources.config import validate_source
from sentinel.web.services import AppServices
from support import signed_in

FEED_URL = "https://vendor.test/duyurular.xml"


def rss(*titles: str) -> bytes:
    items = "".join(
        f"<item><title>{t}</title><link>https://vendor.test/{len(t)}</link><guid>{t}</guid></item>"
        for t in titles
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()


class Vendor:
    """A fake vendor site: robots.txt, a feed with ETag support, a request log."""

    def __init__(self) -> None:
        self.titles = ["Hoş geldiniz"]
        self.robots = "User-agent: *\nAllow: /\n"
        self.status = 200
        self.version = 1  # the ETag
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers={"Retry-After": "120"})
        etag = f'"{self.version}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=rss(*self.titles), headers={"ETag": etag})


async def run_feed(vendor: Vendor, **overrides: Any) -> Any:
    adapter = adapter_for("feed")
    assert adapter is not None
    async with outbound(vendor) as http:
        return await adapter.run(job(kind="feed", url=FEED_URL, **overrides), env_for(http))


async def test_feed_adapter() -> None:
    vendor = Vendor()
    first = await run_feed(vendor)
    assert first.ok
    assert first.snapshot is not None
    assert [i["title"] for i in first.snapshot.items] == ["Hoş geldiniz"]
    assert first.snapshot.validators == {"etag": '"1"'}
    assert "rss+xml" in vendor.requests[-1].headers["accept"]

    unchanged = await run_feed(vendor, validators={"etag": '"1"'})
    assert (unchanged.ok, unchanged.status_code, unchanged.snapshot) == (True, 304, None)
    assert vendor.requests[-1].headers["if-none-match"] == '"1"'

    vendor.status = 503
    busy = await run_feed(vendor)
    assert (busy.error_class, busy.retry_after_s) == ("http_5xx", 120)

    vendor.status = 200
    vendor.robots = "User-agent: *\nDisallow: /duyurular\n"
    fetched = len(vendor.requests)
    blocked = await run_feed(vendor, ignores_robots=True)  # never optional for content
    assert blocked.error_class == "blocked_by_robots"
    assert len(vendor.requests) == fetched  # the feed itself was not requested


@pytest.mark.parametrize(
    ("body", "error"), [(b"<html></html>", "parse_error"), (rss(), "no_items")]
)
async def test_unusable_feeds(body: bytes, error: str) -> None:
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    adapter = adapter_for("feed")
    assert adapter is not None
    async with outbound(site) as http:
        outcome = await adapter.run(job(kind="feed", url=FEED_URL), env_for(http))
    assert outcome.error_class == error


# --- through the core ---------------------------------------------------------------


async def feed_source(services: AppServices, org_id: uuid.UUID | None = None) -> Source:
    valid = validate_source("feed", {"url": FEED_URL})
    async with services.db.system("test setup") as s:
        service = Service(
            org_id=org_id, slug=f"v-{uuid.uuid4().hex[:6]}", name="Vendor", kind="other", tags=[]
        )
        s.add(service)
        await s.flush()
        component = Component(
            org_id=org_id, service_id=service.id, slug="duyurular", name="Duyurular", tags=[]
        )
        s.add(component)
        await s.flush()
        source = Source(
            org_id=org_id,
            component_id=component.id,
            kind=valid.kind.value,
            identity=valid.identity,
            config=valid.config,
            interval_s=valid.interval_s,
            timeout_s=valid.timeout_s,
        )
        s.add(source)
        return source


async def probe_once(app: FastAPI, services: AppServices, vendor: Vendor, tmp_path: Any) -> None:
    """Fetch jobs as node n1, run the feed job, ship the result."""
    creds = getattr(probe_once, "creds", None) or await services.nodes.register("n1", "test")
    probe_once.creds = creds  # type: ignore[attr-defined]
    clock = Clock()
    buffer = ResultBuffer(tmp_path / f"b-{uuid.uuid4().hex}.sqlite3")
    async with outbound(vendor) as http:
        prober = Prober(
            core=client_for(app, creds),
            env=env_for(http),
            buffer=buffer,
            health=NodeHealth([], http),
            scheduler=Scheduler(clock=clock, rng=Midpoint()),
        )
        assert await prober.refresh_jobs()
        clock.advance(3600)
        await asyncio.gather(*await prober.run_due())
        assert await prober.ship() == 1
    buffer.close()


async def test_feed_changes_flow(app: FastAPI, services: AppServices, tmp_path: Any) -> None:
    probe_once.__dict__.pop("creds", None)
    source = await feed_source(services)
    vendor = Vendor()
    await probe_once(app, services, vendor, tmp_path)  # baseline
    vendor.titles = ["UBL-TR 1.2.1 paketi 1 Ocak 2027 itibarıyla zorunlu", "Hoş geldiniz"]
    vendor.version = 2
    await probe_once(app, services, vendor, tmp_path)
    await probe_once(app, services, vendor, tmp_path)  # 304: nothing new
    assert vendor.requests[-1].headers.get("if-none-match") == '"2"'
    vendor.titles.reverse()
    vendor.version = 3  # new ETag, same items in another order: not a change
    await probe_once(app, services, vendor, tmp_path)

    report = await run_changes(services.db)
    assert (report.snapshots, report.changes) == (2, 1)
    async with services.db.system("test check") as s:
        changes = (await s.scalars(select(Change))).all()
        snapshots = await s.scalar(select(func.count()).select_from(Snapshot))
        watch = await s.get(SourceWatch, source.id)
    assert snapshots == 2
    (change,) = changes
    assert change.category == "package_update"
    assert change.title.startswith("UBL-TR 1.2.1")
    assert change.effective_at is not None
    assert change.effective_at.date().isoformat() == "2026-12-31"  # 1 Jan 2027 00:00 in Turkey
    assert change.component_id == source.component_id
    assert change.org_id is None
    assert watch is not None
    assert watch.attention is None
    assert watch.last_changed_at is not None
    assert (await run_changes(services.db)).changes == 0  # processed once


async def test_watch_state_follows_results(
    app: FastAPI, services: AppServices, tmp_path: Any
) -> None:
    probe_once.__dict__.pop("creds", None)
    source = await feed_source(services)
    vendor = Vendor()
    vendor.robots = "User-agent: *\nDisallow: /\n"
    await probe_once(app, services, vendor, tmp_path)
    async with services.db.system("test check") as s:
        watch = await s.get(SourceWatch, source.id)
        assert watch is not None
        assert (watch.attention, watch.reason) == ("blocked_by_robots", "disallowed")
    vendor.robots = "User-agent: *\nAllow: /\n"
    vendor.status = 500
    for _ in range(5):
        await probe_once(app, services, vendor, tmp_path)
    async with services.db.system("test check") as s:
        watch = await s.get(SourceWatch, source.id)
        assert watch is not None
        assert (watch.attention, watch.failures) == ("failing", 5)
    vendor.status = 200
    await probe_once(app, services, vendor, tmp_path)
    async with services.db.system("test check") as s:
        watch = await s.get(SourceWatch, source.id)
        assert watch is not None
        assert (watch.attention, watch.failures) == (None, 0)


async def test_one_node_fetches_each_change_source(services: AppServices) -> None:
    a, b = await two_orgs(services)
    feeds = [await feed_source(services, org) for org in (a, b, None)]
    nodes = [await services.nodes.register(f"n{i}", "test") for i in range(3)]
    async with services.db.system("test setup") as s:
        await s.execute(update(ProbeNode).values(last_seen_at=utcnow()))
    owners: dict[str, list[int]] = {}
    for i, creds in enumerate(nodes):
        for j in await services.nodes.jobs(creds.node_id):
            owners.setdefault(j.key, []).append(i)
    assert len(owners) == 1  # identical feeds of three owners: one job
    assert [len(v) for v in owners.values()] == [1]  # fetched by exactly one node
    everything = await services.nodes.jobs()
    assert sorted(everything[0].source_ids) == sorted(str(f.id) for f in feeds)


async def test_ingest_checks_snapshot_hashes(app: FastAPI, services: AppServices) -> None:
    source = await feed_source(services)
    creds = await services.nodes.register("n1", "test")
    forged = {
        "id": str(uuid.uuid4()),
        "source_ids": [str(source.id)],
        "observed_at": utcnow().isoformat(),
        "ok": True,
        "snapshot": {"hash": "0" * 64, "items": [{"id": "x", "title": "Sahte"}]},
    }
    from sentinel.security.internal import CoreRejectedError

    with pytest.raises(CoreRejectedError) as caught:
        await client_for(app, creds).send(uuid.uuid4(), [forged])
    assert caught.value.status_code == 422


async def test_service_page_shows_changes_and_watch(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    service = await create_service(http, slug)
    async with services.db.system("test setup") as s:
        component = await s.scalar(
            select(Component).where(Component.service_id == uuid.UUID(service))
        )
        assert component is not None
        source = Source(
            org_id=component.org_id,
            component_id=component.id,
            kind="feed",
            identity=f"feed:{FEED_URL}",
            config={"url": FEED_URL},
            interval_s=3600,
            timeout_s=10,
        )
        s.add(source)
        await s.flush()
        s.add(
            SourceWatch(
                org_id=component.org_id,
                source_id=source.id,
                last_checked_at=utcnow(),
                attention="needs_attention",
                reason="no_items",
            )
        )
        s.add(
            Change(
                org_id=component.org_id,
                source_id=source.id,
                component_id=component.id,
                kind="announcement",
                category="deprecation",
                title="Eski uç kullanımdan kaldırılacak",
                link="https://vendor.test/eski",
                detected_at=utcnow() - timedelta(hours=1),
                fingerprint="f" * 64,
            )
        )
    page = await http.get(f"/o/{slug}/servisler/{service}")
    assert "Eski uç kullanımdan kaldırılacak" in page.text
    assert "Kullanımdan kaldırma" in page.text
    assert 'rel="noopener noreferrer nofollow"' in page.text
    assert "Kontrol gerekiyor" in page.text
    assert "hiç öğe bulunamadı" in page.text


async def test_old_snapshots_are_pruned_but_the_latest_is_kept(services: AppServices) -> None:
    source = await feed_source(services)
    old = utcnow() - timedelta(days=40)
    async with services.db.system("test setup") as s:
        for days in (0, 1, 2):
            s.add(
                Snapshot(
                    org_id=None,
                    source_id=source.id,
                    taken_at=old + timedelta(days=days),
                    hash=f"{days}" * 64,
                    items=[],
                    processed_at=old,
                )
            )
    report = await run_retention(services.db)
    assert report.snapshots == 2
    async with services.db.system("test check") as s:
        kept = (await s.scalars(select(Snapshot.taken_at))).all()
    assert kept == [old + timedelta(days=2)]
