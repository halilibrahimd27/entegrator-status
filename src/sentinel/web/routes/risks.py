"""The risk register and the calendar of planned changes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.risk.register import RegisterError
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect
from sentinel.web.templating import LOCAL_TZ

router = APIRouter(dependencies=[Depends(csrf_protect)])


async def _register(
    request: Request, ctx: OrgContext, *, status: int = 200, error: str | None = None
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/risks.html",
        status=status,
        error=error,
        ctx=ctx,
        section="risks",
        entries=await svc.register.entries(ctx.scope),
    )


@router.get("/o/{slug}/riskler")
async def register(request: Request, ctx: InOrg) -> Response:
    return await _register(request, ctx)


@router.post("/o/{slug}/riskler/{risk_id}/karar")
async def decide(
    request: Request,
    ctx: InOrg,
    risk_id: uuid.UUID,
    decision: Annotated[str, Form(max_length=20)],
    until: Annotated[str, Form(max_length=20)] = "",
    note: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    try:
        moment = (
            datetime.fromisoformat(until.strip() + "T00:00").replace(tzinfo=LOCAL_TZ)
            if until.strip()
            else None
        )
    except ValueError:
        moment = None
    try:
        await services(request).register.decide(
            ctx.scope, risk_id, decision, until=moment, note=note, ip=client_ip(request)
        )
    except RegisterError as exc:
        status = 404 if exc.key == "not_found" else 400
        return await _register(request, ctx, status=status, error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/riskler", status_code=303)


@router.get("/o/{slug}/takvim")
async def calendar(request: Request, ctx: InOrg) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/calendar.html",
        ctx=ctx,
        section="calendar",
        items=await svc.register.calendar(ctx.scope),
    )


@router.get("/o/{slug}/ozet")
async def digest(request: Request, ctx: InOrg) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/digest.html",
        ctx=ctx,
        section="digest",
        digest=await svc.digest.weekly(ctx.scope),
    )
