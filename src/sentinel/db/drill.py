"""Restore drill: is a restored database usable? ``python -m sentinel.db.drill``.

Run against the restored copy (``DATABASE_URL`` pointing at it, see tools/restore-drill.sh):

1. the schema is at the latest migration,
2. every organization's data key and every sealed secret opens with
   CREDENTIALS_MASTER_KEY (``sentinel.tenancy.keys check``),
3. the site renders from it: the landing page, the methodology and every enabled
   public status page, in-process (nothing listens on a port).

Exit status 0 only when all three hold. Prints counts, never values.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from sentinel.config import Settings, load_settings_or_exit
from sentinel.db.migrate import make_config
from sentinel.db.session import Database
from sentinel.models import Organization, Product, User
from sentinel.tenancy.keys import check


async def schema_is_current(db: Database) -> tuple[bool, str]:
    head = ScriptDirectory.from_config(make_config()).get_current_head()
    async with db.engine.connect() as connection:
        current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    return current == head, f"schema {current} (latest {head})"


async def get_status(app: Any, path: str, host: str) -> int:
    """One in-process GET through the ASGI interface (no network, no HTTP client)."""
    status = 0

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host.encode())],
        "client": ("127.0.0.1", 0),
        "server": (host, 443),
    }
    await app(scope, receive, send)
    return status


async def pages_render(settings: Settings) -> tuple[bool, list[str]]:
    from sentinel.web.app import create_app

    app = create_app(settings)
    host = urlsplit(settings.public_base).netloc
    lines: list[str] = []
    ok = True
    async with app.router.lifespan_context(app):
        published = await app.state.services.status_pages.published()
        for path in ["/", "/yontem", *(f"/s/{slug}" for slug in published)]:
            status = await get_status(app, path, host)
            ok &= status == 200
            lines.append(f"{path}: {status}")
    return ok, lines


async def run(settings: Settings) -> int:
    db = Database(settings.database_url.get_secret_value(), pool_size=1)
    try:
        current, line = await schema_is_current(db)
        print(("✓ " if current else "✗ ") + line)
        async with db.system("restore drill: row counts") as s:
            counts = {
                model.__tablename__: await s.scalar(select(func.count()).select_from(model))
                for model in (Organization, User, Product)
            }
        print("rows: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        report = await check(db, settings.credentials_master_key.get_secret_value())
        print(
            ("✓ " if report.ok else "✗ ")
            + f"secrets: {report.organizations} data keys, {report.credentials} credentials, "
            + f"{report.channels} alert channels"
            + (f", {len(report.failures)} do not open" if report.failures else "")
        )
    finally:
        await db.dispose()
    rendered, lines = await pages_render(settings)
    print(("✓ " if rendered else "✗ ") + "pages: " + ", ".join(lines))
    return 0 if current and report.ok and rendered else 1


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return asyncio.run(run(load_settings_or_exit()))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
