"""Credential vault pages. Secrets go in and never come back out (ADR 0011)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.models import Component, CredentialKind, Source
from sentinel.models.credentials import SECRET_FIELDS
from sentinel.tenancy.credentials import CredentialError
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

Short = Annotated[str, Form(max_length=200)]
Url = Annotated[str, Form(max_length=2000)]
Secret = Annotated[str, Form(max_length=4096)]


def _status(exc: CredentialError) -> int:
    return 404 if exc.key == "not_found" else 403 if exc.key == "credentials.shared_source" else 400


async def _page(
    request: Request, ctx: OrgContext, *, status: int = 200, error: str | None = None
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/credentials.html",
        status=status,
        error=error,
        ctx=ctx,
        section="credentials",
        credentials=await svc.credentials.list(ctx.scope),
        kinds=[k.value for k in CredentialKind],
        secret_fields=SECRET_FIELDS,
    )


@router.get("/o/{slug}/kimlik-bilgileri")
async def credentials(request: Request, ctx: InOrg) -> Response:
    return await _page(request, ctx)


@router.post("/o/{slug}/kimlik-bilgileri")
async def create(
    request: Request,
    ctx: InOrg,
    name: Short,
    kind: Short,
    host: Short,
    header_name: Short = "",
    token_url: Url = "",
    oauth_scope: Short = "",
    value: Secret = "",
    token: Secret = "",
    username: Secret = "",
    password: Secret = "",
    client_id: Secret = "",
    client_secret: Secret = "",
    entitled: Short = "",
) -> Response:
    secret = {
        "value": value,
        "token": token,
        "username": username,
        "password": password,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        await services(request).credentials.create(
            ctx.scope,
            name=name,
            kind=kind,
            host=host,
            secret=secret,
            entitled=entitled == "true",
            header_name=header_name,
            token_url=token_url,
            oauth_scope=oauth_scope,
            ip=client_ip(request),
        )
    except CredentialError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/kimlik-bilgileri", status_code=303)


@router.post("/o/{slug}/kimlik-bilgileri/{credential_id}/yenile")
async def replace(
    request: Request,
    ctx: InOrg,
    credential_id: uuid.UUID,
    value: Secret = "",
    token: Secret = "",
    username: Secret = "",
    password: Secret = "",
    client_id: Secret = "",
    client_secret: Secret = "",
) -> Response:
    secret = {
        "value": value,
        "token": token,
        "username": username,
        "password": password,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        await services(request).credentials.replace_secret(
            ctx.scope, credential_id, secret, ip=client_ip(request)
        )
    except CredentialError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/kimlik-bilgileri", status_code=303)


@router.post("/o/{slug}/kimlik-bilgileri/{credential_id}/sil")
async def delete(request: Request, ctx: InOrg, credential_id: uuid.UUID) -> Response:
    try:
        await services(request).credentials.delete(ctx.scope, credential_id, ip=client_ip(request))
    except CredentialError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/kimlik-bilgileri", status_code=303)


@router.post("/o/{slug}/kaynaklar/{source_id}/kimlik")
async def attach(
    request: Request, ctx: InOrg, source_id: uuid.UUID, credential_id: Short = ""
) -> Response:
    svc = services(request)
    async with svc.db.tenant(ctx.scope) as s:
        source = await s.get(Source, source_id)
        component = await s.get(Component, source.component_id) if source else None
    if component is None:
        return svc.renderer.message(
            request, "errors.not_found_title", "errors.not_found_body", status=404
        )
    try:
        chosen = uuid.UUID(credential_id) if credential_id else None
    except ValueError:
        chosen = uuid.uuid4()  # unknown: reported as not found below
    try:
        await svc.credentials.attach(ctx.scope, source_id, chosen, ip=client_ip(request))
    except CredentialError as exc:
        return svc.renderer.message(
            request, "errors.bad_request_title", exc.key, status=_status(exc)
        )
    return RedirectResponse(f"/o/{ctx.org.slug}/servisler/{component.service_id}", status_code=303)
