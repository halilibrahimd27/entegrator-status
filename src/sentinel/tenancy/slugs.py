"""URL slugs for organization names (Turkish-aware)."""

from __future__ import annotations

import re
import unicodedata

_TURKISH = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u"})
MAX_SLUG = 48


def slugify(name: str) -> str:
    text = name.translate(_TURKISH).lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:MAX_SLUG].rstrip("-") or "org"


def with_suffix(slug: str, n: int) -> str:
    suffix = f"-{n}"
    return slug[: MAX_SLUG - len(suffix)].rstrip("-") + suffix
