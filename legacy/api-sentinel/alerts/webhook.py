"""Webhook Alert Kanalı — değişiklikleri kullanıcının belirttiği URL'ye POST eder.

Retry mekanizması: Başarısız isteklerde exponential backoff ile yeniden dener.
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from alerts.base import BaseAlertChannel

logger = logging.getLogger(__name__)

# Retry sabitleri
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # saniye


class WebhookAlertChannel(BaseAlertChannel):
    """Generic Webhook alert kanalı.

    Kullanıcının belirttiği URL'ye JSON payload ile POST isteği gönderir.
    Custom header desteği vardır.

    Attributes:
        channel_name: "webhook"
    """

    channel_name: str = "webhook"

    async def send(
        self,
        severity: str,
        title: str,
        message: str,
        details: dict[str, Any],
    ) -> bool:
        """Webhook URL'sine alert payload'ını POST et.

        Args:
            severity: Severity seviyesi (CRITICAL, WARNING, INFO)
            title: Alert başlığı
            message: Alert mesajı
            details: Ek detaylar

        Returns:
            Başarılıysa True
        """
        # Config'i al — _config üzerinden DB kayıtlı config gelir
        config = details.get("_config", {})
        webhook_url = config.get("webhook_url", "")

        if not webhook_url:
            logger.error("Webhook URL tanımlanmamış")
            return False

        # Custom header'ları parse et
        custom_headers: dict[str, str] = {}
        headers_raw = config.get("headers", "")
        if headers_raw:
            if isinstance(headers_raw, dict):
                custom_headers = headers_raw
            elif isinstance(headers_raw, str):
                for line in headers_raw.strip().split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        custom_headers[key.strip()] = value.strip()

        # Payload oluştur
        payload = {
            "severity": severity,
            "title": title,
            "message": message,
            "provider": details.get("provider", ""),
            "endpoint": details.get("endpoint", ""),
            "summary": details.get("summary", ""),
            "changes": details.get("changes", []),
            "max_severity": details.get("max_severity", severity),
            "source": "api-sentinel",
        }

        # Secret key varsa ekle
        secret = config.get("secret", "")
        if secret:
            import hashlib
            import hmac
            payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
            signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            custom_headers["X-Sentinel-Signature"] = f"sha256={signature}"

        # Retry ayarları (config'den okunabilir)
        max_retries = int(config.get("max_retries", MAX_RETRIES))
        retry_delay = float(config.get("retry_delay", RETRY_BASE_DELAY))
        headers = {"Content-Type": "application/json", **custom_headers}
        last_error: str = ""

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        webhook_url,
                        json=payload,
                        headers=headers,
                    )
                    if response.status_code < 400:
                        logger.info(f"Webhook gönderildi: {webhook_url} (HTTP {response.status_code})")
                        return True

                    last_error = f"HTTP {response.status_code} — {response.text[:200]}"
                    logger.warning(
                        f"Webhook başarısız (deneme {attempt}/{max_retries}): {last_error}"
                    )

            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                logger.warning(f"Webhook timeout (deneme {attempt}/{max_retries}): {e}")
            except httpx.ConnectError as e:
                last_error = f"Bağlantı hatası: {e}"
                logger.warning(f"Webhook bağlantı hatası (deneme {attempt}/{max_retries}): {e}")
            except Exception as e:
                last_error = str(e)
                logger.error(f"Webhook gönderim hatası (deneme {attempt}/{max_retries}): {e}")

            # Son denemede değilsek bekle (exponential backoff)
            if attempt < max_retries:
                wait = retry_delay * (2 ** (attempt - 1))
                logger.debug(f"Webhook retry bekleniyor: {wait:.1f}s")
                await asyncio.sleep(wait)

        logger.error(f"Webhook tüm denemeler başarısız ({max_retries}x): {last_error}")
        return False

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Webhook config doğrula.

        Args:
            config: Doğrulanacak config

        Returns:
            (geçerli_mi, mesaj) tuple
        """
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            return False, "webhook_url zorunludur"

        if not webhook_url.startswith(("http://", "https://")):
            return False, "webhook_url geçerli bir URL olmalıdır"

        return True, "Config geçerli"

    @classmethod
    def required_packages(cls) -> list[str]:
        """Gerekli paketler.

        Returns:
            Paket listesi
        """
        return ["httpx"]
