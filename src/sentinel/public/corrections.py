"""Correction requests: anyone may say a measurement or page is wrong; admins decide.

The form needs no account. It is rate limited per IP, carries a honeypot and the form
token, and asks for an e-mail address only if the sender wants an answer. Every
decision is audit-logged (platform audit log) and, when there is an address, sent back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import case, select

from sentinel import audit
from sentinel.accounts.mail import Mail, Mailer
from sentinel.accounts.throttle import Limit, Throttle
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import CorrectionRequest, CorrectionStatus
from sentinel.web.i18n import t

PER_IP = Limit("correction-ip", 5, 3600)
MAX_SUBJECT = 300
MAX_BODY = 4000
MAX_NOTE = 2000
QUEUE = 200


class CorrectionError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _contact(raw: str) -> str | None:
    address = raw.strip().lower()
    if not address:
        return None
    if not (3 <= len(address) <= 320 and address.count("@") == 1) or any(
        c in address for c in " \r\n<>,;\"'"
    ):
        raise CorrectionError("corrections.invalid_contact")
    return address


class CorrectionService:
    def __init__(
        self,
        db: Database,
        mailer: Mailer,
        throttle: Throttle,
        *,
        operator: str,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._mailer = mailer
        self._throttle = throttle
        self._operator = operator  # CONTACT_EMAIL: told about every new request
        self._clock = clock

    async def submit(self, *, subject: str, body: str, contact: str, ip: str) -> None:
        subject = " ".join(subject.split())
        body = body.strip()
        if not 1 <= len(subject) <= MAX_SUBJECT:
            raise CorrectionError("corrections.invalid_subject")
        if not 10 <= len(body) <= MAX_BODY:
            raise CorrectionError("corrections.invalid_body")
        address = _contact(contact)
        if not await self._throttle.allow(PER_IP, ip):
            raise CorrectionError("corrections.throttled")
        async with self._db.system("corrections: store one request") as s:
            row = CorrectionRequest(subject=subject, body=body, contact=address)
            s.add(row)
            await s.flush()
            request_id = row.id
        await self._mailer.send(
            Mail(
                to=self._operator,
                subject=t("corrections.operator_mail.subject").format(subject=subject[:120]),
                text=t("corrections.operator_mail.body").format(
                    subject=subject, body=body, id=request_id
                ),
            )
        )

    async def queue(self) -> list[CorrectionRequest]:
        async with self._db.system("corrections: the admin queue") as s:
            open_first = case((CorrectionRequest.status == CorrectionStatus.OPEN, 0), else_=1)
            return list(
                (
                    await s.scalars(
                        select(CorrectionRequest)
                        .order_by(open_first, CorrectionRequest.created_at.desc())
                        .limit(QUEUE)
                    )
                ).all()
            )

    async def decide(
        self, admin_id: uuid.UUID, request_id: uuid.UUID, decision: str, note: str, ip: str
    ) -> None:
        if decision not in (CorrectionStatus.ACCEPTED, CorrectionStatus.REJECTED):
            raise CorrectionError("corrections.invalid_decision")
        note = note.strip()
        if len(note) > MAX_NOTE:
            raise CorrectionError("corrections.invalid_note")
        async with self._db.system("corrections: decide one request") as s:
            row = await s.get(CorrectionRequest, request_id)
            if row is None or row.status != CorrectionStatus.OPEN:
                raise CorrectionError("not_found")
            row.status, row.note = decision, note or None
            row.decided_at, row.decided_by = self._clock(), admin_id
            audit.record(
                s,
                f"correction.{decision}",
                actor_user_id=admin_id,
                target_type="correction",
                target_id=row.id,
                details={"subject": row.subject[:120]},
                ip=ip,
            )
            contact, subject = row.contact, row.subject
        if contact:
            await self._mailer.send(
                Mail(
                    to=contact,
                    subject=t("corrections.answer_mail.subject"),
                    text=t(f"corrections.answer_mail.{decision}").format(
                        subject=subject, note=note or "—"
                    ),
                )
            )
