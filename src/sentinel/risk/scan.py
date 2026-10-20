"""Open, update and resolve risks from the latest result of each source.

- ``http``: a ``Sunset`` header opens a ``sunset`` risk due on that date; a
  ``Deprecation`` header opens a ``deprecation`` risk (dated when the header has one).
- ``tls_cert`` / ``domain_expiry``: inside the warning window (``expiring_soon``) a risk
  is open, due on the expiry date.

A risk is resolved when the latest result no longer shows it (header gone, certificate
renewed). For ``http`` only successful results count, so an outage does not resolve a
sunset; an expired certificate fails its check but still carries its dates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import distinct_on

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Observation, Risk, RiskKind, Source
from sentinel.risk.headers import deprecation, explanation, sunset

LOOKBACK = timedelta(hours=1)
KINDS_BY_SOURCE: dict[str, frozenset[RiskKind]] = {
    "http": frozenset({RiskKind.SUNSET, RiskKind.DEPRECATION}),
    "tls_cert": frozenset({RiskKind.CERT_EXPIRY}),
    "domain_expiry": frozenset({RiskKind.DOMAIN_EXPIRY}),
}


@dataclass(frozen=True)
class Finding:
    kind: RiskKind
    due_at: datetime | None
    link: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskReport:
    opened: int = 0
    updated: int = 0
    resolved: int = 0


def _iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def findings(kind: str, details: dict[str, Any], base: str) -> list[Finding]:
    """What one result says about risks. Pure."""
    if kind == "http":
        found = []
        link = explanation(details.get("link"), base)
        if due := sunset(details.get("sunset")):
            found.append(
                Finding(RiskKind.SUNSET, due, link, {"header": str(details["sunset"])[:100]})
            )
        deprecated, since = deprecation(details.get("deprecation"))
        if deprecated:
            header = str(details["deprecation"])[:100]
            found.append(Finding(RiskKind.DEPRECATION, since, link, {"header": header}))
        return found
    if kind in ("tls_cert", "domain_expiry") and details.get("expiring_soon"):
        risk = RiskKind.CERT_EXPIRY if kind == "tls_cert" else RiskKind.DOMAIN_EXPIRY
        due = _iso(details.get("not_after") or details.get("expires_at"))
        detail: dict[str, Any] = {k: details[k] for k in ("days_left", "issuer") if k in details}
        return [Finding(risk, due, None, detail)]
    return []


async def run_risks(db: Database, clock: Callable[[], datetime] = utcnow) -> RiskReport:
    now = clock()
    report = RiskReport()
    async with db.system("risks: latest results of every source") as s:
        rows = (
            await s.execute(
                select(Observation.details, Observation.observed_at, Source)
                .join(Source, Source.id == Observation.source_id)
                .ext(distinct_on(Observation.source_id))
                .where(
                    Observation.observed_at > now - LOOKBACK,
                    Observation.node_healthy.is_(True),
                    Source.kind.in_(list(KINDS_BY_SOURCE)),
                    or_(Observation.ok.is_(True), Observation.details.has_key("days_left")),
                )
                .order_by(Observation.source_id, Observation.observed_at.desc())
            )
        ).all()
        sources = [source.id for _, _, source in rows]
        open_risks = {
            (risk.source_id, risk.kind): risk
            for risk in (
                await s.scalars(
                    select(Risk).where(
                        Risk.source_id.in_(sources),
                        Risk.resolved_at.is_(None),
                        Risk.subject == "",  # source-level risks; others have their own scans
                    )
                )
            ).all()
        }
        for details, observed_at, source in rows:
            base = str(source.config.get("url") or "https://invalid.invalid/")
            found = {f.kind: f for f in findings(source.kind, details or {}, base)}
            for kind in KINDS_BY_SOURCE[source.kind]:
                current = open_risks.get((source.id, kind.value))
                finding = found.get(kind)
                if finding and current:
                    if (current.due_at, current.link) != (finding.due_at, finding.link):
                        current.due_at, current.link = finding.due_at, finding.link
                        report.updated += 1
                    current.detail = finding.detail
                    current.last_seen_at = observed_at
                elif finding:
                    s.add(
                        Risk(
                            org_id=source.org_id,
                            source_id=source.id,
                            component_id=source.component_id,
                            kind=kind,
                            due_at=finding.due_at,
                            link=finding.link,
                            detail=finding.detail,
                            first_seen_at=observed_at,
                            last_seen_at=observed_at,
                        )
                    )
                    report.opened += 1
                elif current:
                    current.resolved_at = observed_at
                    report.resolved += 1
    return report
