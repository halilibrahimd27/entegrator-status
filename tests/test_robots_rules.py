"""robots.txt per RFC 9309: groups, wildcards, end anchors, longest match."""

from __future__ import annotations

import pytest

from sentinel.probe.robots_rules import parse

PYPI_STYLE = """
Sitemap: https://pkg.example.invalid/sitemap.xml

User-agent: *
Disallow: /simple/
Disallow: /pypi/*/json
Disallow: /pypi*?
Disallow: /search*
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/pypi/requests/json", False),  # urllib.robotparser allowed this one
        ("/pypi/requests/2.0/json", False),
        ("/pypi/requests/json/extra", False),
        ("/pypi?q=x", False),
        ("/searchresults", False),
        ("/simple/requests/", False),
        ("/rss/project/requests/releases.xml", True),
        ("/project/requests/", True),
        ("/robots.txt", True),  # always readable
    ],
)
def test_wildcards(path: str, allowed: bool) -> None:
    assert parse(PYPI_STYLE).allows(f"https://pkg.example.invalid{path}") is allowed


def test_end_anchor_longest_match_and_ties() -> None:
    robots = parse(
        """
User-agent: *
Disallow: /private$
Disallow: /docs/
Allow: /docs/public/
Disallow: /tie
Allow: /tie
"""
    )
    base = "https://site.example.invalid"
    assert not robots.allows(f"{base}/private")
    assert robots.allows(f"{base}/privateer")  # $ anchors the end
    assert not robots.allows(f"{base}/docs/internal")
    assert robots.allows(f"{base}/docs/public/page")  # the longer rule wins
    assert robots.allows(f"{base}/tie")  # equal length: the least restrictive
    assert robots.allows(f"{base}/%64ocs/public/x")  # percent-encoding is normalised


def test_our_group_wins_and_groups_merge() -> None:
    robots = parse(
        """
User-agent: *
Disallow: /

User-agent: SomeBot
User-agent: EntegratorStatus
Disallow: /admin
Crawl-delay: 5

User-agent: entegratorstatus
Disallow: /drafts
"""
    )
    base = "https://site.example.invalid"
    assert robots.allows(f"{base}/page")  # our own group, not "*"
    assert not robots.allows(f"{base}/admin/x")
    assert not robots.allows(f"{base}/drafts/x")  # both of our groups apply
    assert robots.crawl_delay == 5.0


def test_garbage_and_empty_rules() -> None:
    robots = parse("Disallow: /orphan\nUser-agent: *\nDisallow:\nCrawl-delay: soon\nnonsense\n")
    assert robots.allows("https://site.example.invalid/orphan")  # rule before any group
    assert robots.crawl_delay is None
    assert parse("").allows("https://site.example.invalid/anything")
