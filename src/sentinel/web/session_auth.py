"""Who is asking (session cookie), CSRF protection and the cookies themselves.

CSRF tokens are derived, not stored: HMAC(SESSION_KEY, session token) for signed-in
users, HMAC(SESSION_KEY, pre-session cookie) for forms shown before sign-in (so login
CSRF is covered too). Both cookies are HttpOnly and SameSite=Strict.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from starlette.responses import Response

from sentinel.accounts.tokens import new_token
from sentinel.models import User, UserSession

if TYPE_CHECKING:
    from sentinel.web.services import AppServices

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PRE_SESSION_MAX_AGE = 24 * 3600
ADMIN_TOTP_ALLOWED_PREFIX = "/hesap"


class LoginRequiredError(Exception):
    pass


class SecondFactorRequiredError(Exception):
    pass


class AdminTotpRequiredError(Exception):
    pass


class CsrfError(Exception):
    pass


@dataclass(frozen=True)
class Viewer:
    user: User
    session: UserSession
    token: str


def _svc(request: Request) -> AppServices:
    svc: AppServices = request.app.state.services
    return svc


def session_cookie_name(secure: bool) -> str:
    return "__Host-es_session" if secure else "es_session"


def pre_cookie_name(secure: bool) -> str:
    return "__Host-es_pre" if secure else "es_pre"


async def optional_viewer(request: Request) -> Viewer | None:
    if hasattr(request.state, "viewer"):
        cached: Viewer | None = request.state.viewer
        return cached
    svc = _svc(request)
    token = request.cookies.get(session_cookie_name(svc.settings.cookies_secure))
    viewer = None
    if token:
        loaded = await svc.accounts.sessions.load(token)
        if loaded is not None:
            viewer = Viewer(user=loaded.user, session=loaded.session, token=token)
    request.state.viewer = viewer
    return viewer


async def require_viewer(
    request: Request, viewer: Annotated[Viewer | None, Depends(optional_viewer)]
) -> Viewer:
    if viewer is None:
        raise LoginRequiredError
    if viewer.user.totp_enabled_at is not None and not viewer.session.mfa_verified:
        raise SecondFactorRequiredError
    if (
        viewer.user.is_platform_admin
        and viewer.user.totp_enabled_at is None
        and not request.url.path.startswith(ADMIN_TOTP_ALLOWED_PREFIX)
    ):
        raise AdminTotpRequiredError
    return viewer


def csrf_token(request: Request) -> str:
    svc = _svc(request)
    viewer: Viewer | None = getattr(request.state, "viewer", None)
    if viewer is not None:
        source = "s\x00" + viewer.token
    else:
        pre = getattr(request.state, "new_pre", None) or request.cookies.get(
            pre_cookie_name(svc.settings.cookies_secure)
        )
        if not pre:
            pre = new_token()
            request.state.new_pre = pre
        source = "p\x00" + pre
    key = svc.settings.session_key.get_secret_value().encode()
    return hmac.new(key, b"csrf\x00" + source.encode(), hashlib.sha256).hexdigest()


async def csrf_protect(
    request: Request, viewer: Annotated[Viewer | None, Depends(optional_viewer)]
) -> None:
    """Router-level dependency: every unsafe HTML request must carry the form token."""
    if request.method in SAFE_METHODS:
        return
    form = await request.form()
    sent = form.get("csrf")
    if not isinstance(sent, str) or not hmac.compare_digest(sent, csrf_token(request)):
        raise CsrfError


def _set_cookie(request: Request, response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=_svc(request).settings.cookies_secure,
        httponly=True,
        samesite="strict",
    )


def _delete_cookie(request: Request, response: Response, name: str) -> None:
    response.delete_cookie(
        name,
        path="/",
        secure=_svc(request).settings.cookies_secure,
        httponly=True,
        samesite="strict",
    )


def attach_pre_cookie(request: Request, response: Response) -> None:
    pre = getattr(request.state, "new_pre", None)
    if pre:
        name = pre_cookie_name(_svc(request).settings.cookies_secure)
        _set_cookie(request, response, name, pre, PRE_SESSION_MAX_AGE)


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    svc = _svc(request)
    secure = svc.settings.cookies_secure
    _set_cookie(
        request, response, session_cookie_name(secure), token, svc.accounts.sessions.max_age_s
    )
    _delete_cookie(request, response, pre_cookie_name(secure))


def clear_session_cookie(request: Request, response: Response) -> None:
    _delete_cookie(request, response, session_cookie_name(_svc(request).settings.cookies_secure))
