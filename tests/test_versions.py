"""Version parsing, ordering and release classes: semver, PEP 440, calver, odd tags."""

from __future__ import annotations

from functools import cmp_to_key

import pytest

from sentinel.sources.versions import behind, classify, compare, newest, parse


@pytest.mark.parametrize(
    ("raw", "release", "pre"),
    [
        ("1.2.3", (1, 2, 3), ()),
        ("v1.2.3", (1, 2, 3), ()),
        ("V2.0", (2, 0), ()),
        ("1.2.3+build.5", (1, 2, 3), ()),  # build metadata ignored
        ("2.0.0-rc.1", (2, 0, 0), ("rc", 1)),
        ("react@18.3.1", (18, 3, 1), ()),  # npm monorepo tag
        ("@scope/pkg@1.0.0", (1, 0, 0), ()),
        ("release-1.4.0", (1, 4, 0), ()),
        ("my-lib-v3.2.1", (3, 2, 1), ()),
        ("1.0.0rc1", (1, 0, 0), ("rc", 1)),  # PEP 440
        ("2.0a1", (2, 0), ("a", 1)),
        ("1.0.0.dev3", (1, 0, 0), ("dev", 3)),
        ("2026.10.1", (2026, 10, 1), ()),
    ],
)
def test_parse(raw: str, release: tuple[int, ...], pre: tuple[object, ...]) -> None:
    version = parse(raw)
    assert version is not None
    assert (version.release, version.pre) == (release, pre)


@pytest.mark.parametrize("raw", ["nightly", "", "latest", "1.27-alpine", "1.0.0-x..y", "x" * 120])
def test_not_versions(raw: str) -> None:
    assert parse(raw) is None


def test_semver_precedence() -> None:
    # semver §11 example, shuffled
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1",
        "2.0.0",
    ]
    versions = [parse(v) for v in reversed(ordered)]
    assert all(v is not None for v in versions)
    ranked = sorted(versions, key=cmp_to_key(compare))  # type: ignore[arg-type]
    assert [v.raw for v in ranked if v] == ordered
    assert compare(parse("1.0"), parse("1.0.0")) == 0  # type: ignore[arg-type]
    stable = newest([v for v in versions if v])
    assert stable is not None
    assert stable.raw == "2.0.0"
    with_pre = newest([v for v in [parse("1.0.0"), parse("2.0.0-rc.1")] if v], stable=False)
    assert with_pre is not None
    assert with_pre.raw == "2.0.0-rc.1"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("1.9.4", "2.0.0", "major"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "1.2.4", "patch"),
        ("1.2.3", "2.0.0-rc.1", "prerelease"),
        ("0.3.1", "0.4.0", "major"),  # 0.y: a minor bump may break
        ("0.3.1", "0.3.2", "patch"),
        ("2025.12.1", "2026.1.0", "major"),  # calver: a new year
        ("2026.9.0", "2026.10.0", "minor"),
        ("2026.10.0", "2026.10.1", "patch"),
        ("2.0.0", "1.9.9", "other"),  # older than the latest
        (None, "1.0.0", "other"),  # first sighting
    ],
)
def test_classify(old: str | None, new: str, expected: str) -> None:
    assert classify(parse(old) if old else None, parse(new)) == expected


def test_behind() -> None:
    latest = parse("3.1.0")
    assert latest is not None
    assert behind(parse("2.4.0"), latest) == "major"  # type: ignore[arg-type]
    assert behind(parse("3.0.2"), latest) == "minor"  # type: ignore[arg-type]
    assert behind(parse("3.1.0"), latest) is None  # type: ignore[arg-type]
    assert classify(None, None) == "other"
