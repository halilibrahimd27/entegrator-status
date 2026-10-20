"""Products: list, create, edit, dependencies, imports and the graph view."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from starlette.responses import RedirectResponse, Response

from sentinel.catalog.parsers import MAX_UPLOAD_BYTES, ImportFormatError
from sentinel.catalog.service import CatalogError
from sentinel.graph.service import GraphError
from sentinel.models.status import MANUAL_STATUSES
from sentinel.web import graph_svg
from sentinel.web.i18n import t
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

Name = Annotated[str, Form(max_length=200)]
Description = Annotated[str, Form(max_length=2000)]
Failure = CatalogError | GraphError | ImportFormatError


def status_of(exc: Failure) -> int:
    if exc.key == "not_found":
        return 404
    if exc.key in {"catalog.shared_readonly", "graph.shared_edge"}:
        return 403
    return 400


def _products_url(ctx: OrgContext) -> str:
    return f"/o/{ctx.org.slug}/urunler"


@router.get("/o/{slug}/urunler")
async def products(request: Request, ctx: InOrg) -> Response:
    rows = await services(request).catalog.products(ctx.scope)
    return services(request).renderer.page(
        request, "org/products.html", ctx=ctx, section="products", rows=rows
    )


@router.post("/o/{slug}/urunler")
async def create_product(
    request: Request, ctx: InOrg, name: Name, description: Description = ""
) -> Response:
    svc = services(request)
    try:
        product = await svc.catalog.create_product(ctx.scope, name, description, client_ip(request))
    except CatalogError as exc:
        rows = await svc.catalog.products(ctx.scope)
        return svc.renderer.page(
            request,
            "org/products.html",
            status=status_of(exc),
            error=exc.key,
            ctx=ctx,
            section="products",
            rows=rows,
        )
    return RedirectResponse(f"{_products_url(ctx)}/{product.id}", status_code=303)


async def product_page(
    request: Request,
    ctx: OrgContext,
    product_id: uuid.UUID,
    *,
    query: str = "",
    status: int = 200,
    error: str | None = None,
    detail: str = "",
) -> Response:
    svc = services(request)
    try:
        product = await svc.catalog.product(ctx.scope, product_id)
    except CatalogError as exc:
        return svc.renderer.message(
            request, "errors.not_found_title", "errors.not_found_body", status=status_of(exc)
        )
    graph = await svc.graph.downstream(ctx.scope, product_id)
    svg = graph_svg.render(
        product.name,
        graph,
        title=t("products.graph_title").format(product=product.name),
        description=t("products.graph_desc").format(count=len(graph.nodes)),
    )
    dependencies = await svc.catalog.dependencies(ctx.scope, product_id)
    return svc.renderer.page(
        request,
        "org/product.html",
        status=status,
        error=error,
        detail=detail,
        ctx=ctx,
        section="products",
        product=product,
        dependencies=dependencies,
        statuses=await svc.status.current(ctx.scope, [c.id for _, c, _ in dependencies]),
        incidents=await svc.notices.product_incidents(ctx.scope, product_id),
        incident_statuses=[s.value for s in MANUAL_STATUSES],
        drift=await svc.risks.drift(ctx.scope, [d for d, _, _ in dependencies]),
        results=await svc.catalog.find_components(ctx.scope, query),
        query=query,
        svg=svg,
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
    )


@router.get("/o/{slug}/urunler/{product_id}")
async def product(
    request: Request,
    ctx: InOrg,
    product_id: uuid.UUID,
    q: Annotated[str, Query(max_length=80)] = "",
) -> Response:
    return await product_page(request, ctx, product_id, query=q)


@router.post("/o/{slug}/urunler/{product_id}/ayarlar")
async def update_product(
    request: Request, ctx: InOrg, product_id: uuid.UUID, name: Name, description: Description = ""
) -> Response:
    try:
        await services(request).catalog.update_product(
            ctx.scope, product_id, name, description, client_ip(request)
        )
    except CatalogError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"{_products_url(ctx)}/{product_id}", status_code=303)


@router.post("/o/{slug}/urunler/{product_id}/sil")
async def delete_product(request: Request, ctx: InOrg, product_id: uuid.UUID) -> Response:
    try:
        await services(request).catalog.delete_product(ctx.scope, product_id, client_ip(request))
    except CatalogError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(_products_url(ctx), status_code=303)


@router.post("/o/{slug}/urunler/{product_id}/bagimlilik")
async def add_dependency(
    request: Request,
    ctx: InOrg,
    product_id: uuid.UUID,
    to_component_id: Annotated[uuid.UUID, Form()],
    version: Annotated[str, Form(max_length=100)] = "",
    operations: Annotated[str, Form(max_length=4000)] = "",
) -> Response:
    usage: dict[str, object] = {}
    if version.strip():
        usage["version"] = version.strip()
    ops = sorted({o.strip() for o in operations.replace(",", "\n").splitlines() if o.strip()})
    if ops:
        usage["operations"] = ops[:500]
    try:
        await services(request).graph.add(
            ctx.scope,
            from_product_id=product_id,
            to_component_id=to_component_id,
            usage=usage,
            ip=client_ip(request),
        )
    except GraphError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"{_products_url(ctx)}/{product_id}", status_code=303)


@router.post("/o/{slug}/bagimliliklar/{dependency_id}/sil")
async def remove_dependency(request: Request, ctx: InOrg, dependency_id: uuid.UUID) -> Response:
    svc = services(request)
    try:
        await svc.graph.remove(ctx.scope, dependency_id, client_ip(request))
    except GraphError as exc:
        return svc.renderer.message(
            request, "errors.not_found_title", exc.key, status=status_of(exc)
        )
    return RedirectResponse(request.headers.get("referer") or _products_url(ctx), status_code=303)


@router.post("/o/{slug}/urunler/{product_id}/ice-aktar")
async def import_preview(
    request: Request, ctx: InOrg, product_id: uuid.UUID, file: Annotated[UploadFile, File()]
) -> Response:
    svc = services(request)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        preview = await svc.imports.preview(ctx.scope, product_id, file.filename or "", content)
    except ImportFormatError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return svc.renderer.page(
        request,
        "org/import_preview.html",
        ctx=ctx,
        section="products",
        product_id=product_id,
        preview=preview,
    )


@router.post("/o/{slug}/urunler/{product_id}/ice-aktar/onayla")
async def import_confirm(
    request: Request, ctx: InOrg, product_id: uuid.UUID, token: Annotated[str, Form(max_length=64)]
) -> Response:
    try:
        await services(request).imports.confirm(ctx.scope, product_id, token, client_ip(request))
    except ImportFormatError as exc:
        return await product_page(request, ctx, product_id, status=status_of(exc), error=exc.key)
    return RedirectResponse(f"{_products_url(ctx)}/{product_id}", status_code=303)
