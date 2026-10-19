"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from sentinel import __version__
from sentinel.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Without ``settings`` it loads them and fails closed."""
    settings = settings or load_settings()
    # No Swagger/ReDoc: they pull scripts from a CDN, which the CSP will not allow.
    app = FastAPI(
        title="Entegratör Status",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
