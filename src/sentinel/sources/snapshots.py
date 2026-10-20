"""Snapshots: the normalised items a change source said, and their hash.

The prober builds them; the core recomputes the hash on ingest, so a node cannot ship
items that do not match what it claims. Items are flat JSON objects; order does not
matter (the hash is over items sorted by ``id``), so reordering is not a change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

MAX_ITEMS = 100
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_VALIDATOR = 256


def canonical(items: list[dict[str, Any]]) -> bytes:
    ordered = sorted(items, key=lambda item: str(item.get("id", "")))
    return json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def snapshot_hash(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical(items)).hexdigest()


def safe_validator(value: str | None) -> str | None:
    """An ``ETag`` / ``Last-Modified`` we may send back later, or None."""
    if not value or len(value) > MAX_VALIDATOR:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


@dataclass(frozen=True)
class SnapshotData:
    hash: str
    items: list[dict[str, Any]]
    validators: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"hash": self.hash, "items": self.items, "validators": self.validators}


def make_snapshot(
    items: list[dict[str, Any]], *, etag: str | None = None, last_modified: str | None = None
) -> SnapshotData:
    items = items[:MAX_ITEMS]
    while items and len(canonical(items)) > MAX_SNAPSHOT_BYTES:
        items = items[: len(items) // 2]
    validators = {
        name: value
        for name, raw in (("etag", etag), ("last_modified", last_modified))
        if (value := safe_validator(raw))
    }
    return SnapshotData(snapshot_hash(items), items, validators)
