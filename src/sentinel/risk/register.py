"""The organization's risk register and its calendar of planned changes.

The register lists every open risk that reaches one of the organization's products,
soonest first, with the organization's own decision about it: ``open`` (no decision),
``snoozed`` until a date (then open again), ``accepted`` with a note, ``resolved`` on
the organization's side. Decisions are per organization, also for shared risks, and
audit-logged. The calendar lists what is planned in the next months: effective dates of
announcements, risk due dates and maintenance windows on the components in use.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from sentinel import audit
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    Change,
    Component,
    MaintenanceWindow,
    Product,
    Risk,
    RiskDecision,
    RiskState,
)
from sentinel.risk.impact import LEVELS, ImpactViews
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

SNOOZE_MAX = timedelta(days=180)
CALENDAR_DAYS = 180
STATE_ORDER = {"open": 0, "snoozed": 1, "accepted": 2, "resolved": 3}


class RegisterError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class RegisterEntry:
    risk: Risk
    severity: str
    component: str
    products: list[str]
    state: str = "open"
    until: datetime | None = None
    note: str | None = None


@dataclass(frozen=True)
class CalendarItem:
    when: datetime
    kind: str  # change | risk | maintenance
    title: str
    component: str
    link: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class RiskRegister:
    def __init__(
        self, db: Database, impact: ImpactViews, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._impact = impact
        self._clock = clock

    async def entries(self, scope: TenantScope) -> list[RegisterEntry]:
        scope.require(Action.GRAPH_VIEW)
        now = self._clock()
        by_product = await self._impact.for_org(scope)
        async with self._db.tenant(scope) as s:
            names = {p.id: p.name for p in (await s.scalars(select(Product))).all()}
            found: dict[uuid.UUID, dict[str, Any]] = {}
            for product_id, impacts in by_product.items():
                for impact in impacts:
                    if impact.kind != "risk":
                        continue
                    entry = found.setdefault(
                        impact.subject_id,
                        {"level": impact.level, "component": impact.path[-1], "products": set()},
                    )
                    entry["level"] = max(entry["level"], impact.level)
                    entry["products"].add(names.get(product_id, ""))
            ids = list(found)
            risks = {r.id: r for r in (await s.scalars(select(Risk).where(Risk.id.in_(ids)))).all()}
            states = {
                st.risk_id: st
                for st in (
                    await s.scalars(select(RiskState).where(RiskState.risk_id.in_(ids)))
                ).all()
            }
        entries = []
        for risk_id, data in found.items():
            state = states.get(risk_id)
            decision = state.state if state else "open"
            if decision == "snoozed" and state is not None and state.until and state.until <= now:
                decision = "open"  # the snooze has run out
            entries.append(
                RegisterEntry(
                    risks[risk_id],
                    LEVELS[data["level"]],
                    data["component"],
                    sorted(data["products"]),
                    decision,
                    state.until if state else None,
                    state.note if state else None,
                )
            )
        far = now + timedelta(days=36500)
        entries.sort(
            key=lambda e: (STATE_ORDER[e.state], e.risk.due_at or far, -LEVELS.index(e.severity))
        )
        return entries

    async def decide(
        self,
        scope: TenantScope,
        risk_id: uuid.UUID,
        decision: str,
        *,
        until: datetime | None = None,
        note: str = "",
        ip: str = "",
    ) -> None:
        scope.require(Action.STATUS_MANAGE)
        now = self._clock()
        note = " ".join(note.split())
        if decision not in {*STATE_ORDER}:
            raise RegisterError("register.invalid_decision")
        if decision == "snoozed" and (until is None or not now < until <= now + SNOOZE_MAX):
            raise RegisterError("register.invalid_until")
        if decision == "accepted" and not 1 <= len(note) <= 500:
            raise RegisterError("register.note_required")
        if len(note) > 500:
            raise RegisterError("register.note_required")
        async with self._db.tenant(scope) as s:
            if await s.get(Risk, risk_id) is None:
                raise RegisterError("not_found")
            await s.execute(delete(RiskState).where(RiskState.risk_id == risk_id))
            if decision != "open":
                s.add(
                    RiskState(
                        org_id=scope.org_id,
                        risk_id=risk_id,
                        state=RiskDecision(decision),
                        until=until if decision == "snoozed" else None,
                        note=note or None,
                        updated_by=scope.user_id,
                    )
                )
            audit.record(
                s,
                f"risk.{'reopened' if decision == 'open' else decision}",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="risk",
                target_id=risk_id,
                ip=ip,
            )

    async def calendar(self, scope: TenantScope, days: int = CALENDAR_DAYS) -> list[CalendarItem]:
        scope.require(Action.GRAPH_VIEW)
        now = self._clock()
        horizon = now + timedelta(days=days)
        by_product = await self._impact.for_org(scope)
        labels: dict[uuid.UUID, str] = {}
        for impacts in by_product.values():
            for impact in impacts:
                labels.setdefault(impact.component_id, impact.path[-1])
        async with self._db.tenant(scope) as s:
            products = (await s.scalars(select(Product.id))).all()
            for product_id in products:
                graph = await self._impact.graph.downstream(scope, product_id)
                for node in graph.nodes.values():
                    labels.setdefault(node.component_id, f"{node.service} — {node.component}")
            ids = list(labels)
            owners = {
                c.id: c.org_id
                for c in (await s.scalars(select(Component).where(Component.id.in_(ids)))).all()
            }
            decided = set(
                (
                    await s.scalars(
                        select(RiskState.risk_id).where(
                            RiskState.state.in_([RiskDecision.ACCEPTED, RiskDecision.RESOLVED])
                        )
                    )
                ).all()
            )
            changes = (
                await s.scalars(
                    select(Change).where(
                        Change.component_id.in_(ids),
                        Change.effective_at >= now,
                        Change.effective_at <= horizon,
                    )
                )
            ).all()
            risks = (
                await s.scalars(
                    select(Risk).where(
                        Risk.component_id.in_(ids),
                        Risk.resolved_at.is_(None),
                        Risk.due_at.is_not(None),
                        Risk.due_at <= horizon,
                    )
                )
            ).all()
            windows = (
                await s.scalars(
                    select(MaintenanceWindow).where(
                        MaintenanceWindow.component_id.in_(ids),
                        MaintenanceWindow.ends_at >= now,
                        MaintenanceWindow.starts_at <= horizon,
                    )
                )
            ).all()
        items = [
            CalendarItem(
                c.effective_at,
                "change",
                c.title,
                labels[c.component_id],
                c.link,
                {"category": c.category},
            )
            for c in changes
            if c.effective_at is not None
        ]
        items += [
            CalendarItem(r.due_at, "risk", r.kind, labels[r.component_id], r.link)
            for r in risks
            if r.due_at is not None and r.id not in decided
        ]
        items += [
            CalendarItem(
                w.starts_at,
                "maintenance",
                w.title,
                labels[w.component_id],
                detail={"ends_at": w.ends_at.isoformat()},
            )
            for w in windows
            if w.org_id == owners.get(w.component_id)  # only the owner's windows count
        ]
        items.sort(key=lambda i: i.when)
        return items
