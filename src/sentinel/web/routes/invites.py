"""Accepting invitations.

The invitation link arrives from a mail client, so (``SameSite=Strict``) the first
request never carries the session cookie. The landing page therefore keeps the token in
a short-lived HttpOnly cookie and the user accepts from inside the site, never with the
token in a query string.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from starlette.responses import RedirectResponse, Response

from sentinel.tenancy.invitations import InvitationPreview
from sentinel.tenancy.orgs import OrgError
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import Viewer, csrf_protect, require_viewer

router = APIRouter(dependencies=[Depends(csrf_protect)])

INVITE_COOKIE_MAX_AGE = 3600


def invite_cookie_name(secure: bool) -> str:
    return "__Host-es_invite" if secure else "es_invite"


def _cookie(request: Request) -> str:
    return invite_cookie_name(services(request).settings.cookies_secure)


def _clear(request: Request, response: Response) -> None:
    secure = services(request).settings.cookies_secure
    response.delete_cookie(
        _cookie(request), path="/", secure=secure, httponly=True, samesite="strict"
    )


async def pending_invitation(request: Request) -> InvitationPreview | None:
    token = request.cookies.get(_cookie(request))
    if not token:
        return None
    return await services(request).invitations.preview(token)


@router.get("/davet/{token}")
async def landing(
    request: Request, token: Annotated[str, Path(min_length=16, max_length=128)]
) -> Response:
    svc = services(request)
    preview = await svc.invitations.preview(token)
    if preview is None:
        return svc.renderer.message(
            request, "invite.invalid_title", "invite.invalid_body", status=400
        )
    response = svc.renderer.page(
        request, "invite/landing.html", headers={"Referrer-Policy": "no-referrer"}, preview=preview
    )
    response.set_cookie(
        _cookie(request),
        token,
        max_age=INVITE_COOKIE_MAX_AGE,
        path="/",
        secure=svc.settings.cookies_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/davet/kabul")
async def accept(request: Request, viewer: Annotated[Viewer, Depends(require_viewer)]) -> Response:
    svc = services(request)
    token = request.cookies.get(_cookie(request))
    if not token:
        return RedirectResponse("/orglar", status_code=303)
    try:
        slug = await svc.invitations.accept(token, viewer.user.id, client_ip(request))
    except OrgError as exc:
        failed = svc.renderer.message(request, "invite.invalid_title", exc.key, status=400)
        _clear(request, failed)
        return failed
    response = RedirectResponse(f"/o/{slug}", status_code=303)
    _clear(request, response)
    return response


@router.post("/davet/vazgec")
async def decline(request: Request, viewer: Annotated[Viewer, Depends(require_viewer)]) -> Response:
    response = RedirectResponse("/orglar", status_code=303)
    _clear(request, response)
    return response
