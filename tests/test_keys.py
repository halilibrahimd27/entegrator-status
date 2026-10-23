"""Master key drills: every secret decrypts after a restore; rotation is all or nothing."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from conftest import random_secret
from probe_support import two_orgs
from sentinel.models import AlertChannel, AuditEvent, Credential, OrgDataKey
from sentinel.tenancy import keys
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices


async def seed(services: AppServices) -> uuid.UUID:
    org, _ = await two_orgs(services)
    scope = TenantScope(org, None, Role.OWNER)
    await services.credentials.create(
        scope,
        name="Portal",
        kind="bearer",
        host="portal.example.invalid",
        secret={"token": "t" + uuid.uuid4().hex},
        entitled=True,
    )
    await services.alerts.create_channel(
        scope, name="Kanca", kind="webhook", url="https://hooks.example/in"
    )
    return org


async def test_check_and_rotate(services: AppServices) -> None:
    await seed(services)
    current = services.settings.credentials_master_key.get_secret_value()
    report = await keys.check(services.db, current)
    assert (report.organizations, report.credentials, report.channels) == (1, 1, 1)
    assert report.ok

    new_key = random_secret()
    with pytest.raises(ValueError, match="equals"):
        await keys.rewrap(services.db, current, current)
    assert await keys.rewrap(services.db, current, new_key) == 1
    assert (await keys.check(services.db, new_key)).ok
    stale = await keys.check(services.db, current)
    assert not stale.ok  # the old key opens nothing any more
    assert stale.failures[0].startswith("data key of organization")
    async with services.db.system("test check") as s:
        actions = (await s.scalars(select(AuditEvent.action))).all()
    assert "platform.master_key_rotated" in actions

    with pytest.raises(keys.VaultError):  # wrong old key: nothing changes
        await keys.rewrap(services.db, current, random_secret())
    assert (await keys.check(services.db, new_key)).ok


async def test_check_names_rows_that_do_not_open(services: AppServices) -> None:
    await seed(services)
    current = services.settings.credentials_master_key.get_secret_value()
    async with services.db.system("test tamper") as s:
        credential = await s.scalar(select(Credential))
        channel = await s.scalar(select(AlertChannel))
        assert credential is not None
        assert channel is not None
        credential.sealed = channel.sealed  # a blob on the wrong row
        credential_id = credential.id
    report = await keys.check(services.db, current)
    assert report.failures == [f"credential {credential_id}"]
    assert report.channels == 1
    async with services.db.system("test tamper") as s:
        await s.execute(delete(OrgDataKey))
    assert len((await keys.check(services.db, current)).failures) == 2  # no data key


@pytest.mark.usefixtures("app")
def test_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert keys.main([]) == 2
    assert keys.main(["check"]) == 0
    assert "organizations: 0" in capsys.readouterr().out
    monkeypatch.delenv(keys.NEW_KEY_ENV, raising=False)
    assert keys.main(["rewrap"]) == 2  # no new key given
    monkeypatch.setenv(keys.NEW_KEY_ENV, "changeme-changeme-changeme-changeme")
    assert keys.main(["rewrap"]) == 2  # a placeholder is refused
    monkeypatch.setenv(keys.NEW_KEY_ENV, random_secret())
    assert keys.main(["rewrap"]) == 0
    assert "re-wrapped 0 data key(s)" in capsys.readouterr().out
