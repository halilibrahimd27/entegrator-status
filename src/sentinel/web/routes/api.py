"""JSON API v1. Bearer tokens only: cookies are ignored, so CSRF does not apply."""

from __future__ import annotations

from typing import Annotated, Any

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


@router.get("/status")
async def status(request: Request, scope: ApiScope) -> dict[str, Any]:
    """Every component the organization's products depend on, worst first."""
    rows = await services(request).status.overview(scope)
    return {
        "components": [
            {
                "component_id": str(row.node.component_id),
                "service": row.node.service,
                "component": row.node.component,
                "shared": row.node.shared,
                "depth": row.node.depth,
                "state": row.status.state,
                "since": row.status.since.isoformat() if row.status.since else None,
                "cause": row.status.cause,
                "incident_started_at": (
                    row.status.incident.started_at.isoformat() if row.status.incident else None
                ),
                "uptime": row.uptime,
                "products": row.products,
            }
            for row in rows
        ]
    }


async def unauthorized(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )
