"""Sign-up, e-mail verification, sign-in (+ TOTP), sign-out and password reset."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Request
from starlette.responses import RedirectResponse, Response

from sentinel.accounts.service import AccountError
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import (
    Viewer,
    clear_session_cookie,
    csrf_protect,
    optional_viewer,
    set_session_cookie,
)

router = APIRouter(dependencies=[Depends(csrf_protect)])

Token = Annotated[str, Path(min_length=16, max_length=128)]
Email = Annotated[str, Form(max_length=320)]
Password = Annotated[str, Form(max_length=256)]
OptionalViewer = Annotated[Viewer | None, Depends(optional_viewer)]
NO_REFERRER = {"Referrer-Policy": "no-referrer"}  # pages whose URL holds a token
HOME_AFTER_LOGIN = "/orglar"


def _status(key: str) -> int:
    return {"throttled": 429, "login.failed": 401, "login.unverified": 403}.get(key, 400)


# --- sign-up ----------------------------------------------------------------


@router.get("/kayit")
async def signup_form(request: Request) -> Response:
    return services(request).renderer.page(request, "auth/signup.html", form={})


@router.post("/kayit")
async def signup(
    request: Request,
    email: Email,
    display_name: Annotated[str, Form(max_length=120)],
    password: Password,
    privacy_ack: Annotated[bool, Form()] = False,
) -> Response:
    svc = services(request)
    try:
        await svc.accounts.register(
            email=email,
            display_name=display_name,
            password=password,
            privacy_ack=privacy_ack,
            ip=client_ip(request),
        )
    except AccountError as exc:
        return svc.renderer.page(
            request,
            "auth/signup.html",
            status=_status(exc.key),
            error=exc.key,
            form={"email": email, "display_name": display_name},
        )
    return svc.renderer.message(request, "signup.done_title", "signup.done_body")


@router.get("/dogrula/{token}")
async def verify_form(request: Request, token: Token) -> Response:
    return services(request).renderer.page(
        request, "auth/verify.html", headers=NO_REFERRER, token=token
    )


@router.post("/dogrula/{token}")
async def verify(request: Request, token: Token) -> Response:
    svc = services(request)
    if await svc.accounts.verify_email(token):
        return svc.renderer.message(
            request, "verify.done_title", "verify.done_body", links=[("/giris", "nav.login")]
        )
    return svc.renderer.message(
        request,
        "verify.invalid_title",
        "verify.invalid_body",
        status=400,
        links=[("/dogrula-tekrar", "verify.resend_title")],
    )


@router.get("/dogrula-tekrar")
async def resend_form(request: Request) -> Response:
    return services(request).renderer.page(request, "auth/resend.html")


@router.post("/dogrula-tekrar")
async def resend(request: Request, email: Email) -> Response:
    svc = services(request)
    try:
        await svc.accounts.resend_verification(email, client_ip(request))
    except AccountError as exc:
        return svc.renderer.page(
            request, "auth/resend.html", status=_status(exc.key), error=exc.key
        )
    return svc.renderer.message(request, "verify.resend_done_title", "verify.resend_done_body")


# --- sign-in ----------------------------------------------------------------


@router.get("/giris")
async def login_form(request: Request, viewer: OptionalViewer) -> Response:
    if viewer is not None and (viewer.session.mfa_verified or viewer.user.totp_enabled_at is None):
        return RedirectResponse(HOME_AFTER_LOGIN, status_code=303)
    return services(request).renderer.page(request, "auth/login.html", form={})


@router.post("/giris")
async def login(
    request: Request, email: Email, password: Password, viewer: OptionalViewer
) -> Response:
    svc = services(request)
    try:
        outcome = await svc.accounts.authenticate(
            email=email, password=password, ip=client_ip(request)
        )
    except AccountError as exc:
        return svc.renderer.page(
            request,
            "auth/login.html",
            status=_status(exc.key),
            error=exc.key,
            form={"email": email},
        )
    if viewer is not None:  # never reuse a session across sign-ins
        await svc.accounts.sessions.destroy(viewer.token)
    token = await svc.accounts.start_session(outcome.user_id, mfa_verified=False)
    target = "/giris/2fa" if outcome.needs_totp else HOME_AFTER_LOGIN
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(request, response, token)
    return response


@router.get("/giris/2fa")
async def totp_form(request: Request, viewer: OptionalViewer) -> Response:
    if viewer is None:
        return RedirectResponse("/giris", status_code=303)
    if viewer.session.mfa_verified or viewer.user.totp_enabled_at is None:
        return RedirectResponse(HOME_AFTER_LOGIN, status_code=303)
    return services(request).renderer.page(request, "auth/login_totp.html")


@router.post("/giris/2fa")
async def totp_login(
    request: Request, code: Annotated[str, Form(max_length=16)], viewer: OptionalViewer
) -> Response:
    svc = services(request)
    if viewer is None:
        return RedirectResponse("/giris", status_code=303)
    try:
        ok = await svc.two_factor.check_totp(viewer.user.id, code, client_ip(request))
    except AccountError as exc:
        return svc.renderer.page(request, "auth/login_totp.html", status=429, error=exc.key)
    if not ok:
        return svc.renderer.page(
            request, "auth/login_totp.html", status=401, error="login.totp_failed"
        )
    token = await svc.accounts.sessions.rotate(viewer.token, mfa_verified=True)
    if token is None:
        return RedirectResponse("/giris", status_code=303)
    response = RedirectResponse(HOME_AFTER_LOGIN, status_code=303)
    set_session_cookie(request, response, token)
    return response


@router.post("/cikis")
async def logout(request: Request, viewer: OptionalViewer) -> Response:
    if viewer is not None:
        await services(request).accounts.sessions.destroy(viewer.token)
    response = RedirectResponse("/giris", status_code=303)
    clear_session_cookie(request, response)
    return response


# --- password reset -----------------------------------------------------------


@router.get("/sifre-sifirla")
async def reset_request_form(request: Request) -> Response:
    return services(request).renderer.page(request, "auth/reset_request.html")


@router.post("/sifre-sifirla")
async def reset_request(request: Request, email: Email) -> Response:
    svc = services(request)
    try:
        await svc.accounts.request_password_reset(email, client_ip(request))
    except AccountError as exc:
        return svc.renderer.page(
            request, "auth/reset_request.html", status=_status(exc.key), error=exc.key
        )
    return svc.renderer.message(request, "reset.sent_title", "reset.sent_body")


@router.get("/sifre-sifirla/{token}")
async def reset_form(request: Request, token: Token) -> Response:
    svc = services(request)
    if not await svc.accounts.reset_token_is_valid(token):
        return svc.renderer.message(
            request,
            "reset.title",
            "reset.invalid",
            status=400,
            links=[("/sifre-sifirla", "reset.title")],
        )
    return svc.renderer.page(request, "auth/reset_form.html", headers=NO_REFERRER, token=token)


@router.post("/sifre-sifirla/{token}")
async def reset(request: Request, token: Token, password: Password) -> Response:
    svc = services(request)
    try:
        await svc.accounts.reset_password(token, password)
    except AccountError as exc:
        if exc.key == "reset.invalid":
            return svc.renderer.message(request, "reset.title", "reset.invalid", status=400)
        return svc.renderer.page(
            request,
            "auth/reset_form.html",
            status=400,
            headers=NO_REFERRER,
            token=token,
            error=exc.key,
        )
    response = svc.renderer.message(
        request, "reset.done_title", "reset.done_body", links=[("/giris", "nav.login")]
    )
    clear_session_cookie(request, response)
    return response
