"""Two-tenant harness: every organization route refuses another tenant's objects.

Routes are discovered from the app, so a new ``/o/{slug}`` route is covered without
editing this file. Expected answer for anything foreign: 404.
"""

from __future__ import annotations

import re
import uuid

import httpx2
from fastapi import FastAPI
from sqlalchemy import select

from org_support import any_csrf, browser, create_org, join, new_token
from sentinel.accounts.mail import Mail
from sentinel.models import ApiToken, Invitation, Membership, Organization
from sentinel.web.services import AppServices
from support import api_routes, signed_in

FORM = {"name": "Yeni Ad", "email": "x@example.invalid", "role": "viewer", "days": "30"}
ID_PARAMS: dict[str, type[Membership] | type[Invitation] | type[ApiToken]] = {
    "membership_id": Membership,
    "invitation_id": Invitation,
    "token_id": ApiToken,
}


async def ids_of(services: AppServices, slug: str) -> dict[str, uuid.UUID]:
    async with services.db.system("test harness lookup") as s:
        org_id = await s.scalar(select(Organization.id).where(Organization.slug == slug))
        found: dict[str, uuid.UUID] = {}
        for param, model in ID_PARAMS.items():
            value = await s.scalar(select(model.id).where(model.org_id == org_id).limit(1))
            assert value is not None, f"fixture has no {model.__name__} in {slug}"
            found[param] = value
    return found


def org_routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for path, route in api_routes(app)
        if path.startswith("/o/{slug}")
        for method in (route.methods or set())
    )


def fill(path: str, slug: str, ids: dict[str, uuid.UUID]) -> str:
    path = path.replace("{slug}", slug)
    return re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)


async def build_tenant(
    app: FastAPI, outbox: list[Mail], owner: httpx2.AsyncClient, email: str, name: str
) -> str:
    await signed_in(owner, outbox, email)
    slug = await create_org(owner, name)
    async with join(app, outbox, owner, slug, f"member-of-{slug}@example.invalid"):
        pass
    await post_invite_only(owner, slug)
    await new_token(owner, slug)
    return slug


async def post_invite_only(owner: httpx2.AsyncClient, slug: str) -> None:
    from org_support import invite

    assert (await invite(owner, slug, f"pending-{slug}@example.invalid")).status_code == 303


async def test_every_org_route_hides_other_tenants(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as alice, browser(app) as bob:
        a = await build_tenant(app, outbox, alice, "alice@example.invalid", "Alpha")
        b = await build_tenant(app, outbox, bob, "bob@example.invalid", "Beta")
        ids_b = await ids_of(services, b)
        routes = org_routes(app)
        assert len(routes) >= 13  # the discovery itself works (5 GET + 8 POST in phase 1c)

        failures = []
        for method, template in routes:
            targets = [fill(template, b, ids_b)]  # the other tenant's organization
            if "_id}" in template:
                targets.append(fill(template, a, ids_b))  # own org, foreign object id
            for url in targets:
                if method == "GET":
                    response = await alice.get(url)
                else:
                    response = await alice.request(
                        method, url, data={**FORM, "csrf": await any_csrf(alice)}
                    )
                if response.status_code != 404:
                    failures.append(f"{method} {url} -> {response.status_code}")
        assert failures == []

        # Nothing of Beta changed.
        assert (await bob.get(f"/o/{b}")).status_code == 200
        assert "Beta" in (await bob.get(f"/o/{b}/ayarlar")).text


async def test_api_token_sees_only_its_own_tenant(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as alice, browser(app) as bob:
        await signed_in(alice, outbox, "alice@example.invalid")
        await signed_in(bob, outbox, "bob@example.invalid")
        a = await create_org(alice, "Alpha")
        b = await create_org(bob, "Beta")
        token_a, token_b = await new_token(alice, a), await new_token(bob, b)
        async with browser(app) as api:
            got_a = (
                await api.get("/api/v1/org", headers={"Authorization": f"Bearer {token_a}"})
            ).json()
            got_b = (
                await api.get("/api/v1/org", headers={"Authorization": f"Bearer {token_b}"})
            ).json()
        assert got_a["slug"] == a
        assert got_b["slug"] == b
