"""Static-ish pages: landing, privacy notice, methodology, the bot, corrections, robots."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from sentinel.probe.robots_rules import AGENT
from sentinel.public.corrections import CorrectionError
from sentinel.security.http import build_user_agent
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import Viewer, csrf_protect, optional_viewer

router = APIRouter(dependencies=[Depends(csrf_protect)])


@router.get("/")
async def home(
    request: Request, viewer: Annotated[Viewer | None, Depends(optional_viewer)]
) -> Response:
    if viewer is not None:
        return RedirectResponse("/orglar", status_code=303)
    return services(request).renderer.page(request, "home.html")


@router.get("/aydinlatma")
async def privacy(request: Request) -> Response:
    svc = services(request)
    return svc.renderer.page(request, "pages/privacy.html", contact=svc.settings.contact_email)


@router.get("/yontem")
async def method(request: Request) -> Response:
    return services(request).renderer.page(request, "pages/method.html")


@router.get("/hakkinda/bot")
async def bot(request: Request) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "pages/bot.html",
        agent=AGENT,
        contact=svc.settings.contact_email,
        user_agent=build_user_agent(svc.settings.public_base, svc.settings.contact_email),
    )


@router.get("/duzeltme")
async def correction_form(request: Request) -> Response:
    return services(request).renderer.page(request, "pages/correction.html", done=False)


@router.post("/duzeltme")
async def correction(
    request: Request,
    subject: Annotated[str, Form(max_length=400)] = "",
    body: Annotated[str, Form(max_length=5000)] = "",
    contact: Annotated[str, Form(max_length=320)] = "",
    website: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    svc = services(request)
    if website:  # the honeypot
        return svc.renderer.page(request, "pages/correction.html", done=True)
    try:
        await svc.corrections.submit(
            subject=subject, body=body, contact=contact, ip=client_ip(request)
        )
    except CorrectionError as exc:
        status = 429 if exc.key == "corrections.throttled" else 400
        return svc.renderer.page(
            request,
            "pages/correction.html",
            status=status,
            error=exc.key,
            done=False,
            subject=subject,
            body=body,
            contact=contact,
        )
    return svc.renderer.page(request, "pages/correction.html", done=True)


ROBOTS = """User-agent: *
Disallow: /o/
Disallow: /api/
Disallow: /ingest/
Disallow: /abone/
Disallow: /yonetim/
Disallow: /hesap
Disallow: /giris
Disallow: /kayit
Allow: /
Sitemap: {base}/sitemap.xml
"""


@router.get("/robots.txt")
async def robots(request: Request) -> Response:
    base = services(request).settings.public_base
    return PlainTextResponse(
        ROBOTS.format(base=base), headers={"Cache-Control": "public, max-age=3600"}
    )


@router.get("/sitemap.xml")
async def sitemap(request: Request) -> Response:
    svc = services(request)
    base = svc.settings.public_base
    paths = ["/", "/yontem", "/hakkinda/bot", "/aydinlatma"]
    paths += [f"/s/{slug}" for slug in await svc.status_pages.published()]
    urls = "".join(f"  <url><loc>{base}{path}</loc></url>\n" for path in paths)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )
    return Response(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
