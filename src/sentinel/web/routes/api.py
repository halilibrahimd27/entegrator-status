"""JSON API v1. Bearer tokens only: cookies are ignored, so CSRF does not apply."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sentinel.models import Organization
from sentinel.risk.impact import Impact
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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _impact(i: Impact) -> dict[str, Any]:
    return {
        "type": i.kind,
        "id": str(i.subject_id),
        "component_id": str(i.component_id),
        "depth": i.depth,
        "path": list(i.path),
        "severity": i.severity,
        "reason": i.reason,
        "title": i.title,
        "when": _iso(i.when),
        "link": i.link,
    }


@router.get("/products")
async def products(request: Request, scope: ApiScope) -> dict[str, Any]:
    """Every product with its derived state (see ``sentinel.risk.dashboard``)."""
    board = await services(request).dashboard.build(scope)
    return {
        "products": [
            {
                "product_id": str(p.id),
                "name": p.name,
                "state": p.state,
                "reason": _impact(p.reason) if p.reason else None,
                "high": p.high,
                "medium": p.medium,
            }
            for p in board.products
        ]
    }


@router.get("/impacts")
async def impacts(
    request: Request,
    scope: ApiScope,
    product: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    """Incidents, changes and risks that reach the products, worst first, per product."""
    by_product = await services(request).impact.for_org(scope)
    return {
        "products": {
            str(pid): [_impact(i) for i in found]
            for pid, found in by_product.items()
            if product is None or pid == product
        }
    }


@router.get("/risks")
async def risks(request: Request, scope: ApiScope) -> dict[str, Any]:
    """The risk register with the organization's decisions."""
    entries = await services(request).register.entries(scope)
    return {
        "risks": [
            {
                "risk_id": str(e.risk.id),
                "kind": e.risk.kind,
                "severity": e.severity,
                "state": e.state,
                "until": _iso(e.until),
                "due_at": _iso(e.risk.due_at),
                "component": e.component,
                "products": e.products,
                "link": e.risk.link,
            }
            for e in entries
        ]
    }


@router.get("/calendar")
async def calendar(request: Request, scope: ApiScope) -> dict[str, Any]:
    """Planned changes: effective dates, risk dates and maintenance windows."""
    items = await services(request).register.calendar(scope)
    return {
        "items": [
            {
                "when": i.when.isoformat(),
                "type": i.kind,
                "title": i.title,
                "component": i.component,
                "link": i.link,
            }
            for i in items
        ]
    }


async def unauthorized(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )
