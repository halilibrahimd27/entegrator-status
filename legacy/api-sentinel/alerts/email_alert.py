"""Email alert kanalı — HTML tablo formatında bildirim gönderir.

Config-based çalışır: DB'deki alert config'den SMTP ayarlarını okur.
Geriye uyumluluk için env var'lardan da okuyabilir (fallback).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from alerts.base import BaseAlertChannel

logger = logging.getLogger(__name__)

# Severity renk eşlemesi
SEVERITY_COLORS: dict[str, str] = {
    "CRITICAL": "#dc3545",
    "WARNING": "#ffc107",
    "INFO": "#17a2b8",
}


class EmailAlertChannel(BaseAlertChannel):
    """Email alert kanalı.

    smtplib + MIME ile HTML tablo formatında email gönderir.
    Config öncelikli olarak details["_config"] dict'inden,
    bulunamazsa environment variable'lardan okunur.

    Attributes:
        channel_name: "email"
    """

    channel_name: str = "email"

    async def send(self, severity: str, title: str, message: str, details: dict[str, Any]) -> bool:
        """Email ile alert gönder.

        Args:
            severity: Severity seviyesi
            title: Alert başlığı
            message: Alert mesajı
            details: Ek detaylar (_config key'i ile SMTP ayarları içerebilir)

        Returns:
            Başarılıysa True
        """
        cfg = details.get("_config", {})

        # SMTP ayarlarını config'den al, yoksa env var'dan fallback
        smtp_host = cfg.get("smtp_host") or os.environ.get("SMTP_HOST", "")
        smtp_port = int(cfg.get("smtp_port") or os.environ.get("SMTP_PORT", "587"))
        smtp_user = cfg.get("username") or os.environ.get("SMTP_USER", "")
        smtp_pass = cfg.get("password") or os.environ.get("SMTP_PASS", "")
        from_addr = cfg.get("from_addr") or os.environ.get("SMTP_FROM", "sentinel@example.com")
        from_name = cfg.get("from_name", "")
        to_addr_raw = cfg.get("to_addr") or os.environ.get("SMTP_TO", "")
        use_tls = cfg.get("use_tls", True)
        use_ssl = cfg.get("use_ssl", False)

        if not smtp_host:
            logger.error("SMTP yapılandırması eksik: smtp_host")
            return False

        # Alıcıları parse et
        to_addrs = [a.strip() for a in to_addr_raw.split(",") if a.strip()]
        if not to_addrs:
            logger.error("E-posta alıcı listesi boş")
            return False

        provider = details.get("provider", "bilinmiyor")
        endpoint = details.get("endpoint", "bilinmiyor")

        # Subject
        subject = f"[API-SENTINEL] {severity} - {provider}/{endpoint}"

        # Gönderen formatı
        sender = f"{from_name} <{from_addr}>" if from_name else from_addr

        # HTML body oluştur
        color = SEVERITY_COLORS.get(severity, "#6c757d")
        changes = details.get("changes", [])

        # Değişiklik tablosu
        changes_html = ""
        if changes:
            rows = ""
            for change in changes:
                rows += (
                    "<tr>"
                    f'<td style="padding: 8px; border: 1px solid #dee2e6;">{change.get("path", "")}</td>'
                    f'<td style="padding: 8px; border: 1px solid #dee2e6;">{change.get("type", "")}</td>'
                    f'<td style="padding: 8px; border: 1px solid #dee2e6;">{change.get("old_type", "-")}</td>'
                    f'<td style="padding: 8px; border: 1px solid #dee2e6;">{change.get("new_type", "-")}</td>'
                    f'<td style="padding: 8px; border: 1px solid #dee2e6;">{change.get("severity", "")}</td>'
                    "</tr>"
                )
            changes_html = (
                '<table style="width: 100%; border-collapse: collapse; margin-top: 16px;">'
                "<thead>"
                '<tr style="background-color: #f8f9fa;">'
                '<th style="padding: 8px; border: 1px solid #dee2e6; text-align: left;">Path</th>'
                '<th style="padding: 8px; border: 1px solid #dee2e6; text-align: left;">Tip</th>'
                '<th style="padding: 8px; border: 1px solid #dee2e6; text-align: left;">Eski</th>'
                '<th style="padding: 8px; border: 1px solid #dee2e6; text-align: left;">Yeni</th>'
                '<th style="padding: 8px; border: 1px solid #dee2e6; text-align: left;">Severity</th>'
                "</tr>"
                "</thead>"
                f"<tbody>{rows}</tbody>"
                "</table>"
            )

        html_body = (
            "<html>"
            '<body style="font-family: Arial, sans-serif; color: #333;">'
            f'<div style="background-color: {color}; color: white; padding: 16px; border-radius: 4px;">'
            f'<h2 style="margin: 0;">{severity}: {title}</h2>'
            "</div>"
            '<div style="padding: 16px;">'
            f"<p><strong>Provider:</strong> {provider}</p>"
            f"<p><strong>Endpoint:</strong> {endpoint}</p>"
            f"<p><strong>Mesaj:</strong> {message}</p>"
            f'<p><strong>Özet:</strong> {details.get("summary", "-")}</p>'
            f"{changes_html}"
            "</div>"
            '<div style="padding: 16px; color: #6c757d; font-size: 12px;">'
            "<p>Bu mesaj API-SENTINEL tarafından otomatik olarak gönderilmiştir.</p>"
            "</div>"
            "</body>"
            "</html>"
        )

        # Email oluştur
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(to_addrs)

        # Plain text alternatifi
        plain_text = f"{severity}: {title}\n\nProvider: {provider}\nEndpoint: {endpoint}\n\n{message}"
        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port)
                if use_tls:
                    server.starttls()

            with server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(from_addr, to_addrs, msg.as_string())

            logger.info(f"Email alert gönderildi: {subject}")
            return True
        except smtplib.SMTPException as e:
            logger.error(f"Email alert gönderilemedi: {e}")
            return False
        except OSError as e:
            logger.error(f"Email bağlantı hatası: {e}")
            return False

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Config doğrulama.

        Args:
            config: Doğrulanacak config

        Returns:
            (geçerli_mi, mesaj) tuple
        """
        # Config-based doğrulama
        if config.get("smtp_host"):
            if not config.get("from_addr"):
                return False, "Gönderen e-posta adresi boş"
            if not config.get("to_addr"):
                return False, "Alıcı e-posta adresi boş"
            return True, "Email yapılandırması geçerli"

        # Env var fallback doğrulama
        smtp_host_env = config.get("smtp_host_env", "SMTP_HOST")
        if not os.environ.get(smtp_host_env):
            return False, f"SMTP sunucu adresi tanımlı değil"

        return True, "Email yapılandırması geçerli"

    @classmethod
    def required_env_vars(cls) -> list[str]:
        """Gerekli env değişkenleri (config-based çalışırken gerekli değil).

        Returns:
            Env var adları listesi
        """
        return []

    @classmethod
    def required_packages(cls) -> list[str]:
        """Gerekli pip paketleri (stdlib).

        Returns:
            Boş liste (smtplib stdlib)
        """
        return []
