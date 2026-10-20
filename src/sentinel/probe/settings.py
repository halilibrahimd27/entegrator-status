"""Settings of the prober process. A remote node needs only these, never core secrets."""

from __future__ import annotations

import sys
import uuid
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.config import ConfigError, secret_problem


class ProbeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Identify the bot in every request (same values as the core).
    public_base_url: AnyHttpUrl
    contact_email: str

    probe_ingest_url: AnyHttpUrl
    probe_node_id: uuid.UUID
    probe_node_token: SecretStr
    probe_signing_key: SecretStr
    # Stable public URLs the node checks to judge its own network (comma separated).
    probe_control_targets: str = ""
    probe_buffer_path: str = "/data/probe-buffer.sqlite3"
    probe_jobs_refresh_s: int = Field(default=60, ge=10, le=3600)
    probe_ship_every_s: int = Field(default=10, ge=1, le=600)

    @field_validator("probe_node_token", "probe_signing_key")
    @classmethod
    def _secret(cls, value: SecretStr) -> SecretStr:
        problem = secret_problem(value.get_secret_value())
        if problem:
            raise ValueError(problem)
        return value

    @property
    def control_targets(self) -> list[str]:
        return [t.strip() for t in self.probe_control_targets.split(",") if t.strip()]


def load_probe_settings() -> ProbeSettings:
    try:
        return ProbeSettings()  # values come from the environment
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in e['loc']).upper()}: "
            f"{'is required' if e['type'] == 'missing' else e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("Invalid prober configuration:\n" + "\n".join(lines)) from None


def load_probe_settings_or_exit() -> ProbeSettings:
    try:
        return load_probe_settings()
    except ConfigError as exc:
        print(f"Refusing to start. {exc}", file=sys.stderr)
        print(
            "Register this node with: python -m sentinel.probe register <name> <location>",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
