"""Vendor status pages → snapshot items. Pure functions over parsed JSON.

Every provider is normalised to the same item types:

- ``page``: the overall indicator (``none | minor | major | critical | maintenance``)
- ``component``: a vendor component and its status (``operational | degraded |
  partial_outage | major_outage | maintenance``)
- ``incident`` / ``maintenance``: id, title, status, times, link, affected components

Only fields that mean something are kept: ``updated_at`` timestamps and incident
update texts are dropped, so a vendor touching a page is not a change. Parsers are
tolerant: unknown fields are ignored and missing ones become ``None``; a document
without the provider's expected shape raises ``StatusPageError``.
"""

from __future__ import annotations

from typing import Any

from sentinel.sources.feeds import clean_title, parse_date, safe_link

MAX_COMPONENTS = 60
MAX_EVENTS = 20


class StatusPageError(Exception):
    pass


STATUSPAGE_COMPONENT = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "partial_outage": "partial_outage",
    "major_outage": "major_outage",
    "under_maintenance": "maintenance",
}
INSTATUS_PAGE = {"UP": "none", "HASISSUES": "minor", "UNDERMAINTENANCE": "maintenance"}
INSTATUS_COMPONENT = {
    "OPERATIONAL": "operational",
    "DEGRADEDPERFORMANCE": "degraded",
    "PARTIALOUTAGE": "partial_outage",
    "MAJOROUTAGE": "major_outage",
    "UNDERMAINTENANCE": "maintenance",
}


def _str(value: Any, limit: int = 200) -> str:
    return clean_title(str(value))[:limit] if value is not None else ""


def _date(value: Any) -> str | None:
    return parse_date(str(value)) if value else None


def _names(components: Any) -> list[str]:
    if not isinstance(components, list):
        return []
    return sorted(
        {_str(c.get("name")) for c in components if isinstance(c, dict) and c.get("name")}
    )


def _event(kind: str, raw: dict[str, Any], base: str, **times: str | None) -> dict[str, Any]:
    return {
        "id": f"{kind[0]}:{_str(raw.get('id'), 100)}",
        "type": kind,
        "title": _str(raw.get("name"), 300),
        "status": _str(raw.get("status"), 40).lower(),
        "impact": _str(raw.get("impact"), 40).lower() or None,
        "link": safe_link(str(raw.get("shortlink") or raw.get("url") or ""), base),
        "components": _names(raw.get("components")),
        **times,
    }


def parse_statuspage(data: Any, base: str) -> list[dict[str, Any]]:
    """Atlassian Statuspage ``/api/v2/summary.json``."""
    if not isinstance(data, dict) or not isinstance(data.get("status"), dict):
        raise StatusPageError("not a Statuspage summary")
    status = data["status"]
    items: list[dict[str, Any]] = [
        {"id": "page", "type": "page", "indicator": _str(status.get("indicator"), 20) or "none"}
    ]
    for component in (data.get("components") or [])[:MAX_COMPONENTS]:
        if not isinstance(component, dict) or component.get("group"):
            continue  # groups only aggregate their children
        items.append(
            {
                "id": f"c:{_str(component.get('id'), 100)}",
                "type": "component",
                "name": _str(component.get("name")),
                "status": STATUSPAGE_COMPONENT.get(str(component.get("status")), "unknown"),
            }
        )
    for incident in (data.get("incidents") or [])[:MAX_EVENTS]:
        if isinstance(incident, dict):
            started = _date(incident.get("started_at") or incident.get("created_at"))
            items.append(_event("incident", incident, base, started=started))
    for window in (data.get("scheduled_maintenances") or [])[:MAX_EVENTS]:
        if isinstance(window, dict):
            items.append(
                _event(
                    "maintenance",
                    window,
                    base,
                    starts=_date(window.get("scheduled_for")),
                    ends=_date(window.get("scheduled_until")),
                )
            )
    return items


def parse_instatus(data: Any, base: str) -> list[dict[str, Any]]:
    """Instatus public ``/summary.json`` (page status, active incidents and maintenances)."""
    page = data.get("page") if isinstance(data, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get("status"), str):
        raise StatusPageError("not an Instatus summary")
    page_status = page["status"].upper()
    items: list[dict[str, Any]] = [
        {"id": "page", "type": "page", "indicator": INSTATUS_PAGE.get(page_status, "minor")}
    ]
    for component in (data.get("components") or [])[:MAX_COMPONENTS]:
        if isinstance(component, dict):
            items.append(
                {
                    "id": f"c:{_str(component.get('id'), 100)}",
                    "type": "component",
                    "name": _str(component.get("name")),
                    "status": INSTATUS_COMPONENT.get(
                        str(component.get("status", "")).upper(), "unknown"
                    ),
                }
            )
    for incident in (data.get("activeIncidents") or [])[:MAX_EVENTS]:
        if isinstance(incident, dict):
            items.append(_event("incident", incident, base, started=_date(incident.get("started"))))
    for window in (data.get("activeMaintenances") or [])[:MAX_EVENTS]:
        if isinstance(window, dict):
            items.append(
                _event(
                    "maintenance",
                    window,
                    base,
                    starts=_date(window.get("start")),
                    ends=_date(window.get("end")),
                )
            )
    return items
