"""Abstract Alert Channel — tüm alert kanallarının temel sınıfı."""

from abc import ABC, abstractmethod
from typing import Any


class BaseAlertChannel(ABC):
    """Her alert kanalı bu class'tan türer.

    Attributes:
        channel_name: alerts.yaml'daki tanımlayıcı
    """

    channel_name: str = ""

    @abstractmethod
    async def send(self, severity: str, title: str, message: str, details: dict[str, Any]) -> bool:
        """Alert gönderir.

        Args:
            severity: Severity seviyesi (CRITICAL, WARNING, INFO)
            title: Alert başlığı
            message: Alert mesajı
            details: Ek detaylar

        Returns:
            Başarılıysa True
        """
        ...

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Kanal config'ini doğrular.

        Args:
            config: Doğrulanacak config

        Returns:
            (geçerli_mi, mesaj) tuple
        """
        ...

    @classmethod
    def required_env_vars(cls) -> list[str]:
        """Bu kanal için gerekli env değişkenleri.

        Returns:
            Env var adları listesi
        """
        return []

    @classmethod
    def required_packages(cls) -> list[str]:
        """Bu kanal için gerekli pip paketleri.

        Returns:
            Paket adları listesi
        """
        return []
