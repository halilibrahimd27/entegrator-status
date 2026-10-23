"""Platform admin: catalog review queue, corrections, organizations and quotas, the
platform audit log, notices on shared services. Everyone else gets a 404."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response

from sentinel.admin.service import PlatformError
from sentinel.catalog.service import CatalogError
from sentinel.engine.notices import NoticeError
from sentinel.models.changes import ANNOUNCEMENT_CATEGORIES
from sentinel.public.corrections import CorrectionError
from sentinel.web.routes.status import parse_day, parse_local
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


def _admin_error(request: Request, exc: PlatformError | NoticeError) -> Response:
    if exc.key == "not_found":
        raise HTTPException(status_code=404)
    return services(request).renderer.message(
        request, "errors.bad_request_title", exc.key, status=400
    )


@router.get("/yonetim")
async def admin_home(request: Request, admin: Admin) -> Response:
    return services(request).renderer.page(request, "admin/index.html")


@router.get("/yonetim/orglar")
async def orgs(request: Request, admin: Admin) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "admin/orgs.html",
        orgs=await svc.platform.orgs(),
        default_quota=svc.settings.org_max_sources,
    )


@router.post("/yonetim/orglar/{org_id}/kota")
async def set_quota(
    request: Request,
    admin: Admin,
    org_id: uuid.UUID,
    max_sources: Annotated[str, Form(max_length=10)] = "",
) -> Response:
    try:
        await services(request).platform.set_quota(
            admin.user.id, org_id, max_sources, ip=client_ip(request)
        )
    except PlatformError as exc:
        return _admin_error(request, exc)
    return RedirectResponse("/yonetim/orglar", status_code=303)


@router.get("/yonetim/denetim")
async def platform_audit(request: Request, admin: Admin) -> Response:
    svc = services(request)
    return svc.renderer.page(request, "admin/audit.html", entries=await svc.platform.audit_log())


@router.get("/yonetim/servisler")
async def shared_services(
    request: Request, admin: Admin, q: Annotated[str, Query(max_length=80)] = ""
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request, "admin/services.html", found=await svc.platform.shared_services(q), query=q
    )


@router.get("/yonetim/servisler/{service_id}")
async def shared_service(request: Request, admin: Admin, service_id: uuid.UUID) -> Response:
    svc = services(request)
    found = await svc.platform.shared_service(service_id)
    if found is None:
        raise HTTPException(status_code=404)
    return svc.renderer.page(
        request, "admin/service.html", shared=found, categories=ANNOUNCEMENT_CATEGORIES
    )


@router.post("/yonetim/servisler/{service_id}/bakim")
async def shared_window(
    request: Request,
    admin: Admin,
    service_id: uuid.UUID,
    component_id: Annotated[uuid.UUID, Form()],
    title: Annotated[str, Form(max_length=400)],
    starts_at: Annotated[str, Form(max_length=20)],
    ends_at: Annotated[str, Form(max_length=20)],
) -> Response:
    try:
        await services(request).platform.add_window(
            admin.user.id,
            component_id,
            title=title,
            starts_at=parse_local(starts_at),
            ends_at=parse_local(ends_at),
            ip=client_ip(request),
        )
    except (PlatformError, NoticeError) as exc:
        return _admin_error(request, exc)
    return RedirectResponse(f"/yonetim/servisler/{service_id}", status_code=303)


@router.post("/yonetim/bakimlar/{window_id}/bitir")
async def end_shared_window(
    request: Request,
    admin: Admin,
    window_id: uuid.UUID,
    service_id: Annotated[uuid.UUID, Form()],
) -> Response:
    try:
        await services(request).platform.end_window(admin.user.id, window_id, ip=client_ip(request))
    except PlatformError as exc:
        return _admin_error(request, exc)
    return RedirectResponse(f"/yonetim/servisler/{service_id}", status_code=303)


@router.post("/yonetim/servisler/{service_id}/duyuru")
async def shared_announcement(
    request: Request,
    admin: Admin,
    service_id: uuid.UUID,
    title: Annotated[str, Form(max_length=400)],
    category: Annotated[str, Form(max_length=20)],
    link: Annotated[str, Form(max_length=2000)] = "",
    effective_on: Annotated[str, Form(max_length=20)] = "",
    component_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    try:
        await services(request).platform.add_announcement(
            admin.user.id,
            service_id,
            title=title,
            category=category,
            component_ids=component_ids,
            link=link,
            effective_at=parse_day(effective_on),
            ip=client_ip(request),
        )
    except (PlatformError, NoticeError) as exc:
        return _admin_error(request, exc)
    return RedirectResponse(f"/yonetim/servisler/{service_id}", status_code=303)


@router.post("/yonetim/degisiklikler/{change_id}/sil")
async def delete_shared_announcement(
    request: Request, admin: Admin, change_id: uuid.UUID
) -> Response:
    try:
        service_id = await services(request).platform.delete_announcement(
            admin.user.id, change_id, ip=client_ip(request)
        )
    except PlatformError as exc:
        return _admin_error(request, exc)
    return RedirectResponse(f"/yonetim/servisler/{service_id}", status_code=303)
