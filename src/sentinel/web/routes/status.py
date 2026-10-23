"""Manual incidents, maintenance windows, manual announcements, the org status page."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.engine.notices import NoticeError
from sentinel.models import Change, Component, Incident, MaintenanceWindow
from sentinel.models import Service as ServiceModel
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.routes.catalog import service_page
from sentinel.web.routes.products import product_page
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect
from sentinel.web.templating import LOCAL_TZ

router = APIRouter(dependencies=[Depends(csrf_protect)])

Title = Annotated[str, Form(max_length=400)]
Body = Annotated[str, Form(max_length=8000)]
Status = Annotated[str, Form(max_length=20)]
LocalTime = Annotated[str, Form(max_length=20)]


def status_of(exc: NoticeError) -> int:
    if exc.key == "not_found":
        return 404
    return 403 if exc.key == "status.shared_component" else 400


def parse_local(value: str) -> datetime:
    """A ``datetime-local`` value, entered in Europe/Istanbul, as an aware datetime."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise NoticeError("status.invalid_time") from None
    if parsed.tzinfo is not None:
        raise NoticeError("status.invalid_time")
    return parsed.replace(tzinfo=LOCAL_TZ)


@router.get("/o/{slug}/durum")
async def overview(request: Request, ctx: InOrg) -> Response:
    svc = services(request)
    rows = await svc.status.overview(ctx.scope)
    return svc.renderer.page(
        request,
        "org/status.html",
        ctx=ctx,
        section="status",
        rows=rows,
        hints=await svc.correlation.hints(ctx.scope),
    )


def parse_day(value: str) -> datetime | None:
    """A ``date`` input (Turkey time) as midnight, or None when empty."""
    if not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip() + "T00:00").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise NoticeError("status.invalid_time") from None


def _not_found(request: Request) -> Response:
    return services(request).renderer.message(
        request, "errors.not_found_title", "errors.not_found_body", status=404
    )


@router.post("/o/{slug}/urunler/{product_id}/olaylar")
async def open_incident(
    request: Request,
    ctx: InOrg,
    product_id: uuid.UUID,
    title: Title,
    body: Body,
    status: Status = "investigating",
) -> Response:
    try:
        await services(request).notices.open_incident(
            ctx.scope, product_id, title=title, body=body, status=status, ip=client_ip(request)
        )
    except NoticeError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/urunler/{product_id}#olaylar", status_code=303)


@router.post("/o/{slug}/olaylar/{incident_id}/guncelle")
async def update_incident(
    request: Request, ctx: InOrg, incident_id: uuid.UUID, body: Body, status: Status
) -> Response:
    svc = services(request)
    async with svc.db.tenant(ctx.scope) as s:
        incident = await s.get(Incident, incident_id)
        product_id = incident.product_id if incident else None
    if product_id is None:
        return _not_found(request)
    try:
        await svc.notices.update_incident(
            ctx.scope, incident_id, status=status, body=body, ip=client_ip(request)
        )
    except NoticeError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/urunler/{product_id}#olaylar", status_code=303)


async def _service_of(
    ctx: OrgContext, request: Request, component_id: uuid.UUID
) -> uuid.UUID | None:
    async with services(request).db.tenant(ctx.scope) as s:
        component = await s.get(Component, component_id)
        return component.service_id if component else None


@router.post("/o/{slug}/bilesenler/{component_id}/bakim")
async def add_window(
    request: Request,
    ctx: InOrg,
    component_id: uuid.UUID,
    title: Title,
    starts_at: LocalTime,
    ends_at: LocalTime,
) -> Response:
    service_id = await _service_of(ctx, request, component_id)
    if service_id is None:
        return _not_found(request)
    try:
        await services(request).notices.add_window(
            ctx.scope,
            component_id,
            title=title,
            starts_at=parse_local(starts_at),
            ends_at=parse_local(ends_at),
            ip=client_ip(request),
        )
    except NoticeError as exc:
        return await service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)


@router.post("/o/{slug}/bakimlar/{window_id}/sil")
async def end_window(request: Request, ctx: InOrg, window_id: uuid.UUID) -> Response:
    svc = services(request)
    async with svc.db.tenant(ctx.scope) as s:
        window = await s.get(MaintenanceWindow, window_id)
        component_id = window.component_id if window else None
    service_id = await _service_of(ctx, request, component_id) if component_id else None
    if service_id is None:
        return _not_found(request)
    try:
        await svc.notices.end_window(ctx.scope, window_id, ip=client_ip(request))
    except NoticeError as exc:
        return await service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)


@router.post("/o/{slug}/servisler/{service_id}/duyuru")
async def add_announcement(
    request: Request,
    ctx: InOrg,
    service_id: uuid.UUID,
    title: Title,
    category: Status,
    link: Annotated[str, Form(max_length=2000)] = "",
    published_on: LocalTime = "",
    effective_on: LocalTime = "",
    component_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    svc = services(request)
    async with svc.db.tenant(ctx.scope) as s:
        if await s.get(ServiceModel, service_id) is None:
            return _not_found(request)
    try:
        await svc.notices.add_announcement(
            ctx.scope,
            service_id,
            title=title,
            category=category,
            component_ids=component_ids,
            link=link,
            published_at=parse_day(published_on),
            effective_at=parse_day(effective_on),
            ip=client_ip(request),
        )
    except NoticeError as exc:
        return await service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)


@router.post("/o/{slug}/degisiklikler/{change_id}/sil")
async def delete_announcement(request: Request, ctx: InOrg, change_id: uuid.UUID) -> Response:
    svc = services(request)
    async with svc.db.tenant(ctx.scope) as s:
        change = await s.get(Change, change_id)
        component_id = change.component_id if change else None
    service_id = await _service_of(ctx, request, component_id) if component_id else None
    if service_id is None:
        return _not_found(request)
    try:
        await svc.notices.delete_announcement(ctx.scope, change_id, ip=client_ip(request))
    except NoticeError as exc:
        return await service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)
