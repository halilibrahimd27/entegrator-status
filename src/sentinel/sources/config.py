"""Per-kind source configuration, validated on every write (UI, API, catalog import).

These models are the single definition: the catalog pack JSON Schema is generated from
them. URLs and host names go through the SSRF URL policy here (scheme, credentials,
host syntax, blocked IP literals); DNS answers are checked again at fetch time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sentinel.models.catalog import SourceKind
from sentinel.security.addresses import parse_target
from sentinel.security.errors import URLPolicyError
from sentinel.sources.pages import PageError, check_selector

AVAILABILITY_KINDS = frozenset(
    {SourceKind.HTTP, SourceKind.TLS_CERT, SourceKind.DNS, SourceKind.DOMAIN_EXPIRY}
)
MIN_INTERVAL_S = {"availability": 60, "content": 3600}
# Config keys the engine reads but the prober does not need. They stay out of the job
# identity, so organizations with different thresholds still share one probe.
ENGINE_ONLY_KEYS = frozenset({"latency_degraded_ms"})
DEFAULT_INTERVAL_S: dict[SourceKind, int] = {
    SourceKind.HTTP: 120,
    SourceKind.DNS: 300,
    SourceKind.TLS_CERT: 6 * 3600,
    SourceKind.DOMAIN_EXPIRY: 24 * 3600,
}
DEFAULT_CONTENT_INTERVAL_S = 6 * 3600
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 30
_NAME = re.compile(r"^[A-Za-z0-9@._/-]{1,214}$")


def _check_url(url: str) -> str:
    # allow_http is decided per source; here plain http is accepted and the caller
    # re-checks with the source's flag (see ``validate_source``).
    try:
        parse_target(url, allow_http=True)
    except URLPolicyError as exc:
        raise ValueError(str(exc)) from None
    return url


def _check_host(host: str) -> str:
    try:
        parse_target(f"https://{host}/")
    except URLPolicyError as exc:
        raise ValueError(str(exc)) from None
    return host.lower()


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _UrlConfig(_Config):
    url: Annotated[str, Field(max_length=2000)]

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _check_url(value)


class HttpConfig(_UrlConfig):
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: list[Annotated[int, Field(ge=100, le=599)]] = Field(
        default=[200], min_length=1, max_length=10
    )
    body_must_contain: Annotated[str, Field(max_length=200)] | None = None
    latency_degraded_ms: Annotated[int, Field(ge=1, le=60_000)] | None = None


class TlsCertConfig(_Config):
    host: Annotated[str, Field(max_length=253)]
    port: Annotated[int, Field(ge=1, le=65535)] = 443
    warn_days: Annotated[int, Field(ge=1, le=365)] = 21

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _check_host(value)


class DnsConfig(_Config):
    host: Annotated[str, Field(max_length=253)]
    record_type: Literal["A", "AAAA", "CNAME", "MX", "TXT", "NS"] = "A"

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return _check_host(value)


class DomainExpiryConfig(_Config):
    domain: Annotated[str, Field(max_length=253)]
    warn_days: Annotated[int, Field(ge=1, le=365)] = 30

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return _check_host(value)


class StatusPageConfig(_UrlConfig):
    provider: Literal["statuspage", "instatus", "rss"]
    # vendor component name -> our component slug
    component_map: dict[
        Annotated[str, Field(max_length=200)], Annotated[str, Field(max_length=63)]
    ] = {}


class ApiContractConfig(_UrlConfig):
    format: Literal["openapi", "wsdl", "json"]
    operations: list[Annotated[str, Field(max_length=200)]] = Field(default=[], max_length=500)


class _PackageRef(_Config):
    name: Annotated[str, Field(max_length=214)]

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("invalid package or repository name")
        return value


class ReleaseConfig(_PackageRef):
    ecosystem: Literal["github", "npm", "pypi", "oci"]


class AdvisoryConfig(_PackageRef):
    ecosystem: Literal["npm", "pypi", "go", "maven", "nuget", "packagist", "rubygems", "crates.io"]
    version: Annotated[str, Field(max_length=100)] | None = None


class FeedConfig(_UrlConfig):
    pass


class PageWatchConfig(_UrlConfig):
    item_selector: Annotated[str, Field(min_length=1, max_length=200)]
    title_selector: Annotated[str, Field(min_length=1, max_length=200)]
    date_selector: Annotated[str, Field(max_length=200)] | None = None
    link_selector: Annotated[str, Field(max_length=200)] | None = None

    @field_validator("item_selector", "title_selector", "date_selector", "link_selector")
    @classmethod
    def _selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return check_selector(value)
        except PageError as exc:
            raise ValueError(str(exc)) from None


class EolConfig(_Config):
    product: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,62}$")]
    cycle: Annotated[str, Field(max_length=40)] | None = None


KIND_CONFIG: dict[SourceKind, type[_Config]] = {
    SourceKind.HTTP: HttpConfig,
    SourceKind.TLS_CERT: TlsCertConfig,
    SourceKind.DNS: DnsConfig,
    SourceKind.DOMAIN_EXPIRY: DomainExpiryConfig,
    SourceKind.STATUS_PAGE: StatusPageConfig,
    SourceKind.API_CONTRACT: ApiContractConfig,
    SourceKind.RELEASE: ReleaseConfig,
    SourceKind.FEED: FeedConfig,
    SourceKind.PAGE_WATCH: PageWatchConfig,
    SourceKind.ADVISORY: AdvisoryConfig,
    SourceKind.EOL: EolConfig,
}


class SourceConfigError(ValueError):
    """Human-readable list of problems with a source definition."""


@dataclass(frozen=True)
class ValidSource:
    kind: SourceKind
    config: dict[str, Any]
    identity: str
    interval_s: int
    timeout_s: int


def probe_config(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` without the keys only the engine reads."""
    return {key: value for key, value in config.items() if key not in ENGINE_ONLY_KEYS}


