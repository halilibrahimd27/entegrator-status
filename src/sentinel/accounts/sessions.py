"""Server-side sessions. The cookie carries a random token; the database only its HMAC."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.accounts.tokens import digest, new_token
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import User, UserSession

TOUCH_EVERY = timedelta(seconds=60)


@dataclass(frozen=True)
class LoadedSession:
    session: UserSession
    user: User


class SessionManager:
    def __init__(
        self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._key = settings.session_key.get_secret_value()
        self._idle = timedelta(minutes=settings.session_idle_minutes)
        self._max = timedelta(hours=settings.session_max_hours)
        self._clock = clock

    @property
    def max_age_s(self) -> int:
        return int(self._max.total_seconds())

    def _digest(self, token: str) -> bytes:
        return digest(self._key, token, purpose="session")

    def create(self, s: AsyncSession, user_id: uuid.UUID, *, mfa_verified: bool) -> str:
        """Add a session in the caller's transaction and return the raw cookie token."""
        token = new_token()
        now = self._clock()
        s.add(
            UserSession(
                user_id=user_id,
                token_hash=self._digest(token),
                mfa_verified=mfa_verified,
                created_at=now,
                last_seen_at=now,
                expires_at=now + self._max,
            )
        )
        return token

    async def load(self, token: str) -> LoadedSession | None:
        async with self._db.session() as s:
            row = (
                await s.execute(
                    select(UserSession, User)
                    .join(User, User.id == UserSession.user_id)
                    .where(UserSession.token_hash == self._digest(token))
                )
            ).one_or_none()
            if row is None:
                return None
            session, user = row
            now = self._clock()
            stale = (
                now >= session.expires_at
                or now - session.last_seen_at > self._idle
                or session.created_at < user.password_changed_at
            )
            if stale:
                await s.delete(session)
                return None
            if now - session.last_seen_at > TOUCH_EVERY:
                session.last_seen_at = now
            return LoadedSession(session=session, user=user)

    async def rotate(self, token: str, *, mfa_verified: bool) -> str | None:
        """Replace a session with a fresh one (privilege change). Returns the new token."""
        async with self._db.session() as s:
            old = await s.scalar(
                select(UserSession).where(UserSession.token_hash == self._digest(token))
            )
            if old is None:
                return None
            await s.delete(old)
            return self.create(s, old.user_id, mfa_verified=mfa_verified)

    async def destroy(self, token: str) -> None:
        async with self._db.session() as s:
            await s.execute(
                delete(UserSession).where(UserSession.token_hash == self._digest(token))
            )

    @staticmethod
    async def destroy_all(s: AsyncSession, user_id: uuid.UUID) -> None:
        await s.execute(delete(UserSession).where(UserSession.user_id == user_id))
