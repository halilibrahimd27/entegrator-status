"""Outgoing account e-mail (plain text). Alert e-mail lives in ``sentinel.alerts``."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Protocol

from sentinel.config import Settings

SITE_NAME = "Entegratör Status"


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    # extra headers (List-Unsubscribe and friends); values with CR/LF are refused
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in self.headers.items():
            if any(c in name + value for c in "\r\n"):
                raise ValueError(f"header {name!r} contains a line break")


class Mailer(Protocol):
    async def send(self, mail: Mail) -> None: ...


@dataclass
class MemoryMailer:
    """Keeps messages in memory. Refused in production by the settings validator."""

    outbox: list[Mail] = field(default_factory=list)

    async def send(self, mail: Mail) -> None:
        self.outbox.append(mail)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, mail: Mail) -> None:
        await asyncio.to_thread(self._send, mail)

    def _send(self, mail: Mail) -> None:
        s = self._settings
        message = EmailMessage()
        message["From"] = str(Address(SITE_NAME, addr_spec=s.mail_from))
        message["To"] = mail.to
        message["Subject"] = mail.subject
        for name, value in mail.headers.items():
            message[name] = value
        message.set_content(mail.text)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if s.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15, context=context)
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15)
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)


def build_mailer(settings: Settings) -> Mailer:
    if settings.mail_backend == "memory":
        return MemoryMailer()
    return SmtpMailer(settings)
