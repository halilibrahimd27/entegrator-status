"""Helpers for organization tests: clients per user, orgs, invitations."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI

from conftest import BASE_URL
from sentinel.accounts.mail import Mail
from support import csrf, link_in, post_form, signed_in

TOKEN_RE = re.compile(r'<code class="secret">(es_[0-9a-f]{8}_[A-Za-z0-9_-]+)</code>')


@asynccontextmanager
async def browser(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


async def create_org(client: httpx2.AsyncClient, name: str) -> str:
    response = await post_form(client, "/orglar", {"name": name}, csrf_from="/orglar")
    assert response.status_code == 303, response.text
    return response.headers["location"].removeprefix("/o/")


async def invite(
    client: httpx2.AsyncClient, slug: str, email: str, role: str = "member"
) -> httpx2.Response:
    return await post_form(
        client,
        f"/o/{slug}/uyeler/davet",
        {"email": email, "role": role},
        csrf_from=f"/o/{slug}/uyeler",
    )


@asynccontextmanager
async def join(
    app: FastAPI,
    outbox: list[Mail],
    inviter: httpx2.AsyncClient,
    slug: str,
    email: str,
    role: str = "member",
) -> AsyncIterator[httpx2.AsyncClient]:
    """Invite ``email``, sign them up and accept; yields their signed-in client."""
    assert (await invite(inviter, slug, email, role)).status_code == 303
    path = link_in(outbox[-1], "davet")
    async with browser(app) as client:
        assert (await client.get(path)).status_code == 200
        await signed_in(client, outbox, email)
        accepted = await post_form(client, "/davet/kabul", {}, csrf_from="/orglar")
        assert accepted.headers["location"] == f"/o/{slug}", accepted.text
        yield client


async def new_token(client: httpx2.AsyncClient, slug: str, name: str = "ci") -> str:
    page = await post_form(
        client, f"/o/{slug}/tokenlar", {"name": name, "days": "30"}, csrf_from=f"/o/{slug}/tokenlar"
    )
    assert page.status_code == 200, page.text
    match = TOKEN_RE.search(page.text)
    assert match
    return match.group(1)


async def any_csrf(client: httpx2.AsyncClient) -> str:
    return await csrf(client, "/orglar")
