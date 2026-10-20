"""``Sunset`` (RFC 8594) and ``Deprecation`` (RFC 9745) response headers. Pure functions.

``Deprecation`` is a structured date (``@1688169599``); older drafts sent ``true`` or an
HTTP date, and both are still common, so all three are read. A ``Link`` with
``rel="deprecation"`` or ``rel="sunset"`` points at the provider's explanation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from sentinel.sources.feeds import safe_link

_LINK = re.compile(r'<([^>]{1,2000})>\s*;[^,]*?rel="?(?:deprecation|sunset)"?', re.IGNORECASE)


def http_date(value: str) -> datetime | None:
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def sunset(value: str | None) -> datetime | None:
    return http_date(value) if value else None


def deprecation(value: str | None) -> tuple[bool, datetime | None]:
    """``(deprecated, since)``; ``since`` may be None ("deprecated, no date given")."""
    if not value:
        return False, None
    value = value.strip()
    if value.startswith("@") and value[1:].lstrip("-").isdigit():
        try:
            return True, datetime.fromtimestamp(int(value[1:]), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return True, None
    if value.lower() == "true":
        return True, None
    when = http_date(value)
    return (when is not None), when


def explanation(link_header: str | None, base: str) -> str | None:
    match = _LINK.search(link_header or "")
    return safe_link(match.group(1), base) if match else None
