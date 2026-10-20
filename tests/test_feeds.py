"""Feed parsing and snapshots: RSS 2.0, RSS 1.0, Atom, hostile XML, stable hashes."""

from __future__ import annotations

import pytest

from sentinel.sources.feeds import FeedError, parse_date, parse_feed
from sentinel.sources.snapshots import (
    MAX_ITEMS,
    MAX_SNAPSHOT_BYTES,
    canonical,
    make_snapshot,
    safe_validator,
    snapshot_hash,
)

BASE = "https://vendor.example.invalid/duyurular/"

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Duyurular</title>
  <item><title>  Planl\xc4\xb1 bak\xc4\xb1m
     duyurusu </title><link>https://vendor.example.invalid/a</link>
    <guid>a-1</guid><pubDate>Tue, 20 Oct 2026 09:00:00 +0300</pubDate>
    <description>Uzun metin, saklanmaz.</description></item>
  <item><title>Relative</title><link>/b</link></item>
  <item><title>Script</title><link>javascript:alert(1)</link></item>
  <item><title></title><link>https://vendor.example.invalid/empty</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
  <entry><title type="html">API v1 kullan\xc4\xb1mdan kald\xc4\xb1r\xc4\xb1lacak</title>
    <link rel="self" href="https://vendor.example.invalid/self"/>
    <link rel="alternate" href="https://vendor.example.invalid/v1"/>
    <id>urn:uuid:1</id><updated>2026-10-19T08:00:00Z</updated></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="https://vendor.example.invalid/r1"><title>RDF item</title>
    <dc:date>2026-10-18</dc:date></item>
</rdf:RDF>"""


def test_rss() -> None:
    items = parse_feed(RSS, BASE)
    assert [i["title"] for i in items] == ["Planlı bakım duyurusu", "Relative", "Script"]
    first, relative, script = items
    assert first["link"] == "https://vendor.example.invalid/a"
    assert first["published"] == "2026-10-20T06:00:00+00:00"
    assert relative["link"] == "https://vendor.example.invalid/b"
    assert script["link"] is None  # never a javascript: URL in our pages
    assert all(set(i) == {"id", "title", "link", "published"} for i in items)  # no bodies
    assert parse_feed(RSS, BASE)[0]["id"] == first["id"]  # stable


def test_atom_and_rdf() -> None:
    (entry,) = parse_feed(ATOM, BASE)
    assert entry["title"] == "API v1 kullanımdan kaldırılacak"
    assert entry["link"] == "https://vendor.example.invalid/v1"  # alternate, not self
    assert entry["published"] == "2026-10-19T08:00:00+00:00"
    (item,) = parse_feed(RDF, BASE)
    assert item["link"] == "https://vendor.example.invalid/r1"
    assert item["published"] == "2026-10-18T00:00:00+00:00"


@pytest.mark.parametrize(
    "body",
    [
        # entity expansion ("billion laughs") and external entities are refused
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><rss>&b;</rss>',
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>',
        b"<html><body>not a feed</body></html>",
        b"\x00\x01 not xml",
    ],
)
def test_hostile_or_wrong_documents_are_refused(body: bytes) -> None:
    with pytest.raises(FeedError):
        parse_feed(body, BASE)


def test_dates() -> None:
    assert parse_date("") is None
    assert parse_date("yarın") is None
    assert parse_date("2026-10-20T09:00:00") == "2026-10-20T09:00:00+00:00"


def test_snapshot_hash_ignores_order_and_caps_size() -> None:
    items = parse_feed(RSS, BASE)
    assert snapshot_hash(items) == snapshot_hash(list(reversed(items)))
    assert snapshot_hash(items) != snapshot_hash(items[:-1])
    many = [{"id": str(i), "title": "x" * 200} for i in range(MAX_ITEMS + 50)]
    snapshot = make_snapshot(many, etag='"v1"', last_modified="bad\r\nvalue")
    assert len(snapshot.items) == MAX_ITEMS
    assert snapshot.validators == {"etag": '"v1"'}  # unsafe validators are dropped
    huge = [{"id": str(i), "title": "y" * 9000} for i in range(MAX_ITEMS)]
    assert len(canonical(make_snapshot(huge).items)) <= MAX_SNAPSHOT_BYTES
    assert safe_validator("x" * 300) is None
    assert safe_validator(None) is None
