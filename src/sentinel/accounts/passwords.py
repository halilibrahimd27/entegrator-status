"""Password policy and argon2id hashing."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128  # argon2 is slow on purpose; unbounded input would be a DoS lever


class PasswordPolicyError(ValueError):
    """``key`` is the i18n key of the explanation shown to the user."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def check_policy(password: str, *, email: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError("password.too_short")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError("password.too_long")
    if len(set(password)) < 5:
        raise PasswordPolicyError("password.too_simple")
    local = email.split("@", 1)[0].lower()
    if len(local) >= 4 and local in password.lower():
        raise PasswordPolicyError("password.contains_email")


def make_hasher(environment: str) -> PasswordHasher:
    if environment == "test":
        # Fast parameters for the test suite only; never used by dev or prod.
        return PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    return PasswordHasher()  # argon2id, RFC 9106 low-memory profile


class Passwords:
    def __init__(self, hasher: PasswordHasher) -> None:
        self._hasher = hasher
        # Verified when the account does not exist, so timing does not reveal it.
        self._dummy = hasher.hash(secrets.token_urlsafe(24))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored: str | None, password: str) -> tuple[bool, bool]:
        """Return ``(matches, needs_rehash)``."""
        try:
            self._hasher.verify(stored or self._dummy, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
        if stored is None:
            return False, False
        return True, self._hasher.check_needs_rehash(stored)
