"""Platform admin: the catalog review queue and corrections. Everyone else gets a 404."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response

from sentinel.catalog.service import CatalogError
from sentinel.public.corrections import CorrectionError
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


@router.get("/yonetim/duzeltmeler")
async def corrections(request: Request, admin: Admin) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request, "admin/corrections.html", requests=await svc.corrections.queue()
    )


@router.post("/yonetim/duzeltmeler/{correction_id}/karar")
async def decide_correction(
    request: Request,
    admin: Admin,
    correction_id: uuid.UUID,
    decision: Annotated[str, Form(max_length=16)],
    note: Note = "",
) -> Response:
    svc = services(request)
    try:
        await svc.corrections.decide(
            admin.user.id, correction_id, decision, note, client_ip(request)
        )
    except CorrectionError as exc:
        if exc.key == "not_found":
            raise HTTPException(status_code=404) from exc
        return svc.renderer.page(
            request,
            "admin/corrections.html",
            status=400,
            error=exc.key,
            requests=await svc.corrections.queue(),
        )
    return RedirectResponse("/yonetim/duzeltmeler", status_code=303)
