"""Security boundary. All outbound traffic goes through ``sentinel.security.http``."""

from sentinel.security.errors import (
    AddressBlockedError,
    ConnectFailedError,
    ConnectTimeoutError,
    DNSResolutionError,
    OutboundError,
    ReadTimeoutError,
    ResponseTooLargeError,
    TLSError,
    TooManyRedirectsError,
    URLPolicyError,
)
from sentinel.security.http import (
    CONTRACT_MAX_BYTES,
    PAGE_MAX_BYTES,
    FetchPolicy,
    FetchResult,
    OutboundClient,
    Timeouts,
    build_user_agent,
)
from sentinel.security.ratelimit import HostLimiter, TokenBucket

__all__ = [
    "CONTRACT_MAX_BYTES",
    "PAGE_MAX_BYTES",
    "AddressBlockedError",
    "ConnectFailedError",
    "ConnectTimeoutError",
    "DNSResolutionError",
    "FetchPolicy",
    "FetchResult",
    "HostLimiter",
    "OutboundClient",
    "OutboundError",
    "ReadTimeoutError",
    "ResponseTooLargeError",
    "TLSError",
    "Timeouts",
    "TokenBucket",
    "TooManyRedirectsError",
    "URLPolicyError",
    "build_user_agent",
]
