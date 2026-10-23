"""Diff two fact lists and classify each difference: breaking, non-breaking or cosmetic.

Direction matters. A client *sends* requests and *reads* responses, so a new required
request field breaks it while a new response field does not; a response field that may
now be missing or null breaks it, a request field that becomes optional does not. XSD
elements (``dir`` ``both``) are treated like request fields: a stricter document breaks.
Anything not covered by a rule is ``non_breaking``; documentation is ``cosmetic``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["breaking", "non_breaking", "cosmetic"]
RANK: dict[str, int] = {"cosmetic": 0, "non_breaking": 1, "breaking": 2}
MAX_REPORTED = 200
KINDS = ("op", "param", "field", "server", "port", "auth", "msg", "enum", "status", "import")
REASONS = frozenset(
    {f"{kind}_{change}" for kind in KINDS for change in ("added", "removed")}
    | {
        "doc_changed",
        "required_param_added",
        "required_field_added",
        "import_refused",
        "type_changed",
        "now_required",
        "now_optional",
        "may_be_absent",
        "now_present",
        "now_nullable",
        "no_longer_null",
        "null_not_accepted",
        "null_accepted",
        "enum_value_removed",
        "enum_value_added",
        "enum_changed",
        "cardinality_narrowed",
        "cardinality_widened",
        "endpoint_changed",
        "soap_version_changed",
        "auth_changed",
        "message_changed",
        "deprecated",
        "undeprecated",
        "format_changed",
        "changed",
    }
)


@dataclass(frozen=True)
class FactChange:
    fact: str
    change: Literal["added", "removed", "changed"]
    severity: Severity
    reason: str
    operation: str | None = None
    attribute: str | None = None
    before: Any = None
    after: Any = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _added(fact: dict[str, Any]) -> tuple[Severity, str]:
    kind = fact.get("t")
    if kind == "doc":
        return "cosmetic", "doc_changed"
    if kind == "param" and fact.get("required"):
        return "breaking", "required_param_added"
    if kind == "field" and fact.get("dir") in ("request", "both") and fact.get("required"):
        return "breaking", "required_field_added"
    if kind == "import":
        return "non_breaking", "import_refused"
    return "non_breaking", f"{kind}_added"


def _removed(fact: dict[str, Any]) -> tuple[Severity, str]:
    kind = fact.get("t")
    if kind == "doc":
        return "cosmetic", "doc_changed"
    if kind == "status" and not str(fact.get("status", "")).startswith("2"):
        return "non_breaking", "status_removed"
    if kind in ("op", "param", "field", "server", "port", "auth", "msg", "enum", "status"):
        return "breaking", f"{kind}_removed"
    return "non_breaking", f"{kind}_removed"


def _narrower_max(before: Any, after: Any) -> bool:
    def value(raw: Any) -> float:
        return float("inf") if raw in (None, "unbounded") else float(raw)

    try:
        return value(after) < value(before)
    except (TypeError, ValueError):
        return False


def _changed(fact: dict[str, Any], attribute: str, before: Any, after: Any) -> tuple[Severity, str]:
    kind, direction = fact.get("t"), fact.get("dir")
    strict_side = direction in ("request", "both") or kind == "param"
    if kind == "doc":
        return "cosmetic", "doc_changed"
    if attribute == "type" or attribute == "location":
        return "breaking", "type_changed"
    if attribute == "required":
        if strict_side:
            return ("breaking", "now_required") if after else ("non_breaking", "now_optional")
        return ("breaking", "may_be_absent") if not after else ("non_breaking", "now_present")
    if attribute == "nullable":
        if direction == "response":
            return ("breaking", "now_nullable") if after else ("non_breaking", "no_longer_null")
        return ("breaking", "null_not_accepted") if not after else ("non_breaking", "null_accepted")
    if attribute in ("enum", "values"):
        removed = set(before or []) - set(after or [])
        if removed and (strict_side or kind == "enum"):
            return "breaking", "enum_value_removed"
        return "non_breaking", "enum_value_added" if set(after or []) - set(
            before or []
        ) else "enum_changed"
    if attribute == "min":
        narrowed = int(after or 0) > int(before or 0)
        return (
            ("breaking", "cardinality_narrowed")
            if narrowed
            else ("non_breaking", "cardinality_widened")
        )
    if attribute in ("max", "max_items"):
        if _narrower_max(before, after):
            return "breaking", "cardinality_narrowed"
        return "non_breaking", "cardinality_widened"
    if attribute == "address":
        return "breaking", "endpoint_changed"
    if attribute == "soap":
        return "breaking", "soap_version_changed"
    if attribute in ("security", "scheme", "header") or kind == "auth":
        return "breaking", "auth_changed"
    if attribute in ("input", "output", "element"):
        return "breaking", "message_changed"
    if attribute == "deprecated":
        return "non_breaking", "deprecated" if after else "undeprecated"
    if attribute == "format":
        return "non_breaking", "format_changed"
    return "non_breaking", "changed"


def diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[FactChange]:
    before = {str(f["id"]): f for f in old if "id" in f}
    after = {str(f["id"]): f for f in new if "id" in f}
    changes: list[FactChange] = []
    for fact_id in sorted(before.keys() - after.keys()):
        fact = before[fact_id]
        severity, reason = _removed(fact)
        changes.append(FactChange(fact_id, "removed", severity, reason, fact.get("op")))
    for fact_id in sorted(after.keys() - before.keys()):
        fact = after[fact_id]
        severity, reason = _added(fact)
        changes.append(FactChange(fact_id, "added", severity, reason, fact.get("op")))
    for fact_id in sorted(before.keys() & after.keys()):
        old_fact, new_fact = before[fact_id], after[fact_id]
        for attribute in sorted(
            (old_fact.keys() | new_fact.keys()) - {"id", "t", "op", "dir", "status"}
        ):
            was, now = old_fact.get(attribute), new_fact.get(attribute)
            if was == now:
                continue
            severity, reason = _changed(new_fact, attribute, was, now)
            changes.append(
                FactChange(
                    fact_id, "changed", severity, reason, new_fact.get("op"), attribute, was, now
                )
            )
    changes.sort(key=lambda c: (-RANK[c.severity], c.fact))
    return changes


def overall(changes: list[FactChange]) -> Severity | None:
    if not changes:
        return None
    worst = max(changes, key=lambda c: RANK[c.severity])
    return worst.severity


def summary(changes: list[FactChange]) -> dict[str, Any]:
    counts = dict.fromkeys(RANK, 0)
    for change in changes:
        counts[change.severity] += 1
    return {
        "counts": counts,
        "operations": sorted({c.operation for c in changes if c.operation}),
        "changes": [c.to_json() for c in changes[:MAX_REPORTED]],
        "truncated": len(changes) > MAX_REPORTED,
    }
