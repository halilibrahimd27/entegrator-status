"""Random tokens and their keyed digests. Raw tokens are never stored or logged."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def digest(key: str, token: str, *, purpose: str) -> bytes:
    """HMAC-SHA256 with domain separation, so a digest for one use is useless for another."""
    return hmac.new(key.encode(), f"{purpose}\x00{token}".encode(), hashlib.sha256).digest()


def equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
