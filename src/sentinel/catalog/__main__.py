"""``python -m sentinel.catalog validate|import|schema [PATH ...]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sentinel.catalog.importer import import_packs
from sentinel.catalog.pack import Pack, PackError, load_pack, pack_json_schema, validate_packs

DEFAULT_DIR = Path("catalog/packs")


def _load(paths: list[Path]) -> tuple[list[Pack], list[str]]:
    files = paths or sorted(DEFAULT_DIR.glob("*.yaml"))
    packs, problems = [], []
    for path in files:
        try:
            packs.append(load_pack(path))
        except (PackError, OSError) as exc:
            problems.append(f"{path}: {exc}")
    return packs, problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.catalog")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "import"):
        cmd = sub.add_parser(name)
        cmd.add_argument("paths", nargs="*", type=Path)
        if name == "import":
            cmd.add_argument("--apply", action="store_true", help="write (default: dry run)")
    sub.add_parser("schema")
    args = parser.parse_args(argv)

    if args.command == "schema":
        print(json.dumps(pack_json_schema(), indent=2, ensure_ascii=False))
        return 0

    packs, problems = _load(args.paths)
    problems += validate_packs(packs)
    if problems:
        for problem in problems:
            print(f"✗ {problem}", file=sys.stderr)
        return 1
    if args.command == "validate":
        services = sum(len(p.services) for p in packs)
        print(f"✓ {len(packs)} pack(s), {services} service(s) valid")
        return 0

    from sentinel.config import load_settings_or_exit
    from sentinel.db.session import Database

    settings = load_settings_or_exit()

    async def run() -> int:
        db = Database(settings.database_url.get_secret_value(), pool_size=1)
        try:
            plan = await import_packs(db, packs, apply=args.apply)
        finally:
            await db.dispose()
        for change in plan.changes:
            print(change)
        if not plan.changes:
            print("= no changes")
        if not args.apply:
            print("dry run: nothing written (use --apply)")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
