"""Catalog packs: loading, validation, schema, import, CLI."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select

from sentinel.catalog.__main__ import main as catalog_cli
from sentinel.catalog.importer import InvalidPacksError, import_packs
from sentinel.catalog.pack import (
    MAX_PACK_BYTES,
    Pack,
    PackError,
    load_pack,
    pack_json_schema,
    validate_packs,
)
from sentinel.db.session import Database
from sentinel.models import (
    AuditEvent,
    CatalogTagRule,
    Component,
    Dependency,
    Organization,
    Service,
    Source,
)
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope

ROOT = Path(__file__).resolve().parents[1]
PACKS = sorted((ROOT / "catalog" / "packs").glob("*.yaml"))


def pack(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "test",
        "name": "Test",
        "services": [
            {
                "slug": "alpha",
                "name": "Alpha",
                "kind": "saas_api",
                "components": [
                    {
                        "slug": "api",
                        "name": "API",
                        "tags": ["efatura_service"],
                        "sources": [
                            {
                                "kind": "http",
                                "config": {"url": "https://api.alpha.example.invalid/"},
                                "source": "test",
                            }
                        ],
                    }
                ],
            },
            {
                "slug": "beta",
                "name": "Beta",
                "kind": "cloud",
                "components": [{"slug": "region", "name": "R"}],
            },
        ],
        "edges": [{"from": "alpha/api", "to": "beta/region"}],
    }
    data.update(overrides)
    return data


def parsed(data: dict[str, Any]) -> Pack:
    return Pack.model_validate(data)


def test_shipped_packs_are_valid() -> None:
    assert {p.name for p in PACKS} >= {"examples.yaml", "tr-efatura.yaml"}
    assert validate_packs([load_pack(p) for p in PACKS]) == []


def test_shipped_packs_use_no_real_hosts() -> None:
    for path in PACKS:
        for match in re.findall(r"https?://([^/\s\"']+)", path.read_text()):
            assert match.endswith(".example.invalid"), (path.name, match)


def test_committed_schema_is_current() -> None:
    committed = json.loads((ROOT / "catalog" / "pack.schema.json").read_text())
    assert committed == pack_json_schema(), "run `make catalog-schema`"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["services"].append(dict(d["services"][0])), "service slug also in pack"),
        (
            lambda d: d["services"][0]["components"].append(
                dict(d["services"][0]["components"][0])
            ),
            "duplicate component slug",
        ),
        (
            lambda d: d["services"][0]["components"][0]["sources"][0]["config"].update(
                url="http://api.alpha.example.invalid/"
            ),
            "scheme 'http'",
        ),
        (
            lambda d: d["services"][0]["components"][0]["sources"][0]["config"].update(
                url="https://10.1.2.3/"
            ),
            "blocked",
        ),
        (
            lambda d: d["services"][0]["components"][0]["sources"][0].update(interval_s=30),
            "interval_s",
        ),
        (
            lambda d: d["services"][0]["components"][0]["sources"].append(
                dict(d["services"][0]["components"][0]["sources"][0])
            ),
            "duplicate source",
        ),
        (
            lambda d: d["edges"].append({"from": "alpha/api", "to": "gamma/x"}),
            "unknown component gamma/x",
        ),
        (lambda d: d["edges"].append({"from": "beta/region", "to": "alpha/api"}), "cycle"),
        (lambda d: d["edges"].append({"from": "alpha/api", "to": "alpha/api"}), "self edge"),
        (
            lambda d: d.update(tag_rules=[{"tag": "efatura_service", "to": "nope/x"}]),
            "unknown component",
        ),
    ],
)
def test_validation_refuses(mutate: Any, message: str) -> None:
    data = pack()
    mutate(data)
    problems = validate_packs([parsed(data)])
    assert any(message in p for p in problems), problems


def test_tag_rules_count_as_edges_for_cycles() -> None:
    data = pack(tag_rules=[{"tag": "efatura_service", "to": "beta/region"}])
    data["services"][1]["components"][0]["tags"] = ["efatura_service"]
    data["edges"] = [{"from": "beta/region", "to": "alpha/api"}]
    assert any("cycle" in p for p in validate_packs([parsed(data)]))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {
                "id": "x",
                "name": "X",
                "services": [
                    {
                        "slug": "a",
                        "name": "A",
                        "kind": "x",
                        "components": [{"slug": "c", "name": "C"}],
                    }
                ],
            },
            "kind",
        ),
        ({"id": "x", "name": "X", "unexpected": 1}, "unexpected"),
        ({"id": "Bad Id", "name": "X"}, "id"),
        (
            {
                "id": "x",
                "name": "X",
                "services": [{"slug": "a", "name": "A", "kind": "cloud", "components": []}],
            },
            "components",
        ),
        (
            {
                "id": "x",
                "name": "X",
                "services": [
                    {
                        "slug": "a",
                        "name": "A",
                        "kind": "cloud",
                        "components": [
                            {
                                "slug": "c",
                                "name": "C",
                                "sources": [
                                    {
                                        "kind": "http",
                                        "config": {"url": "https://a.example.invalid/"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "source",
        ),
    ],
)
def test_malformed_packs_are_refused(tmp_path: Path, data: dict[str, Any], message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(PackError, match=message):
        load_pack(path)


def test_yaml_aliases_are_refused(tmp_path: Path) -> None:
    bomb = "a: &a [x, x, x]\nb: &b [*a, *a, *a]\nc: [*b, *b, *b]\n"
    path = tmp_path / "bomb.yaml"
    path.write_text(bomb, encoding="utf-8")
    with pytest.raises(PackError, match="aliases are not allowed"):
        load_pack(path)


def test_oversized_pack_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.yaml"
    path.write_text("# " + "x" * MAX_PACK_BYTES, encoding="utf-8")
    with pytest.raises(PackError, match="larger than"):
        load_pack(path)


async def counts(db: Database) -> dict[str, int]:
    async with db.system("test check") as s:
        return {
            model.__name__: int(await s.scalar(select(func.count()).select_from(model)) or 0)
            for model in (Service, Component, Source, Dependency, CatalogTagRule)
        }


async def test_import_dry_run_then_apply_then_idempotent(db: Database) -> None:
    packs = [load_pack(p) for p in PACKS]
    dry = await import_packs(db, packs, apply=False)
    assert dry.changes
    assert set((await counts(db)).values()) == {0}  # nothing written

    applied = await import_packs(db, packs, apply=True)
    assert [str(c) for c in applied.changes] == [str(c) for c in dry.changes]
    after = await counts(db)
    assert after["Service"] == 4
    assert after["Source"] == 4
    assert after["CatalogTagRule"] == 1

    again = await import_packs(db, packs, apply=True)
    assert again.changes == []
    assert await counts(db) == after

    async with db.system("test check") as s:
        audit = (
            await s.scalars(select(AuditEvent).where(AuditEvent.action == "catalog.imported"))
        ).all()
    assert len(audit) == 2  # applied runs only, platform-level
    assert all(e.org_id is None for e in audit)


async def test_import_updates_and_applies_tag_rules(db: Database) -> None:
    await import_packs(db, [parsed(pack())], apply=True)
    changed = pack(tag_rules=[{"tag": "efatura_service", "to": "beta/region"}])
    changed["services"][0]["name"] = "Alpha Renamed"
    changed["edges"] = []
    plan = await import_packs(db, [parsed(changed)], apply=True)
    rendered = [str(c) for c in plan.changes]
    assert "~ service alpha (name)" in rendered
    assert "+ tag rule efatura_service -> beta/region" in rendered
    # The edge from the first import already exists, so the tag rule adds nothing new.
    assert not any("(tag rule)" in r for r in rendered)


async def test_import_refuses_invalid_packs(db: Database) -> None:
    data = pack()
    data["edges"].append({"from": "beta/region", "to": "alpha/api"})
    with pytest.raises(InvalidPacksError, match="cycle"):
        await import_packs(db, [parsed(data)], apply=True)


async def test_imported_rows_are_shared_with_every_tenant(db: Database) -> None:
    await import_packs(db, [parsed(pack())], apply=True)
    async with db.system("test setup") as s:
        orgs = [Organization(slug=f"o{i}", name=f"O{i}") for i in range(2)]
        s.add_all(orgs)
        await s.flush()
        ids = [o.id for o in orgs]
    for org_id in ids:
        scope = TenantScope(org_id=org_id, user_id=uuid.uuid4(), role=Role.VIEWER)
        async with db.tenant(scope) as s:
            assert sorted((await s.scalars(select(Service.slug))).all()) == ["alpha", "beta"]


def test_cli_validate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert catalog_cli(["validate", *map(str, PACKS)]) == 0
    assert "valid" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    data = pack()
    data["edges"].append({"from": "alpha/api", "to": "nowhere/x"})
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert catalog_cli(["validate", str(bad)]) == 1
    assert "unknown component" in capsys.readouterr().err


def test_cli_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert catalog_cli(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Entegratör Status catalog pack"
