"""Read a server's TLS certificate through the same SSRF rules as HTTP requests."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509

from sentinel.security.addresses import (
    STRICT,
    AddressPolicy,
    Resolver,
    parse_target,
    resolve_pinned,
    system_resolver,
)
from sentinel.security.errors import (
    ConnectFailedError,
    ConnectTimeoutError,
    OutboundError,
    TLSError,
)


@dataclass(frozen=True)
class CertInfo:
    not_after: datetime
    issuer: str
    subject: str
    verified: bool  # chain and host name valid for the trust store in use
    problem: str | None  # why verification failed

    def days_left(self, now: datetime) -> int:
        return (self.not_after - now).days


async def _peer_certificate(ip: str, port: int, host: str, context: ssl.SSLContext) -> bytes:
    _, writer = await asyncio.open_connection(ip, port, ssl=context, server_hostname=host)
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            raise TLSError("no certificate presented")
        return bytes(der)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def fetch_certificate(
    host: str,
    port: int = 443,
    *,
    timeout_s: float = 10.0,
    resolver: Resolver = system_resolver,
    policy: AddressPolicy = STRICT,
    trust: ssl.SSLContext | None = None,
) -> CertInfo:
    """Connect to the validated, pinned address with SNI ``host`` and read the leaf cert.

    If verification fails the certificate is read again without verification, so the
    expiry date is still known; ``verified`` is then False with the reason.
    """
    target = parse_target(f"https://{host}:{port}/", policy=policy)
    ip = await resolve_pinned(target, resolver, timeout_s, policy)
    verified_context = trust or ssl.create_default_context()
    problem: str | None = None
    try:
        async with asyncio.timeout(timeout_s):
            try:
                der = await _peer_certificate(str(ip), port, target.host, verified_context)
            except ssl.SSLCertVerificationError as exc:
                problem = exc.verify_message or "certificate verification failed"
                insecure = ssl.create_default_context()
                insecure.check_hostname = False
                insecure.verify_mode = ssl.CERT_NONE
                der = await _peer_certificate(str(ip), port, target.host, insecure)
    except TimeoutError as exc:
        raise ConnectTimeoutError("TLS connection timed out") from exc
    except ssl.SSLError as exc:
        raise TLSError(f"TLS handshake failed: {exc.reason or exc}") from exc
    except OutboundError:
        raise
    except OSError as exc:
        raise ConnectFailedError(type(exc).__name__) from exc
    cert = x509.load_der_x509_certificate(der)
    return CertInfo(
        not_after=cert.not_valid_after_utc.astimezone(UTC),
        issuer=cert.issuer.rfc4514_string()[:200],
        subject=cert.subject.rfc4514_string()[:200],
        verified=problem is None,
        problem=problem,
    )
