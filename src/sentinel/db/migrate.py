"""Alembic, configured in code (no alembic.ini): ``python -m sentinel.db <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

SCRIPT_LOCATION = "sentinel.db:migrations"


def make_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if database_url:
        config.attributes["database_url"] = database_url
    return config


def _run(connection: Connection, action: str, revision: str) -> None:
    config = make_config()
    config.attributes["connection"] = connection
    getattr(command, action)(config, revision)


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    """Upgrade from inside a running event loop (services, tests)."""
    async with engine.begin() as connection:
        await connection.run_sync(_run, "upgrade", revision)


async def downgrade(engine: AsyncEngine, revision: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_run, "downgrade", revision)


def main(argv: Sequence[str] | None = None) -> None:
    from sentinel.config import load_settings_or_exit

    parser = argparse.ArgumentParser(prog="python -m sentinel.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upgrade").add_argument("revision", nargs="?", default="head")
    sub.add_parser("downgrade").add_argument("revision")
    sub.add_parser("current")
    rev = sub.add_parser("revision")
    rev.add_argument("-m", "--message", required=True)
    rev.add_argument("--autogenerate", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings_or_exit()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = make_config(settings.database_url.get_secret_value())
    if args.command == "upgrade":
        command.upgrade(config, args.revision)
    elif args.command == "downgrade":
        command.downgrade(config, args.revision)
    elif args.command == "current":
        command.current(config, verbose=True)
    else:
        command.revision(config, message=args.message, autogenerate=args.autogenerate)
    sys.stdout.flush()
