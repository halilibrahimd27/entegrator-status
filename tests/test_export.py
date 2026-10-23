"""Static export of a public status page (ADR 0012)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI

from org_support import browser
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.public import export as exporter
from sentinel.public.export import ExportError, export
from sentinel.web.services import AppServices
from test_public_page import setup_page

SITE = "https://pages.example.invalid/alfa-durum"


def text(root: Path, name: str) -> str:
    return (root / name).read_text()


def leftovers(root: Path) -> list[Path]:
    return list(root.rglob("*.tmp"))


async def test_export_writes_a_self_contained_site(
    app: FastAPI,
    outbox: list[Mail],
    services: AppServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with browser(app) as owner:
        slug, _, _ = await setup_page(app, outbox, owner)
    clock = iter([utcnow().replace(minute=10), utcnow().replace(minute=50)] * 3)
    monkeypatch.setattr(services.status_pages, "_clock", lambda: next(clock))
    (tmp_path / "CNAME").write_text("durum.example.invalid\n")
    written = await export(services, slug, tmp_path, site_url=SITE + "/")
    names = sorted(str(p.relative_to(tmp_path)) for p in written)
    assert names == [
        ".nojekyll",
        "akis.atom",
        "index.html",
        "rozet.svg",
        "static/app.css",
        "yontem.html",
    ]
    assert text(tmp_path, "CNAME") == "durum.example.invalid\n"  # left alone
    index = text(tmp_path, "index.html")
    assert f'<link rel="canonical" href="{SITE}/">' in index
    assert 'href="static/app.css"' in index
    assert 'href="yontem.html"' in index
    assert 'href="akis.atom"' in index
    assert "E-postayla haber al" not in index  # no reachable instance given
    assert not re.search(r'(href|src)="/(?!/)', index)  # nothing absolute to a missing host
    assert "/o/" not in index
    assert "en az iki sağlıklı ölçüm noktasının" in text(tmp_path, "yontem.html")
    assert f"<id>{SITE}/</id>" in text(tmp_path, "akis.atom")
    assert leftovers(tmp_path) == []

    again = await export(
        services, slug, tmp_path, site_url=SITE, subscribe_url="https://status.example.invalid/"
    )
    assert len(again) == 6
    first = text(tmp_path, "index.html")
    await export(
        services, slug, tmp_path, site_url=SITE, subscribe_url="https://status.example.invalid/"
    )
    assert text(tmp_path, "index.html") == first  # nothing changed: same bytes, no commit
    index = text(tmp_path, "index.html")
    assert f'href="https://status.example.invalid/s/{slug}/abone"' in index


async def test_export_refuses_bad_input(
    app: FastAPI, outbox: list[Mail], services: AppServices, tmp_path: Path
) -> None:
    with pytest.raises(ExportError):
        await export(services, "olmayan", tmp_path, site_url=SITE)
    with pytest.raises(ExportError):
        await export(services, "olmayan", tmp_path, site_url="http://pages.example.invalid")
    with pytest.raises(ExportError):
        await export(services, "olmayan", tmp_path, site_url=SITE, subscribe_url="ftp://x")
    async with browser(app) as owner:
        slug, _, _ = await setup_page(app, outbox, owner)
    with pytest.raises(ExportError):
        await export(services, slug, tmp_path, site_url=SITE, stamp_minutes=0)


@pytest.mark.usefixtures("app")
def test_cli_reports_a_missing_page(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = exporter.main(["olmayan", str(tmp_path), "--site-url", SITE])
    assert code == 1
    assert "no enabled status page" in capsys.readouterr().err
