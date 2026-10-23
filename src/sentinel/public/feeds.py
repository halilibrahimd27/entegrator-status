"""The Atom feed and the SVG badge of a public status page."""

from __future__ import annotations

import html
from datetime import datetime

from sentinel.public.service import PublicPage
from sentinel.web.i18n import t

BADGE_COLORS = {"operational": "#2e7d32", "upstream": "#b26a00", "incident": "#c62828"}


def escape(value: str) -> str:
    """Text for XML element content and attributes (we only write XML, never parse it)."""
    return html.escape(value, quote=True)


def quoteattr(value: str) -> str:
    return f'"{escape(value)}"'


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def atom(page: PublicPage, page_url: str) -> str:
    """Every update of the page's incidents (last 30 days), newest first."""
    entries: list[tuple[datetime, str]] = []
    for item in page.incidents:
        incident = item.view.incident
        for update in item.view.updates:
            title = f"{item.product}: {incident.title} — {t(f'incident_status.{update.status}')}"
            entries.append(
                (
                    update.created_at,
                    "  <entry>\n"
                    f"    <id>urn:uuid:{update.id}</id>\n"
                    f"    <title>{escape(title)}</title>\n"
                    f"    <updated>{_stamp(update.created_at)}</updated>\n"
                    f"    <link href={quoteattr(page_url)}/>\n"
                    f'    <content type="text">{escape(update.body)}</content>\n'
                    "  </entry>\n",
                )
            )
    entries.sort(key=lambda e: e[0], reverse=True)
    updated = entries[0][0] if entries else page.generated_at
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <id>{escape(page_url)}</id>\n"
        f"  <title>{escape(page.title)}</title>\n"
        f"  <updated>{_stamp(updated)}</updated>\n"
        f"  <link href={quoteattr(page_url)}/>\n"
        f"  <author><name>{escape(page.org_name)}</name></author>\n"
        + "".join(body for _, body in entries[:50])
        + "</feed>\n"
    )


def badge(page: PublicPage) -> str:
    """A flat two-part badge: "durum | Çalışıyor". Text only from i18n, colours fixed."""
    label = t("public.badge_label")
    message = t(f"public.state_{page.state}")
    left, right = 7 * len(label) + 12, 7 * len(message) + 12
    width = left + right
    color = BADGE_COLORS[page.state]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img"'
        f" aria-label={quoteattr(f'{label}: {message}')}>"
        f"<title>{escape(f'{label}: {message}')}</title>"
        f'<rect width="{left}" height="20" fill="#555"/>'
        f'<rect x="{left}" width="{right}" height="20" fill="{color}"/>'
        '<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="6" y="14">{escape(label)}</text>'
        f'<text x="{left + 6}" y="14">{escape(message)}</text>'
        "</g></svg>"
    )
