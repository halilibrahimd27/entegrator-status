"""Which rules an event matches. Pure."""

from __future__ import annotations

from typing import Any, Protocol

LEVEL = {"low": 0, "medium": 1, "high": 2}
KINDS = ("incident", "change", "risk")


class RuleLike(Protocol):
    enabled: bool
    product_id: Any
    service_id: Any
    kinds: list[str]
    min_severity: str
    include_non_breaking: bool


class EventLike(Protocol):
    product_id: Any
    subject_type: str
    severity: str
    detail: dict[str, Any]


def matches(rule: RuleLike, event: EventLike) -> bool:
    """Same organization is the caller's job: rules are only ever read for the event's org."""
    if not rule.enabled:
        return False
    if rule.product_id is not None and rule.product_id != event.product_id:
        return False
    if rule.service_id is not None and str(rule.service_id) not in (
        event.detail.get("service_ids") or []
    ):
        return False
    if event.subject_type not in (rule.kinds or KINDS):
        return False
    if LEVEL.get(event.severity, 0) < LEVEL.get(rule.min_severity, 1):
        return False
    contract = event.detail.get("kind") == "contract"
    return not (
        contract and not rule.include_non_breaking and event.detail.get("category") != "breaking"
    )
