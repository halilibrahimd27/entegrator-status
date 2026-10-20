from __future__ import annotations

from typing import Any

import pytest

from sentinel.models import SourceKind
from sentinel.sources.config import KIND_CONFIG, SourceConfigError, validate_source

VALID: dict[str, dict[str, Any]] = {
    "http": {"url": "https://portal.example.invalid/giris"},
    "tls_cert": {"host": "portal.example.invalid"},
    "dns": {"host": "api.example.invalid", "record_type": "AAAA"},
    "domain_expiry": {"domain": "example.invalid"},
    "status_page": {
        "url": "https://status.example.invalid/api/v2/summary.json",
        "provider": "statuspage",
    },
    "api_contract": {"url": "https://api.example.invalid/openapi.json", "format": "openapi"},
    "release": {"ecosystem": "npm", "name": "@scope/pkg"},
    "feed": {"url": "https://blog.example.invalid/feed.xml"},
    "page_watch": {
        "url": "https://example.invalid/duyurular",
        "item_selector": "article",
        "title_selector": "h2",
    },
    "advisory": {"ecosystem": "pypi", "name": "fastapi", "version": "0.142.2"},
    "eol": {"product": "python", "cycle": "3.12"},
}


def test_every_kind_has_a_config_model_and_an_example() -> None:
    assert set(KIND_CONFIG) == set(SourceKind)
    assert set(VALID) == {k.value for k in SourceKind}


@pytest.mark.parametrize(("kind", "config"), list(VALID.items()))
def test_valid_examples(kind: str, config: dict[str, Any]) -> None:
    source = validate_source(kind, config)
    assert source.kind.value == kind
    assert source.identity.startswith(f"{kind}:")
    assert source.interval_s >= 60


@pytest.mark.parametrize(
    ("kind", "config", "message"),
    [
        ("http", {"url": "http://portal.example.invalid/"}, "scheme 'http'"),
        ("http", {"url": "ftp://portal.example.invalid/"}, "scheme"),
        ("http", {"url": "https://user:pw@portal.example.invalid/"}, "credentials"),
        ("http", {"url": "https://10.0.0.1/admin"}, "blocked"),
        ("http", {"url": "https://169.254.169.254/latest/meta-data"}, "blocked"),
        ("http", {"url": "https://portal.example.invalid/", "method": "POST"}, "method"),
        ("http", {"url": "https://portal.example.invalid/", "extra": 1}, "extra"),
        ("tls_cert", {"host": "127.0.0.1"}, "blocked"),
        ("dns", {"host": "bad host"}, "host"),
        ("release", {"ecosystem": "npm", "name": "../../etc/passwd x"}, "invalid package"),
        ("release", {"ecosystem": "cpan", "name": "x"}, "ecosystem"),
        ("eol", {"product": "Python 3"}, "product"),
        ("nope", {}, "unknown source kind"),
    ],
)
def test_invalid_configs(kind: str, config: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceConfigError, match=message):
        validate_source(kind, config)


def test_plain_http_needs_the_flag() -> None:
    assert validate_source("http", {"url": "http://legacy.example.invalid/"}, allow_http=True)


@pytest.mark.parametrize(
    ("kind", "interval", "ok"),
    [
        ("http", 0, False),
        ("http", 59, False),
        ("http", 60, True),
        ("feed", 3599, False),
        ("feed", 3600, True),
    ],
)
def test_minimum_intervals(kind: str, interval: int, ok: bool) -> None:
    if ok:
        assert validate_source(kind, VALID[kind], interval_s=interval).interval_s == interval
    else:
        with pytest.raises(SourceConfigError, match="interval_s"):
            validate_source(kind, VALID[kind], interval_s=interval)


@pytest.mark.parametrize("timeout", [0, 31])
def test_timeout_bounds(timeout: int) -> None:
    with pytest.raises(SourceConfigError, match="timeout_s"):
        validate_source("http", VALID["http"], timeout_s=timeout)


def test_identity_is_stable_and_specific() -> None:
    a = validate_source("http", {"url": "https://a.example.invalid/x"})
    b = validate_source("http", {"url": "https://a.example.invalid/x", "method": "HEAD"})
    c = validate_source("release", {"ecosystem": "pypi", "name": "httpx"})
    assert a.identity == b.identity == "http:https://a.example.invalid/x"
    assert c.identity == "release:pypi:httpx"
