from __future__ import annotations

import pytest

from sentinel.config import SECRET_FIELDS, ConfigError, load_settings, load_settings_or_exit
from sentinel.runtime import run_service
from sentinel.web.app import create_app

SECRET_ENV = [name.upper() for name in SECRET_FIELDS]

PLACEHOLDERS = [
    "<SESSION_KEY>",
    "${SESSION_KEY}-and-enough-padding-to-be-long",
    "change-me-change-me-change-me-change-me",
    "Change_Me_Please_0123456789abcdefghijkl",
    "api-sentinel-default-key-change-me-0123456789",
    "your-secret-key-goes-here-0123456789abcdef",
    "this-is-an-example-value-0123456789abcdefgh",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "abababababababababababababababababababababab",
]


def test_valid_environment_loads(valid_env: dict[str, str]) -> None:
    settings = load_settings()
    assert settings.public_base == "https://status.example.invalid"
    assert settings.session_key.get_secret_value() == valid_env["SESSION_KEY"]


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize("name", SECRET_ENV)
def test_missing_secret_aborts(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ConfigError, match=f"{name}: is required"):
        load_settings()


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize("name", SECRET_ENV)
def test_short_secret_aborts(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "Zx9-short-but-random-q")
    with pytest.raises(ConfigError, match=f"{name}: .*at least 32 bytes"):
        load_settings()


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_placeholder_secret_aborts(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TOKEN_HASH_KEY", value)
    with pytest.raises(ConfigError, match="TOKEN_HASH_KEY") as caught:
        load_settings()
    assert value not in str(caught.value)


def test_reused_key_aborts(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> None:
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", valid_env["SESSION_KEY"])
    with pytest.raises(ConfigError, match="must all be different"):
        load_settings()


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize(
    ("name", "value"),
    [("CONTACT_EMAIL", "<CONTACT_EMAIL>"), ("PUBLIC_BASE_URL", "<PUBLIC_BASE_URL>")],
)
def test_unfilled_public_settings_abort(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load_settings()


def test_error_message_never_contains_secret_values(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]
) -> None:
    monkeypatch.setenv("INGEST_HMAC_KEY", "short-secret-value")
    monkeypatch.setenv("WEBHOOK_SIGNING_KEY", valid_env["SESSION_KEY"])
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "short-secret-value" not in message
    assert all(value not in message for value in valid_env.values() if len(value) > 30)


@pytest.mark.usefixtures("valid_env")
def test_entry_point_exits_with_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SESSION_KEY")
    with pytest.raises(SystemExit) as caught:
        load_settings_or_exit()
    assert caught.value.code == 2
    assert "Refusing to start" in capsys.readouterr().err


@pytest.mark.usefixtures("valid_env")
def test_web_app_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_KEY", "<SESSION_KEY>")
    with pytest.raises(ConfigError):
        create_app()


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize("service", ["prober", "engine"])
def test_services_fail_closed(monkeypatch: pytest.MonkeyPatch, service: str) -> None:
    monkeypatch.delenv("INGEST_HMAC_KEY")
    with pytest.raises(SystemExit) as caught:
        run_service(service)
    assert caught.value.code == 2
