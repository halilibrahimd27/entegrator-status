"""TOTP (RFC 6238): encrypted secrets, replay-safe verification, inline SVG QR codes."""

from __future__ import annotations

import hmac
import os
import uuid
from datetime import datetime

import pyotp
import segno
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ISSUER = "Entegratör Status"
_VERSION = b"\x01"


class TotpSecretError(Exception):
    """The stored secret could not be decrypted (wrong key or tampered row)."""


class TotpBox:
    """Encrypts TOTP secrets with a key derived from ``TOTP_ENCRYPTION_KEY``.

    The user id is authenticated data, so a secret copied onto another user's row
    does not decrypt.
    """

    def __init__(self, key: str) -> None:
        derived = HKDF(
            algorithm=SHA256(), length=32, salt=None, info=b"entegrator-status/totp/v1"
        ).derive(key.encode())
        self._aead = AESGCM(derived)

    def seal(self, secret: str, user_id: uuid.UUID) -> bytes:
        nonce = os.urandom(12)
        return _VERSION + nonce + self._aead.encrypt(nonce, secret.encode(), user_id.bytes)

    def open(self, blob: bytes, user_id: uuid.UUID) -> str:
        if blob[:1] != _VERSION:
            raise TotpSecretError("unknown format")
        try:
            return self._aead.decrypt(blob[1:13], blob[13:], user_id.bytes).decode()
        except InvalidTag as exc:
            raise TotpSecretError("cannot decrypt") from exc


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """Inline SVG (no JavaScript, no external request)."""
    return str(segno.make(uri, error="m").svg_inline(scale=4, border=2))


def accept(secret: str, code: str, *, last_step: int | None, now: datetime) -> int | None:
    """Return the accepted time step, or ``None``.

    One step of clock drift either way is tolerated; a step at or before ``last_step``
    is refused, so a code works only once.
    """
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def code_at(secret: str, when: datetime) -> str:
    """The code an authenticator would show at ``when`` (used by tests and docs)."""
    return pyotp.TOTP(secret).at(when)
