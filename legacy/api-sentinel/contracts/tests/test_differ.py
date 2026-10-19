"""Schema differ testleri."""

import pytest

from core.differ import compare_schemas
from core.schema_extractor import extract_schema


class TestCompareSchemas:
    """compare_schemas fonksiyonu testleri."""

    def test_no_changes(self, v1_data: dict) -> None:
        """Değişiklik olmadığında sonuç testi."""
        schema = extract_schema(v1_data)
        result = compare_schemas(schema, schema, "trendyol", "send_invoice")

        assert result["has_changes"] is False
        assert result["provider"] == "trendyol"
        assert result["endpoint"] == "send_invoice"
        assert result["changes"] == []
        assert result["max_severity"] == "INFO"

    def test_breaking_changes(self, v1_data: dict, v2_breaking_data: dict) -> None:
        """Breaking change testi — birden fazla CRITICAL beklenir."""
        old_schema = extract_schema(v1_data)
        new_schema = extract_schema(v2_breaking_data)

        result = compare_schemas(old_schema, new_schema, "trendyol", "send_invoice")

        assert result["has_changes"] is True
        assert result["max_severity"] == "CRITICAL"

        # En az 5 CRITICAL değişiklik beklenir
        critical_changes = [c for c in result["changes"] if c["severity"] == "CRITICAL"]
        assert len(critical_changes) >= 5

        # field_removed türünde değişiklikler olmalı
        removed_types = [c["type"] for c in result["changes"]]
        assert "field_removed" in removed_types

    def test_safe_changes(self, v1_data: dict, v2_safe_data: dict) -> None:
        """Safe change testi — sadece INFO beklenir."""
        old_schema = extract_schema(v1_data)
        new_schema = extract_schema(v2_safe_data)

        result = compare_schemas(old_schema, new_schema, "trendyol", "send_invoice")

        assert result["has_changes"] is True
        assert result["max_severity"] == "INFO"

        # Sadece field_added değişiklikleri olmalı
        for change in result["changes"]:
            assert change["type"] == "field_added"
            assert change["severity"] == "INFO"

        # 2 yeni alan eklendi: trackingCode ve estimatedDelivery
        assert len(result["changes"]) == 2

    def test_field_removed_is_critical(self) -> None:
        """Alan kaldırma CRITICAL severity olmalı."""
        old = {"type": "object", "fields": {"name": {"type": "string", "nullable": False}}, "nullable": False}
        new = {"type": "object", "fields": {}, "nullable": False}

        result = compare_schemas(old, new, "test", "test")

        assert result["has_changes"] is True
        assert result["changes"][0]["type"] == "field_removed"
        assert result["changes"][0]["severity"] == "CRITICAL"

    def test_type_changed_is_critical(self) -> None:
        """Tip değişikliği CRITICAL severity olmalı."""
        old = {"type": "object", "fields": {"status": {"type": "string", "nullable": False}}, "nullable": False}
        new = {"type": "object", "fields": {"status": {"type": "integer", "nullable": False}}, "nullable": False}

        result = compare_schemas(old, new, "test", "test")

        assert result["has_changes"] is True
        type_changes = [c for c in result["changes"] if c["type"] == "type_changed"]
        assert len(type_changes) == 1
        assert type_changes[0]["severity"] == "CRITICAL"

    def test_field_added_is_info(self) -> None:
        """Alan ekleme INFO severity olmalı."""
        old = {"type": "object", "fields": {}, "nullable": False}
        new = {"type": "object", "fields": {"newField": {"type": "string", "nullable": False}}, "nullable": False}

        result = compare_schemas(old, new, "test", "test")

        assert result["has_changes"] is True
        assert result["changes"][0]["type"] == "field_added"
        assert result["changes"][0]["severity"] == "INFO"

    def test_summary_format(self, v1_data: dict, v2_breaking_data: dict) -> None:
        """Özet formatı testi."""
        old_schema = extract_schema(v1_data)
        new_schema = extract_schema(v2_breaking_data)

        result = compare_schemas(old_schema, new_schema, "trendyol", "send_invoice")

        assert "CRITICAL" in result["summary"]
        assert "WARNING" in result["summary"]
        assert "INFO" in result["summary"]

    def test_json_path_format(self) -> None:
        """JSONPath format testi."""
        old = {"type": "object", "fields": {"name": {"type": "string", "nullable": False}}, "nullable": False}
        new = {"type": "object", "fields": {}, "nullable": False}

        result = compare_schemas(old, new, "test", "test")

        assert result["changes"][0]["path"].startswith("$.")

    def test_timestamp_present(self) -> None:
        """Timestamp alanı testi."""
        old = {"type": "object", "fields": {}, "nullable": False}
        result = compare_schemas(old, old, "test", "test")

        assert "timestamp" in result

    def test_nested_field_changes(self) -> None:
        """İç içe alan değişikliği testi."""
        old = {
            "type": "object",
            "nullable": False,
            "fields": {
                "metadata": {
                    "type": "object",
                    "nullable": False,
                    "fields": {
                        "region": {"type": "string", "nullable": False},
                    },
                },
            },
        }
        new = {
            "type": "object",
            "nullable": False,
            "fields": {
                "metadata": {
                    "type": "object",
                    "nullable": False,
                    "fields": {
                        "region": {"type": "integer", "nullable": False},
                    },
                },
            },
        }

        result = compare_schemas(old, new, "test", "test")

        assert result["has_changes"] is True
        type_changes = [c for c in result["changes"] if c["type"] == "type_changed"]
        assert len(type_changes) == 1
        assert "metadata.region" in type_changes[0]["path"]
