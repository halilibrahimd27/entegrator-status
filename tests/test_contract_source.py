"""Contract sources end to end: adapter, imports, credentials, classified changes, UI."""

from __future__ import annotations

import ipaddress
import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx
import httpx2
from sqlalchemy import select

from org_support import create_org, create_service
from probe_support import env_for, job
from sentinel.accounts.mail import Mail
from sentinel.contracts import openapi
from sentinel.db.base import utcnow
from sentinel.engine.changes import run_changes
from sentinel.models import Change, Component, Snapshot, Source
from sentinel.security.addresses import AddressPolicy, IPAddress
from sentinel.security.http import OutboundClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.security.vault import seal_for_node
from sentinel.sources.adapters import adapter_for
from sentinel.sources.auth import AuthProvider
from sentinel.sources.snapshots import snapshot_hash
from sentinel.web.services import AppServices
from support import signed_in

SPEC = {
    "openapi": "3.0.3",
    "paths": {
        "/orders": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id"],
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        }
                    }
                }
            }
        },
        "/legacy": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
}


async def anywhere(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("127.0.0.1")]


def client(handler: Any) -> OutboundClient:
    return OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=anywhere,
        policy=AddressPolicy.for_tests(),
        transport=httpx.MockTransport(handler),
        limiter=HostLimiter(rate_per_s=100, burst=10),
    )


async def contract(config: dict[str, Any], handler: Any, **overrides: Any) -> Any:
    adapter = adapter_for("api_contract")
    assert adapter is not None
    async with client(handler) as http:
        env = env_for(http)
        if "signing_key" in overrides:
            env.auth = AuthProvider(overrides.pop("signing_key"), http)
        return await adapter.run(
            job(kind="api_contract", url=config["url"], config=config, **overrides), env
        )


def site(routes: dict[str, Any], asked: list[httpx.Request] | None = None) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if asked is not None:
            asked.append(request)
        answer = routes.get(f"{request.headers['host']}{request.url.path}")
        if answer is None:
            return httpx.Response(404)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return httpx.Response(200, content=body, headers={"ETag": '"v1"'})

    return handle


async def test_openapi_source_and_conditional_fetch() -> None:
    config = {"url": "https://api.vendor.test/openapi.json", "format": "openapi"}
    handler = site({"api.vendor.test/openapi.json": SPEC})
    outcome = await contract(config, handler)
    assert outcome.ok
    ids = {f["id"] for f in outcome.snapshot.items}
    assert {"op:GET /orders", "res:GET /orders:200:$.id"} <= ids
    unchanged = await contract(config, handler, validators={"etag": '"v1"'})
    assert (unchanged.status_code, unchanged.snapshot) == (304, None)


async def test_wsdl_imports_same_host_only() -> None:
    wsdl_doc = b"""<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <types><xs:schema><xs:import schemaLocation="types.xsd"/>
        <xs:import schemaLocation="https://evil.example/steal.xsd"/></xs:schema></types>
      <portType name="P"><operation name="Send"/></portType></definitions>"""
    xsd = b"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Invoice"><xs:sequence>
        <xs:element name="uuid" type="xs:string"/>
      </xs:sequence></xs:complexType>
    </xs:schema>"""
    asked: list[httpx.Request] = []
    handler = site({"ws.vendor.test/fatura.wsdl": wsdl_doc, "ws.vendor.test/types.xsd": xsd}, asked)
    outcome = await contract(
        {"url": "https://ws.vendor.test/fatura.wsdl", "format": "wsdl"}, handler
    )
    assert outcome.ok, outcome
    ids = {f["id"] for f in outcome.snapshot.items}
    assert {"op:Send", "el:Invoice/uuid", "import:https://evil.example/steal.xsd"} <= ids
    assert {r.headers["host"] for r in asked} == {"ws.vendor.test"}  # the other host never asked


async def test_broken_and_oversized_contracts() -> None:
    broken = await contract(
        {"url": "https://api.vendor.test/openapi.json", "format": "openapi"},
        site({"api.vendor.test/openapi.json": b"<html>login</html>"}),
    )
    assert broken.error_class == "parse_error"
    big = {
        "openapi": "3.0.0",
        "paths": {f"/p{i}": {"get": {"responses": {}}} for i in range(11_000)},
    }
    huge = await contract(
        {"url": "https://api.vendor.test/openapi.json", "format": "openapi"},
        site({"api.vendor.test/openapi.json": big}),
    )
    assert huge.error_class in ("parse_error", "response_too_large")  # refused, never truncated


async def test_json_contract_with_the_organizations_credential() -> None:
    asked: list[httpx.Request] = []
    handler = site({"api.vendor.test/v1/me": {"id": "u-1", "plan": "pro"}}, asked)
    key = secrets.token_hex(32)
    canary = "canary-" + secrets.token_hex(8)
    sealed = seal_for_node(
        key, "job-1", {"kind": "bearer", "host": "api.vendor.test", "secret": {"token": canary}}
    )
    outcome = await contract(
        {"url": "https://api.vendor.test/v1/me", "format": "json"},
        handler,
        key="job-1",
        auth_sealed=sealed,
        signing_key=key,
    )
    assert outcome.ok
    assert asked[0].headers["authorization"] == f"Bearer {canary}"
    assert "u-1" not in json.dumps(outcome.snapshot.items)  # shape only, never values
    assert canary not in json.dumps(outcome.snapshot.items)


async def test_contract_changes_and_service_page(
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
            kind="api_contract",
            identity="api_contract:https://api.vendor.test/openapi.json",
            config={"url": "https://api.vendor.test/openapi.json", "format": "openapi"},
            interval_s=21600,
            timeout_s=10,
        )
        s.add(source)
        await s.flush()
        newer = json.loads(json.dumps(SPEC))
        newer["paths"].pop("/legacy")
        for minutes, spec in ((0, SPEC), (10, newer)):
            items = openapi.normalise(spec)
            s.add(
                Snapshot(
                    org_id=component.org_id,
                    source_id=source.id,
                    taken_at=utcnow() - timedelta(hours=1) + timedelta(minutes=minutes),
                    hash=snapshot_hash(items),
                    items=items,
                )
            )
    assert (await run_changes(services.db)).changes == 1
    async with services.db.system("test check") as s:
        change = await s.scalar(select(Change))
    assert change is not None
    assert (change.kind, change.category) == ("contract", "breaking")
    assert change.detail["operations"] == ["GET /legacy"]
    page = await http.get(f"/o/{slug}/servisler/{service}")
    assert "Kırıcı değişiklik" in page.text
    assert "İşlem kaldırıldı" in page.text
    assert "op:GET /legacy" in page.text
