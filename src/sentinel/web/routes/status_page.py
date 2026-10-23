"""Settings of the organization's public status page (admins)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.public.service import PublicError
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

Flag = Annotated[str, Form(max_length=8)]


async def _page(
    request: Request, ctx: OrgContext, *, status: int = 200, error: str | None = None
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/status_page.html",
        status=status,
        error=error,
        ctx=ctx,
        section="status_page",
        settings=await svc.status_pages.settings(ctx.scope),
        public_url=f"{svc.settings.public_base}/s/{ctx.org.slug}",
    )


@router.get("/o/{slug}/durum-sayfasi")
async def settings(request: Request, ctx: InOrg) -> Response:
    return await _page(request, ctx)


@router.post("/o/{slug}/durum-sayfasi")
async def save(
    request: Request,
    ctx: InOrg,
    title: Annotated[str, Form(max_length=200)],
    intro: Annotated[str, Form(max_length=4000)] = "",
    enabled: Flag = "",
    show_dependencies: Flag = "",
    subscriptions: Flag = "",
) -> Response:
    form = await request.form()
    try:
        product_ids = [uuid.UUID(str(v)) for v in form.getlist("product_ids")][:100]
    except ValueError:
        return await _page(request, ctx, status=404, error="not_found")
    try:
        await services(request).status_pages.save(
            ctx.scope,
            enabled=enabled == "true",
            title=title,
            intro=intro,
            product_ids=product_ids,
            show_dependencies=show_dependencies == "true",
            subscriptions=subscriptions == "true",
            ip=client_ip(request),
        )
    except PublicError as exc:
        status = 404 if exc.key == "not_found" else 400
        return await _page(request, ctx, status=status, error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/durum-sayfasi", status_code=303)
