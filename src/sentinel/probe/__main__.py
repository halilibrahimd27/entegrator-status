"""``python -m sentinel.probe`` runs the prober; ``... register NAME LOCATION`` adds a node."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence


def register(name: str, location: str) -> int:
    from sentinel.config import load_settings_or_exit
    from sentinel.db.session import Database
    from sentinel.probe.nodes import NodeService

    settings = load_settings_or_exit()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            creds = await NodeService(db, settings).register(name, location)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        finally:
            await db.dispose()
        # Printed once; the core keeps only a hash of the token.
        print(f"PROBE_NODE_ID={creds.node_id}")
        print(f"PROBE_NODE_TOKEN={creds.token}")
        print(f"PROBE_SIGNING_KEY={creds.signing_key}")
        return 0

    return asyncio.run(run())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.probe")
    sub = parser.add_subparsers(dest="command")
    reg = sub.add_parser("register", help="register a probe node and print its credentials")
    reg.add_argument("name")
    reg.add_argument("location")
    args = parser.parse_args(argv)
    if args.command == "register":
        return register(args.name, args.location)
    from sentinel.probe.runner import main as run_prober

    run_prober()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
