from __future__ import annotations

import asyncio
import os
import signal

import pytest
from fastapi.testclient import TestClient

from sentinel import runtime
from sentinel.config import load_settings
from sentinel.web.app import create_app
from sentinel.web.i18n import _flatten, messages, t


@pytest.mark.usefixtures("valid_env")
def test_healthz() -> None:
    with TestClient(create_app(load_settings())) as http:
        response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_served(path: str) -> None:
    with TestClient(create_app()) as http:
        assert http.get(path).status_code == 404


def test_turkish_copy_has_the_disclaimer() -> None:
    assert t("site.disclaimer") == "Bağımsız ölçümdür, resmî değildir"
    assert {f"status.{s}" for s in ("operational", "degraded", "down", "unverified")} <= set(
        messages()
    )


def test_copy_values_must_be_strings() -> None:
    with pytest.raises(TypeError, match=r"status\.down"):
        _flatten({"status": {"down": 3}}, "", {})


@pytest.mark.usefixtures("valid_env")
def test_service_runs_until_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stop_soon() -> None:
        asyncio.get_running_loop().call_later(0.01, os.kill, os.getpid(), signal.SIGTERM)
        await original()

    original = runtime.wait_for_shutdown
    monkeypatch.setattr(runtime, "wait_for_shutdown", stop_soon)
    runtime.run_service("prober")  # returns once SIGTERM is handled
