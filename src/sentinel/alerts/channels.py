"""Messages and the four channel senders: e-mail, Slack-compatible, signed webhook, Telegram.

Rewritten from api-sentinel's alert channels (ADR 0002) with their problems fixed: every POST goes
through the SSRF guard with no redirects; the webhook signature covers the exact bytes
sent, with a timestamp (``sha256=HMAC(secret, "<timestamp>." + body)``); e-mail carries
``List-Unsubscribe`` (one click, RFC 8058); errors are short codes and never contain a
URL (Slack and Telegram URLs are secrets).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sentinel.accounts.mail import Mail, Mailer
from sentinel.models import AlertEvent
from sentinel.risk.digest import Digest
from sentinel.security.errors import OutboundError
from sentinel.security.http import FetchPolicy, OutboundClient, Timeouts
from sentinel.web.i18n import t

SIGNATURE_HEADER = "X-Sentinel-Signature"
TIMESTAMP_HEADER = "X-Sentinel-Timestamp"
EVENT_HEADER = "X-Sentinel-Event"
_TIMEOUTS = Timeouts(connect_s=5.0, read_s=10.0, total_s=20.0)
_LOCAL = ZoneInfo("Europe/Istanbul")


def _local(value: datetime | None) -> str:
    return value.astimezone(_LOCAL).strftime("%d.%m.%Y %H:%M") if value else "—"


class DeliveryError(Exception):
    """A send failed; ``str()`` is a short code (``http_500``, ``connect_timeout``)."""


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    payload: dict[str, Any]


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return (
        "sha256="
        + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )


def unsubscribe_token(key: str, recipient_id: uuid.UUID) -> str:
    mac = hmac.new(key.encode(), b"unsubscribe:" + recipient_id.bytes, hashlib.sha256).hexdigest()
    return f"{recipient_id.hex}.{mac[:32]}"


def check_unsubscribe(key: str, token: str) -> uuid.UUID | None:
    raw, _, _ = token.partition(".")
    try:
        recipient_id = uuid.UUID(hex=raw)
    except ValueError:
        return None
    return (
        recipient_id if hmac.compare_digest(unsubscribe_token(key, recipient_id), token) else None
    )


def render(event: AlertEvent, panel_url: str) -> Message:
    severity = t(f"impact.{event.severity}")
    reason = t(f"impact_reasons.{event.reason}")
    subject = f"[{severity}] {reason}: {event.title}"[:200]
    lines = [reason, event.title, "", t("alerts.path") + ": " + " → ".join(event.path)]
    if event.link:
        lines.append(t("alerts.source") + ": " + event.link)
    lines += [t("alerts.panel") + ": " + panel_url, "", t("site.disclaimer")]
    payload = {
        "event": str(event.id),
        "type": event.subject_type,
        "phase": event.phase,
        "severity": event.severity,
        "reason": event.reason,
        "title": event.title,
        "path": event.path,
        "link": event.link,
        "occurred_at": event.occurred_at.isoformat(),
        "product_id": str(event.product_id) if event.product_id else None,
    }
    return Message(subject, "\n".join(lines), payload)


def render_notice(event: AlertEvent, page_url: str) -> Message:
    """An organization's own incident update, for the subscribers of its public page."""
    status = t(f"incident_status.{event.detail.get('status', 'investigating')}")
    product = event.path[0] if event.path else ""
    subject = f"{product}: {event.title} — {status}"[:200]
    lines = [event.title, status, "", str(event.detail.get("body", "")), "", page_url]
    payload = {
        "event": str(event.id),
        "type": "notice",
        "product": product,
        "title": event.title,
        "status": event.detail.get("status"),
        "body": event.detail.get("body"),
        "occurred_at": event.occurred_at.isoformat(),
    }
    return Message(subject, "\n".join(lines), payload)


def render_batch(events: list[AlertEvent], panel_url: str) -> Message:
    """One message for a day's events (channels in daily mode)."""
    parts = [render(e, panel_url) for e in events]
    subject = t("alerts.daily_subject").format(count=len(parts))
    text = "\n\n".join(
        f"- {p.subject}\n  {' → '.join(e.path)}" for p, e in zip(parts, events, strict=True)
    )
    return Message(
        subject,
        text + f"\n\n{t('alerts.panel')}: {panel_url}",
        {"events": [p.payload for p in parts]},
    )


