"""``/metrics`` in Prometheus text format: aggregate counts only, never per tenant.

Off unless METRICS_TOKEN is set, and then only with that bearer token. In the
production stack Caddy also answers 404 for it, so it is reachable only from the
internal network.
"""

from __future__ import annotations

import hmac
from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from starlette.responses import PlainTextResponse, Response

from sentinel.db.base import utcnow
from sentinel.models import (
    AlertDelivery,
    CorrectionRequest,
    CorrectionStatus,
    Incident,
    Organization,
    ProbeNode,
    Source,
)
from sentinel.web.services import services

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    svc = services(request)
    token = svc.settings.metrics_token
    sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token is None or not hmac.compare_digest(sent.encode(), token.get_secret_value().encode()):
        return Response(status_code=404)
    since = utcnow() - timedelta(hours=24)
    async with svc.db.system("metrics: platform-wide counts") as s:
        orgs = await s.scalar(
            select(func.count()).select_from(Organization).where(Organization.deleted_at.is_(None))
        )
        sources = dict(
            (
                await s.execute(
                    select(Source.org_id.is_(None), func.count()).group_by(Source.org_id.is_(None))
                )
            ).all()
        )
        open_incidents = await s.scalar(
            select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None))
        )
        deliveries = dict(
            (
                await s.execute(
                    select(AlertDelivery.status, func.count())
                    .where(AlertDelivery.created_at > since)
                    .group_by(AlertDelivery.status)
                )
            ).all()
        )
        nodes = await s.scalar(select(func.count()).select_from(ProbeNode))
        corrections = await s.scalar(
            select(func.count())
            .select_from(CorrectionRequest)
            .where(CorrectionRequest.status == CorrectionStatus.OPEN)
        )
    lines = [
        "# HELP entegrator_organizations Organizations that are not closed.",
        "# TYPE entegrator_organizations gauge",
        f"entegrator_organizations {orgs or 0}",
        "# HELP entegrator_sources Sources by owner (shared catalog or an organization).",
        "# TYPE entegrator_sources gauge",
        f'entegrator_sources{{owner="shared"}} {sources.get(True, 0)}',
        f'entegrator_sources{{owner="organization"}} {sources.get(False, 0)}',
        "# HELP entegrator_open_incidents Incidents not resolved yet.",
        "# TYPE entegrator_open_incidents gauge",
        f"entegrator_open_incidents {open_incidents or 0}",
        "# HELP entegrator_alert_deliveries Alert deliveries created in the last 24 hours.",
        "# TYPE entegrator_alert_deliveries gauge",
        *(
            f'entegrator_alert_deliveries{{status="{status}"}} {deliveries.get(status, 0)}'
            for status in ("queued", "sent", "failed", "skipped")
        ),
        "# HELP entegrator_probe_nodes Registered probe nodes.",
        "# TYPE entegrator_probe_nodes gauge",
        f"entegrator_probe_nodes {nodes or 0}",
        "# HELP entegrator_open_corrections Correction requests waiting for a decision.",
        "# TYPE entegrator_open_corrections gauge",
        f"entegrator_open_corrections {corrections or 0}",
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )
