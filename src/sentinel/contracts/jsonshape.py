"""The shape of a JSON response → facts: types and presence, never values.

A field present in every object seen at its place is ``required``; one that is
sometimes missing is not; one that was ``null`` is ``nullable``. Arrays are sampled
(the first ``SAMPLE`` items) and their item shapes merged. Fact ids match the OpenAPI
normaliser's response ids, so both kinds of source classify the same way.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sentinel.contracts.facts import MAX_DEPTH, Facts

SAMPLE = 50


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "any"


def shape(value: Any, op: str, status: str = "200") -> list[dict[str, Any]]:
    types: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, int] = defaultdict(int)  # objects seen at a place
    present: dict[str, int] = defaultdict(int)  # ... that carried a given child
    order: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if path not in types:
            order.append(path)
        types[path].add(_type(node))
        if depth >= MAX_DEPTH:
            return
        if isinstance(node, dict):
            parents[path] += 1
            for key in sorted(node):
                child = f"{path}.{str(key)[:100]}"
                present[child] += 1
                walk(node[key], child, depth + 1)
        elif isinstance(node, list):
            for item in node[:SAMPLE]:
                walk(item, f"{path}[]", depth + 1)

    walk(value, "$", 0)
    facts = Facts()
    for path in order:
        kinds = types[path]
        parent = path.rsplit(".", 1)[0] if "." in path and not path.endswith("[]") else None
        required = True if parent is None else present[path] >= parents.get(parent, 0)
        facts.add(
            f"res:{op}:{status}:{path}",
            "field",
            dir="response",
            op=op,
            status=status,
            type="|".join(sorted(kinds - {"null"})) or "null",
            nullable="null" in kinds,
            required=required,
        )
    return facts.items()
