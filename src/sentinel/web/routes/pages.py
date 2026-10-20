"""Static-ish pages: landing and the privacy notice."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response

from sentinel.web.services import services
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
    return services(request).renderer.page(request, "pages/privacy.html")
