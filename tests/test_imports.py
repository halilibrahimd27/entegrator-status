"""Dependency imports: parsers (no I/O) and the preview/confirm flow."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from sentinel.catalog.imports import ImportService
from sentinel.catalog.parsers import (
    MAX_UPLOAD_BYTES,
    ImportFormatError,
    Purl,
    from_compose,
    from_cyclonedx,
    from_package_lock,
    from_poetry_lock,
    from_requirements,
    from_spdx,
    parse_purl,
    parse_upload,
)
from sentinel.db.session import Database
from sentinel.models import Component, Dependency, Organization, Product, Service, Source
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import PermissionDeniedError, TenantScope

# --- parsers ---------------------------------------------------------------------


def names(purls: list[Purl]) -> list[tuple[str, str | None]]:
    return [(p.base, p.version) for p in purls]


def test_purl_parsing() -> None:
    scoped = parse_purl("pkg:npm/%40scope/pkg@1.2.3")
    assert scoped is not None
    assert (scoped.namespace, scoped.name, scoped.version) == ("@scope", "pkg", "1.2.3")
    assert scoped.base == "pkg:npm/@scope/pkg"
    pypi = parse_purl("pkg:pypi/Django_REST.framework@3.15")
    assert pypi is not None
    assert pypi.base == "pkg:pypi/django-rest-framework"
    assert parse_purl("not-a-purl") is None
    assert parse_purl("pkg:npm/") is None


def test_cyclonedx_and_spdx() -> None:
    cdx = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "purl": "pkg:npm/react@18.3.1",
                "components": [{"purl": "pkg:npm/loose-envify@1.4.0"}],
            },
            {"name": "no purl"},
        ],
    }
    assert names(from_cyclonedx(cdx)) == [
        ("pkg:npm/react", "18.3.1"),
        ("pkg:npm/loose-envify", "1.4.0"),
    ]
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "externalRefs": [
                    {"referenceType": "purl", "referenceLocator": "pkg:pypi/httpx@0.28.1"}
                ]
            },
            {"externalRefs": [{"referenceType": "cpe23Type", "referenceLocator": "cpe:2.3:a:x"}]},
        ],
    }
    assert names(from_spdx(spdx)) == [("pkg:pypi/httpx", "0.28.1")]


def test_package_lock_v3_and_v1() -> None:
    v3 = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app"},
            "node_modules/react": {"version": "18.3.1"},
            "node_modules/@types/node": {"version": "22.0.0"},
            "node_modules/a/node_modules/b": {"version": "1.0.0"},
            "packages/local": {"link": True},
        },
    }
    assert sorted(names(from_package_lock(v3))) == [
        ("pkg:npm/@types/node", "22.0.0"),
        ("pkg:npm/b", "1.0.0"),
        ("pkg:npm/react", "18.3.1"),
    ]
    v1 = {
        "dependencies": {
            "left-pad": {"version": "1.3.0", "dependencies": {"x": {"version": "2.0.0"}}}
        }
    }
    assert names(from_package_lock(v1)) == [("pkg:npm/left-pad", "1.3.0"), ("pkg:npm/x", "2.0.0")]


def test_requirements_and_poetry() -> None:
    text = """
    # comment
    fastapi==0.142.2 \\
        --hash=sha256:abc
    uvicorn[standard]==0.54.0  # trailing comment
    httpx
    -r other.txt
    -e git+https://example.invalid/repo.git
    """
    assert names(from_requirements(text)) == [
        ("pkg:pypi/fastapi", "0.142.2"),
        ("pkg:pypi/uvicorn", "0.54.0"),
        ("pkg:pypi/httpx", None),
    ]
    lock = (
        '[[package]]\nname = "Pydantic_Core"\nversion = "2.50.1"\n\n'
        '[[package]]\nname = "anyio"\nversion = "4.15.1"\n'
    )
    assert names(from_poetry_lock(lock)) == [
        ("pkg:pypi/pydantic-core", "2.50.1"),
        ("pkg:pypi/anyio", "4.15.1"),
    ]


def test_compose_images() -> None:
    text = (
        """
x-common: &common
  restart: always
services:
  web:
    <<: *common
    image: ghcr.io/acme/web:1.4
  db:
    image: postgres:16-alpine@sha256:"""
        + "a" * 64
        + """
  cache:
    image: redis
  built:
    build: .
