"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse, Response

from sentinel import __version__
from sentinel.config import Settings, load_settings
from sentinel.web.headers import SecurityHeaders
from sentinel.web.routes import account, auth, pages
from sentinel.web.services import build_services, services
from sentinel.web.session_auth import (
    AdminTotpRequiredError,
    CsrfError,
    LoginRequiredError,
    SecondFactorRequiredError,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Without ``settings`` it loads them and fails closed."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = build_services(settings)
        try:
            yield
        finally:
            await app.state.services.aclose()

    # No Swagger/ReDoc: they pull scripts from a CDN, which the CSP will not allow.
    app = FastAPI(
        title="Entegratör Status",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(SecurityHeaders, hsts=settings.cookies_secure)
    app.mount("/static", StaticFiles(packages=[("sentinel.web", "static")]), name="static")
    for router in (pages.router, auth.router, account.router):
        app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    _register_error_pages(app)
    return app


def _register_error_pages(app: FastAPI) -> None:
    async def to_login(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/giris", status_code=303)

    async def to_second_factor(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/giris/2fa", status_code=303)

    async def to_admin_totp(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/hesap?ok=admin_totp", status_code=303)

    async def csrf_failed(request: Request, exc: Exception) -> Response:
        return services(request).renderer.message(
            request, "errors.csrf_title", "errors.csrf_body", status=403
        )

    async def bad_form(request: Request, exc: Exception) -> Response:
        return services(request).renderer.message(
            request, "errors.bad_request_title", "errors.bad_request_body", status=400
        )

    async def http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        if status == 404:
            return services(request).renderer.message(
                request, "errors.not_found_title", "errors.not_found_body", status=404
            )
        return services(request).renderer.message(
            request, "errors.bad_request_title", "errors.bad_request_body", status=status
        )

    app.add_exception_handler(LoginRequiredError, to_login)
    app.add_exception_handler(SecondFactorRequiredError, to_second_factor)
    app.add_exception_handler(AdminTotpRequiredError, to_admin_totp)
    app.add_exception_handler(CsrfError, csrf_failed)
    app.add_exception_handler(RequestValidationError, bad_form)
    app.add_exception_handler(StarletteHTTPException, http_error)
