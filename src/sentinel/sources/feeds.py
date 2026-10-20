"""RSS 2.0, RSS 1.0 (RDF) and Atom parsing into feed items.

Parsed with ``defusedxml``: no DTDs, no entity expansion, no external resources.
Only the title, link, publication date and a stable id are kept, never the article
text (METHODOLOGY "What we do not do"). Links are kept only when they are http(s),
so a feed cannot plant ``javascript:`` URLs in our pages.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element  # nosec B405 - the type only; parsing is defusedxml

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from sentinel.sources.snapshots import MAX_ITEMS

MAX_TITLE = 300
MAX_LINK = 2000
_SPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FeedError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: Element, *names: str) -> Element | None:
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element: Element | None) -> str:
    if element is None:
        return ""
    text = "".join(element.itertext())
    return _SPACE.sub(" ", _CONTROL.sub("", text)).strip()


def clean_title(raw: str) -> str:
    return _SPACE.sub(" ", _CONTROL.sub("", raw)).strip()[:MAX_TITLE]


def safe_link(raw: str, base: str) -> str | None:
    if not raw:
        return None
    link = urljoin(base, raw.strip())
    if urlsplit(link).scheme not in ("http", "https") or len(link) > MAX_LINK:
        return None
    return link


def parse_date(raw: str) -> str | None:
    """RFC 822 (RSS) or ISO 8601 (Atom, Dublin Core) as an ISO string in UTC."""
    raw = raw.strip()
    if not raw:
        return None
    when: datetime | None = None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def _atom_link(entry: Element) -> str:
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href", "")
        if child.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def item(*, title: str, link: str | None, published: str | None, ident: str) -> dict[str, Any]:
    stable = ident or link or f"{title}|{published or ''}"
    return {
        "id": hashlib.sha256(stable.encode()).hexdigest()[:32],
        "title": clean_title(title),
        "link": link,
        "published": published,
    }


def parse_feed(body: bytes, base: str) -> list[dict[str, Any]]:
    try:
        root = fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise FeedError(type(exc).__name__) from None
    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        entries = [e for e in channel if _local(e.tag) == "item"] if channel is not None else []
    elif kind == "RDF":
        entries = [e for e in root if _local(e.tag) == "item"]
    elif kind == "feed":
        entries = [e for e in root if _local(e.tag) == "entry"]
    else:
        raise FeedError(f"not a feed: <{kind[:40]}>")
    items: list[dict[str, Any]] = []
    for entry in entries[:MAX_ITEMS]:
        title = _text(_child(entry, "title"))
        if kind == "feed":
            raw_link = _atom_link(entry)
            date = _text(_child(entry, "published", "updated"))
            ident = _text(_child(entry, "id"))
        else:
            raw_link = _text(_child(entry, "link")) or entry.get(
                "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""
            )
            date = _text(_child(entry, "pubDate", "date"))
            ident = _text(_child(entry, "guid"))
        if not title:
            continue
        link = safe_link(raw_link, base)
        items.append(item(title=title, link=link, published=parse_date(date), ident=ident))
    return items
