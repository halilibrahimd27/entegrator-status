"""Account use cases, independent of HTTP. Errors carry i18n keys, never details.

Responses never reveal whether an e-mail address has an account: sign-up, verification
resend and password reset answer the same way either way.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.accounts.mail import Mail, Mailer
from sentinel.accounts.passwords import PasswordPolicyError, Passwords, check_policy
from sentinel.accounts.sessions import SessionManager
from sentinel.accounts.throttle import (
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_IP,
    RECOVERY_PER_ACCOUNT,
    RECOVERY_PER_IP,
    SIGNUP_PER_IP,
    Throttle,
)
from sentinel.accounts.tokens import digest, new_token
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import OneTimeToken, TokenPurpose, User
from sentinel.web.i18n import t

PRIVACY_NOTICE_VERSION = "2026-10-19"
MAX_FAILED_LOGINS = 5
LOCK_FOR = timedelta(minutes=15)
VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
_EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[^@\s<>]{2,63}$")


class AccountError(Exception):
    """``key`` is the i18n key shown to the user."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class LoginOutcome:
    user_id: uuid.UUID
    needs_totp: bool


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > 320 or not _EMAIL_RE.match(email):
        raise AccountError("signup.invalid_email")
    return email


class AccountService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        passwords: Passwords,
        sessions: SessionManager,
        mailer: Mailer,
        throttle: Throttle,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._passwords = passwords
        self.sessions = sessions
        self._mailer = mailer
        self._throttle = throttle
        self._clock = clock
        self._token_key = settings.token_hash_key.get_secret_value()

    # --- registration -----------------------------------------------------

    async def register(
        self, *, email: str, display_name: str, password: str, privacy_ack: bool, ip: str
    ) -> None:
        if not self._settings.signup_enabled:
            raise AccountError("signup.disabled")
        if not privacy_ack:
            raise AccountError("signup.privacy_required")
        email = normalize_email(email)
        name = display_name.strip()[:120]
        if not name:
            raise AccountError("signup.name_required")
        try:
            check_policy(password, email=email)
        except PasswordPolicyError as exc:
            raise AccountError(exc.key) from None
        if not await self._throttle.allow(SIGNUP_PER_IP, ip):
            raise AccountError("throttled")

        token: str | None = None
        try:
            async with self._db.session() as s:
                if await s.scalar(select(User.id).where(User.email == email)) is None:
                    user = User(
                        email=email,
                        display_name=name,
                        password_hash=self._passwords.hash(password),
                        privacy_notice_version=PRIVACY_NOTICE_VERSION,
                    )
                    s.add(user)
                    await s.flush()
                    token = await self._issue(s, user.id, TokenPurpose.VERIFY_EMAIL, VERIFY_TTL)
                    audit.record(s, "account.signup", actor_user_id=user.id, ip=ip)
        except IntegrityError:
            token = None  # concurrent sign-up with the same address
        if token is None:
            await self._send(email, "mail.signup_exists")
        else:
            await self._send(email, "mail.verify", link=self._link(f"/dogrula/{token}"))

    async def verify_email(self, token: str) -> bool:
        async with self._db.session() as s:
            row = await self._consume(s, token, TokenPurpose.VERIFY_EMAIL)
            if row is None:
                return False
            await s.execute(
                update(User)
                .where(User.id == row.user_id, User.email_verified_at.is_(None))
                .values(email_verified_at=self._clock())
            )
            audit.record(s, "account.email_verified", actor_user_id=row.user_id)
            return True

    async def resend_verification(self, email: str, ip: str) -> None:
        email = normalize_email(email)
        if not (
            await self._throttle.allow(RECOVERY_PER_IP, ip)
            and await self._throttle.allow(RECOVERY_PER_ACCOUNT, email)
        ):
            raise AccountError("throttled")
        async with self._db.session() as s:
            user = await s.scalar(select(User).where(User.email == email))
            if user is None or user.email_verified_at is not None:
                return
            token = await self._issue(s, user.id, TokenPurpose.VERIFY_EMAIL, VERIFY_TTL)
        await self._send(email, "mail.verify", link=self._link(f"/dogrula/{token}"))

    # --- sign-in ----------------------------------------------------------

    async def authenticate(self, *, email: str, password: str, ip: str) -> LoginOutcome:
        try:
            email = normalize_email(email)
        except AccountError:
            raise AccountError("login.failed") from None
        if not (
            await self._throttle.allow(LOGIN_PER_IP, ip)
            and await self._throttle.allow(LOGIN_PER_ACCOUNT, email)
        ):
            raise AccountError("throttled")

        error: str | None = None
        outcome: LoginOutcome | None = None
        async with self._db.session() as s:  # commits failure counters before we raise
            user = await s.scalar(select(User).where(User.email == email).with_for_update())
            ok, rehash = self._passwords.verify(user.password_hash if user else None, password)
            now = self._clock()
            if user is None:
                error = "login.failed"
            elif user.locked_until is not None and user.locked_until > now:
                audit.record(s, "account.login_refused_locked", actor_user_id=user.id, ip=ip)
                error = "login.failed"
            elif not ok:
                user.failed_logins += 1
                audit.record(s, "account.login_failed", actor_user_id=user.id, ip=ip)
                if user.failed_logins >= MAX_FAILED_LOGINS:
                    user.locked_until = now + LOCK_FOR
                    user.failed_logins = 0
                    audit.record(s, "account.locked", actor_user_id=user.id, ip=ip)
                error = "login.failed"
            elif user.email_verified_at is None:
                error = "login.unverified"
            else:
                user.failed_logins = 0
                user.locked_until = None
                user.last_login_at = now
                if rehash:
                    user.password_hash = self._passwords.hash(password)
                needs_totp = user.totp_enabled_at is not None
                audit.record(
                    s,
                    "account.password_ok" if needs_totp else "account.login",
                    actor_user_id=user.id,
                    ip=ip,
                )
                outcome = LoginOutcome(user_id=user.id, needs_totp=needs_totp)
        if outcome is None:
            raise AccountError(error or "login.failed")
        return outcome

    async def start_session(self, user_id: uuid.UUID, *, mfa_verified: bool) -> str:
        async with self._db.session() as s:
            return self.sessions.create(s, user_id, mfa_verified=mfa_verified)

    # --- password recovery ------------------------------------------------

    async def request_password_reset(self, email: str, ip: str) -> None:
        try:
            email = normalize_email(email)
        except AccountError:
            return
        if not (
            await self._throttle.allow(RECOVERY_PER_IP, ip)
            and await self._throttle.allow(RECOVERY_PER_ACCOUNT, email)
        ):
            raise AccountError("throttled")
        async with self._db.session() as s:
            user = await s.scalar(select(User).where(User.email == email))
            if user is None or user.email_verified_at is None:
                return
            token = await self._issue(s, user.id, TokenPurpose.RESET_PASSWORD, RESET_TTL)
            audit.record(s, "account.reset_requested", actor_user_id=user.id, ip=ip)
        await self._send(email, "mail.reset", link=self._link(f"/sifre-sifirla/{token}"))

    async def reset_token_is_valid(self, token: str) -> bool:
        async with self._db.session() as s:
            return await self._find(s, token, TokenPurpose.RESET_PASSWORD) is not None

    async def reset_password(self, token: str, new_password: str) -> None:
        async with self._db.session() as s:
            row = await self._find(s, token, TokenPurpose.RESET_PASSWORD)
            if row is None:
                raise AccountError("reset.invalid")
            user = await s.get(User, row.user_id, with_for_update=True)
            if user is None:
                raise AccountError("reset.invalid")
            try:
                check_policy(new_password, email=user.email)
            except PasswordPolicyError as exc:
                raise AccountError(exc.key) from None  # the token is not spent
            now = self._clock()
            row.used_at = now
            user.password_hash = self._passwords.hash(new_password)
            user.password_changed_at = now
            user.failed_logins = 0
            user.locked_until = None
            await SessionManager.destroy_all(s, user.id)
            audit.record(s, "account.password_reset", actor_user_id=user.id)

    # --- helpers ------------------------------------------------------------

    def _digest(self, token: str, purpose: TokenPurpose) -> bytes:
        return digest(self._token_key, token, purpose=purpose.value)

    async def _issue(
        self, s: AsyncSession, user_id: uuid.UUID, purpose: TokenPurpose, ttl: timedelta
    ) -> str:
        now = self._clock()
        await s.execute(
            update(OneTimeToken)
            .where(
                OneTimeToken.user_id == user_id,
                OneTimeToken.purpose == purpose.value,
                OneTimeToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        token = new_token()
        s.add(
            OneTimeToken(
                user_id=user_id,
                purpose=purpose.value,
                token_hash=self._digest(token, purpose),
                expires_at=now + ttl,
            )
        )
        return token

    async def _find(
        self, s: AsyncSession, token: str, purpose: TokenPurpose
    ) -> OneTimeToken | None:
        row = await s.scalar(
            select(OneTimeToken)
            .where(
                OneTimeToken.token_hash == self._digest(token, purpose),
                OneTimeToken.purpose == purpose.value,
            )
            .with_for_update()
        )
        if row is None or row.used_at is not None or row.expires_at <= self._clock():
            return None
        return row

    async def _consume(
        self, s: AsyncSession, token: str, purpose: TokenPurpose
    ) -> OneTimeToken | None:
        row = await self._find(s, token, purpose)
        if row is not None:
            row.used_at = self._clock()
        return row

    def _link(self, path: str) -> str:
        return f"{self._settings.public_base}{path}"

    async def _send(self, to: str, key: str, **values: str) -> None:
        await self._mailer.send(
            Mail(to=to, subject=t(f"{key}.subject"), text=t(f"{key}.body").format(**values))
        )
