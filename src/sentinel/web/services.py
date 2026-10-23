"""Process-wide services, built in the app lifespan and reached through ``request.app``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis

from sentinel.accounts.mail import Mailer, build_mailer
from sentinel.accounts.passwords import Passwords, make_hasher
from sentinel.accounts.privacy import PrivacyService
from sentinel.accounts.service import AccountService
from sentinel.accounts.sessions import SessionManager
from sentinel.accounts.throttle import Throttle
from sentinel.accounts.totp import TotpBox
from sentinel.accounts.two_factor import TwoFactorService
from sentinel.alerts.subscriptions import SubscriptionService
from sentinel.catalog.imports import ImportService
from sentinel.catalog.proposals import ProposalService
from sentinel.catalog.service import CatalogService
from sentinel.config import Settings
from sentinel.db.session import Database
from sentinel.engine.change_views import ChangeViews
from sentinel.engine.notices import NoticeService
from sentinel.engine.views import StatusViews
from sentinel.graph.service import GraphService
from sentinel.probe.nodes import NodeService
from sentinel.risk.correlation import CorrelationViews
from sentinel.risk.digest import DigestViews
from sentinel.risk.impact import ImpactViews
from sentinel.risk.register import RiskRegister
from sentinel.risk.views import RiskViews
from sentinel.tenancy.api_tokens import ApiTokenService
from sentinel.tenancy.credentials import CredentialService
from sentinel.tenancy.invitations import InvitationService
from sentinel.tenancy.orgs import OrgService
from sentinel.web.templating import Renderer


@dataclass
class AppServices:
    settings: Settings
    db: Database
    redis: Redis
    mailer: Mailer
    accounts: AccountService
    two_factor: TwoFactorService
    privacy: PrivacyService
    orgs: OrgService
    invitations: InvitationService
    api_tokens: ApiTokenService
    catalog: CatalogService
    graph: GraphService
    imports: ImportService
    proposals: ProposalService
    nodes: NodeService
    notices: NoticeService
    status: StatusViews
    changes: ChangeViews
    risks: RiskViews
    impact: ImpactViews
    register: RiskRegister
    correlation: CorrelationViews
    digest: DigestViews
    credentials: CredentialService
    subscriptions: SubscriptionService
    renderer: Renderer

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.db.dispose()


def build_services(settings: Settings) -> AppServices:
    db = Database(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    mailer = build_mailer(settings)
    passwords = Passwords(make_hasher(settings.environment))
    throttle = Throttle(redis)
    accounts = AccountService(
        db=db,
        settings=settings,
        passwords=passwords,
        sessions=SessionManager(db, settings),
        mailer=mailer,
        throttle=throttle,
    )
    totp_box = TotpBox(settings.totp_encryption_key.get_secret_value())
    two_factor = TwoFactorService(db=db, passwords=passwords, throttle=throttle, totp_box=totp_box)
    graph = GraphService(db)
    impact = ImpactViews(db, graph)
    register = RiskRegister(db, impact)
    return AppServices(
        settings=settings,
        db=db,
        redis=redis,
        mailer=mailer,
        accounts=accounts,
        two_factor=two_factor,
        privacy=PrivacyService(db=db, passwords=passwords, totp_box=totp_box),
        orgs=OrgService(db),
        invitations=InvitationService(db=db, settings=settings, mailer=mailer, throttle=throttle),
        api_tokens=ApiTokenService(db, settings),
        catalog=CatalogService(db),
        graph=graph,
        imports=ImportService(db, redis),
        proposals=ProposalService(db),
        nodes=NodeService(db, settings),
        notices=NoticeService(db),
        status=StatusViews(db, graph),
        changes=ChangeViews(db),
        risks=RiskViews(db),
        impact=impact,
        register=register,
        correlation=CorrelationViews(db, ratio=settings.correlated_failure_ratio),
        digest=DigestViews(impact, register),
        credentials=CredentialService(db, settings),
        subscriptions=SubscriptionService(db, settings, mailer),
        renderer=Renderer(),
    )


def services(request: Request) -> AppServices:
    svc: AppServices = request.app.state.services
    return svc


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""
