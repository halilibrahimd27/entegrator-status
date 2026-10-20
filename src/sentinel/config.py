"""Typed settings. Fail closed: a missing, short or placeholder secret aborts startup."""

from __future__ import annotations

import re
import sys
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_BYTES = 32
MIN_DISTINCT_CHARS = 8
SECRET_FIELDS = (
    "session_key",
    "token_hash_key",
    "webhook_signing_key",
    "ingest_hmac_key",
    "totp_encryption_key",
    "credentials_master_key",
)

# Compared against the value lower-cased with separators removed, so "Change_Me",
# "change-me" and "CHANGEME" all match. Markers are long enough that a random
# token practically never contains one by accident.
_PLACEHOLDER_MARKERS = (
    "changeme",
    "replaceme",
    "placeholder",
    "example",
    "dummy",
    "default",
    "insecure",
    "yoursecret",
    "yourkey",
    "secretkey",
    "notasecret",
    "password",
)
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class ConfigError(RuntimeError):
    """Raised when settings are invalid. The message never contains secret values."""


def secret_problem(value: str) -> str | None:
    """Return why ``value`` is unusable as a secret, or ``None`` if it is acceptable."""
    if len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        return f"must be at least {MIN_SECRET_BYTES} bytes"
    if "<" in value or ">" in value or "${" in value:
        return "looks like an unfilled placeholder"
    normalised = re.sub(r"[^a-z0-9]", "", value.lower())
    if any(marker in normalised for marker in _PLACEHOLDER_MARKERS):
        return "looks like a placeholder or a well-known default"
    if len(set(value)) < MIN_DISTINCT_CHARS:
        return "is not random enough"
    return None


class Settings(BaseSettings):
    """Process settings, read from the environment only (compose injects them)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    public_base_url: AnyHttpUrl
    contact_email: str

    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_reload: bool = False

    # Connection strings may carry passwords, so they are secrets too (not validated
    # for randomness: the password part is checked by the database itself).
    database_url: SecretStr
    redis_url: SecretStr

    # One key, one purpose: these must all be different.
    session_key: SecretStr
    token_hash_key: SecretStr
    webhook_signing_key: SecretStr
    ingest_hmac_key: SecretStr
    totp_encryption_key: SecretStr
    credentials_master_key: SecretStr  # wraps per-organization data keys (ADR 0011)

    signup_enabled: bool = True
    session_idle_minutes: int = 120
    session_max_hours: int = 24 * 7

    mail_backend: Literal["smtp", "memory"] = "smtp"
    mail_from: str = ""  # address only; the display name is the site name
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")

    @field_validator(*SECRET_FIELDS)
    @classmethod
    def _secret_is_usable(cls, value: SecretStr) -> SecretStr:
        problem = secret_problem(value.get_secret_value())
        if problem:
            raise ValueError(problem)
        return value

    @field_validator("database_url")
    @classmethod
    def _database_is_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_url_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("redis://", "rediss://")):
            raise ValueError("must start with redis:// or rediss://")
        return value

    @field_validator("contact_email")
    @classmethod
    def _email_is_plausible(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("must be an e-mail address")
        return value

    @model_validator(mode="after")
    def _keys_are_distinct(self) -> Settings:
        values = [getattr(self, name).get_secret_value() for name in SECRET_FIELDS]
        if len(set(values)) != len(values):
            raise ValueError(f"{', '.join(SECRET_FIELDS)} must all be different")
        return self

    @model_validator(mode="after")
    def _mail_is_deliverable(self) -> Settings:
        if self.mail_backend == "memory" and self.environment == "prod":
            raise ValueError("MAIL_BACKEND=memory is not allowed in production")
        if self.mail_backend == "smtp" and not (self.smtp_host and self.mail_from):
            raise ValueError("SMTP_HOST and MAIL_FROM are required when MAIL_BACKEND=smtp")
        if self.mail_from and not _EMAIL_RE.match(self.mail_from):
            raise ValueError("MAIL_FROM must be an e-mail address")
        if self.smtp_security == "none" and self.environment == "prod":
            raise ValueError("SMTP_SECURITY=none is not allowed in production")
        return self

    @property
    def public_base(self) -> str:
        return str(self.public_base_url).rstrip("/")

    @property
    def cookies_secure(self) -> bool:
        return self.public_base.startswith("https://") or self.environment == "prod"


def load_settings() -> Settings:
    """Build settings from the environment or raise :class:`ConfigError`."""
    try:
        return Settings()  # values come from the environment
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error["loc"]) or "settings"
            reason = "is required" if error["type"] == "missing" else error["msg"]
            lines.append(f"  - {field.upper()}: {reason}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from None


def load_settings_or_exit() -> Settings:
    """Entry-point helper: print a clear message and exit 2 instead of a traceback."""
    try:
        return load_settings()
    except ConfigError as exc:
        print(f"Refusing to start. {exc}", file=sys.stderr)
        raise SystemExit(2) from None
