"""Organization list, creation, home, settings, leaving and the audit log."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel import audit
from sentinel.accounts.service import AccountError
from sentinel.tenancy.orgs import OrgError
from sentinel.web.org_context import InOrg
from sentinel.web.routes.invites import pending_invitation
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import Viewer, csrf_protect, require_viewer

router = APIRouter(dependencies=[Depends(csrf_protect)])

SignedIn = Annotated[Viewer, Depends(require_viewer)]
Name = Annotated[str, Form(max_length=200)]


@router.get("/orglar")
async def org_list(request: Request, viewer: SignedIn) -> Response:
    svc = services(request)
    orgs = await svc.orgs.of_user(viewer.user.id)
    return svc.renderer.page(
        request, "orgs/index.html", orgs=orgs, invitation=await pending_invitation(request)
    )


@router.post("/orglar")
async def org_create(request: Request, viewer: SignedIn, name: Name) -> Response:
    svc = services(request)
    try:
        org = await svc.orgs.create(viewer.user.id, name, client_ip(request))
    except OrgError as exc:
        orgs = await svc.orgs.of_user(viewer.user.id)
        return svc.renderer.page(
            request, "orgs/index.html", status=400, error=exc.key, orgs=orgs, invitation=None
        )
    return RedirectResponse(f"/o/{org.slug}", status_code=303)


@router.get("/o/{slug}")
async def org_home(request: Request, ctx: InOrg) -> Response:
    return services(request).renderer.page(request, "org/home.html", ctx=ctx, section="home")


@router.get("/o/{slug}/ayarlar")
async def org_settings(request: Request, ctx: InOrg) -> Response:
    return services(request).renderer.page(
        request, "org/settings.html", ctx=ctx, section="settings"
    )


@router.post("/o/{slug}/ayarlar")
async def org_rename(request: Request, ctx: InOrg, name: Name) -> Response:
    svc = services(request)
    try:
        await svc.orgs.rename(ctx.scope, name, client_ip(request))
    except OrgError as exc:
        return svc.renderer.page(
            request, "org/settings.html", status=400, error=exc.key, ctx=ctx, section="settings"
        )
    return RedirectResponse(f"/o/{ctx.org.slug}/ayarlar", status_code=303)


@router.post("/o/{slug}/ayril")
async def org_leave(request: Request, ctx: InOrg) -> Response:
    svc = services(request)
    try:
        await svc.orgs.leave(ctx.scope, client_ip(request))
    except OrgError as exc:
        return svc.renderer.page(
            request, "org/settings.html", status=400, error=exc.key, ctx=ctx, section="settings"
        )
    return RedirectResponse("/orglar", status_code=303)


@router.get("/o/{slug}/denetim")
async def org_audit(request: Request, ctx: InOrg) -> Response:
    svc = services(request)
    entries = await audit.recent(svc.db, ctx.scope)
    return svc.renderer.page(request, "org/audit.html", ctx=ctx, section="audit", entries=entries)


@router.post("/o/{slug}/sil")
async def org_close(
    request: Request,
    ctx: InOrg,
    confirm: Annotated[str, Form(max_length=63)],
    password: Annotated[str, Form(max_length=256)],
    code: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    svc = services(request)
    try:
        await svc.privacy.close_org(
            ctx.scope,
            ctx.viewer.user.id,
            password=password,
            code=code,
            confirm=confirm,
            ip=client_ip(request),
        )
    except AccountError as exc:
        return svc.renderer.page(
            request, "org/settings.html", status=400, error=exc.key, ctx=ctx, section="settings"
        )
    return RedirectResponse("/orglar", status_code=303)
