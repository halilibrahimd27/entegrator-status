"""Public status pages: settings, the public view and e-mail subscriptions.

A page is off until an admin switches it on, and shows only the products they pick.
Each product's state comes first from the organization's own incidents; second, when
a component the product uses directly is down by our measurements, the page says so
with a disclaimer (third-party measurement, not the vendor's status). Nothing deeper
than the first hop, no changes and no risks: those are for the organization, not for
its customers.

Subscribers are recipients of a hidden e-mail channel (``AlertChannel.public``): double
opt-in, one-click unsubscribe, retries and retention all come from the alert system.
They get the organization's own incident updates for the page's products, nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from sentinel import audit
from sentinel.accounts.throttle import Limit, Throttle
from sentinel.alerts.subscriptions import SubscriptionService
from sentinel.catalog.service import CatalogService
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.notices import IncidentView, NoticeService
from sentinel.engine.views import ComponentStatus, DayBar, StatusViews
from sentinel.models import (
    AlertChannel,
    AlertRecipient,
    ChannelKind,
    Incident,
    IncidentKind,
    Organization,
    Product,
    StatusPage,
)
from sentinel.tenancy.roles import Action, Role
from sentinel.tenancy.scope import TenantScope

PUBLIC_CHANNEL = "Durum sayfası aboneleri"
MAX_SUBSCRIBERS = 5000
MAX_PRODUCTS = 50
RECENT = timedelta(days=30)
SUBSCRIBE_PER_IP = Limit("public-sub-ip", 5, 3600)
SUBSCRIBE_PER_ADDRESS = Limit("public-sub-addr", 3, 86400)
STATES = ("incident", "upstream", "operational")  # worst first


class PublicError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class PageSettings:
    enabled: bool
    title: str
    intro: str
    product_ids: list[uuid.UUID]
    show_dependencies: bool
    subscriptions: bool
    subscribers: int
    products: list[tuple[uuid.UUID, str]]


@dataclass(frozen=True)
class PublicDependency:
    name: str
    status: ComponentStatus
    uptime: float | None
    bars: list[DayBar]


@dataclass(frozen=True)
class PublicProduct:
    id: uuid.UUID
    name: str
    state: str
    incident_status: str | None
    dependencies: list[PublicDependency] = field(default_factory=list)


@dataclass(frozen=True)
class PublicIncident:
    product: str
    view: IncidentView


@dataclass
class PublicPage:
    slug: str
    org_name: str
    title: str
    intro: str
    subscriptions: bool
    generated_at: datetime
    products: list[PublicProduct] = field(default_factory=list)
    incidents: list[PublicIncident] = field(default_factory=list)

    @property
    def state(self) -> str:
        found = {p.state for p in self.products}
        return next((s for s in STATES if s in found), "operational")


def _email(raw: str) -> str:
    address = raw.strip().lower()
    if not (3 <= len(address) <= 320 and address.count("@") == 1) or any(
        c in address for c in " \r\n<>,;\"'"
    ):
        raise PublicError("public.invalid_email")
    local, _, domain = address.partition("@")
    if not local or "." not in domain:
        raise PublicError("public.invalid_email")
    return address


class StatusPageService:
    def __init__(
        self,
        db: Database,
        catalog: CatalogService,
        status: StatusViews,
        notices: NoticeService,
        subscriptions: SubscriptionService,
        throttle: Throttle,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._catalog = catalog
        self._status = status
        self._notices = notices
        self._subscriptions = subscriptions
        self._throttle = throttle
        self._clock = clock

    # -- settings (admins) ---------------------------------------------------------------

    async def settings(self, scope: TenantScope) -> PageSettings:
        scope.require(Action.ORG_UPDATE)
        async with self._db.tenant(scope) as s:
            page = await s.scalar(select(StatusPage))
            org = await s.get(Organization, scope.org_id)
            products = (
                await s.execute(select(Product.id, Product.name).order_by(Product.name))
            ).all()
            subscribers = await s.scalar(
                select(func.count())
                .select_from(AlertRecipient)
                .join(AlertChannel, AlertChannel.id == AlertRecipient.channel_id)
                .where(
                    AlertChannel.public.is_(True),
                    AlertRecipient.confirmed_at.is_not(None),
                    AlertRecipient.unsubscribed_at.is_(None),
                )
            )
        known = {pid for pid, _ in products}
        return PageSettings(
            enabled=page.enabled if page else False,
            title=page.title if page else (org.name if org else ""),
            intro=(page.intro or "") if page else "",
            product_ids=[
                uuid.UUID(p) for p in (page.product_ids if page else []) if uuid.UUID(p) in known
            ],
            show_dependencies=page.show_dependencies if page else False,
            subscriptions=page.subscriptions if page else True,
            subscribers=subscribers or 0,
            products=[(pid, name) for pid, name in products],
        )

    async def save(
        self,
        scope: TenantScope,
        *,
        enabled: bool,
        title: str,
        intro: str,
        product_ids: list[uuid.UUID],
        show_dependencies: bool,
        subscriptions: bool,
        ip: str = "",
    ) -> None:
        scope.require(Action.ORG_UPDATE)
        title = " ".join(title.split())
        if not 1 <= len(title) <= 120:
            raise PublicError("public.invalid_title")
        intro = intro.strip()
        if len(intro) > 2000:
            raise PublicError("public.invalid_intro")
        if len(product_ids) > MAX_PRODUCTS:
            raise PublicError("public.too_many_products")
        async with self._db.tenant(scope) as s:
            known = set((await s.scalars(select(Product.id))).all())
            if any(p not in known for p in product_ids):
                raise PublicError("not_found")
            if enabled and not product_ids:
                raise PublicError("public.no_products")
            page = await s.scalar(select(StatusPage))
            if page is None:
                page = StatusPage(org_id=scope.org_id, title=title)
                s.add(page)
            page.enabled, page.title, page.intro = enabled, title, intro or None
            page.product_ids = [str(p) for p in dict.fromkeys(product_ids)]
            page.show_dependencies, page.subscriptions = show_dependencies, subscriptions
            page.updated_at = self._clock()
            audit.record(
                s,
                "status_page.updated",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="status_page",
                target_id=scope.org_id,
                details={"enabled": enabled, "products": len(product_ids)},
                ip=ip,
            )

    # -- the public view -----------------------------------------------------------------

    async def _enabled(self, slug: str) -> tuple[Organization, StatusPage] | None:
        async with self._db.system("public status page: look up by slug") as s:
            org = await s.scalar(select(Organization).where(Organization.slug == slug))
            if org is None or org.deleted_at is not None:
                return None
            page = await s.scalar(select(StatusPage).where(StatusPage.org_id == org.id))
        if page is None or not page.enabled:
            return None
        return org, page

    async def page(self, slug: str) -> PublicPage | None:
        found = await self._enabled(slug)
        if found is None:
            return None
        org, page = found
        now = self._clock()
        scope = TenantScope(org_id=org.id, user_id=None, role=Role.VIEWER)
        view = PublicPage(
            slug=org.slug,
            org_name=org.name,
            title=page.title,
            intro=page.intro or "",
            subscriptions=page.subscriptions,
            generated_at=now,
        )
        async with self._db.tenant(scope) as s:
            names = dict((await s.execute(select(Product.id, Product.name))).all())
        for raw in page.product_ids:
            product_id = uuid.UUID(raw)
            if product_id not in names:
                continue  # deleted since the page was saved
            dependencies = await self._catalog.dependencies(scope, product_id)
            components = [c.id for _, c, _ in dependencies]
            current = await self._status.current(scope, components)
            incidents = [
                i
                for i in await self._notices.product_incidents(scope, product_id)
                if i.incident.resolved_at is None or i.incident.resolved_at > now - RECENT
            ]
            open_incident = next((i for i in incidents if i.incident.resolved_at is None), None)
            if open_incident is not None:
                state, incident_status = "incident", open_incident.incident.status
            elif components and await self._upstream_down(scope, components):
                state, incident_status = "upstream", None
            else:
                state, incident_status = "operational", None
            shown: list[PublicDependency] = []
            if page.show_dependencies and components:
                uptime = await self._status.uptime(scope, components)
                bars = await self._status.bars(scope, components)
                shown = [
                    PublicDependency(
                        f"{service.name} — {component.name}",
                        current[component.id],
                        uptime.get(component.id, {}).get("90d"),
                        bars.get(component.id, []),
                    )
                    for _, component, service in dependencies
                    if component.id in current
                ]
            view.products.append(
                PublicProduct(product_id, names[product_id], state, incident_status, shown)
            )
            view.incidents += [PublicIncident(names[product_id], i) for i in incidents]
        view.incidents.sort(
            key=lambda i: (
                i.view.incident.resolved_at is not None,
                -i.view.incident.started_at.timestamp(),
            )
        )
        return view

    async def _upstream_down(self, scope: TenantScope, components: list[uuid.UUID]) -> bool:
        """An open automatic incident on a component the product uses directly."""
        async with self._db.tenant(scope) as s:
            found = await s.scalar(
                select(Incident.id)
                .where(
                    Incident.kind == IncidentKind.AUTO,
                    Incident.resolved_at.is_(None),
                    Incident.component_id.in_(components),
                )
                .limit(1)
            )
        return found is not None

    # -- subscribing ---------------------------------------------------------------------

    async def subscribe(self, slug: str, raw_email: str, ip: str) -> None:
        """Always the same answer, so the form does not reveal who is subscribed."""
        found = await self._enabled(slug)
        if found is None or not found[1].subscriptions:
            raise PublicError("not_found")
        address = _email(raw_email)
        if not await self._throttle.allow(SUBSCRIBE_PER_IP, ip):
            raise PublicError("public.throttled")
        if not await self._throttle.allow(SUBSCRIBE_PER_ADDRESS, f"{slug}:{address}"):
            return  # quietly: no more mail to this address today
        org, _ = found
        async with self._db.system("public status page: add one subscriber") as s:
            channel = await s.scalar(
                select(AlertChannel).where(
                    AlertChannel.org_id == org.id, AlertChannel.public.is_(True)
                )
            )
            if channel is None:
                channel = AlertChannel(
                    org_id=org.id, name=PUBLIC_CHANNEL, kind=ChannelKind.EMAIL, public=True
                )
                s.add(channel)
                await s.flush()
            recipient = await s.scalar(
                select(AlertRecipient).where(
                    AlertRecipient.channel_id == channel.id, AlertRecipient.address == address
                )
            )
            if recipient is not None and recipient.confirmed_at and not recipient.unsubscribed_at:
                return  # already subscribed: nothing to send
            if recipient is None:
                total = await s.scalar(
                    select(func.count())
                    .select_from(AlertRecipient)
                    .where(AlertRecipient.channel_id == channel.id)
                )
                if (total or 0) >= MAX_SUBSCRIBERS:
                    raise PublicError("public.full")
                recipient = AlertRecipient(org_id=org.id, channel_id=channel.id, address=address)
                s.add(recipient)
                await s.flush()
            else:  # unsubscribed or never confirmed: start over
                recipient.confirmed_at, recipient.unsubscribed_at = None, None
            recipient_id = recipient.id
        await self._subscriptions.send_confirmation(recipient_id)
