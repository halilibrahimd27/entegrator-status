"""Rendering a public status page; shared by the live route and the static export."""

from __future__ import annotations

from jinja2 import Environment

from sentinel.public.service import PublicPage


def render_page(
    env: Environment,
    page: PublicPage,
    *,
    page_url: str,
    feed_url: str,
    subscribe_url: str | None,
    method_url: str,
    assets: str,
) -> str:
    return env.get_template("public/page.html").render(
        page=page,
        page_url=page_url,
        feed_url=feed_url,
        subscribe_url=subscribe_url,
        method_url=method_url,
        assets=assets,
    )


def render_method(env: Environment, page: PublicPage, *, page_url: str, assets: str) -> str:
    return env.get_template("public/method.html").render(
        page=page, page_url=page_url, assets=assets
    )
