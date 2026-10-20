"""Second factor (TOTP): enrolment, the sign-in check and disabling it."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sentinel import audit
from sentinel.accounts import totp
from sentinel.accounts.passwords import Passwords
from sentinel.accounts.service import AccountError
from sentinel.accounts.throttle import TOTP_PER_USER, Throttle
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import User


@dataclass(frozen=True)
class TotpEnrolment:
    secret: str
    uri: str
    qr_svg: str


class TwoFactorService:
    def __init__(
        self,
        *,
        db: Database,
        passwords: Passwords,
        throttle: Throttle,
        totp_box: totp.TotpBox,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._passwords = passwords
        self._throttle = throttle
        self._box = totp_box
        self._clock = clock

    async def check_totp(self, user_id: uuid.UUID, code: str, ip: str) -> bool:
        """Second factor at sign-in. Each code works once."""
        if not await self._throttle.allow(TOTP_PER_USER, str(user_id)):
            raise AccountError("throttled")
        async with self._db.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None or user.totp_secret is None or user.totp_enabled_at is None:
                return False
            secret = self._box.open(user.totp_secret, user.id)
            step = totp.accept(secret, code, last_step=user.totp_last_step, now=self._clock())
            if step is None:
                audit.record(s, "account.totp_failed", actor_user_id=user.id, ip=ip)
                return False
            user.totp_last_step = step
            audit.record(s, "account.login", actor_user_id=user.id, ip=ip, details={"mfa": True})
            return True

    # --- TOTP enrolment -----------------------------------------------------

    async def start_totp(self, user_id: uuid.UUID) -> TotpEnrolment:
        secret = totp.new_secret()
        async with self._db.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                raise AccountError("not_found")
            if user.totp_enabled_at is not None:
                raise AccountError("totp.already_enabled")
            user.totp_secret = self._box.seal(secret, user.id)
            email = user.email
        uri = totp.provisioning_uri(secret, email)
        return TotpEnrolment(secret=secret, uri=uri, qr_svg=totp.qr_svg(uri))

    async def pending_totp(self, user_id: uuid.UUID) -> TotpEnrolment | None:
        """The enrolment started but not yet confirmed (to show the QR code again)."""
        async with self._db.session() as s:
            user = await s.get(User, user_id)
            if user is None or user.totp_secret is None or user.totp_enabled_at is not None:
                return None
            secret = self._box.open(user.totp_secret, user.id)
            email = user.email
        uri = totp.provisioning_uri(secret, email)
        return TotpEnrolment(secret=secret, uri=uri, qr_svg=totp.qr_svg(uri))

    async def confirm_totp(self, user_id: uuid.UUID, code: str) -> bool:
        async with self._db.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None or user.totp_secret is None or user.totp_enabled_at is not None:
                return False
            secret = self._box.open(user.totp_secret, user.id)
            step = totp.accept(secret, code, last_step=user.totp_last_step, now=self._clock())
            if step is None:
                return False
            user.totp_last_step = step
            user.totp_enabled_at = self._clock()
            audit.record(s, "account.totp_enabled", actor_user_id=user.id)
            return True

    async def disable_totp(self, user_id: uuid.UUID, *, password: str, code: str) -> None:
        async with self._db.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None or user.totp_enabled_at is None or user.totp_secret is None:
                raise AccountError("totp.not_enabled")
            if user.is_platform_admin:
                raise AccountError("totp.required_for_admin")
            ok, _ = self._passwords.verify(user.password_hash, password)
            secret = self._box.open(user.totp_secret, user.id)
            step = totp.accept(secret, code, last_step=user.totp_last_step, now=self._clock())
            if not ok or step is None:
                raise AccountError("totp.disable_failed")
            user.totp_secret = None
            user.totp_enabled_at = None
            user.totp_last_step = step
            audit.record(s, "account.totp_disabled", actor_user_id=user.id)
