"""The restore drill: schema, secrets and pages on a (restored) database."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import browser
from sentinel.accounts.mail import Mail
from sentinel.db import drill
from sentinel.models import Credential
from sentinel.web.services import AppServices
from test_keys import seed
from test_public_page import setup_page


async def test_drill_passes_on_a_healthy_database(
    app: FastAPI,
    outbox: list[Mail],
    services: AppServices,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await seed(services)
    async with browser(app) as owner:
        slug, _, _ = await setup_page(app, outbox, owner)
    assert await drill.run(services.settings) == 0
    out = capsys.readouterr().out
    assert "✓ schema" in out
    assert "✓ secrets: 1 data keys, 1 credentials, 1 alert channels" in out
    assert f"/s/{slug}: 200" in out
    assert "/yontem: 200" in out


async def test_drill_fails_when_a_secret_does_not_open(
    services: AppServices, capsys: pytest.CaptureFixture[str]
) -> None:
    await seed(services)
    async with services.db.system("test tamper") as s:
        credential = await s.scalar(select(Credential))
        assert credential is not None
        credential.sealed = b"\x01" + b"0" * 40
    assert await drill.run(services.settings) == 1
    assert "1 do not open" in capsys.readouterr().out
