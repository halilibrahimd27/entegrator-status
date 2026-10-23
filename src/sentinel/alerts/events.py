"""New upstream subjects → one alert event per affected product → deliveries.

Runs in the engine. Subjects are auto incidents (opened, resolved), changes and risks
created since the last run (with a small overlap; events are unique per product,
subject and phase, so the overlap never sends twice). Severity follows the impact rules
of Phase 8 (``sentinel.risk.impact``), evaluated at the time of the event. Rules are
read only for the event's own organization, so nothing routes one organization's events
to another. On its very first run the job only sets its cursor: switching alerts on
must not replay history.

The organization's own incident updates go to the subscribers of its public status page
(``AlertChannel.public``) when the product is on the page; they never pass through rules.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.alerts.rules import matches
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.graph.service import MAX_DEPTH
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    AlertRule,
    Change,
    ChannelKind,
    Component,
    Dependency,
    Incident,
    IncidentKind,
    IncidentUpdate,
    JobCursor,
    Organization,
    Product,
    Risk,
    Service,
    StatusPage,
)
from sentinel.risk.impact import LEVELS, applies, attenuate, change_rule, risk_rule

CURSOR = "alerts.events"
OVERLAP = timedelta(minutes=5)


@dataclass(frozen=True)
class Reach:
    product_id: uuid.UUID
    org_id: uuid.UUID
    depth: int
    path: tuple[uuid.UUID, ...]  # from the product's dependency down to the subject
    usage: list[str] | None
    dependency_id: uuid.UUID


@dataclass
class EventReport:
    events: int = 0
    deliveries: int = 0
    delivery_ids: list[uuid.UUID] | None = None


async def reaches(s: AsyncSession, component_id: uuid.UUID) -> list[Reach]:
    """Every product that depends on ``component_id`` within three hops (all tenants)."""
    best: dict[uuid.UUID, Reach] = {}
    queue: deque[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = deque([(component_id, (component_id,))])
    visited = {component_id}
    while queue:
        node, path = queue.popleft()
        for edge in (
            await s.scalars(select(Dependency).where(Dependency.to_component_id == node))
        ).all():
            if edge.from_product_id is not None and edge.org_id is not None:
                found = Reach(
                    edge.from_product_id,
                    edge.org_id,
                    len(path),
                    tuple(reversed(path)),
                    (edge.usage or {}).get("operations"),
                    edge.id,
                )
                if found.product_id not in best or found.depth < best[found.product_id].depth:
                    best[found.product_id] = found
            elif (
                edge.from_component_id is not None
                and edge.from_component_id not in visited
                and len(path) < MAX_DEPTH
            ):
                visited.add(edge.from_component_id)
                queue.append((edge.from_component_id, (*path, edge.from_component_id)))
    return list(best.values())


async def _labels(s: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[str, uuid.UUID]]:
    rows = await s.execute(
        select(Component.id, Component.name, Service.name, Service.id)
        .join(Service, Service.id == Component.service_id)
        .where(Component.id.in_(ids))
    )
    return {cid: (f"{service} — {name}", service_id) for cid, name, service, service_id in rows}


def _subject_rows(
    kind: str, subject: Any, phase: str, reach: Reach, now: datetime
) -> tuple[int, str, str, datetime, str | None, dict[str, Any]] | None:
    """(level, reason, title, occurred_at, link, detail) or None when it does not alert."""
    depth = reach.depth
    if kind == "incident":
        reason = "upstream_down" if phase == "opened" else "upstream_recovered"
        when = subject.started_at if phase == "opened" else subject.resolved_at
        # titled by the component's name (filled in by the caller); the cause is in the detail
        return (
            attenuate(2, depth),
            reason,
            "",
            when,
            None,
            {"cause": subject.cause},
        )
    if kind == "change":
        rule = change_rule(subject, depth, reach.usage if depth == 1 else None)
        if rule is None:
            return None
        level, reason = rule
        detail = {"kind": subject.kind, "category": subject.category}
        return (
            attenuate(level, depth),
            reason,
            subject.title,
            subject.detected_at,
            subject.link,
            detail,
        )
    if not applies(subject, {str(reach.dependency_id)}):
        return None
    level, reason = risk_rule(subject, now)
    detail = {
        "kind": subject.kind,
        "due_at": subject.due_at.isoformat() if subject.due_at else None,
    }
    return (
        attenuate(level, depth),
        reason,
        subject.kind,
        subject.first_seen_at,
        subject.link,
        detail,
    )


async def run_events(db: Database, clock: Callable[[], datetime] = utcnow) -> EventReport:
    now = clock()
    report = EventReport(delivery_ids=[])
    async with db.system("alerts: events for every organization's products") as s:
        cursor = await s.get(JobCursor, CURSOR)
        if cursor is None:
            s.add(JobCursor(name=CURSOR, position=now))  # first run: no history replay
            return report
        since = cursor.position - OVERLAP
        subjects: list[tuple[str, Any, str]] = []
        for incident in (
            await s.scalars(
                select(Incident).where(
                    Incident.kind == IncidentKind.AUTO,
                    or_(Incident.created_at > since, Incident.resolved_at > since),
                )
            )
        ).all():
            if incident.created_at > since:
                subjects.append(("incident", incident, "opened"))
            if incident.resolved_at is not None and incident.resolved_at > since:
                subjects.append(("incident", incident, "resolved"))
        subjects += [
            ("change", c, "new")
            for c in (await s.scalars(select(Change).where(Change.created_at > since))).all()
        ]
        subjects += [
            ("risk", r, "new")
            for r in (await s.scalars(select(Risk).where(Risk.created_at > since))).all()
        ]

        rows: list[dict[str, Any]] = []
        for kind, subject, phase in subjects:
            if subject.component_id is None:  # pragma: no cover - auto incidents have one
                continue
            found = await reaches(s, subject.component_id)
            labels = await _labels(s, {c for r in found for c in r.path})
            names = {
                p.id: p.name
                for p in (
                    await s.scalars(
                        select(Product).where(Product.id.in_([r.product_id for r in found]))
                    )
                ).all()
            }
            for reach in found:
                built = _subject_rows(kind, subject, phase, reach, now)
                if built is None:
                    continue
                level, reason, title, when, link, detail = built
                hops = [labels[c][0] for c in reach.path if c in labels]
                rows.append(
                    {
                        "org_id": reach.org_id,
                        "product_id": reach.product_id,
                        "subject_type": kind,
                        "subject_id": subject.id,
                        "phase": phase,
                        "severity": LEVELS[level],
                        "reason": reason,
                        "title": (title or (hops[-1] if hops else ""))[:300],
                        "link": link,
                        "path": [names.get(reach.product_id, ""), *hops],
                        "detail": {
                            **detail,
                            "depth": reach.depth,
                            "service_ids": sorted(
                                {str(labels[c][1]) for c in reach.path if c in labels}
                            ),
                        },
                        "occurred_at": when or now,
                    }
                )
        rows += await _notice_rows(s, since)
        created: list[AlertEvent] = []
        if rows:
            ids = (
                await s.scalars(
                    insert(AlertEvent)
                    .values(rows)
                    .on_conflict_do_nothing()
                    .returning(AlertEvent.id)
                )
            ).all()
            created = list(
                (await s.scalars(select(AlertEvent).where(AlertEvent.id.in_(ids)))).all()
            )
        report.events = len(created)
        deliveries = await _deliveries(s, [e for e in created if e.subject_type != "notice"], now)
        deliveries += await _public_deliveries(
            s, [e for e in created if e.subject_type == "notice"], now
        )
        report.deliveries = len(deliveries)
        report.delivery_ids = deliveries
        cursor.position = now
    return report


async def _deliveries(s: AsyncSession, events: list[AlertEvent], now: datetime) -> list[uuid.UUID]:
    if not events:
        return []
    orgs = {e.org_id for e in events}
    rules = (
        await s.scalars(
            select(AlertRule).where(AlertRule.org_id.in_(orgs), AlertRule.enabled.is_(True))
        )
    ).all()
    channels = {
        c.id: c
        for c in (
            await s.scalars(
                select(AlertChannel).where(
                    AlertChannel.org_id.in_(orgs),
                    AlertChannel.enabled.is_(True),
                    AlertChannel.public.is_(False),  # page subscribers never get rule events
                )
            )
        ).all()
    }
    recipients: dict[uuid.UUID, list[str]] = {}
    for recipient in (
        await s.scalars(
            select(AlertRecipient).where(
                AlertRecipient.channel_id.in_(list(channels)),
                AlertRecipient.confirmed_at.is_not(None),
                AlertRecipient.unsubscribed_at.is_(None),
            )
        )
    ).all():
        recipients.setdefault(recipient.channel_id, []).append(str(recipient.id))
    rows: list[dict[str, Any]] = []
    for event in events:
        for rule in rules:
            channel = channels.get(rule.channel_id)
            # the rule and its channel belong to the event's own organization, never another's
            if channel is None or rule.org_id != event.org_id or channel.org_id != event.org_id:
                continue
            if not matches(rule, event):
                continue
            per_recipient = channel.kind in (ChannelKind.EMAIL, ChannelKind.TELEGRAM)
            for key in recipients.get(channel.id, []) if per_recipient else [""]:
                rows.append(
                    {
                        "org_id": event.org_id,
                        "event_id": event.id,
                        "channel_id": channel.id,
                        "recipient_key": key,
                        "next_attempt_at": now,
                    }
                )
    if not rows:
        return []
    # several rules may route the same event to the same channel: still one message
    return list(
        (
            await s.scalars(
                insert(AlertDelivery)
                .values(rows)
                .on_conflict_do_nothing()
                .returning(AlertDelivery.id)
            )
        ).all()
    )


async def _notice_rows(s: AsyncSession, since: datetime) -> list[dict[str, Any]]:
    """Own incident updates on products shown on a public page with subscriptions on."""
    rows: list[dict[str, Any]] = []
    found = (
        await s.execute(
            select(IncidentUpdate, Incident, Product, Organization)
            .join(Incident, Incident.id == IncidentUpdate.incident_id)
            .join(Product, Product.id == Incident.product_id)
            .join(Organization, Organization.id == Product.org_id)
            .where(IncidentUpdate.created_at > since, Incident.kind == IncidentKind.MANUAL)
        )
    ).all()
    pages: dict[uuid.UUID, StatusPage | None] = {}
    for update, incident, product, org in found:
        if org.id not in pages:
            pages[org.id] = await s.scalar(
                select(StatusPage).where(
                    StatusPage.org_id == org.id,
                    StatusPage.enabled.is_(True),
                    StatusPage.subscriptions.is_(True),
                )
            )
        page = pages[org.id]
        if page is None or str(product.id) not in page.product_ids:
            continue
        rows.append(
            {
                "org_id": org.id,
                "product_id": product.id,
                "subject_type": "notice",
                "subject_id": update.id,
                "phase": "new",
                "severity": "medium",
                "reason": "notice",
                "title": (incident.title or "")[:300],
                "link": None,
                "path": [product.name],
                "detail": {"status": update.status, "body": update.body[:2000], "slug": org.slug},
                "occurred_at": update.created_at,
            }
        )
    return rows


async def _public_deliveries(
    s: AsyncSession, events: list[AlertEvent], now: datetime
) -> list[uuid.UUID]:
    if not events:
        return []
    channels = {
        c.org_id: c
        for c in (
            await s.scalars(
                select(AlertChannel).where(
                    AlertChannel.org_id.in_({e.org_id for e in events}),
                    AlertChannel.public.is_(True),
                    AlertChannel.enabled.is_(True),
                )
            )
        ).all()
    }
    rows: list[dict[str, Any]] = []
    for event in events:
        channel = channels.get(event.org_id)
        if channel is None:
            continue
        for recipient_id in (
            await s.scalars(
                select(AlertRecipient.id).where(
                    AlertRecipient.channel_id == channel.id,
                    AlertRecipient.confirmed_at.is_not(None),
                    AlertRecipient.unsubscribed_at.is_(None),
                )
            )
        ).all():
            rows.append(
                {
                    "org_id": event.org_id,
                    "event_id": event.id,
                    "channel_id": channel.id,
                    "recipient_key": str(recipient_id),
                    "next_attempt_at": now,
                }
            )
    if not rows:
        return []
    return list(
        (
            await s.scalars(
                insert(AlertDelivery)
                .values(rows)
                .on_conflict_do_nothing()
                .returning(AlertDelivery.id)
            )
        ).all()
    )
