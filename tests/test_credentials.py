"""Credential vault: validation, write-only secrets, host binding, permissions."""

from __future__ import annotations

import importlib.util
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import create_org, create_service, join
from sentinel.accounts.mail import Mail
from sentinel.models import AuditEvent, Component, Credential, OrgDataKey, Source
from sentinel.web.services import AppServices
from support import post_form, signed_in

PAGE = "/kimlik-bilgileri"
OAUTH_OVER_HTTP = {
    "kind": "oauth2_client",
    "client_id": "a",
    "client_secret": "b",
    "token_url": "http://auth.vendor.example/token",
}


def canary() -> str:
    return "canary-" + secrets.token_hex(12)


async def setup(http: httpx2.AsyncClient, outbox: list[Mail]) -> str:
    await signed_in(http, outbox, "alice@example.invalid")
    return await create_org(http, "Alpha")


def form(**overrides: str) -> dict[str, str]:
    values = {
        "name": "Portal API",
        "kind": "api_key_header",
        "host": "api.vendor.example",
        "header_name": "X-Api-Key",
        "value": canary(),
        "entitled": "true",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"entitled": ""}, "yetkili olduğunu onaylaman"),
        ({"host": "10.0.0.5"}, "genel internette değil"),
        ({"host": "localhost"}, "genel internette değil"),
        ({"host": "nas.local"}, "genel internette değil"),
        ({"host": "api.vendor.example/path"}, "genel internette değil"),
        ({"header_name": "Host"}, "Host, Cookie"),
        ({"header_name": "X Api Key"}, "Başlık adı geçersiz"),
        ({"value": ""}, "boş olamaz"),
        ({"value": "line\nbreak"}, "satır sonu"),
        (OAUTH_OVER_HTTP, "https://"),
        ({"kind": "smoke-signals"}, "Geçersiz kimlik bilgisi türü"),
        ({"name": " "}, "1 ile 80"),
    ],
)  # fmt: skip
async def test_validation(
    http: httpx2.AsyncClient, outbox: list[Mail], overrides: dict[str, str], message: str
) -> None:
    slug = await setup(http, outbox)
    url = f"/o/{slug}{PAGE}"
    response = await post_form(http, url, form(**overrides), csrf_from=url)
    assert response.status_code == 400
    assert message in response.text


async def test_secrets_are_write_only(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    slug = await setup(http, outbox)
    url = f"/o/{slug}{PAGE}"
    first = canary()
    created = await post_form(http, url, form(value=first), csrf_from=url)
    assert created.status_code == 303
    page = await http.get(url)
    assert "Portal API" in page.text
    assert "api.vendor.example" in page.text
    assert first not in page.text
    duplicate = await post_form(http, url, form(), csrf_from=url)
    assert duplicate.status_code == 400
    assert "zaten var" in duplicate.text

    async with services.db.system("test lookup") as s:
        credential = await s.scalar(select(Credential))
        assert credential is not None
        assert first.encode() not in credential.sealed
        assert await s.get(OrgDataKey, credential.org_id) is not None
        before = credential.sealed
        opened = await services.credentials.secret_for_job(s, credential)
    assert opened == {"value": first}

    second = canary()
    rotated = await post_form(
        http, f"{url}/{credential.id}/yenile", {"value": second}, csrf_from=url
    )
    assert rotated.status_code == 303
    async with services.db.system("test lookup") as s:
        again = await s.get(Credential, credential.id)
        assert again is not None
        assert again.sealed != before
        assert await services.credentials.secret_for_job(s, again) == {"value": second}
        events = (
            await s.scalars(select(AuditEvent).where(AuditEvent.target_type == "credential"))
        ).all()
    assert {e.action for e in events} == {"credential.created", "credential.rotated"}
    for event in events:
        assert first not in str(event.details)
        assert second not in str(event.details)
    for path in (url, f"/o/{slug}/denetim"):
        text = (await http.get(path)).text
        assert first not in text
        assert second not in text

    deleted = await post_form(http, f"{url}/{credential.id}/sil", {}, csrf_from=url)
    assert deleted.status_code == 303
    assert "Kayıtlı kimlik bilgisi yok" in (await http.get(url)).text


async def test_attach_only_to_own_sources_on_the_same_host(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    slug = await setup(http, outbox)
    url = f"/o/{slug}{PAGE}"
    await post_form(http, url, form(), csrf_from=url)
    service = await create_service(http, slug)
    page_url = f"/o/{slug}/servisler/{service}"
    async with services.db.system("test lookup") as s:
        component = await s.scalar(
            select(Component).where(Component.service_id == uuid.UUID(service))
        )
        credential = await s.scalar(select(Credential))
        assert component is not None
        assert credential is not None
    base = f"/o/{slug}/bilesenler/{component.id}/kaynak"
    for address in ("https://api.vendor.example/v1/health", "https://other.vendor.example/"):
        added = await post_form(http, base, {"kind": "http", "url": address}, csrf_from=page_url)
        assert added.status_code == 303
    async with services.db.system("test lookup") as s:
        sources = {
            src.config["url"]: src.id
            for src in (
                await s.scalars(select(Source).where(Source.component_id == component.id))
            ).all()
        }
    right = sources["https://api.vendor.example/v1/health"]
    wrong = sources["https://other.vendor.example/"]

    def attach(source_id: uuid.UUID, value: str) -> Any:
        return post_form(
            http,
            f"/o/{slug}/kaynaklar/{source_id}/kimlik",
            {"credential_id": value},
            csrf_from=page_url,
        )

    mismatch = await attach(wrong, str(credential.id))
    assert mismatch.status_code == 400
    assert (await attach(right, str(credential.id))).status_code == 303
    async with services.db.system("test lookup") as s:
        attached = await s.get(Source, right)
        assert attached is not None
        assert attached.credential_id == credential.id
    assert "Portal API (api.vendor.example)" in (await http.get(page_url)).text
    assert (await attach(right, "")).status_code == 303  # detach
    async with services.db.system("test lookup") as s:
        detached = await s.get(Source, right)
        assert detached is not None
        assert detached.credential_id is None
    assert (await attach(right, "not-a-uuid")).status_code == 404


async def test_members_cannot_see_or_manage_credentials(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    slug = await setup(http, outbox)
    async with join(app, outbox, http, slug, "member@example.invalid", role="member") as member:
        assert (await member.get(f"/o/{slug}{PAGE}")).status_code == 403
        assert "kimlik-bilgileri" not in (await member.get(f"/o/{slug}")).text


def test_make_env_adds_only_missing_keys() -> None:
    path = Path(__file__).resolve().parents[1] / "tools" / "make_env.py"
    spec = importlib.util.spec_from_file_location("make_env", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    example = (
        "SESSION_KEY=<SESSION_KEY>\n"
        "CREDENTIALS_MASTER_KEY=<CREDENTIALS_MASTER_KEY>\n"
        "LOG_LEVEL=INFO\n"
    )
    existing = "SESSION_KEY=kept-as-is\nLOG_LEVEL=DEBUG\n"
    added = module.missing_lines(existing, example)
    assert [line.partition("=")[0] for line in added] == ["CREDENTIALS_MASTER_KEY"]
    assert len(added[0].partition("=")[2]) >= 48  # a fresh random value
