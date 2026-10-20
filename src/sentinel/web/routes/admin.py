"""Platform admin: the shared catalog review queue. Everyone else gets a 404."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response

from sentinel.catalog.service import CatalogError
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import Viewer, csrf_protect, require_viewer

router = APIRouter(dependencies=[Depends(csrf_protect)])


async def platform_admin(viewer: Annotated[Viewer, Depends(require_viewer)]) -> Viewer:
    # require_viewer already forces TOTP enrolment for platform admins.
    if not viewer.user.is_platform_admin:
        raise HTTPException(status_code=404)
    return viewer


Admin = Annotated[Viewer, Depends(platform_admin)]
Note = Annotated[str, Form(max_length=2000)]


@router.get("/yonetim/katalog")
async def review_queue(request: Request, admin: Admin) -> Response:
    svc = services(request)
    return svc.renderer.page(request, "admin/catalog.html", pending=await svc.proposals.pending())


@router.post("/yonetim/katalog/{proposal_id}/kabul")
async def accept(
    request: Request, admin: Admin, proposal_id: uuid.UUID, note: Note = ""
) -> Response:
    try:
        await services(request).proposals.accept(
            admin.user.id, proposal_id, note, client_ip(request)
        )
    except CatalogError as exc:
        raise HTTPException(status_code=404) from exc
    return RedirectResponse("/yonetim/katalog", status_code=303)


@router.post("/yonetim/katalog/{proposal_id}/ret")
async def reject(
    request: Request, admin: Admin, proposal_id: uuid.UUID, note: Note = ""
) -> Response:
    try:
        await services(request).proposals.reject(
            admin.user.id, proposal_id, note, client_ip(request)
        )
    except CatalogError as exc:
        raise HTTPException(status_code=404) from exc
    return RedirectResponse("/yonetim/katalog", status_code=303)
