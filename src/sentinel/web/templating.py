"""Jinja2 rendering: autoescape on, undefined variables are errors, copy from tr.yaml."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse

from sentinel.tenancy.roles import Action, Role, can
from sentinel.web.i18n import t
from sentinel.web.session_auth import attach_pre_cookie, csrf_token

LOCAL_TZ = ZoneInfo("Europe/Istanbul")


def local_time(value: datetime | None) -> str:
    """UTC in storage, Europe/Istanbul at the presentation edge."""
    if value is None:
        return "—"
    return value.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def build_environment() -> Environment:
    env = Environment(
        loader=PackageLoader("sentinel.web", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["t"] = t
    env.filters["local"] = local_time
    # Templates hide controls with the same permission matrix the routes enforce.
    env.globals["can"] = lambda role, action: can(Role(role), Action(action))
    return env


class Renderer:
    def __init__(self) -> None:
        self.env = build_environment()

    def page(
        self,
        request: Request,
        template: str,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        **context: Any,
    ) -> HTMLResponse:
        values = {
            "viewer": getattr(request.state, "viewer", None),
            "csrf": csrf_token(request),
            "error": None,
            **context,
        }
        response = HTMLResponse(
            self.env.get_template(template).render(values), status_code=status, headers=headers
        )
        attach_pre_cookie(request, response)
        return response

    def message(
        self,
        request: Request,
        title: str,
        body: str,
        *,
        status: int = 200,
        links: Iterable[tuple[str, str]] = (),
    ) -> HTMLResponse:
        """A page with a heading, a sentence and optional ``(href, i18n key)`` links."""
        return self.page(
            request, "message.html", status=status, title=title, body=body, links=list(links)
        )
