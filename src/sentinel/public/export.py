"""Static export of a public status page, for GitHub Pages or any static host (ADR 0012).

    python -m sentinel.public.export <slug> <out_dir> --site-url https://<OWNER>.github.io/<REPO>
        [--subscribe-url <PUBLIC_BASE_URL>] [--stamp-minutes 60]

Writes ``index.html``, ``yontem.html``, ``akis.atom``, ``rozet.svg``, ``static/app.css``
and ``.nojekyll`` into ``out_dir`` and leaves every other file there alone (a Pages
repository may hold a ``CNAME`` or a README). Links between the files are relative; only
the canonical URL and the feed use ``--site-url``. Subscribing needs a running
instance that the internet can reach: pass ``--subscribe-url`` only then.

The "last updated" time is rounded down to ``--stamp-minutes``, so running the export
every few minutes changes the files only when something on the page changed, plus once
per stamp period: a publishing script can commit only when ``git diff`` is not empty.
See docs/STATIC_EXPORT.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from importlib import resources
from pathlib import Path

from sentinel.config import load_settings_or_exit
from sentinel.public.feeds import atom, badge
from sentinel.public.render import render_method, render_page
from sentinel.web.services import AppServices, build_services


class ExportError(Exception):
    pass


def _https(raw: str, what: str) -> str:
    value = raw.strip().rstrip("/")
    if not value.startswith("https://") or len(value) <= len("https://"):
        raise ExportError(f"{what} must be an https URL")
    return value


async def export(
    svc: AppServices,
    slug: str,
    out: Path,
    *,
    site_url: str,
    subscribe_url: str | None = None,
    stamp_minutes: int = 60,
) -> list[Path]:
    site = _https(site_url, "--site-url")
    instance = _https(subscribe_url, "--subscribe-url") if subscribe_url else None
    page = await svc.status_pages.page(slug)
    if page is None:
        raise ExportError(f"no enabled status page for {slug!r}")
    if not 1 <= stamp_minutes <= 1440:
        raise ExportError("--stamp-minutes must be between 1 and 1440")
    stamp = page.generated_at.replace(second=0, microsecond=0)
    page.generated_at = stamp - timedelta(minutes=(stamp.hour * 60 + stamp.minute) % stamp_minutes)
    env = svc.renderer.env
    files: dict[str, str] = {
        "index.html": render_page(
            env,
            page,
            page_url=f"{site}/",
            feed_url="akis.atom",
            subscribe_url=f"{instance}/s/{slug}/abone" if instance and page.subscriptions else None,
            method_url="yontem.html",
            assets="static",
        ),
        "yontem.html": render_method(env, page, page_url="index.html", assets="static"),
        "akis.atom": atom(page, f"{site}/"),
        "rozet.svg": badge(page),
        ".nojekyll": "",
        "static/app.css": resources.files("sentinel.web")
        .joinpath("static/app.css")
        .read_text(encoding="utf-8"),
    }
    return await asyncio.to_thread(_write, out, files)


def _write(out: Path, files: dict[str, str]) -> list[Path]:
    written = []
    for name, content in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)  # readers never see half a file
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.public.export")
    parser.add_argument("slug", help="the organization's slug (its page must be enabled)")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--site-url", required=True, help="where the files will be served")
    parser.add_argument("--subscribe-url", help="this instance's public URL, if reachable")
    parser.add_argument("--stamp-minutes", type=int, default=60, help="round 'updated' down")
    args = parser.parse_args(argv)
    settings = load_settings_or_exit()

    async def run() -> list[Path]:
        svc = build_services(settings)
        try:
            return await export(
                svc,
                args.slug,
                args.out_dir,
                site_url=args.site_url,
                subscribe_url=args.subscribe_url,
                stamp_minutes=args.stamp_minutes,
            )
        finally:
            await svc.aclose()

    try:
        written = asyncio.run(run())
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
