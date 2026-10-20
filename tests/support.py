"""Helpers for web flow tests: forms with CSRF, sign-up, verification, sign-in."""

from __future__ import annotations

import re
import secrets

import httpx2

from sentinel.accounts.mail import Mail

# Generated per run: no password-like literals in the repository.
PASSWORD = f"test-{secrets.token_urlsafe(12)}"
CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]{64})"')


async def csrf(client: httpx2.AsyncClient, path: str) -> str:
    response = await client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    match = CSRF_RE.search(response.text)
    assert match, f"no CSRF field on {path}"
    return match.group(1)


async def post_form(
    client: httpx2.AsyncClient, path: str, data: dict[str, str], *, csrf_from: str | None = None
) -> httpx2.Response:
    token = await csrf(client, csrf_from or path)
    return await client.post(path, data={**data, "csrf": token})


def link_in(mail: Mail, prefix: str) -> str:
    match = re.search(rf"https://[^/\s]+(/{prefix}/[A-Za-z0-9_-]+)", mail.text)
    assert match, f"no /{prefix}/ link in: {mail.text!r}"
    return match.group(1)


async def signup(
    client: httpx2.AsyncClient, email: str, *, password: str = PASSWORD, name: str = "Deneme Kişi"
) -> httpx2.Response:
    return await post_form(
        client,
        "/kayit",
        {"email": email, "display_name": name, "password": password, "privacy_ack": "true"},
    )


async def verify(client: httpx2.AsyncClient, outbox: list[Mail]) -> httpx2.Response:
    path = link_in(outbox[-1], "dogrula")
    return await post_form(client, path, {})


async def login(
    client: httpx2.AsyncClient, email: str, *, password: str = PASSWORD
) -> httpx2.Response:
    return await post_form(client, "/giris", {"email": email, "password": password})


async def registered(client: httpx2.AsyncClient, outbox: list[Mail], email: str) -> None:
    assert (await signup(client, email)).status_code == 200
    assert (await verify(client, outbox)).status_code == 200


async def signed_in(client: httpx2.AsyncClient, outbox: list[Mail], email: str) -> None:
    await registered(client, outbox, email)
    response = await login(client, email)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/hesap"


async def logout(client: httpx2.AsyncClient) -> httpx2.Response:
    return await post_form(client, "/cikis", {}, csrf_from="/hesap")