"""
    )
    assert names(from_compose(text)) == [
        ("pkg:docker/ghcr.io/acme/web", "1.4"),
        ("pkg:docker/postgres", "16-alpine"),
        ("pkg:docker/redis", "latest"),
    ]


@pytest.mark.parametrize(
    ("filename", "content", "key"),
    [
        ("notes.txt", b"hello", "imports.unknown_format"),
        ("bom.json", b'{"something": "else"}', "imports.unknown_format"),
        ("bom.json", b"[" * 100_000, "imports.unreadable"),
        ("package-lock.json", b"\xff\xfe", "imports.unreadable"),
        ("requirements.txt", b"# nothing here\n", "imports.empty"),
        ("requirements.txt", b"x" * (MAX_UPLOAD_BYTES + 1), "imports.too_large"),
        ("poetry.lock", b"[[package]\n", "imports.unreadable"),
    ],
)
def test_bad_uploads(filename: str, content: bytes, key: str) -> None:
    with pytest.raises(ImportFormatError) as caught:
        parse_upload(filename, content)
    assert caught.value.key == key


def test_duplicates_are_merged() -> None:
    items = parse_upload("requirements.txt", b"fastapi==1.0\nfastapi==1.0\nFastAPI==1.0\n")
    assert names(items) == [("pkg:pypi/fastapi", "1.0")]


# --- preview / confirm -----------------------------------------------------------


@dataclass
class Setup:
    a: TenantScope
    b: TenantScope
    product: uuid.UUID


@pytest.fixture
async def setup(db: Database) -> Setup:
    async with db.system("test fixture") as s:
        org_a, org_b = Organization(slug="a", name="A"), Organization(slug="b", name="B")
        s.add_all([org_a, org_b])
        await s.flush()
        shared = Service(
            org_id=None,
            slug="fastapi",
            name="FastAPI",
            kind="package",
            tags=[],
            purl="pkg:pypi/fastapi",
        )
        s.add(shared)
        await s.flush()
        s.add(Component(org_id=None, service_id=shared.id, slug="paket", name="fastapi", tags=[]))
        product = Product(org_id=org_a.id, slug="api", name="API")
        s.add(product)
        await s.flush()
        return Setup(
            a=TenantScope(org_id=org_a.id, user_id=None, role=Role.MEMBER),
            b=TenantScope(org_id=org_b.id, user_id=None, role=Role.MEMBER),
            product=product.id,
        )


async def test_preview_then_confirm(db: Database, redis_client: Redis, setup: Setup) -> None:
    imports = ImportService(db, redis_client)
    preview = await imports.preview(
        setup.a, setup.product, "requirements.txt", b"fastapi==0.142.2\nhttpx==0.28.1\n"
    )
    assert {(i.name, i.match) for i in preview.items} == {("fastapi", "shared"), ("httpx", "new")}
    ttl = await redis_client.ttl(f"import:{setup.a.org_id}:{preview.token}")
    assert 0 < ttl <= 900
    stored = await redis_client.get(f"import:{setup.a.org_id}:{preview.token}")
    assert stored is not None
    assert "fastapi" in json.loads(stored)["items"][0]["name"]

    result = await imports.confirm(setup.a, setup.product, preview.token, ip="")
    assert (result.linked, result.created_services, result.updated) == (2, 1, 0)

    async with db.tenant(setup.a) as s:
        edges = (
            await s.scalars(select(Dependency).where(Dependency.from_product_id == setup.product))
        ).all()
        assert sorted(e.usage["version"] for e in edges) == ["0.142.2", "0.28.1"]
        httpx_service = (
            await s.scalars(select(Service).where(Service.purl == "pkg:pypi/httpx"))
        ).one()
        assert httpx_service.org_id == setup.a.org_id
        kinds = sorted(
            (await s.scalars(select(Source.kind).where(Source.org_id == setup.a.org_id))).all()
        )
        assert kinds == ["advisory", "release"]  # for the new private package only

    with pytest.raises(ImportFormatError, match=r"imports\.expired"):
        await imports.confirm(setup.a, setup.product, preview.token, ip="")  # single use

    again = await imports.preview(
        setup.a, setup.product, "requirements.txt", b"fastapi==0.143.0\nhttpx==0.28.1\n"
    )
    assert {(i.name, i.match) for i in again.items} == {("fastapi", "shared"), ("httpx", "private")}
    second = await imports.confirm(setup.a, setup.product, again.token, ip="")
    assert (second.linked, second.created_services, second.updated) == (0, 0, 1)

    async with db.tenant(setup.b) as s:
        assert (await s.scalars(select(Service.slug))).all() == [
            "fastapi"
        ]  # A's packages are private


async def test_preview_is_bound_to_org_and_product(
    db: Database, redis_client: Redis, setup: Setup
) -> None:
    imports = ImportService(db, redis_client)
    preview = await imports.preview(setup.a, setup.product, "requirements.txt", b"httpx==0.28.1\n")
    with pytest.raises(ImportFormatError, match="not_found"):  # A's product, seen from B
        await imports.confirm(setup.b, setup.product, preview.token, ip="")
    # An unknown product answers "not found" before the token is spent...
    with pytest.raises(ImportFormatError, match="not_found"):
        await imports.confirm(setup.a, uuid.uuid4(), preview.token, ip="")
    # ...so the preview still works for its own product.
    assert (await imports.confirm(setup.a, setup.product, preview.token, ip="")).linked == 1


async def test_foreign_product_and_viewers(db: Database, redis_client: Redis, setup: Setup) -> None:
    imports = ImportService(db, redis_client)
    with pytest.raises(ImportFormatError, match="not_found"):
        await imports.preview(setup.b, setup.product, "requirements.txt", b"httpx==0.28.1\n")
    viewer = TenantScope(org_id=setup.a.org_id, user_id=None, role=Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        await imports.preview(viewer, setup.product, "requirements.txt", b"httpx==0.28.1\n")
