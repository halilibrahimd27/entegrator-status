"""Schema karşılaştırma modülü — iki schema arasındaki farkları bulur."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from deepdiff import DeepDiff

from core.severity import get_severity

logger = logging.getLogger(__name__)

# Değişiklik tipi → okunabilir etiket ve açıklama eşlemesi
_CHANGE_TYPE_LABELS: dict[str, str] = {
    "field_removed": "Field Removed",
    "field_added": "Field Added",
    "type_changed": "Type Changed",
    "nullable_changed_to_false": "Nullable Changed",
    "array_item_schema_changed": "Array Schema Changed",
    "auth_changed": "Auth Changed",
    "format_changed": "Format Changed",
    "enum_value_removed": "Enum Value Removed",
    "enum_value_added": "Enum Value Added",
    "required_field_added": "Required Field Added",
    "depth_changed": "Depth Changed",
    "status_code_changed_2xx": "Status Code Changed",
}

_CHANGE_TYPE_DESCRIPTIONS: dict[str, str] = {
    "field_removed": "Bu alan API response'undan tamamen kaldirildi. Bu alana bagli istemciler hata alabilir.",
    "field_added": "API response'una yeni bir alan eklendi. Mevcut istemciler etkilenmez.",
    "type_changed": "Bu alanin veri tipi degisti. Tip uyumsuzlugu istemcilerde parse hatasina yol acabilir.",
    "nullable_changed_to_false": "Bu alan artik null deger kabul etmiyor. null bekleyen istemciler etkilenebilir.",
    "array_item_schema_changed": "Array icindeki nesnelerin yapisi degisti. Listelemeye bagli istemciler etkilenebilir.",
    "auth_changed": "Kimlik dogrulama yapisi degisti.",
    "format_changed": "Alanin formati degisti (ornek: date -> datetime).",
    "enum_value_removed": "Izin verilen degerlerden biri kaldirildi.",
    "enum_value_added": "Izin verilen degerlere yeni bir deger eklendi.",
    "required_field_added": "Yeni bir zorunlu alan eklendi.",
    "depth_changed": "Nesnelerin icleme derinligi degisti.",
    "status_code_changed_2xx": "HTTP basari durum kodu degisti.",
}


def _field_summary(field_data: dict[str, Any]) -> str:
    """Bir field'ın kısa yapısal özetini oluştur.

    Args:
        field_data: Schema field verisi

    Returns:
        Okunabilir field özeti
    """
    parts: list[str] = []

    field_type = field_data.get("type", "unknown")
    parts.append(f"type: {field_type}")

    if field_data.get("nullable"):
        parts.append("nullable: true")

    if field_type == "object" and "fields" in field_data:
        sub_fields = list(field_data["fields"].keys())
        if sub_fields:
            if len(sub_fields) <= 5:
                parts.append(f"fields: [{', '.join(sub_fields)}]")
            else:
                parts.append(f"fields: [{', '.join(sub_fields[:5])}, ... +{len(sub_fields) - 5}]")

    if field_type == "array" and "items" in field_data:
        items = field_data["items"]
        if items is not None:
            items_type = items.get("type", "unknown")
            if items_type == "object" and "fields" in items:
                item_fields = list(items["fields"].keys())
                if len(item_fields) <= 5:
                    parts.append(f"items: {items_type} [{', '.join(item_fields)}]")
                else:
                    parts.append(f"items: {items_type} [{', '.join(item_fields[:5])}, ... +{len(item_fields) - 5}]")
            else:
                parts.append(f"items: {items_type}")
        else:
            parts.append("items: empty")

    if "sample" in field_data:
        sample = str(field_data["sample"])
        if len(sample) > 50:
            sample = sample[:50] + "..."
        parts.append(f'sample: "{sample}"')

    return "{ " + ", ".join(parts) + " }"


def _field_to_json_preview(field_data: dict[str, Any], max_depth: int = 2) -> dict[str, Any]:
    """Bir field'ın JSON benzeri önizlemesini oluştur.

    Args:
        field_data: Schema field verisi
        max_depth: Maksimum derinlik

    Returns:
        JSON preview dict
    """
    result: dict[str, Any] = {"type": field_data.get("type", "unknown")}

    if field_data.get("nullable"):
        result["nullable"] = True

    if "sample" in field_data:
        result["sample"] = field_data["sample"]

    if max_depth > 0:
        if field_data.get("type") == "object" and "fields" in field_data:
            sub_fields = {}
            for name, sub_data in field_data["fields"].items():
                sub_fields[name] = sub_data.get("type", "unknown")
            result["fields"] = sub_fields

        if field_data.get("type") == "array" and "items" in field_data:
            items = field_data["items"]
            if items is not None:
                if items.get("type") == "object" and "fields" in items:
                    sub_fields = {}
                    for name, sub_data in items["fields"].items():
                        sub_fields[name] = sub_data.get("type", "unknown")
                    result["items"] = {"type": "object", "fields": sub_fields}
                else:
                    result["items"] = {"type": items.get("type", "unknown")}
            else:
                result["items"] = {"type": "empty"}

    return result


def _flatten_changes(old_schema: dict, new_schema: dict) -> list[dict[str, Any]]:
    """İki schema arasındaki değişiklikleri düzleştirilmiş liste olarak döndür.

    Args:
        old_schema: Eski schema
        new_schema: Yeni schema

    Returns:
        Değişiklik listesi
    """
    changes: list[dict[str, Any]] = []

    old_fields = old_schema.get("fields", {})
    new_fields = new_schema.get("fields", {})

    # Kaldırılan alanlar
    for field_name in old_fields:
        if field_name not in new_fields:
            old_field = old_fields[field_name]
            old_type = old_field.get("type", "unknown")
            change_type = "field_removed"
            change = {
                "type": change_type,
                "change_type": _CHANGE_TYPE_LABELS.get(change_type, change_type),
                "path": f"$.{field_name}",
                "old_type": old_type,
                "new_type": None,
                "old_value": _field_summary(old_field),
                "new_value": None,
                "old_preview": json.dumps(_field_to_json_preview(old_field), indent=2, ensure_ascii=False),
                "new_preview": None,
                "severity": get_severity(change_type),
                "message": f"Alan kaldirildi: {field_name} ({old_type})",
                "description": _CHANGE_TYPE_DESCRIPTIONS.get(change_type, ""),
            }
            changes.append(change)

    # Eklenen alanlar
    for field_name in new_fields:
        if field_name not in old_fields:
            new_field = new_fields[field_name]
            new_type = new_field.get("type", "unknown")
            change_type = "field_added"
            change = {
                "type": change_type,
                "change_type": _CHANGE_TYPE_LABELS.get(change_type, change_type),
                "path": f"$.{field_name}",
                "old_type": None,
                "new_type": new_type,
                "old_value": None,
                "new_value": _field_summary(new_field),
                "old_preview": None,
                "new_preview": json.dumps(_field_to_json_preview(new_field), indent=2, ensure_ascii=False),
                "severity": get_severity(change_type),
                "message": f"Yeni alan: {field_name} ({new_type})",
                "description": _CHANGE_TYPE_DESCRIPTIONS.get(change_type, ""),
            }
            changes.append(change)

    # Ortak alanlardaki değişiklikler
    for field_name in old_fields:
        if field_name in new_fields:
            old_field = old_fields[field_name]
            new_field = new_fields[field_name]

            # Tip değişikliği
            old_type = old_field.get("type", "unknown")
            new_type = new_field.get("type", "unknown")
            if old_type != new_type:
                change_type = "type_changed"
                change = {
                    "type": change_type,
                    "change_type": _CHANGE_TYPE_LABELS.get(change_type, change_type),
                    "path": f"$.{field_name}",
                    "old_type": old_type,
                    "new_type": new_type,
                    "old_value": _field_summary(old_field),
                    "new_value": _field_summary(new_field),
                    "old_preview": json.dumps(_field_to_json_preview(old_field), indent=2, ensure_ascii=False),
                    "new_preview": json.dumps(_field_to_json_preview(new_field), indent=2, ensure_ascii=False),
                    "severity": get_severity(change_type),
                    "message": f"Tip degisti: {field_name} ({old_type} -> {new_type})",
                    "description": _CHANGE_TYPE_DESCRIPTIONS.get(change_type, ""),
                }
                changes.append(change)

            # Nullable değişikliği
            old_nullable = old_field.get("nullable", False)
            new_nullable = new_field.get("nullable", False)
            if old_nullable != new_nullable:
                if old_nullable and not new_nullable:
                    change_type = "nullable_changed_to_false"
                else:
                    change_type = "field_added"  # nullable olması INFO seviyesinde
                change = {
                    "type": change_type,
                    "change_type": _CHANGE_TYPE_LABELS.get(change_type, change_type),
                    "path": f"$.{field_name}",
                    "old_type": f"nullable={old_nullable}",
                    "new_type": f"nullable={new_nullable}",
                    "old_value": f"nullable: {str(old_nullable).lower()}",
                    "new_value": f"nullable: {str(new_nullable).lower()}",
                    "old_preview": None,
                    "new_preview": None,
                    "severity": get_severity(change_type),
                    "message": f"Nullable degisti: {field_name} ({old_nullable} -> {new_nullable})",
                    "description": _CHANGE_TYPE_DESCRIPTIONS.get(change_type, ""),
                }
                changes.append(change)

            # İç içe object karşılaştırma
            if old_type == "object" and new_type == "object":
                nested = _flatten_changes(old_field, new_field)
                for nested_change in nested:
                    # Path'i güncelle
                    nested_path = nested_change["path"].replace("$.", "", 1)
                    nested_change["path"] = f"$.{field_name}.{nested_path}"
                    changes.append(nested_change)

            # Array item schema karşılaştırma
            if old_type == "array" and new_type == "array":
                old_items = old_field.get("items")
                new_items = new_field.get("items")

                if old_items and new_items:
                    if old_items.get("type") == "object" and new_items.get("type") == "object":
                        nested = _flatten_changes(old_items, new_items)
                        for nested_change in nested:
                            nested_path = nested_change["path"].replace("$.", "", 1)
                            nested_change["path"] = f"$.{field_name}[].{nested_path}"
                            # Array item schema değişikliği seviyeyi yükselt
                            if nested_change["type"] in ("field_removed", "type_changed"):
                                nested_change["severity"] = get_severity("array_item_schema_changed")
                            changes.append(nested_change)
                    elif old_items.get("type") != new_items.get("type"):
                        change_type = "array_item_schema_changed"
                        change = {
                            "type": change_type,
                            "change_type": _CHANGE_TYPE_LABELS.get(change_type, change_type),
                            "path": f"$.{field_name}[]",
                            "old_type": old_items.get("type", "unknown"),
                            "new_type": new_items.get("type", "unknown"),
                            "old_value": _field_summary(old_items),
                            "new_value": _field_summary(new_items),
                            "old_preview": json.dumps(_field_to_json_preview(old_items), indent=2, ensure_ascii=False),
                            "new_preview": json.dumps(_field_to_json_preview(new_items), indent=2, ensure_ascii=False),
                            "severity": get_severity(change_type),
                            "message": f"Array item tipi degisti: {field_name} ({old_items.get('type')} -> {new_items.get('type')})",
                            "description": _CHANGE_TYPE_DESCRIPTIONS.get(change_type, ""),
                        }
                        changes.append(change)

    return changes


def _build_summary(changes: list[dict]) -> str:
    """Değişiklik özeti oluştur.

    Args:
        changes: Değişiklik listesi

    Returns:
        Özet metni (örn: "2 CRITICAL, 1 WARNING, 0 INFO")
    """
    counts = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for change in changes:
        severity = change.get("severity", "INFO")
        if severity in counts:
            counts[severity] += 1
    return f"{counts['CRITICAL']} CRITICAL, {counts['WARNING']} WARNING, {counts['INFO']} INFO"


def _get_max_severity(changes: list[dict]) -> str:
    """Değişiklikler arasındaki en yüksek severity'yi bul.

    Args:
        changes: Değişiklik listesi

    Returns:
        En yüksek severity (CRITICAL > WARNING > INFO)
    """
    severity_order = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}
    max_sev = "INFO"
    max_val = 0

    for change in changes:
        sev = change.get("severity", "INFO")
        val = severity_order.get(sev, 0)
        if val > max_val:
            max_val = val
            max_sev = sev

    return max_sev


def _compare_text_schemas(old_schema: dict, new_schema: dict) -> list[dict[str, Any]]:
    """Iki text schema'yi karsilastir.

    Text response'lar icin hash, uzunluk, satir sayisi gibi
    metadata farklarini tespit eder.

    Args:
        old_schema: Eski text schema
        new_schema: Yeni text schema

    Returns:
        Degisiklik listesi
    """
    changes: list[dict[str, Any]] = []

    old_hash = old_schema.get("text_hash", "")
    new_hash = new_schema.get("text_hash", "")

    # Hash farki yoksa degisiklik yok
    if old_hash == new_hash:
        return changes

    old_format = old_schema.get("format", "plain")
    new_format = new_schema.get("format", "plain")
    old_length = old_schema.get("text_length", 0)
    new_length = new_schema.get("text_length", 0)
    old_lines = old_schema.get("text_line_count", 0)
    new_lines = new_schema.get("text_line_count", 0)
    old_sample = old_schema.get("text_sample", "")
    new_sample = new_schema.get("text_sample", "")

    # Format degisti mi
    if old_format != new_format:
        changes.append({
            "type": "type_changed",
            "change_type": "Format Changed",
            "path": "$",
            "old_type": old_format,
            "new_type": new_format,
            "old_value": f"format: {old_format}",
            "new_value": f"format: {new_format}",
            "old_preview": None,
            "new_preview": None,
            "severity": "CRITICAL",
            "message": f"Response formati degisti: {old_format} -> {new_format}",
            "description": "API response'unun formati degisti. Istemciler farkli bir format bekliyor olabilir.",
        })

    # Icerik degisti (hash farki)
    length_diff = new_length - old_length
    line_diff = new_lines - old_lines

    # Degisiklik buyuklugune gore severity belirle
    if old_length > 0:
        change_ratio = abs(length_diff) / old_length
    else:
        change_ratio = 1.0

    if change_ratio > 0.5:
        severity = "CRITICAL"
        impact = "Icerik yarisından fazla degisti — buyuk yapisal degisiklik."
    elif change_ratio > 0.1:
        severity = "WARNING"
        impact = "Icerik onemli olcude degisti."
    else:
        severity = "INFO"
        impact = "Kucuk icerik degisikligi tespit edildi."

    length_info = f"{old_length} -> {new_length} byte"
    if length_diff > 0:
        length_info += f" (+{length_diff})"
    elif length_diff < 0:
        length_info += f" ({length_diff})"

    line_info = f"{old_lines} -> {new_lines} satir"

    changes.append({
        "type": "field_removed" if new_length < old_length else "field_added",
        "change_type": "Content Changed",
        "path": "$ (response body)",
        "old_type": old_format,
        "new_type": new_format,
        "old_value": f"hash: {old_hash[:16]}...\n{length_info}\n{line_info}",
        "new_value": f"hash: {new_hash[:16]}...\n{length_info}\n{line_info}",
        "old_preview": old_sample[:300] if old_sample else None,
        "new_preview": new_sample[:300] if new_sample else None,
        "severity": severity,
        "message": f"Response icerigi degisti ({length_info})",
        "description": impact,
    })

    return changes


def compare_schemas(
    old_schema: dict,
    new_schema: dict,
    provider: str,
    endpoint: str,
) -> dict[str, Any]:
    """Iki schema'yi karsilastir ve farklari raporla.

    JSON ve text schema'lari destekler.

    Args:
        old_schema: Eski schema
        new_schema: Yeni schema
        provider: Provider adi
        endpoint: Endpoint adi

    Returns:
        Karsilastirma sonucu
    """
    # Text schema karsilastirma
    if old_schema.get("type") == "text" or new_schema.get("type") == "text":
        # Her ikisi de text ise hash karsilastir
        if old_schema.get("type") == "text" and new_schema.get("type") == "text":
            changes = _compare_text_schemas(old_schema, new_schema)
        else:
            # Biri text biri JSON — format degisikligi (CRITICAL)
            old_type = old_schema.get("type", "unknown")
            new_type = new_schema.get("type", "unknown")
            changes = [{
                "type": "type_changed",
                "change_type": "Response Type Changed",
                "path": "$",
                "old_type": old_type,
                "new_type": new_type,
                "old_value": f"Response type: {old_type}",
                "new_value": f"Response type: {new_type}",
                "old_preview": None,
                "new_preview": None,
                "severity": "CRITICAL",
                "message": f"Response tipi degisti: {old_type} -> {new_type}",
                "description": "API response formati tamamen degisti (JSON <-> Text). Bu buyuk bir breaking change.",
            }]

        return {
            "has_changes": bool(changes),
            "provider": provider,
            "endpoint": endpoint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": changes,
            "max_severity": _get_max_severity(changes) if changes else "INFO",
            "summary": _build_summary(changes) if changes else "0 CRITICAL, 0 WARNING, 0 INFO",
        }

    # JSON schema karsilastirma (mevcut mantik)
    diff = DeepDiff(old_schema, new_schema, ignore_order=True)
    has_changes = bool(diff)

    if not has_changes:
        return {
            "has_changes": False,
            "provider": provider,
            "endpoint": endpoint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": [],
            "max_severity": "INFO",
            "summary": "0 CRITICAL, 0 WARNING, 0 INFO",
        }

    # Kendi format'imiza donustur
    changes = _flatten_changes(old_schema, new_schema)

    return {
        "has_changes": bool(changes),
        "provider": provider,
        "endpoint": endpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
        "max_severity": _get_max_severity(changes),
        "summary": _build_summary(changes),
    }
