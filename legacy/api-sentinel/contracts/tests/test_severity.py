"""Severity modülü testleri."""

import pytest

from core.severity import get_severity, get_max_severity, is_higher_severity, SEVERITY_MAP


class TestGetSeverity:
    """get_severity fonksiyonu testleri."""

    def test_field_removed_is_critical(self) -> None:
        """field_removed CRITICAL olmalı."""
        assert get_severity("field_removed") == "CRITICAL"

    def test_type_changed_is_critical(self) -> None:
        """type_changed CRITICAL olmalı."""
        assert get_severity("type_changed") == "CRITICAL"

    def test_array_item_schema_changed_is_critical(self) -> None:
        """array_item_schema_changed CRITICAL olmalı."""
        assert get_severity("array_item_schema_changed") == "CRITICAL"

    def test_auth_changed_is_critical(self) -> None:
        """auth_changed CRITICAL olmalı."""
        assert get_severity("auth_changed") == "CRITICAL"

    def test_nullable_changed_to_false_is_warning(self) -> None:
        """nullable_changed_to_false WARNING olmalı."""
        assert get_severity("nullable_changed_to_false") == "WARNING"

    def test_field_added_is_info(self) -> None:
        """field_added INFO olmalı."""
        assert get_severity("field_added") == "INFO"

    def test_unknown_change_type_returns_info(self) -> None:
        """Bilinmeyen değişiklik tipi INFO dönmeli."""
        assert get_severity("unknown_type") == "INFO"

    def test_all_severity_map_entries(self) -> None:
        """Tüm severity map girdileri geçerli severity döndürmeli."""
        valid_severities = {"CRITICAL", "WARNING", "INFO"}
        for change_type, severity in SEVERITY_MAP.items():
            assert severity in valid_severities, f"{change_type} geçersiz severity: {severity}"


class TestIsHigherSeverity:
    """is_higher_severity fonksiyonu testleri."""

    def test_critical_higher_than_warning(self) -> None:
        """CRITICAL, WARNING'den yüksek olmalı."""
        assert is_higher_severity("CRITICAL", "WARNING") is True

    def test_critical_higher_than_info(self) -> None:
        """CRITICAL, INFO'dan yüksek olmalı."""
        assert is_higher_severity("CRITICAL", "INFO") is True

    def test_warning_higher_than_info(self) -> None:
        """WARNING, INFO'dan yüksek olmalı."""
        assert is_higher_severity("WARNING", "INFO") is True

    def test_info_not_higher_than_critical(self) -> None:
        """INFO, CRITICAL'dan yüksek olmamalı."""
        assert is_higher_severity("INFO", "CRITICAL") is False

    def test_same_severity_not_higher(self) -> None:
        """Aynı severity yüksek olmamalı."""
        assert is_higher_severity("CRITICAL", "CRITICAL") is False


class TestGetMaxSeverity:
    """get_max_severity fonksiyonu testleri."""

    def test_empty_list_returns_info(self) -> None:
        """Boş liste INFO dönmeli."""
        assert get_max_severity([]) == "INFO"

    def test_single_critical(self) -> None:
        """Tek CRITICAL döndürmeli."""
        assert get_max_severity(["CRITICAL"]) == "CRITICAL"

    def test_mixed_severities(self) -> None:
        """Karışık severity'lerde en yüksek olanı döndürmeli."""
        assert get_max_severity(["INFO", "WARNING", "CRITICAL"]) == "CRITICAL"

    def test_only_info(self) -> None:
        """Sadece INFO olduğunda INFO döndürmeli."""
        assert get_max_severity(["INFO", "INFO"]) == "INFO"
