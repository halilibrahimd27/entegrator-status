"""Watched pages: items picked out of HTML with CSS selectors (title, date, link only).

Ported from ``legacy/api-sentinel/watchers/changelog_watcher.py`` with its known
problems fixed: items instead of one hash over the whole selection, no silent
fallback to ``<body>`` when a selector misses (the caller flags ``needs_attention``),
no network access from the parser, and never the article text.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

from cssselect import GenericTranslator, SelectorError
from lxml import etree
from lxml import html as lxml_html

from sentinel.sources.classify import effective_date
from sentinel.sources.feeds import clean_title, parse_date, safe_link
from sentinel.sources.snapshots import MAX_ITEMS

_PARSER = lxml_html.HTMLParser(
    no_network=True, remove_comments=True, remove_pis=True, recover=True, huge_tree=False
)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_.:-]{1,40})""", re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"charset=\"?([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE)


class PageError(Exception):
    pass


def charset_of(content_type: str | None) -> str | None:
    match = _HEADER_CHARSET.search(content_type or "")
    return match.group(1) if match else None


def decode_html(body: bytes, declared: str | None = None) -> str:
    """HTTP charset, else ``<meta charset>``, else UTF-8 (never lxml's Latin-1 guess)."""
    sniffed = _META_CHARSET.search(body[:4096])
    for charset in (declared, sniffed.group(1).decode() if sniffed else None, "utf-8"):
        if not charset:
            continue
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")  # pragma: no cover - utf-8 always exists


@lru_cache(maxsize=512)
def to_xpath(selector: str) -> str:
    """CSS → XPath, relative to the current node. Raises ``PageError`` when invalid."""
    try:
        return GenericTranslator().css_to_xpath(selector, prefix="descendant-or-self::")
    except SelectorError as exc:
        raise PageError(f"invalid selector: {selector[:60]}") from exc


def check_selector(selector: str) -> str:
    to_xpath(selector)
    return selector


def _elements(node: Any, xpath: str) -> list[lxml_html.HtmlElement]:
    found = node.xpath(xpath)
    if not isinstance(found, list):
        return []
    return [element for element in found if isinstance(element, lxml_html.HtmlElement)]


def _first(node: Any, selector: str | None) -> Any | None:
    if not selector:
        return None
    found = _elements(node, to_xpath(selector))
    return found[0] if found else None


def _date(node: Any) -> str | None:
    if node is None:
        return None
    stamp = node.get("datetime") if hasattr(node, "get") else None
    if stamp and (parsed := parse_date(stamp)):
        return parsed
    text = clean_title(node.text_content())
    if parsed := parse_date(text):
        return parsed
    when = effective_date(text)
    return when.isoformat() if when else None


def _link(item: Any, title: Any, link_node: Any | None, base: str) -> str | None:
    for candidate in (link_node, title):
        if candidate is not None and candidate.get("href"):
            return safe_link(str(candidate.get("href")), base)
    anchors = _elements(item, ".//a[@href]")
    return safe_link(str(anchors[0].get("href")), base) if anchors else None


def parse_page(
    body: bytes,
    base: str,
    *,
    item_selector: str,
    title_selector: str,
    date_selector: str | None = None,
    link_selector: str | None = None,
    charset: str | None = None,
) -> list[dict[str, Any]]:
    try:
        document = lxml_html.document_fromstring(decode_html(body, charset), parser=_PARSER)
    except (etree.ParserError, ValueError) as exc:
        raise PageError(type(exc).__name__) from None
    items: list[dict[str, Any]] = []
    for node in _elements(document, to_xpath(item_selector))[:MAX_ITEMS]:
        title_node = _first(node, title_selector)
        title = clean_title(title_node.text_content()) if title_node is not None else ""
        if not title:
            continue
        link = _link(node, title_node, _first(node, link_selector), base)
        published = _date(_first(node, date_selector))
        stable = link or f"{title}|{published or ''}"
        items.append(
            {
                "id": hashlib.sha256(stable.encode()).hexdigest()[:32],
                "title": title,
                "link": link,
                "published": published,
            }
        )
    return items
