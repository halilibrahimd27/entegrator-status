"""OpenAPI 3.x and Swagger 2.0 → facts.

Operations are named ``METHOD /path`` with path parameters written ``{}``: renaming
``{id}`` to ``{userId}`` changes nothing for a client, so it must not read as "removed
and added". Only local ``$ref`` (``#/...``) is followed, with cycle and depth limits;
external references are recorded, not fetched. Descriptions and examples are reduced to
a hash (``doc`` facts) or dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from sentinel.contracts.facts import MAX_DEPTH, ContractError, Facts, doc_hash, enum_of
from sentinel.security import yamlsafe

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_TEMPLATE = re.compile(r"\{[^}/]*\}")
_JSON_TYPES = ("application/json", "application/problem+json", "*/*")


def load(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace").lstrip("﻿")
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yamlsafe.load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise ContractError(f"unreadable spec: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not (
        str(data.get("openapi", "")).startswith("3") or data.get("swagger") == "2.0"
    ):
        raise ContractError("not an OpenAPI 3 or Swagger 2.0 document")
    return data


class _Walker:
    def __init__(self, spec: dict[str, Any], facts: Facts) -> None:
        self.spec = spec
        self.facts = facts

    def resolve(self, node: Any, seen: frozenset[str]) -> tuple[Any, frozenset[str], str | None]:
        """Follow local ``$ref`` chains; returns (schema, refs seen, problem)."""
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not ref.startswith("#/"):
                return {}, seen, "external_ref"
            if ref in seen:
                return {}, seen, "recursive"
            seen = seen | {ref}
            target: Any = self.spec
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, dict) else None
            if target is None:
                return {}, seen, "broken_ref"
            node = target
        return node, seen, None

    def schema(
        self,
        node: Any,
        fact_id: str,
        *,
        op: str,
        direction: str,
        required: bool | None,
        depth: int = 0,
        seen: frozenset[str] = frozenset(),
        status: str | None = None,
    ) -> None:
        node, seen, problem = self.resolve(node, seen)
        common = {"dir": direction, "op": op, "status": status, "required": required}
        if problem or not isinstance(node, dict) or depth > MAX_DEPTH:
            self.facts.add(fact_id, "field", type=problem or "any", **common)
            return
        properties: dict[str, Any] = dict(node.get("properties") or {})
        required_names = set(node.get("required") or [])
        kinds = node.get("type")
        for part in node.get("allOf") or []:
            merged, _, _ = self.resolve(part, seen)
            if isinstance(merged, dict):
                properties.update(merged.get("properties") or {})
                required_names |= set(merged.get("required") or [])
                kinds = kinds or merged.get("type")
        variants = node.get("oneOf") or node.get("anyOf") or []
        variant_types: set[str] = set()
        for variant in variants:  # union their fields; their types make the parent's type
            resolved, _, problem = self.resolve(variant, seen)
            if not isinstance(resolved, dict):
                continue
            properties.update(resolved.get("properties") or {})
            variant_types.add(problem or str(resolved.get("type") or "object"))
        if isinstance(kinds, list):
            nullable = "null" in kinds
            kind = "|".join(sorted(str(k) for k in kinds if k != "null")) or "null"
        else:
            nullable = bool(node.get("nullable") or node.get("x-nullable"))
            if kinds:
                kind = str(kinds)
            elif variant_types:
                kind = f"oneOf({'|'.join(sorted(variant_types))})"
            else:
                kind = "object" if properties else "any"
        self.facts.add(
            fact_id,
            "field",
            type=kind,
            nullable=nullable,
            enum=enum_of(node.get("enum")),
            format=node.get("format"),
            max_items=node.get("maxItems"),
            **common,
        )
        for name, sub in sorted(properties.items()):
            self.schema(
                sub,
                f"{fact_id}.{name}",
                op=op,
                direction=direction,
                required=name in required_names,
                depth=depth + 1,
                seen=seen,
                status=status,
            )
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            self.schema(extra, f"{fact_id}.*", op=op, direction=direction, required=False,
                        depth=depth + 1, seen=seen, status=status)  # fmt: skip
        if "items" in node:
            self.schema(node["items"], f"{fact_id}[]", op=op, direction=direction, required=None,
                        depth=depth + 1, seen=seen, status=status)  # fmt: skip


def _content_schema(content: Any) -> Any:
    if not isinstance(content, dict) or not content:
        return None
    for media in _JSON_TYPES:
        if media in content:
            return (content[media] or {}).get("schema")
    first = next(iter(content.values()))
    return first.get("schema") if isinstance(first, dict) else None


def normalise(spec: dict[str, Any]) -> list[dict[str, Any]]:
    facts = Facts()
    walker = _Walker(spec, facts)
    swagger = spec.get("swagger") == "2.0"
    if swagger:
        scheme = (spec.get("schemes") or ["https"])[0]
        hosts = [f"{scheme}://{spec.get('host', '')}{spec.get('basePath', '')}"]
        schemes = spec.get("securityDefinitions") or {}
    else:
        hosts = [str(s.get("url")) for s in spec.get("servers") or [] if isinstance(s, dict)]
        schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    for url in hosts:
        facts.add(f"server:{url}", "server", url=url)
    for name, scheme in sorted(schemes.items()):
        if isinstance(scheme, dict):
            facts.add(f"auth:{name}", "auth", type=scheme.get("type"), scheme=scheme.get("scheme"),
                      location=scheme.get("in"), header=scheme.get("name"))  # fmt: skip
    default_security = spec.get("security")
    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            op = f"{method.upper()} {_TEMPLATE.sub('{}', str(path))}"
            security = operation.get("security", default_security) or []
            facts.add(
                f"op:{op}",
                "op",
                op=op,
                deprecated=bool(operation.get("deprecated")),
                security=sorted({n for req in security if isinstance(req, dict) for n in req}),
            )
            facts.add(f"doc:{op}", "doc", op=op,
                      h=doc_hash(operation.get("summary"), operation.get("description"),
                                 operation.get("operationId")))  # fmt: skip
            _parameters(walker, op, [*shared, *(operation.get("parameters") or [])], swagger)
            body = operation.get("requestBody")
            body, _, _ = walker.resolve(body, frozenset())
            if isinstance(body, dict) and body.get("content"):
                walker.schema(_content_schema(body.get("content")), f"req:{op}:$", op=op,
                              direction="request", required=bool(body.get("required")))  # fmt: skip
            for status, response in sorted((operation.get("responses") or {}).items()):
                response, _, _ = walker.resolve(response, frozenset())
                code = str(status)
                facts.add(f"status:{op}:{code}", "status", op=op, status=code)
                if not isinstance(response, dict):
                    continue
                schema = (
                    response.get("schema") if swagger else _content_schema(response.get("content"))
                )
                if schema is not None:
                    walker.schema(schema, f"res:{op}:{code}:$", op=op, direction="response",
                                  required=True, status=code)  # fmt: skip
    return facts.items()


def _parameters(walker: _Walker, op: str, parameters: list[Any], swagger: bool) -> None:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in parameters:
        param, _, _ = walker.resolve(raw, frozenset())
        if isinstance(param, dict) and param.get("name"):
            by_key[(str(param.get("in")), str(param["name"]))] = param  # operation wins
    path_index = 0
    for (location, name), param in by_key.items():
        if location == "body" and swagger:
            walker.schema(param.get("schema"), f"req:{op}:$", op=op, direction="request",
                          required=bool(param.get("required")))  # fmt: skip
            continue
        schema = param.get("schema") if not swagger else param
        schema, _, _ = walker.resolve(schema, frozenset())
        schema = schema if isinstance(schema, dict) else {}
        key = f"path:{path_index}" if location == "path" else f"{location}:{name}"
        path_index += location == "path"
        walker.facts.add(
            f"param:{op}:{key}",
            "param",
            op=op,
            location=location,
            required=bool(param.get("required")) or location == "path",
            type=schema.get("type"),
            enum=enum_of(schema.get("enum")),
        )
