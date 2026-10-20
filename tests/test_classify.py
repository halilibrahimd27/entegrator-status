"""Announcement classification (Turkish and English keyword tables) and effective dates."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sentinel.models import ChangeCategory as C
from sentinel.sources.classify import classify, effective_date, turkish_lower


@pytest.mark.parametrize(
    ("title", "category"),
    [
        # Turkish
        ("Planlı bakım çalışması hakkında", C.MAINTENANCE),
        ("Hafta sonu bakımı nedeniyle kesinti", C.MAINTENANCE),
        ("Portal 02:00-04:00 arası erişime kapalı olacaktır", C.MAINTENANCE),
        ("Eski servis adresi kullanımdan kaldırılacaktır", C.DEPRECATION),
        ("SOAP API desteği sona eriyor", C.DEPRECATION),
        ("UBL-TR 1.2.1 paketi yayımlandı", C.PACKAGE_UPDATE),
        ("Şematron kontrollerinde güncelleme", C.PACKAGE_UPDATE),
        ("e-Fatura kod listesi güncellendi", C.PACKAGE_UPDATE),
        ("XSLT şablonları değişti", C.PACKAGE_UPDATE),
        ("Teknik kılavuz yeni sürümü", C.PACKAGE_UPDATE),
        ("Güvenlik güncellemesi: oturum yönetimi", C.SECURITY),
        ("Fatura gönderiminde yaşanan arıza giderildi", C.INCIDENT),
        ("Portalda erişim sorunu", C.INCIDENT),  # "erişim" alone is not maintenance
        ("Yeni ofisimiz açıldı", C.OTHER),
        # upper case, Turkish dotted and dotless i
        ("PLANLI BAKIM", C.MAINTENANCE),
        ("ŞEMATRON GÜNCELLEMESİ", C.PACKAGE_UPDATE),
        # English
        ("Scheduled maintenance on 15 November", C.MAINTENANCE),
        ("API v1 is deprecated and will be retired", C.DEPRECATION),
        ("Sunset of the legacy endpoint", C.DEPRECATION),
        ("SECURITY ADVISORY: token leak fixed", C.SECURITY),
        ("CVE-2026-1234 patched", C.SECURITY),
        ("SDK 4.0 released", C.PACKAGE_UPDATE),
        ("Partial outage in eu-west", C.INCIDENT),
        ("We are hiring", C.OTHER),
        # precedence: a security notice about maintenance is security
        ("Güvenlik yaması için planlı bakım", C.SECURITY),
        ("Deprecated endpoints will be removed during maintenance", C.DEPRECATION),
    ],
)
def test_classify(title: str, category: C) -> None:
    assert classify(title) is category


def test_keywords_match_word_starts_only() -> None:
    assert classify("Abakımsız") is C.OTHER  # "bakım" inside another word
    assert turkish_lower("IŞIK İzmir") == "ışık izmir"


def midnight_tr(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=ZoneInfo("Europe/Istanbul")).astimezone(UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 Ocak 2027 tarihinden itibaren zorunlu", (2027, 1, 1)),
        ("15 KASIM 2026 bakım", (2026, 11, 15)),
        ("Bakım 15.11.2026 gecesi", (2026, 11, 15)),
        ("Effective 2026-12-01", (2026, 12, 1)),
        ("Retired on November 30, 2026", (2026, 11, 30)),
        ("API sunset: 31 March 2027", (2027, 3, 31)),
        ("between 05/01/2027 and 10/01/2027", (2027, 1, 5)),  # the first date wins
    ],
)
def test_effective_dates(text: str, expected: tuple[int, int, int]) -> None:
    assert effective_date(text) == midnight_tr(*expected)


@pytest.mark.parametrize(
    "text", ["Sürüm 2.10", "31.02.2026", "1 Smarch 2026", "Sabah 09:00", "1.1.1999"]
)
def test_no_effective_date(text: str) -> None:
    assert effective_date(text) is None
