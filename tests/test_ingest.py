"""Probe nodes: registration, signed job list and result ingest."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from probe_support import add_source, client_for, two_orgs
from sentinel.db.base import utcnow
from sentinel.models import Observation, ProbeNode, Source
from sentinel.probe.__main__ import main as probe_cli
from sentinel.probe.nodes import NodeCredentials
from sentinel.probe.settings import load_probe_settings_or_exit
from sentinel.probe.signing import node_signing_key, sign, verify
from sentinel.security.internal import CoreRejectedError
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices


def result(ids: list[uuid.UUID], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "source_ids": [str(s) for s in ids],
        "observed_at": utcnow().isoformat(),
        "ok": True,
        "status_code": 200,
        "latency_ms": 123,
        "details": {"tls_days_left": 40},
    }
    data.update(overrides)
    return data


def test_signatures() -> None:
    key = node_signing_key("k" * 40, uuid.uuid4())
    signature = sign(key, 1000, "batch", b"body")
    assert verify(key, 1000, "batch", b"body", signature)
    assert not verify(key, 1000, "batch", b"bodY", signature)
    assert not verify(key, 1001, "batch", b"body", signature)
    assert not verify(key, 1000, "other", b"body", signature)
    assert not verify(node_signing_key("j" * 40, uuid.uuid4()), 1000, "batch", b"body", signature)


async def test_register_and_authenticate(services: AppServices) -> None:
    creds = await services.nodes.register("istanbul-1", "İstanbul")
    assert await services.nodes.authenticate(creds.node_id, creds.token)
    assert not await services.nodes.authenticate(creds.node_id, creds.token + "x")
    assert not await services.nodes.authenticate(uuid.uuid4(), creds.token)
    async with services.db.system("test check") as s:
        node = await s.get(ProbeNode, creds.node_id)
        assert node is not None
        assert creds.token.encode() not in node.token_hash
        await s.execute(update(ProbeNode).values(enabled=False))
    assert not await services.nodes.authenticate(creds.node_id, creds.token)
    with pytest.raises(ValueError, match="already exists"):
        await services.nodes.register("istanbul-1", "elsewhere")


async def test_register_cli(services: AppServices, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    code = await asyncio.to_thread(probe_cli, ["register", "ankara-1", "Ankara"])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "PROBE_NODE_ID",
        "PROBE_NODE_TOKEN",
        "PROBE_SIGNING_KEY",
    ]


async def test_jobs_group_identical_sources(app: FastAPI, services: AppServices) -> None:
    a, b = await two_orgs(services)
    shared = await add_source(services, None, "https://shared.example.invalid/")
    private_a = await add_source(services, a, "https://same.example.invalid/")
    private_b = await add_source(services, b, "https://same.example.invalid/")
    other = await add_source(services, b, "https://other.example.invalid/")
    await add_source(services, a, "left-pad", kind="release")  # not probed yet
    disabled = await add_source(services, a, "https://disabled.example.invalid/")
    async with services.db.system("test change") as s:
        await s.execute(update(Source).where(Source.id == disabled).values(enabled=False))

    creds = await services.nodes.register("n1", "test")
    jobs = await client_for(app, creds).jobs()
    by_sources = sorted(tuple(sorted(j["source_ids"])) for j in jobs)
    assert by_sources == sorted(
        [
            (str(shared),),
            tuple(sorted([str(private_a), str(private_b)])),
            (str(other),),
        ]
    )
    assert all(j["kind"] == "http" for j in jobs)


async def test_ingest_stores_observations_per_source(app: FastAPI, services: AppServices) -> None:
    a, b = await two_orgs(services)
    source_a = await add_source(services, a, "https://same.example.invalid/")
    source_b = await add_source(services, b, "https://same.example.invalid/")
    shared = await add_source(services, None, "https://shared.example.invalid/")
    creds = await services.nodes.register("n1", "test")
    core = client_for(app, creds)

    batch = uuid.uuid4()
    first = [
        result([source_a, source_b]),
        result([shared], ok=False, error_class="http_5xx", status_code=503),
    ]
    assert await core.send(batch, first) == 3
    assert await core.send(batch, first) == 0  # replayed batch id: already stored
    assert await core.send(uuid.uuid4(), first) == 0  # same result ids: idempotent
    assert await core.send(uuid.uuid4(), [result([uuid.uuid4()])]) == 0  # unknown source skipped

    for org, expected in ((a, {source_a, shared}), (b, {source_b, shared})):
        scope = TenantScope(org_id=org, user_id=None, role=Role.VIEWER)
        async with services.db.tenant(scope) as s:
            seen = set((await s.scalars(select(Observation.source_id))).all())
        assert seen == expected
    async with services.db.system("test check") as s:
        node = await s.get(ProbeNode, creds.node_id)
        assert node is not None
        assert node.last_seen_at is not None
        assert await s.scalar(select(func.count()).select_from(Observation)) == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"error_class": "made_up"},
        {"status_code": 42},
        {"latency_ms": -1},
        {"observed_at": "2026-10-20T12:00:00"},  # no time zone
        {"observed_at": (utcnow() - timedelta(days=8)).isoformat()},
        {"observed_at": (utcnow() + timedelta(hours=1)).isoformat()},
        {"details": {"x": "y" * 5000}},
        {"unexpected": True},
        {"source_ids": []},
    ],
)
async def test_invalid_results_are_rejected(
    app: FastAPI, services: AppServices, bad: dict[str, Any]
) -> None:
    a, _ = await two_orgs(services)
    source = await add_source(services, a, "https://x.example.invalid/")
    creds = await services.nodes.register("n1", "test")
    with pytest.raises(CoreRejectedError, match="422"):
        await client_for(app, creds).send(uuid.uuid4(), [result([source], **bad)])


async def test_unsigned_or_stale_requests_are_refused(app: FastAPI, services: AppServices) -> None:
    creds = await services.nodes.register("n1", "test")
    for client in (
        client_for(app, creds, skew=-400),  # older than the 5-minute window
        client_for(app, creds, key="0" * 64),  # wrong signing key
        client_for(
            app, NodeCredentials(creds.node_id, "wrong-token-" + "x" * 30, creds.signing_key)
        ),
    ):
        with pytest.raises(CoreRejectedError, match="401"):
            await client.jobs()
        with pytest.raises(CoreRejectedError, match="401"):
            await client.send(uuid.uuid4(), [])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test"
    ) as raw:
        response = await raw.post("/ingest/v1/results", json={"results": []})
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}


def test_prober_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("PROBE_NODE_ID", "PROBE_NODE_TOKEN", "PROBE_SIGNING_KEY", "PROBE_INGEST_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as caught:
        load_probe_settings_or_exit()
    assert caught.value.code == 2
    assert "register" in capsys.readouterr().err
