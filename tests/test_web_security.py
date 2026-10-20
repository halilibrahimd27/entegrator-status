"""Cross-cutting web rules: headers, cookies, CSRF, copy keys, no secrets in logs."""

from __future__ import annotations

import ast
import logging
import logging.config
import re
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant

from sentinel.accounts.mail import Mail
from sentinel.web.headers import CSP
from sentinel.web.i18n import messages
from sentinel.web.logging import RedactTokenPaths, redact, uvicorn_log_config
from sentinel.web.session_auth import csrf_protect, optional_viewer
from support import (
    PASSWORD,
    api_routes,
    csrf,
    link_in,
    login,
    post_form,
    registered,
    signed_in,
    signup,
    verify,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "sentinel"
EMAIL = "guvenlik@example.invalid"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PREFIXES = ("/api/", "/ingest/")  # token-authenticated, never cookie-authenticated


@pytest.mark.parametrize("path", ["/", "/giris", "/kayit", "/aydinlatma", "/olmayan-sayfa"])
async def test_security_headers_on_pages(http: httpx2.AsyncClient, path: str) -> None:
    response = await http.get(path)
    assert response.headers["content-security-policy"] == CSP
    assert "unsafe-inline" not in CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["cache-control"] == "no-store"


async def test_static_files_are_cacheable(http: httpx2.AsyncClient) -> None:
    response = await http.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers.get("cache-control") != "no-store"


async def test_unknown_page_is_an_html_404(http: httpx2.AsyncClient) -> None:
    response = await http.get("/olmayan-sayfa")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Sayfa bulunamadı" in response.text


async def test_incomplete_form_is_an_html_400(http: httpx2.AsyncClient) -> None:
    response = await post_form(http, "/giris", {"email": EMAIL})  # no password field
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")


async def test_cookie_flags(http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    pre = await http.get("/giris")
    pre_cookie = pre.headers["set-cookie"]
    assert pre_cookie.startswith("__Host-es_pre=")
    await registered(http, outbox, EMAIL)
    response = await login(http, EMAIL)
    session_cookie = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-es_session=")
    )
    for cookie in (pre_cookie, session_cookie):
        lowered = cookie.lower()
        assert "httponly" in lowered
        assert "secure" in lowered
        assert "samesite=strict" in lowered
        assert "path=/" in lowered
        assert "domain=" not in lowered


async def test_post_without_csrf_is_refused(http: httpx2.AsyncClient) -> None:
    await http.get("/giris")  # gets a pre-session cookie
    missing = await http.post("/giris", data={"email": EMAIL, "password": PASSWORD})
    wrong = await http.post("/giris", data={"email": EMAIL, "password": PASSWORD, "csrf": "0" * 64})
    assert missing.status_code == wrong.status_code == 403
    assert "Form süresi doldu" in missing.text


async def test_csrf_token_is_bound_to_the_browser(app: FastAPI, http: httpx2.AsyncClient) -> None:
    token_of_other_browser = await csrf(http, "/giris")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=str(http.base_url)
    ) as victim:
        await victim.get("/giris")
        response = await victim.post(
            "/giris", data={"email": EMAIL, "password": PASSWORD, "csrf": token_of_other_browser}
        )
    assert response.status_code == 403


