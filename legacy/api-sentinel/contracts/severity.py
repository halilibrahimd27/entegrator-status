"""Severity mapping modülü — değişiklik tipine göre severity belirler."""

import logging

logger = logging.getLogger(__name__)

# Severity kuralları (CLAUDE.md'den — değiştirme)
SEVERITY_MAP: dict[str, str] = {
    "field_removed": "CRITICAL",
    "type_changed": "CRITICAL",
    "array_item_schema_changed": "CRITICAL",
    "auth_changed": "CRITICAL",
    "nullable_changed_to_false": "WARNING",
    "required_field_added": "WARNING",
    "enum_value_removed": "WARNING",
    "depth_changed": "WARNING",
    "format_changed": "WARNING",
    "field_added": "INFO",
    "enum_value_added": "INFO",
    "status_code_changed_2xx": "INFO",
}

# Severity sıralama değerleri
SEVERITY_ORDER: dict[str, int] = {
    "CRITICAL": 3,
    "WARNING": 2,
    "INFO": 1,
}


def get_severity(change_type: str) -> str:
    """Değişiklik tipine göre severity döndür.

    Args:
        change_type: Değişiklik tipi (field_removed, type_changed vb.)

    Returns:
        Severity seviyesi (CRITICAL, WARNING, INFO)
    """
    severity = SEVERITY_MAP.get(change_type)
    if severity is None:
        logger.warning(f"Bilinmeyen değişiklik tipi: {change_type}, varsayılan INFO kullanılıyor")
        return "INFO"
    return severity


def is_higher_severity(severity_a: str, severity_b: str) -> bool:
    """severity_a, severity_b'den daha yüksek mi kontrol et.

    Args:
        severity_a: Karşılaştırılacak ilk severity
        severity_b: Karşılaştırılacak ikinci severity

    Returns:
        severity_a daha yüksekse True
    """
    return SEVERITY_ORDER.get(severity_a, 0) > SEVERITY_ORDER.get(severity_b, 0)


def get_max_severity(severities: list[str]) -> str:
    """Verilen severity listesindeki en yüksek severity'yi döndür.

    Args:
        severities: Severity listesi

    Returns:
        En yüksek severity
    """
    if not severities:
        return "INFO"

    max_severity = "INFO"
    for severity in severities:
        if is_higher_severity(severity, max_severity):
            max_severity = severity
    return max_severity
