"""Errors raised by the outbound HTTP layer.

Callers outside ``sentinel.security`` never see httpx exceptions; every failure is
one of these, and ``kind`` is the stable error class stored with check results.
"""

from __future__ import annotations


class OutboundError(Exception):
    """Base class for every failure of an outbound request."""

    kind = "error"


class URLPolicyError(OutboundError):
    """The URL itself is not allowed (scheme, credentials, malformed host)."""

    kind = "blocked_url"


class AddressBlockedError(URLPolicyError):
    """The host resolves to (or is) an address in a blocked range."""

    kind = "blocked_address"


class DNSResolutionError(OutboundError):
    kind = "dns"


class ConnectTimeoutError(OutboundError):
    kind = "connect_timeout"


class ReadTimeoutError(OutboundError):
    kind = "read_timeout"


class ConnectFailedError(OutboundError):
    kind = "connect"


class TLSError(OutboundError):
    kind = "tls"


class TooManyRedirectsError(OutboundError):
    kind = "too_many_redirects"


class ResponseTooLargeError(OutboundError):
    kind = "response_too_large"