async def test_pre_session_token_is_useless_after_sign_in(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await registered(http, outbox, EMAIL)
    old = await csrf(http, "/giris")
    assert (await login(http, EMAIL)).status_code == 303
    response = await http.post("/hesap/2fa/baslat", data={"csrf": old})
    assert response.status_code == 403


def _calls(dependant: Dependant) -> Iterator[object]:
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def test_every_unsafe_route_is_csrf_protected(app: FastAPI) -> None:
    routes = api_routes(app)
    unsafe = [(path, r) for path, r in routes if (r.methods or set()) & UNSAFE]
    assert len(routes) > 25  # the discovery itself works
    assert len(unsafe) > 15
    unprotected = [
        f"{sorted(r.methods or set())} {path}"
        for path, r in unsafe
        if not path.startswith(CSRF_EXEMPT_PREFIXES)
        and csrf_protect not in set(_calls(r.dependant))
    ]
    assert unprotected == []


def test_api_routes_take_no_cookies(app: FastAPI) -> None:
    """Token-authenticated routes must not depend on the session (no CSRF exemption abuse)."""
    for path, route in api_routes(app):
        if path.startswith(CSRF_EXEMPT_PREFIXES):
            assert optional_viewer not in set(_calls(route.dependant)), path


def _copy_keys_used() -> set[str]:
    """i18n keys referenced from templates and Python code (literal uses only)."""
    keys: set[str] = set()
    for template in (SRC / "web" / "templates").rglob("*.html"):
        keys |= set(
            re.findall(r"""\bt\(\s*["']([a-z_]+(?:\.[a-z_]+)+)["']\s*\)""", template.read_text())
        )
    for path in [*(SRC / "accounts").glob("*.py"), *(SRC / "web").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                literals = [
                    a.value
                    for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                if name in {"AccountError", "PasswordPolicyError"}:
                    keys.update(literals[:1])
                elif name == "message":
                    keys.update(literals[:2])
                elif name == "_send":
                    keys.update(
                        f"{k}.{part}" for k in literals[1:2] for part in ("subject", "body")
                    )
                if name == "page":  # renderer.page(..., error="key")
                    for kw in node.keywords:
                        if (
                            kw.arg == "error"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value
                        ):
                            keys.add(str(kw.value.value))
            elif isinstance(node, ast.Tuple) and len(node.elts) == 2:
                first, second = node.elts
                if (
                    isinstance(first, ast.Constant)
                    and str(first.value).startswith("/")
                    and isinstance(second, ast.Constant)
                    and isinstance(second.value, str)
                ):
                    keys.add(second.value)  # (href, label key) link pairs
            elif isinstance(node, ast.Dict):
                for value in node.values:
                    if (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value.startswith("account.")
                    ):
                        keys.add(value.value)
    return keys


def test_every_copy_key_exists() -> None:
    used = _copy_keys_used()
    assert len(used) > 40  # the scan itself works
    missing = sorted(used - set(messages()))
    assert missing == []


async def test_user_input_is_escaped(http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signup(http, EMAIL, name='<script>alert("x")</script>')
    await verify(http, outbox)
    await login(http, EMAIL)
    page = await http.get("/hesap")
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;" in page.text


async def test_no_secrets_reach_the_logs(
    http: httpx2.AsyncClient, outbox: list[Mail], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await signed_in(http, outbox, EMAIL)
    await post_form(http, "/sifre-sifirla", {"email": EMAIL})
    secrets_seen = [
        PASSWORD,
        link_in(outbox[0], "dogrula").rsplit("/", 1)[1],
        link_in(outbox[-1], "sifre-sifirla").rsplit("/", 1)[1],
        *(cookie.value for cookie in http.cookies.jar if cookie.value),
    ]
    assert all(len(s) > 10 for s in secrets_seen)
    # The test client logs its own request URLs; everything else is the app's logging.
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    leaked = [s[:6] + "…" for s in secrets_seen if s in text]
    assert leaked == []


def test_access_log_redacts_token_paths() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/sifre-sifirla/abcDEF123-_xyz?x=1", "1.1", 200),
        None,
    )
    assert RedactTokenPaths().filter(record)
    line = record.getMessage()
    assert "abcDEF123" not in line
    assert "/sifre-sifirla/[redacted]?x=1" in line
    assert redact("/dogrula/tok3n and /davet/t0ken") == "/dogrula/[redacted] and /davet/[redacted]"
    assert redact("/hesap/2fa/baslat") == "/hesap/2fa/baslat"


def test_uvicorn_config_uses_the_filter() -> None:
    config = uvicorn_log_config()
    assert config["handlers"]["access"]["filters"] == ["redact_tokens"]
    logging.config.dictConfig(config)
    assert any(
        isinstance(f, RedactTokenPaths)
        for f in logging.getLogger("uvicorn.access").handlers[0].filters
    )


@pytest.mark.parametrize("path", ["/", "/giris", "/kayit", "/aydinlatma", "/sifre-sifirla"])
async def test_page_titles_are_plain_text(http: httpx2.AsyncClient, path: str) -> None:
    """A template slip once put a whole section inside <title>; titles hold text only."""
    html = (await http.get(path)).text
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "<" not in title
    assert len(title) < 120


async def test_signed_in_page_titles_are_plain_text(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await signed_in(http, outbox, EMAIL)
    response = await post_form(http, "/orglar", {"name": "Başlık Testi"}, csrf_from="/orglar")
    slug = response.headers["location"]
    for path in (
        "/hesap",
        "/orglar",
        slug,
        f"{slug}/ayarlar",
        f"{slug}/uyeler",
        f"{slug}/tokenlar",
        f"{slug}/denetim",
    ):
        html = (await http.get(path)).text
        title = html.split("<title>", 1)[1].split("</title>", 1)[0]
        assert "<" not in title, path
