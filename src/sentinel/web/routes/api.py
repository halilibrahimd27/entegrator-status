"""JSON API v1. Bearer tokens only: cookies are ignored, so CSRF does not apply."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sentinel.models import Organization
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import services

router = APIRouter(prefix="/api/v1")


class ApiAuthError(Exception):
    pass


async def api_scope(request: Request) -> TenantScope:
    header = request.headers.get("authorization", "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise ApiAuthError
    scope = await services(request).api_tokens.authenticate(raw)
    if scope is None:
        raise ApiAuthError
    return scope


ApiScope = Annotated[TenantScope, Depends(api_scope)]


@router.get("/org")
async def current_org(request: Request, scope: ApiScope) -> dict[str, str]:
    async with services(request).db.tenant(scope) as s:
        org = await s.get(Organization, scope.org_id)
        if org is None or org.deleted_at is not None:
            raise ApiAuthError
        return {"slug": org.slug, "name": org.name}


async def unauthorized(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )
