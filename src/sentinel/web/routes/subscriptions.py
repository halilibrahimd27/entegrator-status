"""Public subscription pages: confirm an e-mail address, unsubscribe.

The links arrive by e-mail and the token in the path is the only secret, so these
routes need no session and are exempt from the form token: a forged request would
have to know the token already. Opening a link only shows a page; the change needs
a POST (mail scanners open links, they do not submit forms). The unsubscribe POST is
also the RFC 8058 one-click target named in ``List-Unsubscribe-Post``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from starlette.responses import Response

from sentinel.web.services import services

router = APIRouter()

Token = Annotated[str, Path(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
}


def _invalid(request: Request) -> Response:
    response = services(request).renderer.message(
        request, "alerts.page.invalid_title", "alerts.page.invalid_body", status=404
    )
    response.headers.update(_HEADERS)
    return response


@router.get("/abone/onay/{token}")
async def confirm_page(request: Request, token: Token) -> Response:
    svc = services(request)
    found = await svc.subscriptions.preview_confirmation(token)
    if found is None:
        return _invalid(request)
    return svc.renderer.page(
        request, "alerts/confirm.html", headers=_HEADERS, subscription=found, done=False
    )


@router.post("/abone/onay/{token}")
async def confirm(request: Request, token: Token) -> Response:
    svc = services(request)
    found = await svc.subscriptions.confirm(token)
    if found is None:
        return _invalid(request)
    return svc.renderer.page(
        request, "alerts/confirm.html", headers=_HEADERS, subscription=found, done=True
    )


@router.get("/abone/iptal/{token}")
async def unsubscribe_page(request: Request, token: Token) -> Response:
    svc = services(request)
    found = await svc.subscriptions.preview_unsubscribe(token)
    if found is None:
        return _invalid(request)
    return svc.renderer.page(
        request,
        "alerts/unsubscribe.html",
        headers=_HEADERS,
        subscription=found,
        done=found.unsubscribed,
    )


@router.post("/abone/iptal/{token}")
async def unsubscribe(request: Request, token: Token) -> Response:
    svc = services(request)
    found = await svc.subscriptions.unsubscribe(token)
    if found is None:
        return _invalid(request)
    return svc.renderer.page(
        request, "alerts/unsubscribe.html", headers=_HEADERS, subscription=found, done=True
    )
