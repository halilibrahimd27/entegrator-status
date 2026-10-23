"""``python -m sentinel.admin promote|demote EMAIL`` and ``list``: platform admins.

The first platform admin of an install is made here, on the server; afterwards the
same command adds or removes others. The account must exist and have a verified
e-mail address; on their next request a new admin is sent to set up TOTP first.
Every change goes to the platform audit log.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy import func, select

from sentinel import audit
from sentinel.config import load_settings_or_exit
from sentinel.db.session import Database
from sentinel.models import User


async def change(db: Database, email: str, admin: bool) -> str:
    """Returns a one-line result; raises ``LookupError`` for an unknown or unverified user."""
    async with db.system(f"admin CLI: {'promote' if admin else 'demote'} a platform admin") as s:
        user = await s.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or user.email_verified_at is None:
            raise LookupError("no verified account with that e-mail address")
        if not admin and user.is_platform_admin:
            others = await s.scalar(
                select(func.count())
                .select_from(User)
                .where(User.is_platform_admin.is_(True), User.id != user.id)
            )
            if not others:
                raise LookupError("refusing to remove the last platform admin")
        if user.is_platform_admin == admin:
            return "unchanged"
        user.is_platform_admin = admin
        audit.record(
            s,
            "platform.admin_granted" if admin else "platform.admin_revoked",
            target_type="user",
            target_id=user.id,
            details={"via": "cli"},
        )
        return "promoted" if admin else "demoted"


async def admins(db: Database) -> list[str]:
    async with db.system("admin CLI: list platform admins") as s:
        return list(
            (
                await s.scalars(
                    select(User.email).where(User.is_platform_admin.is_(True)).order_by(User.email)
                )
            ).all()
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("promote", "demote"):
        sub.add_parser(name).add_argument("email")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    settings = load_settings_or_exit()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            if args.command == "list":
                for email in await admins(db):
                    print(email)
                return 0
            try:
                print(await change(db, args.email, args.command == "promote"))
            except LookupError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                return 1
            return 0
        finally:
            await db.dispose()

    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
