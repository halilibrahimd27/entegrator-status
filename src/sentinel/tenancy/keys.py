"""Master key drills: check that every secret decrypts, and rotate the master key.

    python -m sentinel.tenancy.keys check
    NEW_CREDENTIALS_MASTER_KEY=… python -m sentinel.tenancy.keys rewrap

``check`` unwraps every organization's data key with CREDENTIALS_MASTER_KEY and opens
every credential and alert channel secret with it: run it after a restore. ``rewrap``
re-wraps every data key under the new master key in one transaction and verifies each
one before committing; the secrets themselves are untouched (they are sealed with the
data keys). Afterwards put the new key in .env as CREDENTIALS_MASTER_KEY and restart.
The new key is read from the environment, never from the command line (shell history).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from sentinel import audit
from sentinel.config import load_settings_or_exit, secret_problem
from sentinel.db.session import Database
from sentinel.models import AlertChannel, Credential, OrgDataKey
from sentinel.security.vault import Vault, VaultError

__all__ = ["NEW_KEY_ENV", "KeyReport", "VaultError", "check", "main", "rewrap"]

NEW_KEY_ENV = "NEW_CREDENTIALS_MASTER_KEY"


@dataclass
class KeyReport:
    organizations: int = 0
    credentials: int = 0
    channels: int = 0
    failures: list[str] = field(default_factory=list)  # row ids only, never values

    @property
    def ok(self) -> bool:
        return not self.failures


async def check(db: Database, master_key: str) -> KeyReport:
    vault = Vault(master_key)
    report = KeyReport()
    async with db.system("key drill: open every organization's secrets") as s:
        keys: dict[object, bytes] = {}
        for wrapped in (await s.scalars(select(OrgDataKey))).all():
            try:
                keys[wrapped.org_id] = vault.unwrap(wrapped.wrapped, wrapped.org_id)
                report.organizations += 1
            except VaultError:
                report.failures.append(f"data key of organization {wrapped.org_id}")
        sealed: list[tuple[str, Credential | AlertChannel]] = [
            ("credential", c) for c in (await s.scalars(select(Credential))).all()
        ]
        sealed += [
            ("alert channel", c)
            for c in (
                await s.scalars(select(AlertChannel).where(AlertChannel.sealed.is_not(None)))
            ).all()
        ]
        for label, row in sealed:
            key = keys.get(row.org_id)
            if key is None or row.sealed is None:
                report.failures.append(f"{label} {row.id}: no data key")
                continue
            try:
                Vault.open_secret(key, row.sealed, row.org_id, row.id)
            except VaultError:
                report.failures.append(f"{label} {row.id}")
                continue
            if label == "credential":
                report.credentials += 1
            else:
                report.channels += 1
    return report


async def rewrap(db: Database, old_key: str, new_key: str) -> int:
    """Re-wrap every data key under ``new_key``; all or nothing. Returns how many."""
    if old_key == new_key:
        raise ValueError("the new master key equals the current one")
    old, new = Vault(old_key), Vault(new_key)
    async with db.system("key drill: re-wrap every data key under a new master key") as s:
        rows = (await s.scalars(select(OrgDataKey))).all()
        for row in rows:
            data_key = old.unwrap(row.wrapped, row.org_id)  # VaultError aborts everything
            row.wrapped = new.wrap(data_key, row.org_id)
            if new.unwrap(row.wrapped, row.org_id) != data_key:  # pragma: no cover - paranoia
                raise VaultError("re-wrapped key does not round-trip")
        audit.record(
            s,
            "platform.master_key_rotated",
            target_type="org_data_keys",
            details={"organizations": len(rows), "via": "cli"},
        )
        return len(rows)


def _print(report: KeyReport) -> None:
    print(
        f"organizations: {report.organizations}, credentials: {report.credentials}, "
        f"alert channels: {report.channels}"
    )
    for failure in report.failures:
        print(f"✗ cannot decrypt: {failure}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["check"], ["rewrap"]):
        print("usage: python -m sentinel.tenancy.keys check|rewrap", file=sys.stderr)
        return 2
    settings = load_settings_or_exit()
    current = settings.credentials_master_key.get_secret_value()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            if args == ["check"]:
                report = await check(db, current)
                _print(report)
                return 0 if report.ok else 1
            new_key = os.environ.get(NEW_KEY_ENV, "")
            if problem := secret_problem(new_key):
                print(f"✗ {NEW_KEY_ENV} {problem}", file=sys.stderr)
                return 2
            try:
                count = await rewrap(db, current, new_key)
            except (ValueError, VaultError) as exc:
                print(f"✗ nothing changed: {exc}", file=sys.stderr)
                return 1
            report = await check(db, new_key)
            print(f"re-wrapped {count} data key(s) under the new master key")
            _print(report)
            print("Now set CREDENTIALS_MASTER_KEY to the new key in .env and restart.")
            return 0 if report.ok else 1
        finally:
            await db.dispose()

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
