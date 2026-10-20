"""Version parsing, ordering and change classes. Pure functions.

Handled explicitly:

- **semver** ``1.2.3``, ``v1.2.3``, ``1.2.3-rc.1``, ``1.2.3+build.5`` (build metadata is
  ignored), and npm monorepo tags ``pkg@1.2.3``. In ``0.y.z`` a minor bump is breaking
  (semver §4, npm caret ranges), so it is classified ``major``.
- **PEP 440 pre-releases** ``1.0.0rc1``, ``2.0a1``, ``1.0.0.dev3``, ``1.0b2``.
- **calver** (a first part that looks like a year, ``2026.10.1``): a new year is
  ``major``, a new month or minor ``minor``, anything after ``patch``. Calver promises
  nothing about compatibility; this mapping is a reading aid, not a guarantee.
- Anything else (``nightly``, ``release-candidate``) is not a version: it is never
  "latest" and changes to it are classified ``other``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Literal

Scheme = Literal["semver", "calver"]
ReleaseClass = Literal["major", "minor", "patch", "prerelease", "other"]

_PREFIX = re.compile(r"^(?:.*@|[A-Za-z][\w.-]*?[-_/])?[vV]?(?=\d)")
_CORE = re.compile(r"^(\d+(?:\.\d+){0,3})(.*)$")
_SEMVER_PRE = re.compile(r"^[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*$")
_PEP440_PRE = re.compile(r"^\.?(a|alpha|b|beta|c|rc|pre|preview|dev)\.?(\d*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Version:
    raw: str
    scheme: Scheme
    release: tuple[int, ...]
    pre: tuple[int | str, ...] = ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def part(self, index: int) -> int:
        return self.release[index] if index < len(self.release) else 0


def parse(raw: str) -> Version | None:
    text = raw.strip()
    if not text or len(text) > 100:
        return None
    text = _PREFIX.sub("", text, count=1).split("+", 1)[0]
    match = _CORE.match(text)
    if match is None:
        return None
    release = tuple(int(p) for p in match.group(1).split("."))
    rest = match.group(2)
    pre: tuple[int | str, ...] = ()
    if rest.startswith("-") and len(rest) > 1:
        if len(release) < 3 or not _SEMVER_PRE.match(rest[1:]):
            return None  # "1.27-alpine": an image variant, not a pre-release
        pre = tuple(int(p) if p.isdigit() else p.lower() for p in rest[1:].split("."))
    elif rest:
        pep = _PEP440_PRE.match(rest)
        if pep is None:
            return None  # e.g. "1.27-alpine": a variant, not a version
        pre = (pep.group(1).lower(), int(pep.group(2) or 0))
    scheme: Scheme = "calver" if release[0] >= 1990 else "semver"
    return Version(raw.strip(), scheme, release, pre)


def compare(a: Version, b: Version) -> int:
    width = max(len(a.release), len(b.release))
    left = tuple(a.part(i) for i in range(width))
    right = tuple(b.part(i) for i in range(width))
    if left != right:
        return -1 if left < right else 1
    if a.pre == b.pre:
        return 0
    if not a.pre or not b.pre:
        return 1 if not a.pre else -1  # a release sorts after its pre-releases
    for x, y in zip(a.pre, b.pre, strict=False):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int) != isinstance(y, int):
            return -1 if isinstance(x, int) else 1  # numbers sort before words (semver §11)
        return -1 if str(x) < str(y) else 1
    return -1 if len(a.pre) < len(b.pre) else 1


def newest(versions: list[Version], *, stable: bool = True) -> Version | None:
    candidates = [v for v in versions if not (stable and v.is_prerelease)]
    return max(candidates, key=cmp_to_key(compare), default=None)


def classify(previous: Version | None, new: Version | None) -> ReleaseClass:
    """How big the step from ``previous`` (the latest stable seen) to ``new`` is."""
    if new is None:
        return "other"
    if new.is_prerelease:
        return "prerelease"
    if previous is None or compare(new, previous) <= 0:
        return "other"  # first sighting, or a backport below the latest
    if new.scheme == "calver" or previous.scheme == "calver":
        if new.part(0) != previous.part(0):
            return "major"
        return "minor" if new.part(1) != previous.part(1) else "patch"
    if new.part(0) != previous.part(0):
        return "major"
    if new.part(0) == 0 and new.part(1) != previous.part(1):
        return "major"  # 0.y.z: a minor bump may break
    if new.part(1) != previous.part(1):
        return "minor"
    return "patch"


def behind(pinned: Version, latest: Version) -> ReleaseClass | None:
    """How far a pinned version lags the latest stable, or None when it is current."""
    if compare(pinned, latest) >= 0:
        return None
    return classify(pinned, latest)
