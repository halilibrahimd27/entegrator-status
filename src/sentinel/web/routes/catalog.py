"""The organization's view of the catalog: shared services, private services, components,
sources, and proposals for the shared catalog.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from sentinel.catalog.service import CatalogError
from sentinel.models import Component, ServiceKind, Source, SourceKind
from sentinel.models import Service as ServiceModel
from sentinel.sources.config import KIND_CONFIG
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.routes.products import status_of
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

Text = Annotated[str, Form(max_length=2000)]
Short = Annotated[str, Form(max_length=200)]
# Generic source form: each kind takes the fields its config model declares.
SOURCE_FIELDS = (
    "url",
    "host",
    "domain",
    "name",
    "ecosystem",
    "provider",
    "format",
    "item_selector",
    "title_selector",
    "date_selector",
    "link_selector",
    "product",
    "cycle",
    "version",
    "method",
)


async def _catalog_page(
    request: Request,
    ctx: OrgContext,
    *,
    query: str = "",
    status: int = 200,
    error: str | None = None,
    detail: str = "",
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/catalog.html",
        status=status,
        error=error,
        detail=detail,
        ctx=ctx,
        section="catalog",
        query=query,
        shared=await svc.catalog.services(ctx.scope, query, shared=True),
        own=await svc.catalog.services(ctx.scope, query, shared=False),
        kinds=list(ServiceKind),
    )


@router.get("/o/{slug}/katalog")
async def catalog(
    request: Request, ctx: InOrg, q: Annotated[str, Query(max_length=80)] = ""
) -> Response:
    return await _catalog_page(request, ctx, query=q)


@router.post("/o/{slug}/servisler")
async def create_service(
    request: Request,
    ctx: InOrg,
    name: Short,
    kind: Annotated[str, Form(max_length=32)],
    homepage: Annotated[str, Form(max_length=500)] = "",
    description: Text = "",
    component: Short = "",
    tags: Short = "",
) -> Response:
    svc = services(request)
    try:
        service = await svc.catalog.create_service(
            ctx.scope,
            name=name,
            kind=kind,
            homepage=homepage,
            description=description,
            component=component,
            tags=tags,
            ip=client_ip(request),
        )
    except CatalogError as exc:
        return await _catalog_page(
            request, ctx, status=status_of(exc), error=exc.key, detail=exc.detail
        )
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service.id}", status_code=303)


@router.post("/o/{slug}/servisler/openapi")
async def create_from_openapi(
    request: Request, ctx: InOrg, name: Short, url: Annotated[str, Form(max_length=2000)]
) -> Response:
    svc = services(request)
    try:
        service = await svc.catalog.create_api_from_openapi(
            ctx.scope, name, url, client_ip(request)
        )
    except CatalogError as exc:
        return await _catalog_page(
            request, ctx, status=status_of(exc), error=exc.key, detail=exc.detail
        )
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service.id}", status_code=303)


async def _service_page(
    request: Request,
    ctx: OrgContext,
    service_id: uuid.UUID,
    *,
    status: int = 200,
    error: str | None = None,
    detail: str = "",
) -> Response:
    svc = services(request)
    try:
        service = await svc.catalog.service_detail(ctx.scope, service_id)
    except CatalogError as exc:
        return svc.renderer.message(
            request, "errors.not_found_title", "errors.not_found_body", status=status_of(exc)
        )
    proposals = [p for p in await svc.proposals.of_org(ctx.scope) if p.service_id == service_id]
    return svc.renderer.page(
        request,
        "org/service.html",
        status=status,
        error=error,
        detail=detail,
        ctx=ctx,
        section="catalog",
        detail_view=service,
        proposals=proposals,
        source_kinds=list(SourceKind),
    )


@router.get("/o/{slug}/servisler/{service_id}")
async def service(request: Request, ctx: InOrg, service_id: uuid.UUID) -> Response:
    return await _service_page(request, ctx, service_id)


@router.post("/o/{slug}/servisler/{service_id}/bilesen")
async def add_component(
    request: Request, ctx: InOrg, service_id: uuid.UUID, name: Short, tags: Short = ""
) -> Response:
    try:
        await services(request).catalog.add_component(
            ctx.scope, service_id, name, tags, client_ip(request)
        )
    except CatalogError as exc:
        return await _service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)


@router.post("/o/{slug}/bilesenler/{component_id}/kaynak")
async def add_source(request: Request, ctx: InOrg, component_id: uuid.UUID) -> Response:
    svc = services(request)
    form = await request.form()
    kind = str(form.get("kind", ""))
    accepted = set(KIND_CONFIG[SourceKind(kind)].model_fields) if kind in SourceKind else set()
    config: dict[str, Any] = {
        name: str(value).strip()[:2000]
        for name in SOURCE_FIELDS
        if name in accepted and (value := form.get(name)) and str(value).strip()
    }
    interval_raw = str(form.get("interval_s", "")).strip()
    interval = int(interval_raw) if interval_raw.isdigit() else None
    try:
        source = await svc.catalog.add_source(
            ctx.scope,
            component_id,
            kind,
            config,
            interval_s=interval,
            allow_http=form.get("allow_http") == "true",
            ip=client_ip(request),
        )
    except CatalogError as exc:
        back = await _service_of_component(request, ctx, component_id)
        if back is None:
            return svc.renderer.message(
                request, "errors.not_found_title", "errors.not_found_body", status=404
            )
        return await _service_page(
            request, ctx, back, status=status_of(exc), error=exc.key, detail=exc.detail
        )
    service_id = await _service_of_component(request, ctx, source.component_id)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)


async def _service_of_component(
    request: Request, ctx: OrgContext, component_id: uuid.UUID
) -> uuid.UUID | None:
    async with services(request).db.tenant(ctx.scope) as s:
        component = await s.get(Component, component_id)
        return component.service_id if component else None


async def _delete(
    request: Request,
    ctx: InOrg,
    model: type[ServiceModel] | type[Component] | type[Source],
    row_id: uuid.UUID,
    back: str,
) -> Response:
    svc = services(request)
    try:
        await svc.catalog.delete_private(ctx.scope, model, row_id, client_ip(request))
    except CatalogError as exc:
        return svc.renderer.message(
            request, "errors.not_found_title", exc.key, status=status_of(exc)
        )
    return RedirectResponse(back, status_code=303)


@router.post("/o/{slug}/servisler/{service_id}/sil")
async def delete_service(request: Request, ctx: InOrg, service_id: uuid.UUID) -> Response:
    return await _delete(request, ctx, ServiceModel, service_id, f"/o/{ctx.org.slug}/katalog")


@router.post("/o/{slug}/bilesenler/{component_id}/sil")
async def delete_component(request: Request, ctx: InOrg, component_id: uuid.UUID) -> Response:
    service_id = await _service_of_component(request, ctx, component_id)
    back = (
        f"/o/{ctx.org.slug}/servisler/{service_id}" if service_id else f"/o/{ctx.org.slug}/katalog"
    )
    return await _delete(request, ctx, Component, component_id, back)


@router.post("/o/{slug}/kaynaklar/{source_id}/sil")
async def delete_source(request: Request, ctx: InOrg, source_id: uuid.UUID) -> Response:
    async with services(request).db.tenant(ctx.scope) as s:
        source = await s.get(Source, source_id)
        component_id = source.component_id if source else None
    service_id = await _service_of_component(request, ctx, component_id) if component_id else None
    back = (
        f"/o/{ctx.org.slug}/servisler/{service_id}" if service_id else f"/o/{ctx.org.slug}/katalog"
    )
    return await _delete(request, ctx, Source, source_id, back)


@router.post("/o/{slug}/servisler/{service_id}/oner")
async def propose(request: Request, ctx: InOrg, service_id: uuid.UUID, note: Text = "") -> Response:
    try:
        await services(request).proposals.propose(ctx.scope, service_id, note, client_ip(request))
    except CatalogError as exc:
        return await _service_page(request, ctx, service_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{service_id}", status_code=303)
