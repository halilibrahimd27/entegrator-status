"""Alert settings: channels, recipients, rules and the delivery log of an organization."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from sentinel.alerts.service import KINDS, AlertError
from sentinel.models import ChannelKind
from sentinel.risk.impact import LEVELS
from sentinel.web.org_context import InOrg, OrgContext
from sentinel.web.services import client_ip, services
from sentinel.web.session_auth import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])

Short = Annotated[str, Form(max_length=200)]
Url = Annotated[str, Form(max_length=2000)]
Flag = Annotated[str, Form(max_length=8)]
NOTICES = {"test": "alerts.ui.test_queued", "invited": "alerts.ui.invited"}


def _status(exc: AlertError) -> int:
    return 404 if exc.key == "not_found" else 400


def _on(value: str) -> bool:
    return value == "true"


async def _page(
    request: Request,
    ctx: OrgContext,
    *,
    status: int = 200,
    error: str | None = None,
    notice: str | None = None,
    shown_once: str | None = None,
    shown_kind: str | None = None,
) -> Response:
    svc = services(request)
    return svc.renderer.page(
        request,
        "org/alerts.html",
        status=status,
        error=error,
        notice=notice,
        ctx=ctx,
        section="alerts",
        alerts=await svc.alerts.overview(ctx.scope),
        kinds=[k.value for k in ChannelKind],
        subject_kinds=KINDS,
        levels=LEVELS,
        shown_once=shown_once,
        shown_kind=shown_kind,
        headers={"Cache-Control": "no-store"} if shown_once else None,
    )


def _back(ctx: OrgContext) -> Response:
    return RedirectResponse(f"/o/{ctx.org.slug}/uyarilar", status_code=303)


def _uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        raise AlertError("not_found") from None


@router.get("/o/{slug}/uyarilar")
async def alerts(request: Request, ctx: InOrg) -> Response:
    return await _page(request, ctx, notice=NOTICES.get(request.query_params.get("ok", "")))


@router.post("/o/{slug}/uyarilar/kanal")
async def create_channel(
    request: Request,
    ctx: InOrg,
    name: Short,
    kind: Short,
    url: Url = "",
    quiet_start: Short = "",
    quiet_end: Short = "",
    quiet_bypass_high: Flag = "",
    daily: Flag = "",
    weekly_digest: Flag = "",
) -> Response:
    try:
        _, signing = await services(request).alerts.create_channel(
            ctx.scope,
            name=name,
            kind=kind,
            url=url,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            quiet_bypass_high=_on(quiet_bypass_high),
            daily=_on(daily),
            weekly_digest=_on(weekly_digest),
            ip=client_ip(request),
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    if signing:
        return await _page(request, ctx, shown_once=signing, shown_kind="signing")
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}")
async def update_channel(
    request: Request,
    ctx: InOrg,
    channel_id: uuid.UUID,
    quiet_start: Short = "",
    quiet_end: Short = "",
    quiet_bypass_high: Flag = "",
    daily: Flag = "",
    weekly_digest: Flag = "",
    enabled: Flag = "",
) -> Response:
    try:
        await services(request).alerts.update_channel(
            ctx.scope,
            channel_id,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            quiet_bypass_high=_on(quiet_bypass_high),
            daily=_on(daily),
            weekly_digest=_on(weekly_digest),
            enabled=_on(enabled),
            ip=client_ip(request),
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/adres")
async def replace_url(request: Request, ctx: InOrg, channel_id: uuid.UUID, url: Url) -> Response:
    try:
        await services(request).alerts.replace_url(
            ctx.scope, channel_id, url, ip=client_ip(request)
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/imza")
async def rotate(request: Request, ctx: InOrg, channel_id: uuid.UUID) -> Response:
    try:
        signing = await services(request).alerts.rotate_signing_secret(
            ctx.scope, channel_id, ip=client_ip(request)
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return await _page(request, ctx, shown_once=signing, shown_kind="signing")


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/sil")
async def delete_channel(request: Request, ctx: InOrg, channel_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.delete_channel(ctx.scope, channel_id, ip=client_ip(request))
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/test")
async def send_test(request: Request, ctx: InOrg, channel_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.send_test(ctx.scope, channel_id)
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyarilar?ok=test", status_code=303)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/alici")
async def add_email(request: Request, ctx: InOrg, channel_id: uuid.UUID, email: Short) -> Response:
    try:
        await services(request).alerts.add_email(
            ctx.scope, channel_id, email, ip=client_ip(request)
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyarilar?ok=invited", status_code=303)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/ben")
async def subscribe_me(request: Request, ctx: InOrg, channel_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.subscribe_me(ctx.scope, channel_id)
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kanal/{channel_id}/telegram")
async def telegram_link(request: Request, ctx: InOrg, channel_id: uuid.UUID) -> Response:
    try:
        link = await services(request).alerts.telegram_link(ctx.scope, channel_id)
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return await _page(request, ctx, shown_once=link, shown_kind="telegram")


@router.post("/o/{slug}/uyarilar/alici/{recipient_id}/sil")
async def remove_recipient(request: Request, ctx: InOrg, recipient_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.remove_recipient(
            ctx.scope, recipient_id, ip=client_ip(request)
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/alici/{recipient_id}/yeniden")
async def resend(request: Request, ctx: InOrg, recipient_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.resend_confirmation(ctx.scope, recipient_id)
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return RedirectResponse(f"/o/{ctx.org.slug}/uyarilar?ok=invited", status_code=303)


@router.post("/o/{slug}/uyarilar/kural")
async def create_rule(
    request: Request,
    ctx: InOrg,
    name: Short,
    channel_id: Short,
    min_severity: Short = "medium",
    product_id: Short = "",
    service_id: Short = "",
    include_non_breaking: Flag = "",
) -> Response:
    form = await request.form()
    kinds = [str(k) for k in form.getlist("kinds")][: len(KINDS)]
    try:
        channel = _uuid(channel_id)
        if channel is None:
            raise AlertError("not_found")
        await services(request).alerts.create_rule(
            ctx.scope,
            name=name,
            channel_id=channel,
            kinds=kinds,
            min_severity=min_severity,
            product_id=_uuid(product_id),
            service_id=_uuid(service_id),
            include_non_breaking=_on(include_non_breaking),
            ip=client_ip(request),
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kural/{rule_id}")
async def toggle_rule(
    request: Request, ctx: InOrg, rule_id: uuid.UUID, enabled: Flag = ""
) -> Response:
    try:
        await services(request).alerts.set_rule_enabled(
            ctx.scope, rule_id, _on(enabled), ip=client_ip(request)
        )
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)


@router.post("/o/{slug}/uyarilar/kural/{rule_id}/sil")
async def delete_rule(request: Request, ctx: InOrg, rule_id: uuid.UUID) -> Response:
    try:
        await services(request).alerts.delete_rule(ctx.scope, rule_id, ip=client_ip(request))
    except AlertError as exc:
        return await _page(request, ctx, status=_status(exc), error=exc.key)
    return _back(ctx)