def interval_class(kind: SourceKind) -> str:
    return "availability" if kind in AVAILABILITY_KINDS else "content"


def identity_of(kind: SourceKind, config: _Config) -> str:
    """Stable key of a source within its component (re-imports update, not duplicate)."""
    data = config.model_dump()
    for key in ("url", "host", "domain", "product"):
        if key in data:
            return f"{kind.value}:{data[key]}"
    return f"{kind.value}:{data.get('ecosystem')}:{data.get('name')}"


def validate_source(
    kind: str,
    config: dict[str, Any],
    *,
    interval_s: int | None = None,
    timeout_s: int | None = None,
    allow_http: bool = False,
) -> ValidSource:
    try:
        source_kind = SourceKind(kind)
    except ValueError:
        raise SourceConfigError(f"unknown source kind '{kind}'") from None
    try:
        parsed = KIND_CONFIG[source_kind].model_validate(config)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        raise SourceConfigError(problems) from None
    url = getattr(parsed, "url", None)
    if url is not None:
        try:
            parse_target(url, allow_http=allow_http)
        except URLPolicyError as exc:
            raise SourceConfigError(f"url: {exc}") from None
    klass = interval_class(source_kind)
    # ``is None``, not ``or``: an explicit 0 is an error, not a request for the default.
    interval = (
        DEFAULT_INTERVAL_S.get(source_kind, DEFAULT_CONTENT_INTERVAL_S)
        if interval_s is None
        else interval_s
    )
    if interval < MIN_INTERVAL_S[klass]:
        raise SourceConfigError(f"interval_s: at least {MIN_INTERVAL_S[klass]} for {klass} sources")
    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise SourceConfigError(f"timeout_s: between 1 and {MAX_TIMEOUT_S}")
    return ValidSource(
        kind=source_kind,
        config=parsed.model_dump(),
        identity=identity_of(source_kind, parsed),
        interval_s=interval,
        timeout_s=timeout,
    )
