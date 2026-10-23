"""Public status pages: ``/s/{org}``, its Atom feed, badge and e-mail subscription.

The page, feed and badge are the same for every visitor: no session, no cookies, no
form token, so shared caches may keep them for a minute (``ETag`` for revalidation).
The subscription form is a separate, uncached page with the usual form token.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Request
from starlette.responses import Response

from sentinel.public.feeds import atom, badge
from sentinel.public.render import render_page
from sentinel.public.service import PublicError
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter()

Slug = Annotated[str, Path(min_length=1, max_length=63, pattern=r"^[a-z0-9-]+$")]
CACHE = "public, max-age=60"


def cached(request: Request, body: bytes, media_type: str) -> Response:
    tag = 'W/"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    headers = {"ETag": tag, "Cache-Control": CACHE}
    if tag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


def _missing(request: Request) -> Response:
    return services(request).renderer.message(
        request, "errors.not_found_title", "errors.not_found_body", status=404
    )


@router.get("/s/{slug}")
async def page(request: Request, slug: Slug) -> Response:
    svc = services(request)
    found = await svc.status_pages.page(slug)
    if found is None:
        return _missing(request)
    page_url = f"{svc.settings.public_base}/s/{slug}"
    html = render_page(
        svc.renderer.env,
        found,
        page_url=page_url,
        feed_url=f"{page_url}/akis.atom",
        subscribe_url=f"{page_url}/abone" if found.subscriptions else None,
        method_url=f"{svc.settings.public_base}/yontem",
        assets="/static",
    )
    return cached(request, html.encode(), "text/html; charset=utf-8")


@router.get("/s/{slug}/akis.atom")
async def feed(request: Request, slug: Slug) -> Response:
    svc = services(request)
    found = await svc.status_pages.page(slug)
    if found is None:
        return _missing(request)
    body = atom(found, f"{svc.settings.public_base}/s/{slug}")
    return cached(request, body.encode(), "application/atom+xml; charset=utf-8")


@router.get("/s/{slug}/rozet.svg")
async def status_badge(request: Request, slug: Slug) -> Response:
    found = await services(request).status_pages.page(slug)
    if found is None:
        return Response(status_code=404)
    return cached(request, badge(found).encode(), "image/svg+xml")


@router.get("/s/{slug}/abone")
async def subscribe_page(request: Request, slug: Slug) -> Response:
    svc = services(request)
    found = await svc.status_pages.page(slug)
    if found is None or not found.subscriptions:
        return _missing(request)
    return svc.renderer.page(
        request,
        "public/subscribe.html",
        page=found,
        done=False,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/s/{slug}/abone", dependencies=[Depends(csrf_protect)])
async def subscribe(
    request: Request,
    slug: Slug,
    email: Annotated[str, Form(max_length=320)] = "",
    website: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    svc = services(request)
    found = await svc.status_pages.page(slug)
    if found is None or not found.subscriptions:
        return _missing(request)
    if website:  # the honeypot: people never see this field
        return svc.renderer.page(request, "public/subscribe.html", page=found, done=True)
    try:
        await svc.status_pages.subscribe(slug, email, client_ip(request))
    except PublicError as exc:
        status = 429 if exc.key == "public.throttled" else 404 if exc.key == "not_found" else 400
        return svc.renderer.page(
            request, "public/subscribe.html", status=status, page=found, done=False, error=exc.key
        )
    return svc.renderer.page(request, "public/subscribe.html", page=found, done=True)
