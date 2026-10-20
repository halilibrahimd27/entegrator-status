"""robots.txt rules per RFC 9309. Pure functions.

``urllib.robotparser`` does not implement the ``*`` and ``$`` patterns of RFC 9309: it
reads ``Disallow: /pypi/*/json`` as a literal prefix and allows ``/pypi/requests/json``.
This module does: groups are chosen by our product token (else ``*``), the longest
matching rule wins, and on a tie ``Allow`` wins (the least restrictive, as the RFC
recommends). ``Crawl-delay`` is not in the RFC but widely used; we honour it.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

AGENT = "entegratorstatus"
MAX_RULES = 2000


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def length(self) -> int:
        return len(self.pattern)


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass(frozen=True)
class Robots:
    rules: tuple[Rule, ...] = ()
    crawl_delay: float | None = None

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules:
            if rule.regex.match(path) and (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow)
            ):
                best = rule
        return best is None or best.allow


ALLOW_ALL = Robots()


def _compile(pattern: str) -> re.Pattern[str]:
    pattern = unquote(pattern)
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def parse(text: str) -> Robots:
    groups: list[Group] = []
    current: Group | None = None
    collecting_agents = False
    count = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or not collecting_agents:
                current = Group()
                groups.append(current)
            current.agents.append(value.lower())
            collecting_agents = True
            continue
        collecting_agents = False
        if current is None:
            continue  # rules before any user-agent line belong to nobody
        if key in ("allow", "disallow") and value and count < MAX_RULES:
            current.rules.append(Rule(key == "allow", value, _compile(value)))
            count += 1
        elif key == "crawl-delay":
            with contextlib.suppress(ValueError):
                current.crawl_delay = max(0.0, float(value))
    ours = [g for g in groups if AGENT in g.agents]
    chosen = ours or [g for g in groups if "*" in g.agents]
    rules = tuple(rule for group in chosen for rule in group.rules)
    delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
    return Robots(rules, max(delays) if delays else None)
