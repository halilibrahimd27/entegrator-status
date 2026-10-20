"""Announcement classification from the title, with Turkish and English keyword tables.

Keywords match at the start of a word, so Turkish suffixes still match ("bakım" →
"bakımı", "bakımlar"). Categories are checked in order: a security notice about
maintenance is ``security``. Effective dates are pulled from the same text
(``15.11.2026``, ``2026-11-15``, ``15 Kasım 2026``, ``November 15, 2026``) and read
as midnight in Turkey time.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sentinel.models.changes import ChangeCategory

LOCAL_TZ = ZoneInfo("Europe/Istanbul")

KEYWORDS: tuple[tuple[ChangeCategory, tuple[str, ...]], ...] = (
    (
        ChangeCategory.SECURITY,
        ("güvenlik", "zafiyet", "siber", "cve-", "security", "vulnerab", "advisory", "exploit"),
    ),
    (
        ChangeCategory.DEPRECATION,
        (
            "kullanımdan kaldır",
            "desteği sona",
            "destek sonu",
            "sonlandırıl",
            "kapatılacak",
            "deprecat",
            "sunset",
            "end of life",
            "end-of-life",
            "retire",
            "discontinu",
        ),
    ),
    (
        ChangeCategory.MAINTENANCE,
        # "erişim" alone also matches "erişim sorunu" (an outage); only the future tense
        # ("will be closed / unreachable") announces planned work
        (
            "bakım",
            "kesinti",
            "planlı çalışma",
            "erişime kapal",
            "erişilemeyecek",
            "maintenance",
            "downtime",
        ),
    ),
    (
        ChangeCategory.PACKAGE_UPDATE,
        (
            "ubl-tr",
            "şematron",
            "schematron",
            "paket",
            "kod listesi",
            "xslt",
            "xsd",
            "sdk",
            "kılavuz",
            "teknik doküman",
            "package",
            "release",
        ),
    ),
    (
        ChangeCategory.INCIDENT,
        ("arıza", "sorun", "yavaşla", "incident", "outage", "disruption", "degraded"),
    ),
)

_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6, "temmuz": 7,
    "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATES = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b"), ("d", "m", "y")),
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTH})\.?\s+(\d{{4}})\b"), ("d", "M", "y")),
    (re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b"), ("M", "d", "y")),
)


def turkish_lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def _variants(text: str) -> tuple[str, ...]:
    """Turkish and plain lower case: "SECURITY" must not become "securıty"."""
    return (turkish_lower(text), text.lower())


def _pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])" + re.escape(keyword))


_COMPILED = tuple((category, tuple(_pattern(k) for k in words)) for category, words in KEYWORDS)


def classify(text: str) -> ChangeCategory:
    texts = _variants(text)
    for category, patterns in _COMPILED:
        if any(p.search(t) for p in patterns for t in texts):
            return category
    return ChangeCategory.OTHER


def effective_date(text: str) -> datetime | None:
    """The first plausible calendar date in ``text``, as midnight Turkey time (in UTC)."""
    found: list[tuple[int, date]] = []
    for pattern, order in _DATES:
        for match in (m for t in _variants(text) for m in pattern.finditer(t)):
            parts = dict(zip(order, match.groups(), strict=True))
            try:
                month = _MONTHS[parts["M"]] if "M" in parts else int(parts["m"])
                day = date(int(parts["y"]), month, int(parts["d"]))
            except (KeyError, ValueError):
                continue
            if 2000 <= day.year <= 2100:
                found.append((match.start(), day))
    if not found:
        return None
    _, first = min(found)
    return datetime.combine(first, time(), tzinfo=LOCAL_TZ).astimezone(UTC)
