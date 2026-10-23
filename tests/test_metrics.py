"""/metrics: off by default, bearer token only, aggregate counts; proxy settings."""

from __future__ import annotations

import httpx2
import pytest
from pydantic import SecretStr

from conftest import random_secret
from sentinel.config import ConfigError, load_settings
from sentinel.web.services import AppServices


async def test_metrics_are_off_without_a_token(http: httpx2.AsyncClient) -> None:
    assert (await http.get("/metrics")).status_code == 404
    assert (await http.get("/metrics", headers={"Authorization": "Bearer x"})).status_code == 404


async def test_metrics_with_a_token(http: httpx2.AsyncClient, services: AppServices) -> None:
    token = random_secret()
    services.settings = services.settings.model_copy(update={"metrics_token": SecretStr(token)})
    assert (await http.get("/metrics")).status_code == 404
    wrong = await http.get("/metrics", headers={"Authorization": f"Bearer {random_secret()}"})
    assert wrong.status_code == 404
    ok = await http.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "entegrator_organizations 0" in ok.text
    assert 'entegrator_alert_deliveries{status="failed"} 0' in ok.text
    assert "slug" not in ok.text  # aggregate counts, nothing per tenant


@pytest.mark.usefixtures("valid_env")
def test_proxy_and_metrics_settings_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", "172.30.0.10, 10.0.0.0/8")
    assert load_settings().web_trusted_proxies.startswith("172.30.0.10")
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", "caddy")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("METRICS_TOKEN", "changeme")
    with pytest.raises(ConfigError):
        load_settings()
