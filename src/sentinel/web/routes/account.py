"""The signed-in user's account page: profile and TOTP enrolment."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse, Response

from sentinel.accounts.service import AccountError
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import (
    Viewer,
    clear_session_cookie,
    csrf_protect,
    require_viewer,
    set_session_cookie,
)

router = APIRouter(dependencies=[Depends(csrf_protect)])

SignedIn = Annotated[Viewer, Depends(require_viewer)]
Code = Annotated[str, Form(max_length=16)]
# Notices shown after a redirect; only these keys are accepted from the query string.
NOTICES = {
    "totp_enabled": "account.totp_enabled",
    "totp_disabled": "account.totp_disabled",
    "admin_totp": "account.totp_admin_required",
}


@router.get("/hesap")
async def account(
    request: Request, viewer: SignedIn, ok: Annotated[str | None, Query(max_length=32)] = None
) -> Response:
    notice = NOTICES.get(ok or "")
    if viewer.user.is_platform_admin and viewer.user.totp_enabled_at is None:
        notice = NOTICES["admin_totp"]
    return services(request).renderer.page(request, "account/index.html", notice=notice)


@router.post("/hesap/2fa/baslat")
async def totp_start(request: Request, viewer: SignedIn) -> Response:
    svc = services(request)
    try:
        enrolment = await svc.two_factor.start_totp(viewer.user.id)
    except AccountError as exc:
        return svc.renderer.page(
            request, "account/index.html", status=400, error=exc.key, notice=None
        )
    return svc.renderer.page(request, "account/totp_setup.html", enrolment=enrolment)


@router.post("/hesap/2fa/onayla")
async def totp_confirm(request: Request, viewer: SignedIn, code: Code) -> Response:
    svc = services(request)
    if not await svc.two_factor.confirm_totp(viewer.user.id, code):
        enrolment = await svc.two_factor.pending_totp(viewer.user.id)
        if enrolment is None:
            return RedirectResponse("/hesap", status_code=303)
        return svc.renderer.page(
            request,
            "account/totp_setup.html",
            status=400,
            enrolment=enrolment,
            error="account.totp_confirm_failed",
        )
    # The session now satisfies the second factor; rotate it (privilege change).
    token = await svc.accounts.sessions.rotate(viewer.token, mfa_verified=True)
    response = RedirectResponse("/hesap?ok=totp_enabled", status_code=303)
    if token is not None:
        set_session_cookie(request, response, token)
    return response


@router.post("/hesap/2fa/kapat")
async def totp_disable(
    request: Request,
    viewer: SignedIn,
    password: Annotated[str, Form(max_length=256)],
    code: Code,
) -> Response:
    svc = services(request)
    try:
        await svc.two_factor.disable_totp(viewer.user.id, password=password, code=code)
    except AccountError as exc:
        return svc.renderer.page(
            request, "account/index.html", status=400, error=exc.key, notice=None
        )
    return RedirectResponse("/hesap?ok=totp_disabled", status_code=303)


# --- KVKK: export and deletion ----------------------------------------------------


@router.get("/hesap/veri")
async def export_data(request: Request, viewer: SignedIn) -> Response:
    data = await services(request).privacy.export(viewer.user.id)
    return JSONResponse(
        data,
        headers={"Content-Disposition": 'attachment; filename="entegrator-status-verilerim.json"'},
    )


@router.post("/hesap/sil")
async def delete_account(
    request: Request,
    viewer: SignedIn,
    password: Annotated[str, Form(max_length=256)],
    code: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    svc = services(request)
    try:
        await svc.privacy.delete_account(
            viewer.user.id, password=password, code=code, ip=client_ip(request)
        )
    except AccountError as exc:
        return svc.renderer.page(
            request, "account/index.html", status=400, error=exc.key, notice=None
        )
    request.state.viewer = None  # the page below is rendered for a signed-out visitor
    response = svc.renderer.message(request, "privacy.deleted_title", "privacy.deleted_body")
    clear_session_cookie(request, response)
    return response
