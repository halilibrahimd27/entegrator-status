"""``eol``: release cycles and their end-of-life dates, from endoflife.date."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import quote

from sentinel.models.catalog import SourceKind
from sentinel.security.http import CONTRACT_MAX_BYTES
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import fetch_content, parse_failure, snapshot_outcome


def _day(value: Any) -> str | bool | None:
    """endoflife.date uses a date, ``true`` (ended, date unknown) or ``false`` (no end)."""
    if isinstance(value, bool):
        return value
    try:
        return date.fromisoformat(str(value)).isoformat() if value else None
    except ValueError:
        return None


def cycles(data: Any, product: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError("not a cycle list")
    items = []
    for cycle in data:
        if not isinstance(cycle, dict) or not cycle.get("cycle"):
            continue
        name = str(cycle["cycle"])[:40]
        items.append(
            {
                "id": f"c:{name}",
                "cycle": name,
                "eol": _day(cycle.get("eol")),
                "support": _day(cycle.get("support")),
                "latest": str(cycle.get("latest") or "")[:40] or None,
                "lts": bool(cycle.get("lts")),
                "link": f"https://endoflife.date/{quote(product)}",
            }
        )
    return items


class EolAdapter(Adapter):
    kind = SourceKind.EOL

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        product = str(job.config["product"])
        url = f"https://endoflife.date/api/{quote(product)}.json"
        result = await fetch_content(
            job, env, url, "application/json", max_bytes=CONTRACT_MAX_BYTES
        )
        if isinstance(result, ProbeOutcome):
            return result
        try:
            items = cycles(json.loads(result.body), product)
        except ValueError as exc:
            return parse_failure(result, str(exc))
        return snapshot_outcome(result, items)
