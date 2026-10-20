"""Process-wide services, built in the app lifespan and reached through ``request.app``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis

from sentinel.accounts.mail import Mailer, build_mailer
from sentinel.accounts.passwords import Passwords, make_hasher
from sentinel.accounts.service import AccountService
from sentinel.accounts.sessions import SessionManager
from sentinel.accounts.throttle import Throttle
from sentinel.accounts.totp import TotpBox
from sentinel.accounts.two_factor import TwoFactorService
from sentinel.config import Settings
from sentinel.db.session import Database
from sentinel.web.templating import Renderer


@dataclass
class AppServices:
    settings: Settings
    db: Database
    redis: Redis
    mailer: Mailer
    accounts: AccountService
    two_factor: TwoFactorService
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
    two_factor = TwoFactorService(
        db=db,
        passwords=passwords,
        throttle=throttle,
        totp_box=TotpBox(settings.totp_encryption_key.get_secret_value()),
    )
    return AppServices(
        settings=settings,
        db=db,
        redis=redis,
        mailer=mailer,
        accounts=accounts,
        two_factor=two_factor,
        renderer=Renderer(),
    )


def services(request: Request) -> AppServices:
    svc: AppServices = request.app.state.services
    return svc


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""
