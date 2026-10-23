"""``python -m sentinel.probe`` runs the prober.

``... register NAME LOCATION [--credentials]`` adds a node and prints its credentials;
``... disable NAME`` revokes a node (its token stops working); ``... list`` shows nodes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence


def register(name: str, location: str, *, credentials: bool = False) -> int:
    from sentinel.config import load_settings_or_exit
    from sentinel.db.session import Database
    from sentinel.probe.nodes import NodeService

    settings = load_settings_or_exit()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            creds = await NodeService(db, settings).register(
                name, location, credentials=credentials
            )
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


def manage(command: str, name: str | None = None) -> int:
    from sqlalchemy import select

    from sentinel import audit
    from sentinel.config import load_settings_or_exit
    from sentinel.db.session import Database
    from sentinel.models import ProbeNode

    settings = load_settings_or_exit()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            async with db.system(f"probe CLI: {command} nodes") as s:
                if command == "list":
                    for row in (await s.scalars(select(ProbeNode).order_by(ProbeNode.name))).all():
                        seen = row.last_seen_at.isoformat() if row.last_seen_at else "never"
                        flags = ("enabled" if row.enabled else "disabled") + (
                            ", credentials" if row.handles_credentials else ""
                        )
                        print(f"{row.name}\t{row.location}\t{flags}\tlast seen {seen}")
                    return 0
                node = await s.scalar(select(ProbeNode).where(ProbeNode.name == name))
                if node is None:
                    print(f"✗ no node named {name!r}", file=sys.stderr)
                    return 1
                node.enabled = False
                audit.record(
                    s,
                    "platform.probe_node_disabled",
                    target_type="probe_node",
                    target_id=node.id,
                    details={"name": node.name, "via": "cli"},
                )
                print(f"disabled {node.name}")
                return 0
        finally:
            await db.dispose()

    return asyncio.run(run())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.probe")
    sub = parser.add_subparsers(dest="command")
    reg = sub.add_parser("register", help="register a probe node and print its credentials")
    reg.add_argument("name")
    reg.add_argument("location")
    reg.add_argument(
        "--credentials",
        action="store_true",
        help="this node may run authenticated checks (receives sealed secrets, ADR 0011)",
    )
    sub.add_parser("disable", help="revoke a node: its token stops working").add_argument("name")
    sub.add_parser("list", help="list registered nodes")
    args = parser.parse_args(argv)
    if args.command == "register":
        return register(args.name, args.location, credentials=args.credentials)
    if args.command == "disable":
        return manage("disable", args.name)
    if args.command == "list":
        return manage("list")
    from sentinel.probe.runner import main as run_prober

    run_prober()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
