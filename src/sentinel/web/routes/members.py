"""Members, invitations and API tokens of an organization."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.accounts.service import AccountError
from sentinel.tenancy.orgs import OrgError
from sentinel.tenancy.roles import Action, Role
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

RoleField = Annotated[Role, Form()]


async def _members_page(
    request: Request, ctx: OrgContext, *, status: int = 200, error: str | None = None
) -> Response:
    svc = services(request)
    members = await svc.orgs.members(ctx.scope)
    pending = await svc.invitations.pending(ctx.scope)
    return svc.renderer.page(
        request,
        "org/members.html",
        status=status,
        error=error,
        ctx=ctx,
        section="members",
        members=members,
        pending=pending,
        roles=list(Role),
    )


@router.get("/o/{slug}/uyeler")
async def members(request: Request, ctx: InOrg) -> Response:
    return await _members_page(request, ctx)


@router.post("/o/{slug}/uyeler/davet")
async def invite(
    request: Request, ctx: InOrg, email: Annotated[str, Form(max_length=320)], role: RoleField
) -> Response:
    svc = services(request)
    try:
        await svc.invitations.invite(
            ctx.scope, email, role, ctx.viewer.user.display_name, client_ip(request)
        )
    except (OrgError, AccountError) as exc:
        return await _members_page(request, ctx, status=400, error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyeler", status_code=303)


@router.post("/o/{slug}/uyeler/{membership_id}/rol")
async def change_role(
    request: Request, ctx: InOrg, membership_id: uuid.UUID, role: RoleField
) -> Response:
    try:
        await services(request).orgs.change_role(ctx.scope, membership_id, role, client_ip(request))
    except OrgError as exc:
        return await _members_page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyeler", status_code=303)


@router.post("/o/{slug}/uyeler/{membership_id}/cikar")
async def remove_member(request: Request, ctx: InOrg, membership_id: uuid.UUID) -> Response:
    try:
        await services(request).orgs.remove(ctx.scope, membership_id, client_ip(request))
    except OrgError as exc:
        return await _members_page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyeler", status_code=303)


@router.post("/o/{slug}/davetler/{invitation_id}/iptal")
async def revoke_invitation(request: Request, ctx: InOrg, invitation_id: uuid.UUID) -> Response:
    try:
        await services(request).invitations.revoke(ctx.scope, invitation_id, client_ip(request))
    except OrgError as exc:
        return await _members_page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyeler", status_code=303)


# --- API tokens -------------------------------------------------------------------


async def _tokens_page(
    request: Request,
    ctx: OrgContext,
    *,
    status: int = 200,
    error: str | None = None,
    created: str | None = None,
) -> Response:
    svc = services(request)
    tokens = await svc.api_tokens.list(ctx.scope)
    return svc.renderer.page(
        request,
        "org/tokens.html",
        status=status,
        error=error,
        ctx=ctx,
        section="tokens",
        tokens=tokens,
        created=created,
    )


@router.get("/o/{slug}/tokenlar")
async def tokens(request: Request, ctx: InOrg) -> Response:
    ctx.scope.require(Action.TOKENS_MANAGE)
    return await _tokens_page(request, ctx)


@router.post("/o/{slug}/tokenlar")
async def create_token(
    request: Request,
    ctx: InOrg,
    name: Annotated[str, Form(max_length=200)],
    days: Annotated[int, Form(ge=1, le=365)] = 90,
) -> Response:
    try:
        raw, _ = await services(request).api_tokens.create(
            ctx.scope, name, days, client_ip(request)
        )
    except OrgError as exc:
        return await _tokens_page(request, ctx, status=400, error=exc.key)
    # Shown once, on this response only (Cache-Control: no-store is set globally).
    return await _tokens_page(request, ctx, created=raw)


@router.post("/o/{slug}/tokenlar/{token_id}/iptal")
async def revoke_token(request: Request, ctx: InOrg, token_id: uuid.UUID) -> Response:
    try:
        await services(request).api_tokens.revoke(ctx.scope, token_id, client_ip(request))
    except OrgError as exc:
        return await _tokens_page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/tokenlar", status_code=303)


def _status(exc: OrgError) -> int:
    return 404 if exc.key == "not_found" else 400
