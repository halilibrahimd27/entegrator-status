"""The contract model: a flat list of facts, one per thing a client can depend on.

Every fact has an ``id`` that names its place (``op:GET /v2/payments``,
``res:GET /v2/payments:200:$.items[].id``) and a type ``t``:

- ``op``: an operation (HTTP method + path, or a SOAP operation)
- ``param``: an operation parameter (``in``, ``required``, ``type``, ``enum``)
- ``field``: a field of a request or response body (``dir`` ``request`` | ``response``
  | ``both`` for XSD), with ``type``, ``required``, ``nullable``, ``enum``, ``min``/``max``
- ``server`` / ``port``: where the API lives (URL, SOAP version)
- ``auth``: a security scheme
- ``msg`` / ``enum``: WSDL message parts and XSD enumerations
- ``doc``: a hash of documentation text, so wording changes are visible as cosmetic

Facts never hold example values, descriptions or response bodies, only structure.
"""

from __future__ import annotations

import hashlib
from typing import Any

MAX_FACTS = 20_000
MAX_ENUM = 100
MAX_DEPTH = 12


class ContractError(Exception):
    """The document cannot be read as a contract (too large, malformed, hostile)."""


def doc_hash(*texts: Any) -> str:
    joined = "\x00".join(str(t) for t in texts if t)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def enum_of(values: Any) -> list[str] | None:
    if not isinstance(values, list):
        return None
    return sorted({str(v)[:100] for v in values})[:MAX_ENUM]


class Facts:
    """An ordered, id-unique fact collection with a hard size limit."""

    def __init__(self, limit: int = MAX_FACTS) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._limit = limit

    def add(self, fact_id: str, kind: str, **attrs: Any) -> None:
        if fact_id in self._items:
            self._items[fact_id].update({k: v for k, v in attrs.items() if v is not None})
            return
        if len(self._items) >= self._limit:
            raise ContractError(f"more than {self._limit} facts: too large to track")
        self._items[fact_id] = {
            "id": fact_id[:500],
            "t": kind,
            **{k: v for k, v in attrs.items() if v is not None},
        }

    def items(self) -> list[dict[str, Any]]:
        return list(self._items.values())