def render_digest(digest: Digest, org_name: str, panel_url: str) -> Message:
    """The weekly digest as plain text, with the same sections as the digest page."""
    lines = [f"{_local(digest.start)} - {_local(digest.end)}", ""]
    if digest.empty:
        lines.append(t("digest.quiet"))
    if digest.changes:
        lines.append(t("digest.changes"))
        for i in digest.changes:
            reason = t(f"impact_reasons.{i.reason}")
            lines.append(f"- [{t(f'impact.{i.severity}')}] {reason}: {i.title}")
            lines.append(f"  {' → '.join(i.path)}")
        lines.append("")
    if digest.outages:
        lines.append(t("digest.outages"))
        for o in digest.outages:
            end = _local(o.resolved_at) if o.resolved_at else t("incident_status.open")
            lines.append(f"- {o.component}: {_local(o.started_at)} - {end}")
        lines.append("")
    if digest.due:
        lines.append(t("digest.due"))
        for e in digest.due:
            lines.append(f"- {_local(e.risk.due_at)} {t(f'risks.{e.risk.kind}')} · {e.component}")
        lines.append("")
    if digest.planned:
        lines.append(t("digest.planned"))
        for item in digest.planned:
            title = t(f"risks.{item.title}") if item.kind == "risk" else item.title
            lines.append(f"- {_local(item.when)} {title} · {item.component}")
        lines.append("")
    lines += [t("alerts.panel") + ": " + panel_url, "", t("site.disclaimer")]
    subject = t("alerts.weekly_subject").format(org=org_name)[:200]
    payload = {
        "type": "weekly_digest",
        "start": digest.start.isoformat(),
        "end": digest.end.isoformat(),
        "changes": len(digest.changes),
        "outages": len(digest.outages),
        "due": len(digest.due),
        "planned": len(digest.planned),
    }
    return Message(subject, "\n".join(lines), payload)


class Senders:
    def __init__(
        self, http: OutboundClient, mailer: Mailer, *, telegram_token: str | None = None
    ) -> None:
        self._http = http
        self._mailer = mailer
        self._telegram = telegram_token

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        await self._post_bytes(url, body, headers or {})

    async def _post_bytes(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        policy = FetchPolicy(
            method="POST",
            json_body=body,
            delivery=True,
            max_redirects=0,
            max_bytes=64 * 1024,
            timeouts=_TIMEOUTS,
            headers=headers,
        )
        try:
            result = await self._http.fetch(url, policy)
        except OutboundError as exc:
            raise DeliveryError(exc.kind) from None
        if not 200 <= result.status_code < 300:
            raise DeliveryError(f"http_{result.status_code}")

    async def email(self, to: str, message: Message, unsubscribe_url: str) -> None:
        headers = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        text = message.text + "\n\n" + t("alerts.unsubscribe") + ": " + unsubscribe_url
        try:
            await self._mailer.send(
                Mail(to=to, subject=message.subject, text=text, headers=headers)
            )
        except OSError as exc:
            raise DeliveryError(f"smtp_{type(exc).__name__}") from None

    async def slack(self, url: str, message: Message) -> None:
        await self._post(url, {"text": f"*{message.subject}*\n{message.text}"})

    async def webhook(self, url: str, secret: str, message: Message, event_id: str) -> None:
        body = json.dumps(message.payload, separators=(",", ":"), ensure_ascii=False).encode()
        timestamp = int(time.time())
        headers = {
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign(secret, timestamp, body),  # over the bytes we send
            EVENT_HEADER: event_id,
        }
        await self._post_bytes(url, body, headers)

    async def telegram(self, chat_id: str, text: str) -> None:
        if not self._telegram:
            raise DeliveryError("telegram_disabled")
        url = f"https://api.telegram.org/bot{self._telegram}/sendMessage"
        await self._post(
            url, {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        )
